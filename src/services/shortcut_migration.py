"""Migração Meu Drive -> banco compartilhado, com atalhos e diário local.

Somente metadados: nenhum conteúdo de mídia é aberto ou baixado. Uma operação
entre Drive e diário não é atômica; respostas incertas exigem leitura de retorno.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PyQt6.QtCore import QObject, pyqtSignal
from googleapiclient.errors import HttpError

from src.services.drive_retry import execute_drive_read
from src.services.hash_deduplicator import (
    DriveCatalog, FOLDER_MIME, META_FIELDS, DedupCancelled,
    escape_query, private_service,
)
from src.services.snapshot_management import assert_queue_empty
from src.utils.config_manager import ConfigManager
from src.services.name_safety import name_key, safe_windows_name

SHORTCUT = 'application/vnd.google-apps.shortcut'
FIELDS = META_FIELDS + ',shortcutDetails,appProperties,webViewLink'
MARKER = 'voximagoMigrationKey'


def folder_id(value, label='Pasta'):
    value = value.strip()
    if value.startswith('https://'):
        parsed = urlparse(value)
        if parsed.hostname != 'drive.google.com':
            raise ValueError('Use um link de pasta do Google Drive ou seu ID.')
        match = re.search(r'/folders/([\w-]+)', parsed.path)
        value = match.group(1) if match else parse_qs(parsed.query).get('id', [''])[0]
    if value == 'root':
        raise ValueError(f'{label}: informe uma pasta específica, não a raiz inteira de Meu Drive.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError(f'{label}: informe um link ou ID de pasta do Google Drive, não um caminho local.')
    return value


def fingerprint(item):
    digest = item.get('md5Checksum', '')
    size = int(item.get('size') or 0)
    return (digest.lower(), size) if re.fullmatch('[a-fA-F0-9]{32}', digest) and size > 0 else None


def same_content(a, b):
    key = fingerprint(a)
    return bool(key and key == fingerprint(b) and not (
        a.get('sha256Checksum') and b.get('sha256Checksum')
        and a['sha256Checksum'] != b['sha256Checksum']))


_IDENTITY_FIELDS_NO_VERSION = (
    'id', 'name', 'parents', 'driveId', 'size', 'md5Checksum',
    'sha256Checksum', 'description', 'trashed',
)


def unchanged_file(before, after, *, check_version=True):
    fields = (*_IDENTITY_FIELDS_NO_VERSION, 'version') if check_version else _IDENTITY_FIELDS_NO_VERSION
    return all(before.get(k) == after.get(k) for k in fields)


class MigrationAPI(DriveCatalog):
    def get(self, fid):
        headers = {}

        def make_request():
            headers.clear()
            request = self.service.files().get(fileId=fid, supportsAllDrives=True, fields=FIELDS)
            if hasattr(request, 'add_response_callback'):
                request.add_response_callback(lambda response: headers.update(response))
            return request

        item = execute_drive_read(
            make_request,
            cancelled=self.cancelled,
            cancel_error=DedupCancelled,
            on_wait=self.on_wait,
        )
        item['_etag'] = headers.get('etag') or headers.get('ETag')
        return item

    def listing(self, query, drive=None, fields=FIELDS):
        token, seen = None, set()
        while True:
            if self.cancelled():
                raise DedupCancelled('Operação cancelada.')
            args = dict(q=query, pageSize=1000, supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        fields=f'nextPageToken,incompleteSearch,files({fields})',
                        corpora='drive' if drive else 'user')
            if drive:
                args['driveId'] = drive
            if token:
                args['pageToken'] = token
            page = execute_drive_read(
                lambda a=args: self.service.files().list(**a),
                cancelled=self.cancelled,
                cancel_error=DedupCancelled,
                on_wait=self.on_wait,
            )
            if page.get('incompleteSearch'):
                raise ValueError('Inventário incompleto do Drive. Análise interrompida.')
            yield from page.get('files', [])
            token = page.get('nextPageToken')
            if not token:
                return
            if token in seen:
                raise ValueError('Paginação repetida.')
            seen.add(token)

    def children_of(self, parent, drive=None):
        return list(self.listing(f"'{escape_query(parent)}' in parents and trashed = false", drive))


class MigrationJournal:
    def __init__(self, db_name):
        directory = Path(db_name).resolve().parent / 'shortcut_migration'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'journal.db'
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created REAL, plan TEXT);
            CREATE TABLE IF NOT EXISTS items(job TEXT, id TEXT, position INTEGER,
                payload TEXT, state TEXT, error TEXT, PRIMARY KEY(job,id));
            CREATE TABLE IF NOT EXISTS objects(key TEXT PRIMARY KEY, payload TEXT,
                remote_id TEXT, state TEXT);
        ''')

    def save_plan(self, plan):
        with self.conn:
            self.conn.execute('INSERT INTO jobs VALUES (?,?,?)',
                              (plan['id'], time.time(), json.dumps({k: v for k, v in plan.items() if k != 'items'})))
            self.conn.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)', [
                (plan['id'], row['source']['id'], i, json.dumps(row),
                 'blocked' if row.get('error') else 'pending', row.get('error', ''))
                for i, row in enumerate(plan['items'])])

    def load(self, job):
        found = self.conn.execute('SELECT plan FROM jobs WHERE id=?', (job,)).fetchone()
        if not found:
            raise ValueError('Migração não encontrada no diário desta instalação.')
        plan = json.loads(found[0])
        plan['items'] = []
        for payload, state, error in self.conn.execute(
                'SELECT payload,state,error FROM items WHERE job=? ORDER BY position', (job,)):
            row = json.loads(payload)
            row.update(state=state, error=error)
            plan['items'].append(row)
        return plan

    def jobs(self):
        return [(jid, json.loads(p)['source']['name'], created) for jid, created, p in
                self.conn.execute('SELECT id,created,plan FROM jobs ORDER BY created DESC LIMIT 30')]

    def item(self, job, row, state, error=''):
        with self.conn:
            self.conn.execute('UPDATE items SET payload=?,state=?,error=? WHERE job=? AND id=?',
                              (json.dumps(row), state, error, job, row['source']['id']))
        row.update(state=state, error=error)

    def object(self, key):
        row = self.conn.execute('SELECT payload,remote_id,state FROM objects WHERE key=?', (key,)).fetchone()
        return (json.loads(row[0]), row[1], row[2]) if row else None

    def set_object(self, key, body, fid, state):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?,?)',
                              (key, json.dumps(body), fid, state))

    def delete_creating_object(self, key):
        with self.conn:
            self.conn.execute(
                "DELETE FROM objects WHERE key=? AND remote_id IS NULL AND state='creating'",
                (key,),
            )

    def close(self):
        self.conn.close()


class UncertainCreateError(ValueError):
    """Criação anterior sem retorno e ainda não localizada no Drive."""

    def __init__(self, key, message):
        super().__init__(message)
        self.object_key = key


def _is_uncertain_create_error(exc: Exception) -> bool:
    # Após execute(), erros de transporte/decodificação podem ocorrer quando
    # o objeto já existe. Só rejeições HTTP explícitas liberam nova criação;
    # uma exceção desconhecida nunca comprova que o Drive não gravou nada.
    if isinstance(exc, HttpError):
        try:
            status = int(exc.resp.status)
        except (AttributeError, TypeError, ValueError):
            return True
        return status not in (400, 401, 403, 404)
    return True


class ShortcutMigration:
    def __init__(self, service, db_name, cfg, cancelled=lambda: False, progress=lambda n, t: None):
        self.cfg, self.db_name, self.cancelled, self.progress = cfg, db_name, cancelled, progress
        self.bank = cfg.get('production_drive_id')
        self.archive_drive = cfg.get('sandbox_drive_id')
        if not self.bank or not self.archive_drive or self.bank == self.archive_drive:
            raise ValueError('Configure os Drives distintos Banco de Imagens e _TestesBanco.')
        self.api = MigrationAPI(
            private_service(service), self.bank, cancelled,
            on_wait=lambda msg: self.progress(-1, msg),
        )
        self._destination_names = {}

    def check_cancelled(self):
        if self.cancelled():
            raise DedupCancelled('Cancelado. As etapas confirmadas permanecem registradas.')

    def folder(self, fid, drive=None, *, writable=False):
        item = self.api.get(fid)
        if item.get('trashed') or item.get('mimeType') != FOLDER_MIME:
            raise ValueError('O destino/origem não é uma pasta ativa: ' + fid)
        if item.get('driveId') != drive:
            raise ValueError('Pasta pertence a outro Drive: ' + item.get('name', fid))
        if writable and not item.get('capabilities', {}).get('canAddChildren'):
            raise ValueError('Sem permissão para criar arquivos nesta pasta: ' + item['name'])
        return item

    def _is_own_my_drive_root(self, source):
        if source.get('parents'):
            return False
        own_root = execute_drive_read(
            lambda: self.api.service.files().get(fileId='root', fields='id'),
            cancelled=self.cancelled,
            cancel_error=DedupCancelled,
            on_wait=self.api.on_wait,
        )
        return bool(own_root.get('id') and own_root.get('id') == source['id'])

    def scan(self, source, destination):
        source_id = folder_id(source, 'Origem')
        destination_id = folder_id(destination, 'Destino')
        source = self.folder(source_id, writable=True)
        if self._is_own_my_drive_root(source):
            raise ValueError('Escolha uma pasta dentro de Meu Drive, não sua raiz.')
        destination = self.folder(destination_id, self.bank, writable=True)
        self.folder(self.archive_drive, self.archive_drive, writable=True)
        plan = dict(id=uuid.uuid4().hex, source=source, destination=destination,
                    bank=self.bank, archive_drive=self.archive_drive, folders=[], items=[], skipped_shortcuts=0)
        self.progress(-1, 'Lendo hashes de todo o Banco de Imagens (somente metadados)...')
        hashes = defaultdict(list)
        for i, item in enumerate(self.api.listing('trashed = false', self.bank), 1):
            if item.get('mimeType') not in (FOLDER_MIME, SHORTCUT) and fingerprint(item):
                hashes[fingerprint(item)].append(item)
            if i % 1000 == 0:
                self.progress(-1, f'Banco de Imagens: {i:,} metadados recebidos...')
        # DFS: arquivos de cada nível primeiro; atalhos nunca são seguidos.
        stack, visited = [(source, [source['name']])], set()
        while stack:
            self.check_cancelled()
            directory, parts = stack.pop()
            if directory['id'] in visited:
                raise ValueError('Pasta repetida na origem; análise interrompida.')
            visited.add(directory['id'])
            if directory.get('driveId') or directory.get('trashed'):
                raise ValueError('A origem mudou de Drive durante a análise.')
            plan['folders'].append(dict(meta=directory, parts=parts))
            children = self.api.children_of(directory['id'])
            subfolders = []
            for item in sorted(children, key=lambda v: (v.get('name', '').casefold(), v['id'])):
                if item.get('parents') != [directory['id']]:
                    raise ValueError('Hierarquia da origem mudou durante a análise.')
                if item['mimeType'] == SHORTCUT:
                    plan['skipped_shortcuts'] += 1
                    continue
                if item['mimeType'] == FOLDER_MIME:
                    subfolders.append((item, parts + [item['name']]))
                    continue
                row = dict(source=item, parts=parts + [item['name']], action='import',
                           target_id=item['id'], error='', state='pending')
                if not fingerprint(item):
                    row['error'] = 'Sem MD5/tamanho verificável; não será movido nem baixado.'
                elif not directory.get('capabilities', {}).get('canAddChildren'):
                    row['error'] = 'Sem permissão para criar atalho na pasta de origem.'
                elif not item.get('capabilities', {}).get('canMoveItemIntoTeamDrive'):
                    row['error'] = 'Sem permissão para mover este arquivo para um Drive compartilhado.'
                else:
                    candidates = hashes.get(fingerprint(item), [])
                    if any(not same_content(item, other) for other in candidates):
                        row['error'] = 'MD5/tamanho coincidem, mas SHA-256 diverge. Revisão necessária.'
                    elif candidates:
                        target = min(candidates, key=lambda v: (not bool(v.get('createdTime')), v.get('createdTime', ''), v['id']))
                        row.update(action='duplicate', target_id=target['id'],
                                   target_name=target['name'], candidates=len(candidates))
                    else:
                        hashes[fingerprint(item)].append(item)  # Repetições dentro da própria importação.
                plan['items'].append(row)
            stack.extend(reversed(subfolders))
            self.progress(-1, f'Origem: {len(visited):,} pastas; {len(plan["items"]):,} arquivos analisados...')
        self.plan_names(plan)
        journal = MigrationJournal(self.db_name)
        try:
            journal.save_plan(plan)
        finally:
            journal.close()
        return plan

    def plan_names(self, plan):
        """Prévia somente leitura, inclusive pastas novas e conflitos dentro do lote."""
        folders, inventories = {}, {}
        def children(parent, drive):
            key = (drive, parent)
            if key not in inventories:
                inventories[key] = self.api.children_of(parent, drive) if parent else []
            return inventories[key]
        occupied = {}
        for entry in plan['folders']:
            self.check_cancelled()
            meta = entry['meta']
            ancestor = folders.get((meta.get('parents') or [''])[0])
            parent = plan['destination']['id'] if meta['id'] == plan['source']['id'] else ancestor['remote_id']
            parent_key = parent or 'planned:' + meta['parents'][0]
            siblings = children(parent, self.bank)
            names = occupied.setdefault(parent_key, {name_key(x['name']) for x in siblings})
            key = self.key('folder', meta['id'], parent) if parent else None
            owned = [x for x in siblings if key and x.get('appProperties', {}).get(MARKER) == key]
            if len(owned) > 1:
                raise ValueError('Pasta de destino ambígua; revise antes de importar.')
            name = owned[0]['name'] if owned else meta['name']
            if not owned and name_key(name) in names:
                name += ' [origem ' + meta['id'] + ']'
            if ((not owned and name_key(name) in names) or not safe_windows_name(name)):
                raise ValueError('Nome de pasta indisponível/incompatível: ' + name)
            if owned and (owned[0].get('mimeType') != FOLDER_MIME or
                          sum(name_key(x['name']) == name_key(name) for x in siblings) != 1):
                raise ValueError('Pasta de destino ambígua: ' + name)
            names.add(name_key(name))
            entry.update(planned_name=name, remote_id=owned[0]['id'] if owned else None,
                         destination_parts=(ancestor['destination_parts'] if ancestor else [plan['destination']['name']]) + [name])
            folders[meta['id']] = entry
        archive = self.archive_drive
        for name in ('Banco_Apagado', 'APAGAR'):
            matches = [x for x in children(archive, self.archive_drive) if name_key(x['name']) == name_key(name)] if archive else []
            if len(matches) > 1 or matches and matches[0]['mimeType'] != FOLDER_MIME:
                raise ValueError('Pasta de arquivo ambígua: ' + name)
            archive = matches[0]['id'] if matches else None
        for row in plan['items']:
            folder = folders[row['source']['parents'][0]]
            duplicate = row['action'] == 'duplicate'
            parent = archive if duplicate else folder['remote_id']
            drive = self.archive_drive if duplicate else self.bank
            key = 'archive' if duplicate else (parent or 'planned:' + row['source']['parents'][0])
            names = occupied.setdefault(key, {name_key(x['name']) for x in children(parent, drive)})
            name = row['source']['name']
            if name_key(name) in names:
                stem, dot, ext = name.rpartition('.')
                name = (stem if dot else name) + ' [' + row['source']['id'] + ']' + ('.' + ext if dot else '')
            if name_key(name) in names or not safe_windows_name(name):
                row['error'] = 'Nome final ocupado ou incompatível com Windows; revisão necessária.'
            names.add(name_key(name))
            row['destination_name'] = name
            row['destination_parts'] = (['_TestesBanco', 'Banco_Apagado', 'APAGAR'] if duplicate else folder['destination_parts']) + [name]
        plan['name_plan_version'] = 1

    @staticmethod
    def key(*parts):
        return hashlib.sha256('\0'.join(parts).encode()).hexdigest()

    def create_verified(self, journal, key, body, drive):
        """Intento durável + propriedade remota. Timeout nunca provoca criação cega."""
        parent = body['parents'][0]
        query = (f"'{escape_query(parent)}' in parents and trashed = false and "
                 f"appProperties has {{ key='{MARKER}' and value='{key}' }}")
        found = list(self.api.listing(query, drive))
        if len(found) > 1:
            raise ValueError('Criação ambígua: mais de um objeto com o mesmo marcador.')
        recorded = journal.object(key)
        if found:
            obj = self.api.get(found[0]['id'])
        elif recorded:
            # Não recriar um atalho/pasta sumido ou uma criação com resposta incerta sem confirmação.
            if not recorded[1]:
                raise UncertainCreateError(
                    key,
                    'Criação anterior incerta e ainda não localizada; confira o diário antes de repetir.',
                )
            obj = self.api.get(recorded[1])
        else:
            self.check_cancelled()
            body = dict(body, appProperties={MARKER: key})
            # Construir o HttpRequest não envia a requisição. Se falhar aqui,
            # não há operação remota incerta para registrar no diário.
            request = self.api.service.files().create(body=body, supportsAllDrives=True, fields='id')
            journal.set_object(key, body, None, 'creating')
            try:
                created = request.execute()
            except Exception as exc:
                if not _is_uncertain_create_error(exc):
                    journal.delete_creating_object(key)
                raise
            journal.set_object(key, body, created['id'], 'created')
            obj = self.api.get(created['id'])
        if (obj.get('trashed') or obj.get('parents') != [parent]
                or obj.get('driveId') != drive or obj.get('mimeType') != body['mimeType']
                or obj.get('appProperties', {}).get(MARKER) != key
                or obj.get('name') != body['name']
                or (body.get('shortcutDetails') and obj.get('shortcutDetails', {}).get('targetId') != body['shortcutDetails']['targetId'])):
            raise ValueError('Objeto criado foi alterado ou não corresponde ao planejado.')
        journal.set_object(key, body, obj['id'], 'confirmed')
        return obj

    def ensure_archive(self, journal):
        parent = self.archive_drive
        for name in ('Banco_Apagado', 'APAGAR'):
            children = [v for v in self.api.children_of(parent, self.archive_drive) if name_key(v['name']) == name_key(name)]
            if len(children) > 1 or children and children[0]['mimeType'] != FOLDER_MIME:
                raise ValueError('Pasta de arquivo ambígua: ' + name)
            if children:
                parent = self.folder(children[0]['id'], self.archive_drive, writable=True)['id']
            else:
                obj = self.create_verified(journal, self.key('archive', parent, name),
                                           dict(name=name, mimeType=FOLDER_MIME, parents=[parent]), self.archive_drive)
                parent = obj['id']
        return parent

    def mirrored_folder(self, journal, source_folder, parent, planned_name=None):
        key = self.key('folder', source_folder['id'], parent)
        recorded = journal.object(key)
        if recorded:
            if recorded[0]['name'] != planned_name:
                raise ValueError('Nome da pasta no diário diverge da prévia; revise a operação.')
            confirmed = self.create_verified(journal, key, recorded[0], self.bank)
            if any(x['id'] != confirmed['id'] and name_key(x['name']) == name_key(planned_name)
                   for x in self.api.children_of(parent, self.bank)):
                raise ValueError('Conflito novo no nome da pasta; interrompido para revisão.')
            return confirmed
        children = self.api.children_of(parent, self.bank)
        owned = [x for x in children if x.get('appProperties', {}).get(MARKER) == key]
        if not planned_name:
            raise ValueError('Reanalise: esta revisão não possui nomes finais aprovados.')
        name = planned_name
        if len(owned) > 1 or owned and owned[0]['name'] != name:
            raise ValueError('A pasta mudou desde a prévia; reanalise.')
        if any(name_key(x['name']) == name_key(name) and (not owned or x['id'] != owned[0]['id']) for x in children):
            raise ValueError('Nome de pasta planejado foi ocupado; reanalise.')
        return self.create_verified(journal, key, dict(name=name, parents=[parent], mimeType=FOLDER_MIME), self.bank)

    @staticmethod
    def _folders_needed_for_rows(plan, rows):
        source_id = plan['source']['id']
        by_id = {f['meta']['id']: f for f in plan['folders']}
        writable_ids = {r['source']['parents'][0] for r in rows}
        mirror_ids = set()
        # Não pré-preencher: collect_ancestors para ao encontrar um ID visitado.
        # Verificar toda a hierarquia é diferente de exigir escrita nela.
        tree_ids = set()

        def collect_ancestors(fid, into):
            cur = fid
            while cur and cur not in into and cur in by_id:
                into.add(cur)
                if cur == source_id:
                    break
                parents = by_id[cur]['meta'].get('parents') or []
                cur = parents[0] if parents else None

        for r in rows:
            parent_id = r['source']['parents'][0]
            collect_ancestors(parent_id, tree_ids)
            if r['action'] == 'import':
                collect_ancestors(parent_id, mirror_ids)

        tree_folders = [f for f in plan['folders'] if f['meta']['id'] in tree_ids]
        mirror_folders = [f for f in plan['folders'] if f['meta']['id'] in mirror_ids]
        return writable_ids, tree_folders, mirror_folders

    def verify_source_tree(self, plan, rows=None):
        if rows is None:
            writable_ids = {f['meta']['id'] for f in plan['folders']}
            tree_folders = plan['folders']
        else:
            writable_ids, tree_folders, _ = self._folders_needed_for_rows(plan, rows)
        for folder in tree_folders:
            self.check_cancelled()
            expected = folder['meta']
            fresh = self.folder(expected['id'], writable=expected['id'] in writable_ids)
            if fresh['name'] != expected['name'] or (fresh.get('parents') or []) != (expected.get('parents') or []):
                raise ValueError('Uma pasta da origem mudou desde a análise; faça nova análise.')

    def destination_names(self, parent, drive, refresh=False):
        # Uma leitura por pasta por execução, não uma listagem inteira de APAGAR
        # para cada duplicata. Retomar sempre começa com inventários novos.
        key = (drive, parent)
        if refresh or key not in self._destination_names:
            self._destination_names[key] = {
                name_key(item['name']) for item in self.api.children_of(parent, drive)
            }
        return self._destination_names[key]

    def execute(self, job, selected):
        if self.cfg.is_read_only() or self.cfg.is_sandbox():
            raise ValueError('A execução exige modo Produção e permissão de escrita.')
        assert_queue_empty(self.db_name)
        self._destination_names.clear()
        journal = MigrationJournal(self.db_name)
        result = dict(completed=0, imported=0, duplicates=0, errors=[], cancelled=False,
                      uncertain_creates=[], journal=str(journal.path))
        try:
            plan = journal.load(job)
            if plan.get('name_plan_version') != 1:
                raise ValueError('Revisão antiga sem prévia de nomes finais. Faça uma nova análise antes de executar.')
            if plan['bank'] != self.bank or plan['archive_drive'] != self.archive_drive:
                raise ValueError('Os Drives configurados mudaram desde a análise.')
            selected = set(selected)
            rows = [r for r in plan['items'] if r['source']['id'] in selected and r['state'] not in ('completed', 'blocked')]
            if not rows:
                return result
            self.folder(plan['destination']['id'], self.bank, writable=True)
            self.verify_source_tree(plan, rows)
            _, _, mirror_folders = self._folders_needed_for_rows(plan, rows)
            # Uma revisão pode ser retomada dias depois. Não arquivar uma cópia nova
            # silenciosamente nem importar algo que agora já existe no banco.
            self.progress(-1, 'Revalidando hashes atuais do Banco de Imagens...')
            current_hashes = defaultdict(set)
            for item in self.api.listing('trashed = false', self.bank):
                if item.get('mimeType') not in (FOLDER_MIME, SHORTCUT) and fingerprint(item):
                    current_hashes[fingerprint(item)].add(item['id'])
            folders = {}
            for f in mirror_folders:
                self.check_cancelled()
                meta = f['meta']
                parent = (plan['destination']['id'] if meta['id'] == plan['source']['id']
                          else folders[meta['parents'][0]])
                try:
                    folders[meta['id']] = self.mirrored_folder(journal, meta, parent, f.get('planned_name'))['id']
                except UncertainCreateError as exc:
                    result['errors'].append(f"{meta['name']}: {exc}")
                    result['uncertain_creates'].append(exc.object_key)
                    return result
            try:
                archive = self.ensure_archive(journal) if any(r['action'] == 'duplicate' for r in rows) else None
            except UncertainCreateError as exc:
                result['errors'].append(f"APAGAR: {exc}")
                result['uncertain_creates'].append(exc.object_key)
                return result
            for index, row in enumerate(rows):
                self.check_cancelled()
                self.progress(int(index * 100 / len(rows)), f'Processando {index + 1:,}/{len(rows):,}: {row["source"]["name"]}')
                try:
                    if row['action'] == 'import' and current_hashes.get(fingerprint(row['source']), set()) - {row['source']['id']}:
                        raise ValueError('Surgiu outra cópia no banco após a análise; reanalise antes de escolher o alvo do atalho.')
                    self.execute_one(plan, row, folders, archive, journal)
                    if row['action'] == 'import':
                        current_hashes[fingerprint(row['source'])].add(row['source']['id'])
                    result['completed'] += 1
                    result['duplicates' if row['action'] == 'duplicate' else 'imported'] += 1
                except DedupCancelled:
                    raise
                except UncertainCreateError as exc:
                    journal.item(job, row, 'error', str(exc))
                    result['errors'].append(f'{row["source"]["name"]}: {exc}')
                    result['uncertain_creates'].append(exc.object_key)
                    break
                except Exception as exc:
                    journal.item(job, row, 'error', str(exc))
                    result['errors'].append(f'{row["source"]["name"]}: {exc}')
                    break  # Resposta incerta não é autorização para repetir nem continuar às cegas.
        except DedupCancelled:
            result['cancelled'] = True
        finally:
            journal.close()
        return result

    def execute_one(self, plan, row, folders, archive, journal):
        original = row['source']
        source_parent = original['parents'][0]
        dest_parent = archive if row['action'] == 'duplicate' else folders[source_parent]
        dest_drive = self.archive_drive if row['action'] == 'duplicate' else self.bank
        fresh = self.api.get(original['id'])
        moved_already = fresh.get('parents') == [dest_parent] and fresh.get('driveId') == dest_drive
        if not moved_already and not unchanged_file(original, fresh):
            raise ValueError('Arquivo alterado desde a análise; nenhuma nova movimentação autorizada.')
        if not same_content(original, fresh) or fresh.get('trashed'):
            raise ValueError('Conteúdo mudou ou arquivo está na lixeira.')
        if row['action'] == 'duplicate':
            target = self.api.get(row['target_id'])
            if target.get('driveId') != self.bank or target.get('trashed') or not same_content(fresh, target):
                raise ValueError('Cópia preservada não está confirmada no Banco de Imagens. Execute sua incorporação primeiro, se pendente.')
        else:
            target = fresh
        if not moved_already:
            if not fresh.get('capabilities', {}).get('canMoveItemIntoTeamDrive'):
                raise ValueError('Permissão de movimentação indisponível.')
            self.folder(source_parent, writable=True)
            self.folder(dest_parent, dest_drive, writable=True)
            names = self.destination_names(dest_parent, dest_drive, refresh=True)
            if not row.get('destination_name'):
                raise ValueError('Nome final não aprovado; reanalise.')
            elif name_key(row['destination_name']) in names:
                raise ValueError('Nome planejado foi ocupado desde a tentativa anterior; revisão necessária.')
        elif not row.get('destination_name') or fresh.get('name') != row['destination_name']:
            raise ValueError('Movimentação externa não registrada. Reanalise este arquivo.')
        row['destination_parent'] = dest_parent
        # O atalho já aponta ao alvo definitivo. Não alteramos atalhos preexistentes.
        shortcut_key = self.key('shortcut', original['id'], source_parent, row['target_id'])
        shortcut_body = dict(name=original['name'], mimeType=SHORTCUT, parents=[source_parent],
                             shortcutDetails={'targetId': row['target_id']})
        journal.item(plan['id'], row, 'preparing')
        shortcut = self.create_verified(journal, shortcut_key, shortcut_body, None)
        row['shortcut_id'] = shortcut['id']
        journal.item(plan['id'], row, 'shortcut_confirmed')
        self.check_cancelled()
        if not moved_already:
            current = self.api.get(original['id'])
            if not unchanged_file(fresh, current):
                raise ValueError('Arquivo mudou após a criação do atalho. Movimento bloqueado.')
            if name_key(row['destination_name']) in self.destination_names(dest_parent, dest_drive, refresh=True):
                raise ValueError('Nome ocupado após criar o atalho. Movimento bloqueado; revise o diário.')
            if row['action'] == 'duplicate':
                again = self.api.get(target['id'])
                if again.get('driveId') != self.bank or again.get('trashed') or not same_content(current, again):
                    raise ValueError('O alvo do atalho mudou. Movimento bloqueado.')
            journal.item(plan['id'], row, 'moving')
            self.api.update(current, addParents=dest_parent, removeParents=source_parent,
                            body={'name': row['destination_name']})
        confirmed = self.api.get(original['id'])
        if (confirmed.get('parents') != [dest_parent] or confirmed.get('driveId') != dest_drive
                or confirmed.get('name') != row['destination_name'] or confirmed.get('trashed')
                or not same_content(original, confirmed)):
            raise ValueError('Destino não confirmado; operação preservada para conferência.')
        # Uma requisição de movimento já enviada precisa ser conferida mesmo se
        # Cancelar foi pressionado durante a resposta. Não iniciar outra criação.
        shortcut_after = self.api.get(shortcut['id'])
        if (shortcut_after.get('trashed') or shortcut_after.get('mimeType') != SHORTCUT
                or shortcut_after.get('parents') != [source_parent]
                or shortcut_after.get('name') != original['name']
                or shortcut_after.get('shortcutDetails', {}).get('targetId') != row['target_id']):
            raise ValueError('Atalho mudou após o movimento; confira o diário.')
        final_target = self.api.get(row['target_id'])
        if final_target.get('driveId') != self.bank or final_target.get('trashed') or not same_content(original, final_target):
            raise ValueError('Alvo final do atalho não foi confirmado no banco.')
        names = self._destination_names.get((dest_drive, dest_parent))
        if names is not None:
            names.add(confirmed['name'].casefold())
        journal.item(plan['id'], row, 'completed')


class ShortcutMigrationWorker(QObject):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, service, db_name, *, source=None, destination=None, job=None, selected=()):
        super().__init__()
        self.service, self.db_name = service, db_name
        self.source, self.destination, self.job, self.selected = source, destination, job, selected
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            engine = ShortcutMigration(self.service, self.db_name, ConfigManager(),
                                       lambda: self.cancelled, self.progress.emit)
            if self.job:
                result = engine.execute(self.job, self.selected)
            else:
                result = engine.scan(self.source, self.destination)
            self.finished.emit(result)
        except DedupCancelled:
            self.finished.emit(dict(cancelled=True))
        except Exception as exc:
            self.failed.emit(str(exc))
