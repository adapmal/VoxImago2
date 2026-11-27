"""Fila contextual: intenção durável, efeito local e confirmação remota separados.

Não controla InSync/Drive Desktop e não move arquivos pela API. O diário nunca
autoriza repetir uma escrita incerta. A API escreve somente descrições, com ETag.
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from src.utils.queue_store import QueueStore
from src.utils.staging_schema import StagingItem
from src.utils.path_validation import configured_operation_roots, validate_path_in_roots
from src.services.drive_retry import execute_drive_read
from src.services.hash_deduplicator import DedupCancelled, DriveCatalog, FOLDER_MIME, private_service, escape_query
from src.database.search import SearchEngine
from src.drive.match import extract_drive_file_id, normalize_aggressive
from src.drive.link_validation import VALIDATED_LINK_STATUSES
from src.services.name_safety import name_key

TAGS = ('add_tags', 'remove_tags', 'set_description')
ACTIVE = ('applying', 'local_applied', 'waiting_cloud', 'uncertain')


def norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ''


def identity_key(value):
    # IDs remotos são sensíveis a maiúsculas, caminhos Windows não.
    return norm(value) if os.path.isabs(value) else value


def under(path, root):
    return bool(path and root and (norm(path) == norm(root) or norm(path).startswith(norm(root) + os.sep)))


def rebase(path, old, new):
    return os.path.join(new, os.path.relpath(path, old)) if norm(path) != norm(old) else new


def file_stamp(path):
    stat = os.stat(path)  # Metadados somente: não abrir/hidratar a mídia.
    return dict(device=stat.st_dev, inode=stat.st_ino, size=stat.st_size, directory=os.path.isdir(path))


def same_file(path, stamp):
    try:
        actual = file_stamp(path)
        return bool(stamp.get('inode') and actual['inode'] == stamp['inode']
                    and actual['device'] == stamp['device']
                    and actual['directory'] == stamp['directory']
                    and (actual['directory'] or actual['size'] == stamp['size']))
    except OSError:
        return False


def queue_conflict(item, others):
    """Exclusão nunca descarta outras intenções silenciosamente."""
    for other in others:
        if other.operation_id == item.operation_id:
            continue
        same = (item.asset_id and item.asset_id == other.asset_id) or item.file_id == other.file_id
        a, b = item.path, other.path
        if item.action_type == 'delete' or other.action_type == 'delete':
            deleted, affected = (a, b) if item.action_type == 'delete' else (b, a)
            destination = other.new_value if item.action_type == 'delete' and other.action_type == 'move' else ''
            if same or under(affected, deleted) or under(destination, deleted):
                return 'Exclusão conflita com outra operação deste arquivo/pasta. Remova explicitamente uma das intenções.'
    return ''


class ContextStore(QueueStore):
    def operations(self, active_only=False):
        with self.connect() as conn:
            return {row[0]: dict(asset=row[1], state=row[2], payload=json.loads(row[3]), message=row[4])
                    for row in conn.execute('SELECT id,asset,state,payload,message FROM context_operations' +
                        (" WHERE state IN ('applying','local_applied','waiting_cloud','uncertain')" if active_only else ''))}

    def operation(self, operation_id):
        with self.connect() as conn:
            row = conn.execute('SELECT asset,state,payload,message FROM context_operations WHERE id=?', (operation_id,)).fetchone()
        return dict(asset=row[0], state=row[1], payload=json.loads(row[2]), message=row[3]) if row else None

    def asset(self, item):
        with self.connect() as conn:
            found = conn.execute('SELECT id,path,drive_id FROM context_assets WHERE id=?', (item.asset_id,)).fetchone() if item.asset_id else None
            if not found:
                found = conn.execute('SELECT id,path,drive_id FROM context_assets WHERE path=?', (identity_key(item.path or item.file_id),)).fetchone()
        return found or (item.asset_id or uuid.uuid4().hex, item.path or item.file_id, '')

    def record(self, item, state, payload, message='', changed=(), complete=False, reset=False):
        item.execution_state = state if not complete else 'pending'
        item.execution_message = message
        previous = self.operation(item.operation_id)
        if not complete and not reset and not changed and previous and previous == dict(
                asset=item.asset_id, state=state, payload=payload, message=message):
            return  # Poll sem mudança não aumenta o diário nem regrava a fila.
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.check_revision(conn)
            conn.execute('INSERT OR REPLACE INTO context_operations VALUES (?,?,?,?,?)',
                         (item.operation_id, item.asset_id, state, json.dumps(payload), message))
            conn.execute('INSERT INTO context_events(operation_id,state,payload,created) VALUES (?,?,?,?)',
                         (item.operation_id, state, json.dumps(dict(intent=item.to_dict(), result=payload, message=message)), time.time()))
            for row in [item, *changed]:
                conn.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(row.to_dict()), row.operation_id))
            if payload.get('old_path') and item.asset_id:
                path = payload.get('new_path') if state in ('waiting_cloud', 'completed') else payload['old_path']
                conn.execute('INSERT OR REPLACE INTO context_assets VALUES (?,?,?)',
                             (item.asset_id, identity_key(path), payload.get('drive_id', '')))
            for row in changed:
                if row.asset_id:
                    conn.execute('UPDATE context_assets SET path=? WHERE id=?', (norm(row.path), row.asset_id))
            if state in ACTIVE:
                conn.execute('INSERT OR REPLACE INTO execution VALUES (?,?,?)', (item.operation_id, 'uncertain', time.time()))
            if reset:
                conn.execute('DELETE FROM execution WHERE id=?', (item.operation_id,))
            if complete:
                conn.execute('DELETE FROM items WHERE id=?', (item.operation_id,))
                conn.execute('INSERT OR REPLACE INTO execution VALUES (?,?,?)', (item.operation_id, 'completed', time.time()))
                if payload.get('action') == 'delete':
                    conn.execute('DELETE FROM context_assets WHERE id=?', (item.asset_id,))
            revision = str(time.time_ns())
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('revision',?)", (revision,))
        self.revision = revision


class QueueCloud:
    cancelled = staticmethod(lambda: False)
    on_wait = None

    def __init__(self, service, cfg, cancelled=lambda: False, on_wait=None):
        self.service = private_service(service) if service else None
        self.drive_id = cfg.get_current_drive_id()
        self.roots = configured_operation_roots(cfg)
        self.cancelled = cancelled
        self.on_wait = on_wait
        self.api = DriveCatalog(self.service, self.drive_id, cancelled=cancelled, on_wait=on_wait) if self.service else None

    def require(self):
        if not self.service:
            raise ValueError('Conecte ao Drive para validar a identidade e acompanhar a sincronização.')

    def get(self, fid):
        self.require()
        return self.api.get(fid)

    def drive(self, drive_id):
        """Obtém os metadados reais do Drive Compartilhado configurado."""
        self.require()
        return execute_drive_read(
            lambda: self.service.drives().get(driveId=drive_id),
            cancelled=self.cancelled,
            cancel_error=DedupCancelled,
            on_wait=self.on_wait,
        )

    def children(self, parent, name=None):
        self.require()
        # A base Windows guarda caminhos normalizados. Não depender da semântica
        # de caixa da busca remota, nem ocultar irmãos que diferem só pela caixa.
        query = f"'{escape_query(parent)}' in parents and trashed=false"
        token, seen, rows = None, set(), []
        while True:
            if self.cancelled():
                raise DedupCancelled('Operação cancelada.')
            args = dict(q=query, supportsAllDrives=True, includeItemsFromAllDrives=True,
                        corpora='drive', driveId=self.drive_id, pageSize=1000,
                        fields='nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,sha256Checksum,parents,driveId,trashed)')
            if token:
                args['pageToken'] = token
            page = execute_drive_read(
                lambda a=args: self.service.files().list(**a),
                cancelled=self.cancelled,
                cancel_error=DedupCancelled,
                on_wait=self.on_wait,
            )
            if page.get('incompleteSearch'):
                raise ValueError('Busca incompleta no Drive; nenhuma identidade confirmada.')
            rows.extend(row for row in page.get('files', []) if name is None or name_key(row.get('name', '')) == name_key(name))
            token = page.get('nextPageToken')
            if not token:
                return rows
            if token in seen:
                raise ValueError('Paginação repetida no Drive.')
            seen.add(token)

    def folder(self, path):
        roots = [root for root in self.roots if under(path, root)]
        if len(roots) != 1:
            raise ValueError('Raiz local ambígua ou não mapeada ao Drive atual.')
        parent = self.drive_id
        if not parent:
            raise ValueError('Drive compartilhado não configurado.')
        # files().get no ID raiz pode devolver apenas o nome genérico "Drive";
        # o nome real do Shared Drive vem de drives().get.
        root_meta = self.drive(parent)
        if os.path.basename(os.path.normpath(roots[0])).casefold() != root_meta.get('name', '').casefold():
            raise ValueError('A raiz local precisa corresponder à raiz do Drive configurado; não inferir mapeamento de subpastas.')
        relative = os.path.relpath(path, roots[0])
        for part in ([] if relative == '.' else relative.split(os.sep)):
            matches = self.children(parent, part)
            if len(matches) != 1 or matches[0]['mimeType'] != FOLDER_MIME:
                raise ValueError('Pasta ainda não sincronizada ou ambígua no Drive: ' + path)
            parent = matches[0]['id']
        return parent

    def bind(self, item, catalog, saved_id=''):
        self.require()
        if not item.path:
            if not item.file_id or any(c in item.file_id for c in ('/', '\\', ':')):
                raise ValueError('Arquivo sem ID Drive válido.')
            remote = self.get(item.file_id)
        else:
            row = catalog.find(item.path)
            fid = saved_id or extract_drive_file_id((row or {}).get('webContentLink'))
            validation = catalog.validation(item.path)
            if validation and validation['status'] not in VALIDATED_LINK_STATUSES:
                raise ValueError('Vínculo local/Drive bloqueado pela revalidação. Revise antes de executar.')
            if validation:
                if fid and fid != validation['drive_id']:
                    raise ValueError('IDs do vínculo divergem; execução bloqueada.')
                fid = validation['drive_id']
            parent = self.folder(os.path.dirname(item.path))
            matches = self.children(parent, os.path.basename(item.path))
            if len(matches) != 1 or (fid and matches[0]['id'] != fid):
                raise ValueError('Nome local ambíguo ou vínculo divergente nesta pasta do Drive. Revise antes de executar.')
            if not fid:
                fid = matches[0]['id']
            remote = self.get(fid)
            if remote.get('name', '').casefold() != os.path.basename(item.path).casefold() or remote.get('parents') != [parent]:
                raise ValueError('Nome/pasta no Drive divergem do arquivo local. Aguarde a sincronização ou revise o vínculo.')
            if not os.path.isdir(item.path) and int(remote.get('size') or -1) != os.stat(item.path).st_size:
                raise ValueError('Tamanho local/Drive diverge; arquivo não confirmado.')
        if remote.get('trashed') or remote.get('driveId') != self.drive_id:
            raise ValueError('Arquivo fora do Drive autorizado ou na lixeira.')
        return remote

    def confirm(self, payload):
        action = payload['action']
        fid = payload.get('drive_id')
        if action == 'create_folder' and not fid:
            fid = self.folder(payload['new_path'])
        remote = self.get(fid)
        if action == 'delete':
            return bool(remote.get('trashed')), fid
        if remote.get('trashed') or remote.get('driveId') != self.drive_id:
            return False, fid
        if action in TAGS:
            return (remote.get('description') or '') == payload['description'], fid
        parent = self.folder(os.path.dirname(payload['new_path']))
        matches = remote.get('name', '').casefold() == os.path.basename(payload['new_path']).casefold() and remote.get('parents') == [parent]
        if action == 'rename':
            # Uma renomeação apenas de caixa também precisa ser confirmada.
            matches = matches and remote.get('name') == os.path.basename(payload['new_path'])
        if payload.get('remote_size') is not None:
            matches = matches and remote.get('size') == payload['remote_size']
        return bool(matches), fid

    def set_description(self, remote, text):
        self.api.update(remote, body={'description': text})


class QueueCatalog:
    """Conexão privada; não inicializa/migra nem varre o catálogo inteiro."""
    def __init__(self, path):
        self.conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(files)')}

    def close(self):
        self.conn.close()

    def find(self, path):
        key = norm(path) if os.path.isabs(path) else path
        row = self.conn.execute('SELECT * FROM files WHERE file_id=? OR path=? LIMIT 1', (key, key)).fetchone()
        return dict(row) if row else None

    def validation(self, path):
        if 'drive_link_validation' not in self.tables:
            return None
        row = self.conn.execute('SELECT * FROM drive_link_validation WHERE local_file_id=?', (norm(path),)).fetchone()
        return dict(row) if row else None

    def _search(self, row, old_id):
        if 'search_index' not in self.tables:
            return
        normalize = SearchEngine(None).normalize_text
        self.conn.execute('DELETE FROM search_index WHERE file_id IN (?,?)', (old_id, row['file_id']))
        self.conn.execute('INSERT INTO search_index(name,description,normalized_name,normalized_description,file_id,source) VALUES (?,?,?,?,?,?)',
                          (row.get('name', ''), row.get('description', ''), normalize(row.get('name') or ''),
                           normalize(row.get('description') or ''), row['file_id'], row.get('source', 'local')))

    def apply(self, payload):
        old, new, action = payload['old_path'], payload.get('new_path'), payload['action']
        with self.conn:
            if action in TAGS:
                row = self.find(old)
                if row:
                    row['description'] = payload['description']
                    self.conn.execute('UPDATE files SET description=? WHERE file_id=?', (row['description'], row['file_id']))
                    self._search(row, row['file_id'])
                return
            if action == 'create_folder':
                if not self.find(new):
                    row = dict(file_id=norm(new), name=os.path.basename(new), path=norm(new), mimeType='folder',
                               source='local', parentId=norm(os.path.dirname(new)), description='')
                    cols = list(row)
                    self.conn.execute(f'INSERT INTO files ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})', tuple(row.values()))
                    self._search(row, row['file_id'])
                return
            escaped = norm(old).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            rows = self.conn.execute("SELECT * FROM files WHERE source='local' AND (path=? OR path LIKE ? ESCAPE '\\')",
                                     (norm(old), escaped + '\\' + os.sep + '%')).fetchall()
            for raw in rows:
                row = dict(raw)
                old_id = row['file_id']
                if action == 'delete':
                    self.conn.execute('DELETE FROM files WHERE file_id=?', (old_id,))
                    if 'search_index' in self.tables:
                        self.conn.execute('DELETE FROM search_index WHERE file_id=?', (old_id,))
                    if 'drive_link_validation' in self.tables:
                        self.conn.execute('DELETE FROM drive_link_validation WHERE local_file_id=?', (old_id,))
                    continue
                target = norm(rebase(row['path'], old, new))
                collision = self.find(target)
                if collision and collision['file_id'] != old_id:
                    raise ValueError('Conflito no catálogo após operação local. Conferência necessária.')
                # Só a raiz movida/renomeada muda de nome; filhos preservam a
                # grafia do catálogo, embora seus caminhos Windows sejam canônicos.
                name = os.path.basename(new) if norm(row['path']) == norm(old) else row['name']
                row.update(path=target, file_id=target, name=name, parentId=norm(os.path.dirname(target)))
                for column in ('name_normalized', 'name_aggressive'):
                    if column in self.columns:
                        row[column] = (normalize_aggressive(row['name']) if column == 'name_aggressive'
                                       else SearchEngine(None).normalize_text(row['name']))
                columns = ['path', 'file_id', 'name', 'parentId'] + [c for c in ('name_normalized', 'name_aggressive') if c in self.columns]
                self.conn.execute('UPDATE files SET ' + ','.join(c + '=?' for c in columns) + ' WHERE file_id=?',
                                  [row[c] for c in columns] + [old_id])
                if 'drive_link_validation' in self.tables:
                    self.conn.execute('UPDATE drive_link_validation SET local_file_id=?,remote_name=? WHERE local_file_id=?',
                                      (target, row['name'], old_id))
                self._search(row, old_id)


class ContextEngine:
    def __init__(self, store, catalog, cloud, cfg, cancelled=lambda: False, progress=lambda *a: None):
        self.store, self.catalog, self.cloud, self.cfg = store, catalog, cloud, cfg
        self.cancelled, self.progress = cancelled, progress
        self.items = [StagingItem.from_dict(r) for r in store.load()]
        self.completed = 0
        self.applied = 0

    def allowed(self, path):
        valid, error = validate_path_in_roots(path, configured_operation_roots(self.cfg))
        if os.path.splitdrive(path)[0].upper() == 'O:' or not valid:
            raise ValueError('Caminho bloqueado: ' + (error or path))

    def pending_dependency(self, item):
        states = self.store.operations(active_only=True)
        for other in self.items:
            if other.operation_id == item.operation_id:
                continue
            op = states.get(other.operation_id)
            if op and op['state'] in ACTIVE:
                result = op['payload']
                if (other.asset_id and other.asset_id == item.asset_id) or any(
                        under(item.path, p) or (item.action_type == 'move' and under(item.new_value, p))
                        or (item.action_type in ('move', 'rename', 'delete') and under(p, item.path))
                        for p in (result.get('old_path'), result.get('new_path')) if p):
                    return 'Aguardando confirmação/reconciliação de outra ação deste arquivo ou pasta.'
        return queue_conflict(item, self.items)

    def prepare(self, item):
        item.validate()
        if self.cfg.is_read_only():
            raise ValueError('Modo somente leitura.')
        asset, current, saved_id = self.store.asset(item)
        item.asset_id = asset
        if item.path and norm(current) != norm(item.path):
            raise ValueError('Referência antiga do arquivo. Reabra/revise a fila antes de executar.')
        dependency = self.pending_dependency(item)
        if dependency:
            raise ValueError(dependency)
        old = item.path or item.file_id
        if item.path:
            self.allowed(old)
        payload = dict(action=item.action_type, old_path=old, new_path=old, drive_id=saved_id)
        action = item.action_type
        if action == 'create_folder':
            new = item.new_value or item.path
            self.allowed(new)
            if os.path.exists(new):
                raise ValueError('A pasta já existe. Nenhuma criação será presumida como concluída; revise o item legado.')
            if not os.path.isdir(os.path.dirname(new)):
                raise ValueError('Execute primeiro a criação da pasta pai selecionada na fila.')
            self.cloud.folder(os.path.dirname(new))
            if self.cloud.children(self.cloud.folder(os.path.dirname(new)), os.path.basename(new)):
                raise ValueError('Já existe item com este nome no Drive. Aguarde a sincronização e revise.')
            payload['new_path'] = new
            return payload
        if action not in (*TAGS, 'move', 'rename', 'delete'):
            raise ValueError('Esta ação legada exige revisão; use a ferramenta específica.')
        if action not in TAGS and not item.path:
            raise ValueError('Esta operação exige origem local; não será executada diretamente pela API.')
        if item.path:
            if not os.path.exists(old):
                raise ValueError('Origem local ausente. Não procurar substituto por nome.')
            payload['stamp'] = file_stamp(old)
        remote = self.cloud.bind(item, self.catalog, saved_id)
        payload.update(drive_id=remote['id'], remote_size=remote.get('size'))
        if action in TAGS:
            live = remote.get('description') or ''
            if action == 'set_description':
                if live != (item.old_value or '') and live != item.new_value:
                    raise ValueError('Descrição mudou na nuvem. Revise a substituição antes de sobrescrever.')
                text = item.new_value
            else:
                from src.services.batch_tags import effective_description
                text = effective_description(live, [item])
            payload.update(description=text, remote=remote)
        elif action in ('move', 'rename'):
            new = (os.path.join(os.path.dirname(old), item.new_value) if action == 'rename' else item.new_value)
            self.allowed(new)
            if os.path.isdir(old) and under(new, old):
                raise ValueError('Não mover pasta para dentro de si mesma.')
            if os.path.splitdrive(old)[0].lower() != os.path.splitdrive(new)[0].lower():
                raise ValueError('Movimento entre unidades exige revisão separada; não copiar/baixar automaticamente.')
            if not os.path.isdir(os.path.dirname(new)):
                raise ValueError('Pasta de destino ainda não existe. Aprove e execute a criação primeiro.')
            if norm(old) != norm(new) and os.path.exists(new):
                raise ValueError('Destino ocupado. Resolva a colisão; não será aplicado sufixo sem aprovação.')
            parent_id = self.cloud.folder(os.path.dirname(new))
            self.check_destination(remote, parent_id, os.path.basename(new), action == 'move')
            payload.update(destination_parent=parent_id, remote=remote)
            payload['new_path'] = new
        return payload

    def check_destination(self, remote, parent, name, check_hash):
        siblings = self.cloud.children(parent, None if check_hash else name)
        for sibling in siblings:
            if sibling['id'] == remote['id']:
                continue
            if name_key(sibling.get('name', '')) == name_key(name):
                raise ValueError('Nome de destino ocupado no Drive; nenhuma movimentação efetuada.')
            if (check_hash and remote.get('md5Checksum') and sibling.get('md5Checksum')
                    and remote['md5Checksum'].lower() == sibling['md5Checksum'].lower()
                    and str(remote.get('size')) == str(sibling.get('size'))):
                raise ValueError('Possível cópia de mesmo conteúdo no destino (hash/tamanho). Revise no deduplicador; nada será substituído.')

    def apply_local(self, item, payload):
        action = payload['action']
        old, new = payload['old_path'], payload['new_path']
        if action in ('move', 'rename', 'delete') and not same_file(old, payload['stamp']):
            raise ValueError('A identidade local mudou após a aprovação; nenhuma ação repetida automaticamente.')
        if action in TAGS:
            if (payload['remote'].get('description') or '') != payload['description']:
                self.cloud.set_description(payload['remote'], payload['description'])
        elif action == 'create_folder':
            os.mkdir(new)  # Nunca criar ancestrais sem aprovação.
        elif action == 'delete':
            from src.utils.utils import send_to_recycle_bin
            if not send_to_recycle_bin(old):
                raise ValueError('Não foi possível enviar à lixeira. Nenhuma exclusão definitiva autorizada.')
        else:
            self.check_destination(payload['remote'], payload['destination_parent'], os.path.basename(new), action == 'move')
            # Windows rename falha se o alvo existe; sem shutil.move, sem abrir
            # o conteúdo e sem converter falha em cópia entre volumes.
            if old != new:
                os.rename(old, new)

    def update_pending(self, item, payload):
        old, new = payload['old_path'], payload['new_path']
        changed = []
        if payload['action'] not in ('move', 'rename'):
            return changed
        for other in self.items:
            if other.operation_id == item.operation_id:
                continue
            if under(other.path, old):
                same = norm(other.path) == norm(old)
                other.path = rebase(other.path, old, new)
                other.file_id = norm(other.path)
                other.file_name = os.path.basename(other.path)
                if same:
                    other.asset_id = item.asset_id
                if other.action_type == 'move':
                    other.old_value = other.path
                    # Mover mantém o nome atual; sufixos explicitamente escolhidos
                    # continuam preservados quando diferentes do nome anterior.
                    if os.path.basename(other.new_value) == os.path.basename(old):
                        other.new_value = os.path.join(os.path.dirname(other.new_value), os.path.basename(new))
                if other.action_type == 'rename':
                    other.old_value = other.file_name
                changed.append(other)
            if other.action_type in ('move', 'create_folder') and under(other.new_value, old):
                other.new_value = rebase(other.new_value, old, new)
                if other not in changed:
                    changed.append(other)
        item.path = new
        item.file_id = norm(new)
        item.file_name = os.path.basename(new)
        return changed

    def settle_local(self, item, payload):
        self.catalog.apply(payload)  # Idempotente; diário separado não é transação com o catálogo.
        changed = self.update_pending(item, payload)
        self.store.record(item, 'waiting_cloud', payload, 'Aplicado; aguardando confirmação no Drive.', changed)
        self.applied += 1

    def check_one(self, item, op):
        payload, state = op['payload'], op['state']
        if payload.get('missing_journal'):
            raise ValueError('Fila importada com execução iniciada, mas sem diário local. Confira na instalação de origem; não repetir.')
        if state in ('applying', 'uncertain'):
            action = payload['action']
            if action in TAGS:
                confirmed, _ = self.cloud.confirm(payload)
                if not confirmed:
                    raise ValueError('Resultado da escrita de tags incerto. Nenhuma repetição automática; confira o Drive.')
            elif action in ('rename', 'move'):
                if not same_file(payload['new_path'], payload.get('stamp', {})) or (
                        norm(payload['old_path']) != norm(payload['new_path']) and os.path.exists(payload['old_path'])):
                    raise ValueError('Movimentação interrompida não comprovada pela identidade local; requer revisão.')
            else:
                raise ValueError('Criação/exclusão interrompida exige conferência manual; não será repetida.')
            self.store.record(item, 'local_applied', payload)
            state = 'local_applied'
        if state == 'local_applied':
            self.settle_local(item, payload)
        confirmed, fid = self.cloud.confirm(payload)
        if not confirmed:
            raise ValueError('Ainda aguardando o cliente de sincronização confirmar o estado no Drive.')
        payload['drive_id'] = fid
        self.store.record(item, 'completed', payload, 'Confirmado no Drive.', complete=True)
        self.items.remove(item)
        self.completed += 1

    def run(self, selected=(), check_only=False):
        selected = set(selected)
        errors = []
        cancelled_now = False
        # Pastas explicitamente aprovadas são criadas antes de seus dependentes.
        ordered = sorted(list(self.items), key=lambda i: (0, i.path.count(os.sep)) if i.action_type == 'create_folder' else (1, 0))
        for number, item in enumerate(ordered, 1):
            if self.cancelled():
                cancelled_now = True
                break
            op = self.store.operation(item.operation_id)
            active = op and op['state'] in ACTIVE
            if (check_only and (not active or selected and item.operation_id not in selected)
                    or not check_only and item.operation_id not in selected):
                continue
            self.progress(number, len(ordered), item.file_name)
            try:
                if active:
                    self.check_one(item, op)
                    continue
                if item.execution_state in ACTIVE:
                    self.store.record(item, 'uncertain', dict(action=item.action_type, missing_journal=True),
                                      'Execução importada sem diário local; não repetir.')
                    raise ValueError('Execução importada sem diário local; confira na instalação de origem.')
                with self.store.connect() as conn:
                    if conn.execute('SELECT 1 FROM execution WHERE id=?', (item.operation_id,)).fetchone():
                        raise ValueError('Operação legada executada/incerta. Revise sem recriar o ID para repetir.')
                payload = self.prepare(item)
                if self.cancelled():
                    cancelled_now = True
                    break
                self.store.record(item, 'applying', payload, 'Execução iniciada; não repetir se interrompida.')
                self.apply_local(item, payload)
                self.store.record(item, 'local_applied', payload)
                self.settle_local(item, payload)
                self.check_one(item, self.store.operation(item.operation_id))
            except DedupCancelled:
                cancelled_now = True
                break
            except Exception as exc:
                op = self.store.operation(item.operation_id)
                state = op['state'] if op and op['state'] in ACTIVE else 'blocked'
                payload = op['payload'] if op else {}
                self.store.record(item, state, payload, str(exc))
                errors.append(f'{item.file_name}: {exc}')
        return dict(completed=self.completed, applied=self.applied, errors=errors, cancelled=(cancelled_now or self.cancelled()),
                    waiting=len(self.store.operations(active_only=True)))

    def release_unapplied(self, selected):
        """Apenas mediante confirmação do usuário + releitura local/remota.

        Não desfaz efeitos nem libera ações reconhecidamente aplicadas.
        """
        if self.cfg.is_read_only():
            raise ValueError('Modo somente leitura.')
        released, errors = 0, []
        for item in list(self.items):
            if self.cancelled():
                break
            if item.operation_id not in selected:
                continue
            try:
                op = self.store.operation(item.operation_id)
                if not op or op['state'] not in ('applying', 'uncertain'):
                    raise ValueError('Somente tentativas incertas sem efeito comprovado podem ser liberadas.')
                p = op['payload']
                if p.get('missing_journal'):
                    raise ValueError('Diário de execução ausente: confira na instalação de origem. Não liberar repetição.')
                # Reconciliação explícita de mkdir/lixeira após queda entre o
                # efeito e a gravação do resultado: nunca executar outra vez.
                applied = ((p['action'] == 'create_folder' and os.path.isdir(p['new_path']))
                           or (p['action'] == 'delete' and not os.path.exists(p['old_path'])))
                if applied:
                    self.allowed(p['new_path'])
                    confirmed, fid = self.cloud.confirm(p)
                    if not confirmed:
                        raise ValueError('Efeito ainda não confirmado no Drive; bloqueio preservado.')
                    p['drive_id'] = fid
                    self.store.record(item, 'local_applied', p, 'Efeito reconciliado após conferência explícita e remota.')
                    self.settle_local(item, p)
                    self.check_one(item, self.store.operation(item.operation_id))
                    continue
                if p['action'] == 'create_folder':
                    self.allowed(p['new_path'])
                    if os.path.exists(p['new_path']) or not os.path.isdir(os.path.dirname(p['new_path'])):
                        raise ValueError('Pasta existe ou unidade/pasta pai indisponível.')
                    parent = self.cloud.folder(os.path.dirname(p['new_path']))
                    if self.cloud.children(parent, os.path.basename(p['new_path'])):
                        raise ValueError('Pasta já localizada no Drive; não liberar repetição.')
                else:
                    if item.path:
                        self.allowed(p['old_path'])
                        if not same_file(p['old_path'], p.get('stamp', {})):
                            raise ValueError('Origem original não comprovada pela identidade local.')
                    remote = self.cloud.bind(item, self.catalog, p['drive_id'])
                    if p['action'] in ('rename', 'move') and os.path.exists(p['new_path']):
                        raise ValueError('Destino já existe; reconcilie a operação em vez de repetir.')
                    if p['action'] in TAGS and (remote.get('description') or '') != (p['remote'].get('description') or ''):
                        raise ValueError('Descrição remota mudou; não autorizar nova escrita.')
                self.store.record(item, 'blocked', p,
                    'Nova tentativa liberada por confirmação explícita e verificação de ausência de efeito; exige nova aprovação.', reset=True)
                released += 1
            except DedupCancelled:
                break
            except Exception as exc:
                errors.append(f'{item.file_name}: {exc}')
        return dict(completed=self.completed, applied=self.applied, released=released, errors=errors,
                    waiting=len(self.store.operations(active_only=True)))


class ContextQueueWorker(QThread):
    progress = pyqtSignal(int, int, str)
    result = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, store_path, legacy_path, db_path, service, selected=(), check_only=False, parent=None, release=False):
        super().__init__(parent)
        self.args = store_path, legacy_path, db_path, service, selected, check_only, release

    def run(self):
        catalog = None
        try:
            from src.utils.config_manager import ConfigManager
            path, legacy, db, service, selected, check, release = self.args
            cfg = ConfigManager()
            store = ContextStore(path, legacy)
            store.initialize()
            catalog = QueueCatalog(db)
            last_progress = [0, 1]

            def emit_progress(cur, tot, msg):
                last_progress[0], last_progress[1] = cur, tot
                self.progress.emit(cur, tot, msg)

            cloud = QueueCloud(
                service, cfg,
                cancelled=self.isInterruptionRequested,
                on_wait=lambda msg: self.progress.emit(last_progress[0], last_progress[1], msg),
            )
            engine = ContextEngine(store, catalog, cloud, cfg, self.isInterruptionRequested, emit_progress)
            self.result.emit(engine.release_unapplied(selected) if release else engine.run(selected, check))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if catalog:
                catalog.close()
