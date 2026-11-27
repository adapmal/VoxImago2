"""Desambiguação por ID no Drive: prévia, deduplicação direta na nuvem e diário de retomada.

Não exclui arquivos, não altera pastas/atalhos nem depende de arquivos locais.
Conflitos com conteúdo idêntico na mesma pasta fundem tags e arquivam cópias excedentes em Banco_Apagado.
"""
import json
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, QLockFile

from src.database.database import FileIndexer
from src.services.shortcut_migration import MigrationAPI, FOLDER_MIME, SHORTCUT, fingerprint, unchanged_file
from src.services.hash_deduplicator import (
    DedupCancelled,
    DedupJournal,
    clean_local_by_drive_id,
    compute_merged_tags,
    evaluate_group_discrepancies,
    escape_query,
    parse_tags,
    private_service,
    update_local_description_by_drive_id,
)

SCAN_FIELDS = (
    'id,name,mimeType,parents,driveId,trashed,size,md5Checksum,'
    'sha256Checksum,description,createdTime,version,'
    'capabilities(canEdit,canMoveItemOutOfDrive),imageMediaMetadata(time)'
)
from src.services.name_safety import name_key, numbered_name, safe_windows_name
from src.services.name_history import NameHistory
from src.services.snapshot_management import assert_queue_empty, local_backup
from src.utils.config_manager import ConfigManager
from src.utils.path_validation import validate_path_component

_DEDUP_ACTIONS = ('dedup_keep', 'dedup_move', 'dedup_keep_and_rename')
_RENAME_ACTIONS = ('rename', 'dedup_keep_and_rename')
_KEEP_CONTENT_FIELDS = ('id', 'name', 'parents', 'driveId', 'size', 'md5Checksum', 'sha256Checksum', 'trashed')


def _same_content_rank(item):
    return (
        -len(parse_tags(item.get('description', ''))),
        item.get('createdTime') or '9999',
        item['id'],
    )


def _archive_display(folder_segments, filename):
    return '/'.join(['_TestesBanco', 'Banco_Apagado', *folder_segments, filename])


def _rel_path(folder_segments, filename):
    return '\\'.join([*folder_segments, filename]) if folder_segments else filename


def _sha256_divergent(members):
    sha = {m.get('sha256Checksum') for m in members if m.get('sha256Checksum')}
    return len(sha) > 1


def _compute_subbucket_tags(keeper, dups, folder_segments):
    keeper_rel = dict(keeper, rel_path=_rel_path(folder_segments, keeper['name']))
    dups_rel = [dict(d, rel_path=_rel_path(folder_segments, d['name'])) for d in dups]
    disc = evaluate_group_discrepancies(keeper_rel, [keeper_rel, *dups_rel], roots=None)
    merged = compute_merged_tags(
        keeper.get('description', ''),
        keeper['name'],
        dups_rel,
        keeper_rel_path=keeper_rel['rel_path'],
        roots=None,
        extra_tags=disc['extra_tags'],
    )
    return merged, disc['reasons']


def _uncleaned_dedup_moves(db_name, job=None):
    dedup_db = Path(db_name).resolve().parent / 'dedup' / 'journal.db'
    if not dedup_db.exists():
        return {}
    conn = sqlite3.connect(dedup_db.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        has_moves = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='moves'").fetchone()
        if not has_moves:
            return {}
        out = {}
        for fid, state, raw in conn.execute("SELECT file_id, state, payload FROM moves WHERE state != 'cleaned'").fetchall():
            payload = json.loads(raw) if raw else {}
            if job is not None and not (payload.get('source') == 'name_disambiguation' and payload.get('job') == job):
                continue
            out[fid] = (state, payload)
        return out
    finally:
        conn.close()


def uncertain_operation_ids(db_name, plan):
    """Retorna IDs pendentes de conferência em qualquer dos dois diários."""
    if not plan:
        return set()
    ids = {
        r['source']['id'] for r in plan.get('items', [])
        if r.get('state') in ('writing', 'uncertain')
    }
    dj_moves = _uncleaned_dedup_moves(db_name, job=plan.get('id'))
    for row in plan.get('items', []):
        fid = row['source']['id']
        if (row.get('action') == 'dedup_move') and row.get('state') != 'completed' and fid in dj_moves:
            ids.add(fid)
    return ids


class NameJournal:
    def __init__(self, db_name):
        self.path = Path(db_name).resolve().parent / 'name_disambiguation' / 'journal.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, created REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS operations(job TEXT, fid TEXT, state TEXT, error TEXT,
                PRIMARY KEY(job,fid));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT,
                fid TEXT, action TEXT, payload TEXT, created REAL);
        ''')

    def save(self, plan):
        with self.conn:
            self.conn.execute('INSERT INTO plans VALUES(?,?,?)', (plan['id'], time.time(), json.dumps(plan)))
            self.conn.executemany('INSERT INTO operations VALUES(?,?,?,?)', [
                (plan['id'], row['source']['id'], 'blocked' if row['error'] else 'pending', row['error']) for row in plan['items']])

    def load(self, job):
        row = self.conn.execute('SELECT payload FROM plans WHERE id=?', (job,)).fetchone()
        if not row:
            raise ValueError('Revisão não encontrada.')
        plan = json.loads(row[0])
        states = {fid: (state, error) for fid, state, error in self.conn.execute('SELECT fid,state,error FROM operations WHERE job=?', (job,))}
        for row in plan['items']:
            row['state'], row['error'] = states[row['source']['id']]
            row.setdefault('action', 'rename')
        return plan

    def state(self, job, fid, state, error='', event_action=None, event_payload=None):
        with self.conn:
            self.conn.execute('UPDATE operations SET state=?,error=? WHERE job=? AND fid=?', (state, error, job, fid))
            if event_action:
                self.conn.execute(
                    'INSERT INTO events(job,fid,action,payload,created) VALUES(?,?,?,?,?)',
                    (job, fid, event_action, json.dumps(event_payload or {}, ensure_ascii=False), time.time()),
                )

    def events(self, job=None, fid=None):
        clauses, params = [], []
        if job is not None:
            clauses.append('job=?')
            params.append(job)
        if fid is not None:
            clauses.append('fid=?')
            params.append(fid)
        sql = 'SELECT seq,job,fid,action,payload,created FROM events'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY seq'
        return [
            dict(seq=r[0], job=r[1], fid=r[2], action=r[3], payload=json.loads(r[4]), created=r[5])
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def jobs(self):
        return self.conn.execute('SELECT id,created FROM plans ORDER BY created DESC LIMIT 50').fetchall()

    def uncertain_ids(self):
        return {r[0] for r in self.conn.execute("SELECT fid FROM operations WHERE state IN ('writing','uncertain')")}

    def close(self):
        self.conn.close()


class NameDisambiguation:
    def __init__(self, service, db_name, cfg, cancelled=lambda: False, progress=lambda n, t: None):
        self.db_name, self.cfg, self.cancelled, self.progress = db_name, cfg, cancelled, progress
        self.drive = cfg.get('production_drive_id')
        self.archive_drive = cfg.get('sandbox_drive_id')
        if not self.drive:
            raise ValueError('Configure o Drive de produção.')
        self.api = MigrationAPI(
            private_service(service), self.drive, cancelled,
            on_wait=lambda msg: self.progress(-1, msg),
        )

    def check_cancelled(self):
        if self.cancelled():
            raise DedupCancelled('Cancelado; alterações confirmadas permanecem no diário.')

    def _plan_group(self, parent, key, members, occupied, uncertain, dedup_uncleaned, resolve_folder):
        try:
            path, segments = resolve_folder(parent)
            error = ''
        except ValueError as exc:
            path, segments = f'[Hierarquia incompleta: {parent}]', []
            error = f'Hierarquia incompleta neste ramo; revisão bloqueada sem inferir caminho ({exc})'

        group = parent + ':' + key
        if not error and any(x.get('mimeType') in (FOLDER_MIME, SHORTCUT) for x in members):
            error = 'Conflito inclui pasta/atalho: revisão manual, sem renomeação automática.'
        if any(x['id'] in uncertain for x in members):
            error = 'Operação anterior incerta. Abra a revisão anterior e confira antes de gerar outro plano.'
        elif not error and any(x['id'] in dedup_uncleaned for x in members):
            error = 'Operação anterior de deduplicação registrada no diário; confira antes de gerar outro plano.'

        hashes = [fingerprint(x) for x in members]
        if not error and any(h is None for h in hashes):
            error = 'Sem hash/tamanho em alguma cópia; não foi possível comparar conteúdos.'

        by_hash = defaultdict(list)
        if not error:
            for item, fp in zip(members, hashes):
                by_hash[fp].append(item)
            for sub in by_hash.values():
                if _sha256_divergent(sub):
                    error = 'Divergência de SHA-256 com mesmo MD5/tamanho; revisão manual obrigatória.'
                    break

        if error or len(by_hash) == len(members):
            sorted_members = sorted(members, key=lambda x: (x.get('createdTime') or '9999', x['id']))
            temp_occ = set(occupied[parent])
            rows = []
            for item in sorted_members:
                name = numbered_name(item['name'], temp_occ) if not error else ''
                issue = error
                if not issue and (not safe_windows_name(name) or not item.get('capabilities', {}).get('canEdit')):
                    issue = 'Nome incompatível com Windows ou sem permissão de edição.'
                rows.append(dict(
                    source=item, group=group, folder_path=path, folder_segments=segments,
                    action='rename', new_name=name,
                    state='blocked' if issue else 'pending', error=issue,
                ))
            if not any(r['error'] for r in rows):
                occupied[parent] = temp_occ
            group_error = next((r['error'] for r in rows if r['error']), '')
            if group_error:
                for row in rows:
                    row.update(error=group_error, state='blocked', new_name='')
            return rows

        # Grupo com conteúdo idêntico na mesma pasta (puro ou misto).
        group_error = ''
        if not self.archive_drive or self.archive_drive == self.drive:
            group_error = 'Configure o Drive _TestesBanco distinto do Banco de Imagens para arquivar duplicatas.'
        else:
            for seg in segments:
                valid, reason = validate_path_component(seg, 'pasta de destino')
                if not valid:
                    group_error = f'Subpasta incompatível com arquivamento: {reason}'
                    break

        subbuckets = []
        for fp, sub in by_hash.items():
            ranked = sorted(sub, key=_same_content_rank)
            sub_keeper, sub_dups = ranked[0], ranked[1:]
            merged, disc_reasons = ('', [])
            if sub_dups:
                merged, disc_reasons = _compute_subbucket_tags(sub_keeper, sub_dups, segments)
                if not group_error and len(merged.encode('utf-8')) > 30000:
                    group_error = 'Descrição resultante excede o limite seguro; nenhuma tag foi truncada.'
                if not group_error and not sub_keeper.get('capabilities', {}).get('canEdit'):
                    group_error = 'Sem permissão de edição para gravar tags no preservado.'
                for dup in sub_dups:
                    if not group_error and not dup.get('capabilities', {}).get('canMoveItemOutOfDrive'):
                        group_error = 'Sem permissão para mover cópia duplicada para _TestesBanco.'
                        break
                    if not group_error and not safe_windows_name(dup['name']):
                        group_error = 'Nome incompatível com Windows para arquivar em Banco_Apagado.'
                        break
            subbuckets.append((fp, sub_keeper, sub_dups, merged, disc_reasons))

        rows = []
        if len(by_hash) == 1:
            fp, keeper, dups, merged, disc_reasons = subbuckets[0]
            rows.append(dict(
                source=keeper, group=group, folder_path=path, folder_segments=segments,
                action='dedup_keep', new_name=keeper['name'], keeper_id=keeper['id'],
                dedup_key=[fp[0], fp[1]], merged_description=merged, discrepancy_reasons=disc_reasons,
                state='blocked' if group_error else 'pending', error=group_error,
            ))
            for dup in dups:
                rows.append(dict(
                    source=dup, group=group, folder_path=path, folder_segments=segments,
                    action='dedup_move', new_name='', keeper_id=keeper['id'],
                    dedup_key=[fp[0], fp[1]], archive_path=_archive_display(segments, dup['name']),
                    state='blocked' if group_error else 'pending', error=group_error,
                ))
            return rows

        # Grupo misto: primeiro deduplica subgrupos idênticos e depois renomeia os distintos restantes.
        subbuckets.sort(key=lambda entry: (entry[1].get('createdTime') or '9999', entry[1]['id']))
        temp_occ = set(occupied[parent])
        for fp, survivor, dups, merged, disc_reasons in subbuckets:
            new_name = numbered_name(survivor['name'], temp_occ) if not group_error else ''
            if not group_error and (not safe_windows_name(new_name) or not survivor.get('capabilities', {}).get('canEdit')):
                group_error = 'Nome incompatível com Windows ou sem permissão de edição.'
            action = 'dedup_keep_and_rename' if dups else 'rename'
            keeper_row = dict(
                source=survivor, group=group, folder_path=path, folder_segments=segments,
                action=action, new_name=new_name, keeper_id=survivor['id'] if dups else None,
                dedup_key=[fp[0], fp[1]] if dups else None,
                merged_description=merged if dups else '', discrepancy_reasons=disc_reasons if dups else [],
                state='blocked' if group_error else 'pending', error=group_error,
            )
            rows.append(keeper_row)
            for dup in dups:
                rows.append(dict(
                    source=dup, group=group, folder_path=path, folder_segments=segments,
                    action='dedup_move', new_name='', keeper_id=survivor['id'],
                    dedup_key=[fp[0], fp[1]], archive_path=_archive_display(segments, dup['name']),
                    state='blocked' if group_error else 'pending', error=group_error,
                ))

        if group_error:
            for row in rows:
                row.update(error=group_error, state='blocked')
                if row['action'] in _RENAME_ACTIONS:
                    row['new_name'] = ''
        else:
            occupied[parent] = temp_occ
        return rows

    def scan(self):
        if self.cfg.is_sandbox():
            raise ValueError('Esta ferramenta analisa o Banco de Imagens em modo Produção.')
        self.progress(-1, 'Lendo nomes e hashes do Banco de Imagens; nenhuma mídia será baixada...')
        by_id, buckets, occupied = {}, defaultdict(list), defaultdict(set)
        for index, item in enumerate(self.api.listing('trashed=false', self.drive, fields=SCAN_FIELDS), 1):
            self.check_cancelled()
            if item['id'] in by_id:
                raise ValueError('Inventário repetido; refaça a análise.')
            by_id[item['id']] = item
            parents = item.get('parents') or []
            if len(parents) == 1:
                buckets[(parents[0], name_key(item['name']))].append(item)
                occupied[parents[0]].add(name_key(item['name']))
            if index % 1000 == 0:
                self.progress(-1, f'{index:,} metadados analisados...')

        folder_context = {}

        def resolve_folder(fid):
            names, seen = [], set()
            while fid != self.drive:
                if fid in seen or fid not in by_id:
                    raise ValueError('Hierarquia incompleta ou cíclica; não inferir caminhos.')
                seen.add(fid)
                folder = by_id[fid]
                if folder.get('mimeType') != FOLDER_MIME or len(folder.get('parents', [])) != 1:
                    raise ValueError('Pasta sem hierarquia verificável.')
                names.append(folder['name'])
                folder_context[fid] = {k: folder.get(k) for k in ('id', 'name', 'parents', 'driveId', 'trashed', 'mimeType')}
                fid = folder['parents'][0]
            segments = list(reversed(names))
            return '/'.join(['Banco de Imagens'] + segments), segments

        journal = NameJournal(self.db_name)
        try:
            uncertain = journal.uncertain_ids()
            dedup_uncleaned = set(_uncleaned_dedup_moves(self.db_name))
            plan = dict(id=uuid.uuid4().hex, drive=self.drive, schema=2, items=[], folders=folder_context)
            for (parent, key), members in sorted(buckets.items()):
                if len(members) < 2:
                    continue
                self.check_cancelled()
                plan['items'].extend(
                    self._plan_group(parent, key, members, occupied, uncertain, dedup_uncleaned, resolve_folder)
                )
            journal.save(plan)
            return plan
        finally:
            journal.close()

    IDENTITY_FIELDS = ('id', 'parents', 'driveId', 'size', 'md5Checksum', 'sha256Checksum', 'description', 'trashed')

    def _run_locked(self, action):
        lock_path = Path(self.db_name).resolve().parent / 'name_disambiguation' / 'execution.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(lock_path))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError('Já existe uma desambiguação em execução nesta instalação.')
        try:
            return action()
        finally:
            lock.unlock()

    def _verify_ancestors(self, plan, parent):
        seen = set()
        while parent != self.drive:
            if parent in seen or parent not in plan.get('folders', {}):
                raise ValueError('Plano sem hierarquia verificável; refaça a análise.')
            seen.add(parent)
            expected_folder = plan['folders'][parent]
            current_folder = self.api.get(parent)
            if any(current_folder.get(k) != v for k, v in expected_folder.items()):
                raise ValueError('Pasta foi movida/renomeada desde a prévia; reanalise o grupo.')
            parent = expected_folder['parents'][0]

    @classmethod
    def _matches_renamed(cls, fresh, row, *, expected_desc=None):
        if fresh.get('name') != row['new_name']:
            return False
        for key in cls.IDENTITY_FIELDS:
            expected = expected_desc if (key == 'description' and expected_desc is not None) else row['source'].get(key)
            if fresh.get(key) != expected:
                return False
        return True

    def _latest_event(self, journal, job, fid, actions):
        matches = [ev for ev in journal.events(job=job, fid=fid) if ev['action'] in actions]
        return matches[-1] if matches else None

    @staticmethod
    def _matches_moved(fresh, payload):
        return bool(
            payload
            and not fresh.get('trashed')
            and fresh.get('parents') == [payload['destination_parent']]
            and fresh.get('driveId') == payload['destination_drive']
            and fresh.get('name') == payload['destination_name']
            and fresh.get('md5Checksum') == payload['md5']
            and int(fresh.get('size') or 0) == int(payload['size'])
        )

    def _verify_keeper_state(self, job, keeper_row, fresh_keeper, journal, *, anchor=None):
        """Confere conteúdo, localização e descrição gravada do preservado contra o plano e âncora."""
        if any(fresh_keeper.get(k) != keeper_row['source'].get(k) for k in _KEEP_CONTENT_FIELDS):
            raise ValueError('Arquivo preservado mudou de conteúdo, nome ou pasta; grupo bloqueado.')
        state = keeper_row['state']
        fid = keeper_row['source']['id']
        if state == 'pending':
            if not unchanged_file(keeper_row['source'], fresh_keeper, check_version=False):
                raise ValueError('Arquivo preservado mudou desde a análise; grupo bloqueado.')
        elif state in ('tags_done', 'completed'):
            if keeper_row.get('action') == 'dedup_keep':
                ev = self._latest_event(journal, job, fid, ('dedup_kept', 'uncertain_confirmed_completed'))
                exp_desc = (
                    ev['payload'].get('description', '')
                    if (ev and 'description' in ev['payload'])
                    else keeper_row['source'].get('description', '')
                )
                exp_ver = ev['payload'].get('version', '') if ev else keeper_row['source'].get('version', '')
            else:
                ev = self._latest_event(journal, job, fid, ('tags_confirmed',))
                if not ev:
                    raise ValueError('Registro intermediário de tags ausente; confira o grupo.')
                exp_desc = ev['payload'].get('description', '')
                exp_ver = ev['payload'].get('version', '')
            if fresh_keeper.get('description', '') != exp_desc or (exp_ver and fresh_keeper.get('version') != exp_ver):
                raise ValueError('Descrição ou versão do preservado mudaram após fusão de tags; confira antes de continuar.')
        if anchor is not None and any(
            fresh_keeper.get(k) != anchor.get(k) for k in (*_KEEP_CONTENT_FIELDS, 'description', 'version')
        ):
            raise ValueError('Arquivo preservado mudou durante a execução; movimento bloqueado.')

    def _archive_folder(self, parent, name, dest_drive, dedup_journal, folder_cache):
        valid, reason = validate_path_component(name, 'pasta de destino')
        if not valid:
            raise ValueError(reason)
        cache_key = (parent, name_key(name), dest_drive)
        if cache_key in folder_cache:
            return folder_cache[cache_key]
        found = [
            item for item in self.api.listing(f"'{escape_query(parent)}' in parents and trashed = false", dest_drive, fields='id,name,mimeType')
            if name_key(item.get('name', '')) == name_key(name)
        ]
        if len(found) > 1 or (found and found[0].get('mimeType') != FOLDER_MIME):
            raise ValueError('Pasta de destino ambígua ou ocupada por um arquivo: ' + name)
        if found:
            result = found[0]['id']
        else:
            dedup_journal.event('folder_create_requested', {'parent': parent, 'name': name})
            result = self.api.service.files().create(
                body={'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent]},
                supportsAllDrives=True, fields='id',
            ).execute()['id']
            dedup_journal.event('folder_created', {'parent': parent, 'name': name, 'id': result})
        fresh = self.api.get(result)
        if fresh.get('trashed') or fresh.get('driveId') != dest_drive or not fresh.get('capabilities', {}).get('canAddChildren'):
            raise ValueError('Sem permissão para adicionar arquivos à pasta de destino.')
        folder_cache[cache_key] = result
        return result

    def _archive_destination(self, folder_segments, dest_drive, dedup_journal, folder_cache):
        parent = dest_drive
        for seg in ['Banco_Apagado', *folder_segments]:
            parent = self._archive_folder(parent, seg, dest_drive, dedup_journal, folder_cache)
        return parent

    def _unique_archive_name(self, parent, name, dest_drive, file_id):
        valid, reason = validate_path_component(name, 'nome do arquivo')
        if not valid:
            raise ValueError(reason)
        names = {
            name_key(item.get('name', ''))
            for item in self.api.listing(f"'{escape_query(parent)}' in parents and trashed = false", dest_drive, fields='id,name')
        }
        if name_key(name) not in names:
            return name
        import ntpath
        stem, ext = ntpath.splitext(name)
        candidate = stem[:180] + '_dup_' + file_id + ext
        valid, reason = validate_path_component(candidate, 'nome de arquivamento')
        if not valid or name_key(candidate) in names:
            raise ValueError('Colisão no destino; revisão manual necessária. ' + reason)
        return candidate

    def _validate_group(self, plan, job, members, journal, dedup_journal):
        parent_id = members[0]['source']['parents'][0]
        self._verify_ancestors(plan, parent_id)
        siblings = self.api.children_of(parent_id, self.drive)
        member_ids = {r['source']['id'] for r in members}
        old_key = name_key(members[0]['source']['name'])
        if any(name_key(s['name']) == old_key and s['id'] not in member_ids for s in siblings):
            raise ValueError('Surgiu outra cópia com este nome. Refaça a análise.')

        fresh_by_id = {}
        for row in members:
            fid = row['source']['id']
            fresh = self.api.get(fid)
            fresh_by_id[fid] = fresh
            action = row.get('action') or 'rename'
            if row['state'] in ('writing', 'uncertain'):
                if action == 'dedup_move':
                    move_ev = self._latest_event(journal, job, fid, ('dedup_move_writing',))
                    payload = move_ev['payload'] if move_ev else None
                    if payload is None and dedup_journal:
                        dj_rec = dedup_journal.conn.execute('SELECT payload FROM moves WHERE file_id=?', (fid,)).fetchone()
                        if dj_rec and dj_rec[0]:
                            dj_p = json.loads(dj_rec[0])
                            if dj_p.get('source') == 'name_disambiguation' and dj_p.get('job') == job:
                                payload = dj_p
                    if not self._matches_moved(fresh, payload):
                        raise ValueError('Operação anterior sem confirmação. Não repetir movimentação; confira o arquivo pelo ID.')
                else:
                    tag_ev = self._latest_event(journal, job, fid, ('writing_tags',))
                    rename_ev = self._latest_event(journal, job, fid, ('writing',))
                    stuck_in_tags = bool(tag_ev and (not rename_ev or tag_ev['seq'] > rename_ev['seq']))
                    if stuck_in_tags:
                        target_desc = tag_ev['payload'].get('description', '')
                        if fresh.get('description', '') != target_desc or any(
                            fresh.get(k) != row['source'].get(k) for k in _KEEP_CONTENT_FIELDS
                        ):
                            raise ValueError('Gravação anterior de tags sem confirmação. Confira o arquivo pelo ID.')
                    else:
                        ev = self._latest_event(journal, job, fid, ('tags_confirmed',))
                        exp_desc = ev['payload'].get('description') if ev else None
                        if not self._matches_renamed(fresh, row, expected_desc=exp_desc):
                            raise ValueError('Operação anterior sem confirmação. Não repetir renomeação; confira o arquivo pelo ID.')
            elif row['state'] == 'completed':
                if action == 'dedup_move':
                    if fresh.get('trashed') or parent_id in (fresh.get('parents') or []):
                        raise ValueError('Cópia marcada como movida ainda está na pasta de origem.')
                elif action == 'dedup_keep':
                    self._verify_keeper_state(job, row, fresh, journal)
                else:
                    ev = self._latest_event(journal, job, fid, ('tags_confirmed',))
                    exp_desc = ev['payload'].get('description') if ev else None
                    if not self._matches_renamed(fresh, row, expected_desc=exp_desc):
                        raise ValueError('Membro confirmado mudou após conclusão; confira o grupo.')
            elif row['state'] == 'tags_done':
                self._verify_keeper_state(job, row, fresh, journal)
            else:
                # Abrir o arquivo no Drive Web incrementa `version` via viewedByMeTime sem mudar conteúdo/tags.
                if not unchanged_file(row['source'], fresh, check_version=False):
                    raise ValueError('Arquivo mudou desde a análise; reanalise o grupo.')

            if action in _RENAME_ACTIONS and row['state'] != 'completed':
                if not safe_windows_name(row['new_name']) or not fresh.get('capabilities', {}).get('canEdit'):
                    raise ValueError('Nome inválido ou sem permissão de edição.')
                if any(s['id'] not in member_ids and name_key(s['name']) == name_key(row['new_name']) for s in siblings):
                    raise ValueError('Nome aprovado foi ocupado; nenhuma nova alteração deste grupo.')

        pending_dups = [r for r in members if (r.get('action') == 'dedup_move') and r['state'] != 'completed']
        if pending_dups:
            if not self.archive_drive or self.archive_drive == self.drive:
                raise ValueError('Configure o Drive _TestesBanco distinto do Banco de Imagens.')
            for dup in pending_dups:
                fid = dup['source']['id']
                if dedup_journal:
                    dj_row = dedup_journal.conn.execute('SELECT state, payload FROM moves WHERE file_id=?', (fid,)).fetchone()
                    if dj_row:
                        dj_payload = json.loads(dj_row[1]) if dj_row[1] else {}
                        same_job_entry = (
                            dj_payload.get('source') == 'name_disambiguation'
                            and dj_payload.get('job') == job
                            and dj_row[0] in ('uncertain', 'confirmed')
                        )
                        if not (same_job_entry and self._matches_moved(fresh_by_id[fid], dj_payload)):
                            raise ValueError('Arquivo já consta no histórico de deduplicação; confira o relatório ou use Conferir / liberar incertas antes de repetir.')
                if not fresh_by_id[fid].get('capabilities', {}).get('canMoveItemOutOfDrive'):
                    raise ValueError('Sem permissão para mover cópia duplicada para _TestesBanco.')
                if not safe_windows_name(fresh_by_id[fid]['name']):
                    raise ValueError('Nome incompatível com Windows para arquivar em Banco_Apagado.')
                for seg in dup.get('folder_segments') or []:
                    valid, reason = validate_path_component(seg, 'pasta de destino')
                    if not valid:
                        raise ValueError(reason)
            by_keeper = defaultdict(list)
            rows_by_id = {r['source']['id']: r for r in members}
            for dup in pending_dups:
                by_keeper[dup['keeper_id']].append(dup)
            for keeper_id, dups in by_keeper.items():
                k_row = rows_by_id.get(keeper_id)
                k_fresh = fresh_by_id[keeper_id]
                if not k_fresh.get('capabilities', {}).get('canEdit'):
                    raise ValueError('Sem permissão de edição no arquivo preservado.')
                if _sha256_divergent([k_fresh, *[fresh_by_id[d['source']['id']] for d in dups]]):
                    raise ValueError('Divergência de SHA-256 detectada na conferência; grupo bloqueado.')
                segments = (k_row or dups[0]).get('folder_segments') or []
                merged, _ = _compute_subbucket_tags(k_fresh, [fresh_by_id[d['source']['id']] for d in dups], segments)
                if len(merged.encode('utf-8')) > 30000:
                    raise ValueError('Descrição resultante excede o limite seguro; nenhuma tag foi truncada.')

        return fresh_by_id

    def _run_dedup_subbucket(self, job, keeper_row, dup_rows, fresh_by_id, journal, dedup_journal, indexer, folder_cache, result):
        keeper_id = keeper_row['source']['id']
        parent_id = keeper_row['source']['parents'][0]
        segments = keeper_row.get('folder_segments') or []
        fresh_keeper = self.api.get(keeper_id)
        self._verify_keeper_state(job, keeper_row, fresh_keeper, journal, anchor=fresh_by_id[keeper_id])

        dups_live = []
        for dup_row in dup_rows:
            d_live = self.api.get(dup_row['source']['id'])
            if dup_row['state'] == 'pending' and not unchanged_file(dup_row['source'], d_live, check_version=False):
                raise ValueError('Cópia duplicada mudou antes da fusão; grupo bloqueado.')
            dups_live.append(d_live)
        if _sha256_divergent([fresh_keeper, *dups_live]):
            raise ValueError('Divergência de SHA-256 detectada antes da gravação; grupo bloqueado.')
        merged, _ = _compute_subbucket_tags(fresh_keeper, dups_live, segments)
        if len(merged.encode('utf-8')) > 30000:
            raise ValueError('Descrição resultante excede o limite seguro; nenhuma tag foi truncada.')

        if keeper_row['state'] in ('pending', 'writing', 'uncertain'):
            confirmed_keeper = fresh_keeper
            if keeper_row['state'] == 'pending' and merged != (fresh_keeper.get('description') or ''):
                self.progress(0, 'Fundindo tags do preservado: ' + fresh_keeper['name'])
                guarded_k = self.api.prepare_write(fresh_keeper)
                journal.state(
                    job, keeper_id, 'writing',
                    event_action='writing_tags',
                    event_payload={'description': merged, 'action': keeper_row['action'], 'name': fresh_keeper['name']},
                )
                try:
                    self.api.update(guarded_k, body={'description': merged})
                except Exception as exc:
                    journal.state(job, keeper_id, 'uncertain', str(exc),
                                  event_action='uncertain', event_payload={'error': str(exc), 'phase': 'tags'})
                    raise
                confirmed_keeper = self.api.get(keeper_id)
                if confirmed_keeper.get('description', '') != merged or any(
                    confirmed_keeper.get(k) != fresh_keeper.get(k) for k in _KEEP_CONTENT_FIELDS
                ):
                    raise ValueError('Gravação de tags não confirmada; nenhum duplicado foi movido.')
                result['tags_updated'] += 1
                update_local_description_by_drive_id(indexer, keeper_id, merged)
            elif keeper_row['state'] in ('writing', 'uncertain'):
                update_local_description_by_drive_id(indexer, keeper_id, fresh_keeper.get('description', ''))
            dedup_journal.event('tags_confirmed', {'keeper': keeper_id, 'description': confirmed_keeper.get('description', '')})
            if keeper_row['action'] == 'dedup_keep':
                journal.state(
                    job, keeper_id, 'completed',
                    event_action='dedup_kept',
                    event_payload={'description': confirmed_keeper.get('description', ''), 'version': confirmed_keeper.get('version', ''), 'name': confirmed_keeper.get('name', '')},
                )
                keeper_row['state'] = 'completed'
                result['completed'] += 1
            else:
                journal.state(
                    job, keeper_id, 'tags_done',
                    event_action='tags_confirmed',
                    event_payload={'description': confirmed_keeper.get('description', ''), 'version': confirmed_keeper.get('version', ''), 'name': confirmed_keeper.get('name', '')},
                )
                keeper_row['state'] = 'tags_done'
            fresh_by_id[keeper_id] = confirmed_keeper
        else:
            fresh_by_id[keeper_id] = fresh_keeper

        keeper_anchor = fresh_by_id[keeper_id]
        for dup_row in dup_rows:
            self.check_cancelled()
            dup_id = dup_row['source']['id']
            self.progress(0, 'Arquivando duplicata na nuvem: ' + dup_row['source']['name'])
            dj_rec = dedup_journal.conn.execute('SELECT state, payload FROM moves WHERE file_id=?', (dup_id,)).fetchone()
            dj_payload = json.loads(dj_rec[1]) if (dj_rec and dj_rec[1]) else None
            if dup_row['state'] in ('writing', 'uncertain') or (
                dj_payload and dj_payload.get('source') == 'name_disambiguation' and dj_payload.get('job') == job
            ):
                move_ev = self._latest_event(journal, job, dup_id, ('dedup_move_writing',))
                payload = move_ev['payload'] if move_ev else dj_payload
                moved = self.api.get(dup_id)
                if not self._matches_moved(moved, payload):
                    raise ValueError('Destino remoto não confirmado; operação preservada no diário.')
                dedup_journal.state(dup_id, 'confirmed')
                dedup_journal.event('move_confirmed', payload)
                clean_local_by_drive_id(indexer, dup_id)
                dedup_journal.state(dup_id, 'cleaned')
                journal.state(job, dup_id, 'completed', event_action='dedup_moved', event_payload=payload)
                dup_row['state'] = 'completed'
                result['dedup_moved'] += 1
                result['completed'] += 1
                continue
            fresh_dup = self.api.get(dup_id)
            if not unchanged_file(dup_row['source'], fresh_dup, check_version=False):
                raise ValueError('Duplicado mudou antes da movimentação; bloqueado.')
            keeper_now = self.api.get(keeper_id)
            self._verify_keeper_state(job, keeper_row, keeper_now, journal, anchor=keeper_anchor)
            dest_parent = self._archive_destination(segments, self.archive_drive, dedup_journal, folder_cache)
            dest_name = self._unique_archive_name(dest_parent, fresh_dup['name'], self.archive_drive, dup_id)
            payload = dict(
                file_id=dup_id,
                local_id='',
                origin_path=dup_row['folder_path'] + '/' + fresh_dup['name'],
                original_parents=fresh_dup['parents'],
                original_name=fresh_dup['name'],
                destination_parent=dest_parent,
                destination_drive=self.archive_drive,
                destination_name=dest_name,
                relative_path=_rel_path(segments, fresh_dup['name']),
                keeper_id=keeper_id,
                md5=fresh_dup['md5Checksum'],
                size=int(fresh_dup['size']),
                source='name_disambiguation',
                job=job,
            )
            journal.state(job, dup_id, 'writing', event_action='dedup_move_writing', event_payload=payload)
            dedup_journal.begin_move(dup_id, payload)
            try:
                guarded_dup = self.api.prepare_write(fresh_dup)
                self.api.update(
                    guarded_dup,
                    addParents=dest_parent,
                    removeParents=','.join(fresh_dup['parents']),
                    body={'name': dest_name},
                )
            except Exception as exc:
                journal.state(job, dup_id, 'uncertain', str(exc),
                              event_action='uncertain', event_payload={'error': str(exc), 'phase': 'dedup_move'})
                raise
            moved = self.api.get(dup_id)
            if not self._matches_moved(moved, payload):
                raise ValueError('Destino remoto não confirmado; operação preservada no diário.')
            dedup_journal.state(dup_id, 'confirmed')
            dedup_journal.event('move_confirmed', payload)
            clean_local_by_drive_id(indexer, dup_id)
            dedup_journal.state(dup_id, 'cleaned')
            journal.state(job, dup_id, 'completed', event_action='dedup_moved', event_payload=payload)
            dup_row['state'] = 'completed'
            result['dedup_moved'] += 1
            result['completed'] += 1

    def _rename_row(self, job, row, journal, history, result):
        fid = row['source']['id']
        parent_id = row['source']['parents'][0]
        siblings = self.api.children_of(parent_id, self.drive)
        if any(s['id'] != fid and name_key(s['name']) == name_key(row['new_name']) for s in siblings):
            raise ValueError('Destino ocupado durante a execução; grupo interrompido.')
        tags_ev = self._latest_event(journal, job, fid, ('tags_confirmed',))
        expected_desc = tags_ev['payload'].get('description') if tags_ev else None
        if row['state'] not in ('writing', 'uncertain'):
            fresh = self.api.get(fid)
            if row['state'] == 'tags_done':
                if not tags_ev:
                    raise ValueError('Registro intermediário de tags ausente; grupo interrompido.')
                exp_desc = tags_ev['payload'].get('description', '')
                exp_ver = tags_ev['payload'].get('version', '')
                if any(fresh.get(k) != row['source'].get(k) for k in _KEEP_CONTENT_FIELDS) or (
                    fresh.get('description', '') != exp_desc
                    or (exp_ver and fresh.get('version') != exp_ver)
                ):
                    raise ValueError('Arquivo mudou durante a execução.')
            elif not unchanged_file(row['source'], fresh, check_version=False):
                raise ValueError('Arquivo mudou durante a execução.')
            guarded = self.api.prepare_write(fresh)
            journal.state(
                job, fid, 'writing',
                event_action='writing',
                event_payload={'name': row['source']['name'], 'new_name': row['new_name'], 'description': fresh.get('description', '')},
            )
            try:
                self.api.update(guarded, body={'name': row['new_name']})
            except Exception as exc:
                journal.state(job, fid, 'uncertain', str(exc), event_action='uncertain', event_payload={'error': str(exc), 'phase': 'rename'})
                raise
        confirmed = self.api.get(fid)
        if not self._matches_renamed(confirmed, row, expected_desc=expected_desc):
            raise ValueError('Resposta não confirmada; operação preservada no diário.')
        history.record(
            job + ':' + fid, row['source'], row['new_name'],
            row['folder_path'] + '/' + row['source']['name'], row['folder_path'] + '/' + row['new_name'],
        )
        if any(s['id'] != fid and name_key(s['name']) == name_key(row['new_name']) for s in self.api.children_of(parent_id, self.drive)):
            raise ValueError('Renomeação aplicada, mas surgiu colisão concorrente. Histórico preservado; confira o grupo.')
        journal.state(job, fid, 'completed', event_action='completed', event_payload={'name': row['new_name']})
        row['state'] = 'completed'
        result['completed'] += 1

    def execute(self, job, selected):
        return self._run_locked(lambda: self._execute(job, selected))

    def _execute(self, job, selected):
        if self.cfg.is_read_only() or self.cfg.is_sandbox():
            raise ValueError('Execução exige Produção com permissão de escrita.')
        assert_queue_empty(self.db_name)
        journal, history = NameJournal(self.db_name), NameHistory(self.db_name)
        dedup_journal = None
        indexer = None
        result = dict(completed=0, dedup_moved=0, tags_updated=0, errors=[], cancelled=False)
        try:
            plan = journal.load(job)
            if plan.get('schema') not in (1, 2) or plan['drive'] != self.drive:
                raise ValueError('Plano incompatível com o Drive atual.')
            groups = defaultdict(list)
            selected = set(selected)
            for row in plan['items']:
                groups[row['group']].append(row)

            needs_dedup = any(
                (r.get('action') in _DEDUP_ACTIONS) and r['state'] != 'completed' and r['source']['id'] in selected
                for members in groups.values()
                for r in members
            )
            if needs_dedup:
                dedup_journal = DedupJournal(self.db_name)
                dedup_journal.event('started', {'source': 'name_disambiguation', 'job': job})
                if Path(self.db_name).exists():
                    local_backup(self.db_name)
                    indexer = FileIndexer(self.db_name)

            folder_cache = {}
            for members in groups.values():
                pending = [r for r in members if r['state'] != 'completed']
                if not any(r['source']['id'] in selected for r in pending):
                    continue
                if any(r['source']['id'] not in selected or r['state'] == 'blocked' for r in pending):
                    result['errors'].append('Selecione todos os membros pendentes do grupo; grupos bloqueados não executam.')
                    continue
                self.check_cancelled()
                try:
                    fresh_by_id = self._validate_group(plan, job, members, journal, dedup_journal)
                    rows_by_id = {r['source']['id']: r for r in members}
                    sub_dups = defaultdict(list)
                    for row in pending:
                        if row.get('action') == 'dedup_move':
                            sub_dups[row['keeper_id']].append(row)
                    for keeper_id, dup_rows in sub_dups.items():
                        self._run_dedup_subbucket(
                            job, rows_by_id[keeper_id], dup_rows, fresh_by_id,
                            journal, dedup_journal, indexer, folder_cache, result,
                        )
                    rename_rows = [
                        r for r in pending
                        if (r.get('action') or 'rename') in _RENAME_ACTIONS and r['state'] != 'completed'
                    ]
                    for idx, row in enumerate(rename_rows):
                        self.check_cancelled()
                        self.progress(int(idx * 100 / max(1, len(rename_rows))), 'Conferindo/renomeando: ' + row['source']['name'])
                        self._rename_row(job, row, journal, history, result)
                except DedupCancelled:
                    raise
                except Exception as exc:
                    label = members[0]['source']['name'] if members else ''
                    msg = str(exc)
                    if label and label not in msg:
                        msg = f'{label}: {msg}'
                    result['errors'].append(msg)
                    break
        except DedupCancelled:
            result['cancelled'] = True
        finally:
            if dedup_journal is not None:
                if result['dedup_moved'] or result['tags_updated'] or result['errors']:
                    result['dedup_log_path'] = dedup_journal.report(dict(
                        tags_updated=result['tags_updated'],
                        files_moved=result['dedup_moved'],
                        errors=list(result['errors']),
                        cancelled=result['cancelled'],
                        source='name_disambiguation',
                        job=job,
                    ))
                dedup_journal.close()
            if indexer is not None:
                indexer.close()
            history.close()
            journal.close()
        return result

    def _verify_other_members_consistent(self, journal, job, fid, members, parent_id):
        for other in members:
            other_id = other['source']['id']
            if other_id == fid:
                continue
            other_fresh = self.api.get(other_id)
            other_act = other.get('action') or 'rename'
            if other['state'] == 'completed':
                if other_act == 'dedup_move':
                    if other_fresh.get('trashed') or parent_id in (other_fresh.get('parents') or []):
                        raise ValueError('Cópia movida reapareceu na pasta; bloqueio mantido.')
                elif other_act == 'dedup_keep':
                    self._verify_keeper_state(job, other, other_fresh, journal)
                else:
                    other_ev = self._latest_event(journal, job, other_id, ('tags_confirmed',))
                    other_desc = other_ev['payload'].get('description') if other_ev else None
                    if not self._matches_renamed(other_fresh, other, expected_desc=other_desc):
                        raise ValueError('Outro membro do grupo mudou após confirmação; bloqueio mantido.')
            elif other['state'] == 'tags_done':
                self._verify_keeper_state(job, other, other_fresh, journal)
            elif not unchanged_file(other['source'], other_fresh, check_version=False):
                raise ValueError('Outro membro do grupo mudou desde a prévia; bloqueio mantido.')

    def reconcile_uncertain(self, job, selected):
        """Somente leitura no Drive: confirma já aplicados ou libera comprovadamente não executados."""
        return self._run_locked(lambda: self._reconcile_uncertain(job, selected))

    def _reconcile_uncertain(self, job, selected):
        if self.cfg.is_sandbox():
            raise ValueError('Conferência exige o Drive de produção.')
        journal, history = NameJournal(self.db_name), NameHistory(self.db_name)
        dedup_db = Path(self.db_name).resolve().parent / 'dedup' / 'journal.db'
        dedup_journal = DedupJournal(self.db_name) if dedup_db.exists() else None
        indexer = FileIndexer(self.db_name) if Path(self.db_name).exists() else None
        result = dict(confirmed=0, released=0, blocked=0, errors=[], cancelled=False)
        try:
            plan = journal.load(job)
            if plan.get('schema') not in (1, 2) or plan['drive'] != self.drive:
                raise ValueError('Plano incompatível com o Drive atual.')
            groups = defaultdict(list)
            for row in plan['items']:
                groups[row['group']].append(row)
            selected = set(selected)
            dedup_uncertain = _uncleaned_dedup_moves(self.db_name, job=job)
            targets = [
                (members, row)
                for members in groups.values()
                for row in members
                if row['source']['id'] in selected and (
                    row['state'] in ('writing', 'uncertain')
                    or (row.get('action') == 'dedup_move' and row['source']['id'] in dedup_uncertain)
                )
            ]
            for index, (members, row) in enumerate(targets):
                self.check_cancelled()
                fid = row['source']['id']
                action = row.get('action') or 'rename'
                self.progress(int(index * 100 / max(1, len(targets))),
                              'Conferindo operação incerta: ' + row['source']['name'])
                try:
                    parents = row['source'].get('parents') or []
                    if len(parents) != 1:
                        raise ValueError('Pasta de origem inválida na revisão; bloqueio mantido.')
                    parent_id = parents[0]
                    self._verify_ancestors(plan, parent_id)

                    fresh = self.api.get(fid)
                    if fresh.get('id') != fid or fresh.get('trashed'):
                        raise ValueError('Arquivo ausente ou na lixeira.')

                    if action == 'dedup_move':
                        move_ev = self._latest_event(journal, job, fid, ('dedup_move_writing',))
                        payload = move_ev['payload'] if move_ev else (dedup_uncertain[fid][1] if fid in dedup_uncertain else None)
                        if not payload:
                            raise ValueError('Registro de movimentação ausente; bloqueio mantido.')
                        if self._matches_moved(fresh, payload):
                            clean_local_by_drive_id(indexer, fid)
                            if dedup_journal:
                                row_dj = dedup_journal.conn.execute('SELECT 1 FROM moves WHERE file_id=?', (fid,)).fetchone()
                                if row_dj:
                                    dedup_journal.state(fid, 'cleaned')
                                dedup_journal.event('reconciled', payload)
                            journal.state(
                                job, fid, 'completed', '',
                                event_action='uncertain_confirmed_completed',
                                event_payload={'name': fresh.get('name'), 'parents': fresh.get('parents'),
                                               'md5': fresh.get('md5Checksum', ''), 'size': fresh.get('size', '')},
                            )
                            result['confirmed'] += 1
                            continue
                        if fresh.get('driveId') != self.drive or (fresh.get('parents') or []) != parents:
                            raise ValueError('Arquivo mudou de pasta/Drive; bloqueio mantido.')
                        if not unchanged_file(row['source'], fresh):
                            raise ValueError('Conteúdo ou metadados divergem; bloqueio mantido.')
                        self._verify_other_members_consistent(journal, job, fid, members, parent_id)
                        if dedup_journal:
                            row_dj = dedup_journal.conn.execute('SELECT state FROM moves WHERE file_id=?', (fid,)).fetchone()
                            if row_dj and row_dj[0] != 'uncertain':
                                raise ValueError('Diário de deduplicação já registra avanço; bloqueio mantido.')
                            if row_dj:
                                dedup_journal.conn.execute("DELETE FROM moves WHERE file_id=? AND state='uncertain'", (fid,))
                                dedup_journal.conn.commit()
                                dedup_journal.event('uncertain_released', {'file_id': fid, 'job': job})
                        journal.state(
                            job, fid, 'pending',
                            'Liberado após conferência (movimentação não executada); aprove/execute novamente.',
                            event_action='uncertain_released',
                            event_payload={'previous_state': row['state'], 'action': action},
                        )
                        result['released'] += 1
                        continue

                    if fresh.get('driveId') != self.drive:
                        raise ValueError('Arquivo fora do Drive autorizado.')
                    if (fresh.get('parents') or []) != parents:
                        raise ValueError('Arquivo mudou de pasta no Drive; bloqueio mantido.')

                    tag_ev = self._latest_event(journal, job, fid, ('writing_tags',))
                    rename_ev = self._latest_event(journal, job, fid, ('writing',))
                    stuck_in_tags = bool(tag_ev and (not rename_ev or tag_ev['seq'] > rename_ev['seq']))

                    if stuck_in_tags:
                        if any(fresh.get(k) != row['source'].get(k) for k in _KEEP_CONTENT_FIELDS):
                            raise ValueError('Conteúdo ou localização divergem; bloqueio mantido.')
                        target_desc = tag_ev['payload'].get('description', '')
                        if fresh.get('description', '') == target_desc:
                            update_local_description_by_drive_id(indexer, fid, target_desc)
                            if action == 'dedup_keep':
                                journal.state(
                                    job, fid, 'completed', '',
                                    event_action='uncertain_confirmed_completed',
                                    event_payload={'description': target_desc, 'version': fresh.get('version', ''), 'name': fresh.get('name', '')},
                                )
                            else:
                                journal.state(
                                    job, fid, 'tags_done', '',
                                    event_action='tags_confirmed',
                                    event_payload={'description': target_desc, 'version': fresh.get('version', ''), 'name': fresh.get('name', '')},
                                )
                            result['confirmed'] += 1
                        elif unchanged_file(row['source'], fresh):
                            self._verify_other_members_consistent(journal, job, fid, members, parent_id)
                            journal.state(
                                job, fid, 'pending',
                                'Liberado após conferência (gravação de tags não executada); aprove/execute novamente.',
                                event_action='uncertain_released',
                                event_payload={'previous_state': row['state'], 'action': action},
                            )
                            result['released'] += 1
                        else:
                            raise ValueError('Descrição ou versão divergem; bloqueio mantido.')
                        continue

                    confirmed_tags_ev = self._latest_event(journal, job, fid, ('tags_confirmed',))
                    expected_desc = confirmed_tags_ev['payload'].get('description') if confirmed_tags_ev else None
                    if any(fresh.get(k) != (expected_desc if (k == 'description' and expected_desc is not None) else row['source'].get(k))
                           for k in self.IDENTITY_FIELDS):
                        raise ValueError('Conteúdo, descrição ou metadados do arquivo divergem; bloqueio mantido.')

                    siblings = self.api.children_of(parent_id, self.drive)
                    member_ids = {m['source']['id'] for m in members}
                    old_key = name_key(row['source']['name'])
                    new_key = name_key(row['new_name'])
                    if any(s['id'] not in member_ids and name_key(s['name']) == old_key for s in siblings):
                        raise ValueError('Surgiu outra cópia com o nome original na pasta; bloqueio mantido.')

                    if self._matches_renamed(fresh, row, expected_desc=expected_desc):
                        if any(s['id'] != fid and name_key(s['name']) == new_key for s in siblings):
                            raise ValueError('Nome de destino em colisão concorrente; bloqueio mantido.')
                        history.record(job + ':' + fid, row['source'], row['new_name'],
                                       row['folder_path'] + '/' + row['source']['name'],
                                       row['folder_path'] + '/' + row['new_name'])
                        journal.state(
                            job, fid, 'completed', '',
                            event_action='uncertain_confirmed_completed',
                            event_payload={'name': fresh.get('name'), 'parents': fresh.get('parents'),
                                           'md5': fresh.get('md5Checksum', ''), 'size': fresh.get('size', '')},
                        )
                        result['confirmed'] += 1
                    else:
                        expected_ver = confirmed_tags_ev['payload'].get('version') if confirmed_tags_ev else row['source'].get('version')
                        if fresh.get('name') != row['source'].get('name') or fresh.get('version') != expected_ver:
                            raise ValueError('Nome ou versão divergem do original e do destino aprovado; bloqueio mantido.')
                        if any(s['id'] != fid and name_key(s['name']) == new_key for s in siblings):
                            raise ValueError('Nome de destino aprovado está ocupado; bloqueio mantido.')
                        self._verify_other_members_consistent(journal, job, fid, members, parent_id)
                        release_state = 'tags_done' if confirmed_tags_ev else 'pending'
                        journal.state(
                            job, fid, release_state,
                            'Liberado após conferência (não executado); aprove/execute novamente para renomear.',
                            event_action='uncertain_released',
                            event_payload={'previous_state': row['state'], 'name': fresh.get('name'),
                                           'parents': fresh.get('parents'), 'version': fresh.get('version', ''),
                                           'md5': fresh.get('md5Checksum', ''), 'size': fresh.get('size', '')},
                        )
                        result['released'] += 1
                except DedupCancelled:
                    raise
                except Exception as exc:
                    msg = str(exc)
                    journal.state(
                        job, fid, 'uncertain', msg,
                        event_action='uncertain_check_failed',
                        event_payload={'previous_state': row['state'], 'error': msg},
                    )
                    result['blocked'] += 1
                    result['errors'].append(f"{row['source']['name']}: {msg}")
        except DedupCancelled:
            result['cancelled'] = True
        finally:
            if dedup_journal is not None:
                dedup_journal.close()
            if indexer is not None:
                indexer.close()
            history.close()
            journal.close()
        return result


class NameDisambiguationWorker(QObject):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, service, db_name, job=None, selected=(), history_path=None, import_history=False, reconcile=False):
        super().__init__()
        self.service, self.db_name, self.job, self.selected = service, db_name, job, selected
        self.cancelled = False
        self.history_path, self.import_history, self.reconcile = history_path, import_history, reconcile

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            if self.history_path:
                history = NameHistory(self.db_name)
                try:
                    if self.import_history:
                        count = history.import_file(self.history_path)
                        result = dict(history_message=f'{count} registros lidos; nenhum arquivo alterado na nuvem.')
                    else:
                        history.export(self.history_path)
                        result = dict(history_message='Histórico exportado. Importe nos outros PCs sem substituir o catálogo.')
                finally:
                    history.close()
                self.finished.emit(result)
                return
            engine = NameDisambiguation(self.service, self.db_name, ConfigManager(), lambda: self.cancelled, self.progress.emit)
            if self.job and self.reconcile:
                self.finished.emit(engine.reconcile_uncertain(self.job, self.selected))
            elif self.job:
                self.finished.emit(engine.execute(self.job, self.selected))
            else:
                self.finished.emit(engine.scan())
        except DedupCancelled:
            self.finished.emit(dict(cancelled=True))
        except Exception as exc:
            self.failed.emit(str(exc))
