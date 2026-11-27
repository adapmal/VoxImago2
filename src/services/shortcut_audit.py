"""Pente-fino de atalhos no Banco de Imagens: análise somente leitura e limpeza segura.

Antes de enviar qualquer atalho à Lixeira pela API do Drive, garante que todas
as tags existentes no atalho sejam somadas (sem duplicar e preservando as tags já
existentes) no arquivo real de destino dentro do Banco de Imagens.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path

from googleapiclient.errors import HttpError
from PyQt6.QtCore import QLockFile, QObject, pyqtSignal

from src.database.database import FileIndexer
from src.services.drive_retry import execute_drive_read
from src.services.hash_deduplicator import (
    DedupCancelled,
    DriveCatalog,
    FOLDER_MIME,
    clean_local_by_drive_id,
    parse_tags,
    private_service,
    update_local_description_by_drive_id,
)
from src.services.snapshot_management import assert_queue_empty, local_backup
from src.utils.config_manager import ConfigManager

SHORTCUT = 'application/vnd.google-apps.shortcut'
DESCRIPTION_BYTE_LIMIT = 30000
DRIVE_FOLDER_URL = 'https://drive.google.com/drive/folders/{}'
DRIVE_FILE_URL = 'https://drive.google.com/file/d/{}/view'
SHORTCUT_AUDIT_FIELDS = (
    'id,name,mimeType,parents,driveId,trashed,size,md5Checksum,description,createdTime,version,'
    'capabilities(canEdit,canTrash),shortcutDetails(targetId,targetMimeType)'
)
_UNCERTAIN_STATES = ('uncertain', 'writing_tags', 'writing_trash', 'trashed_waiting_local')


def additive_missing_tags(existing_desc: str | None, shortcut_desc: str | None) -> tuple[list[str], str]:
    """Retorna (tags_ausentes, nova_descricao) preservando todas as tags atuais do alvo."""
    existing_tags = parse_tags(existing_desc)
    seen = {tag.casefold() for tag in existing_tags}
    missing = []
    for tag in parse_tags(shortcut_desc):
        folded = tag.casefold()
        if folded not in seen:
            seen.add(folded)
            missing.append(tag)
    if not missing:
        return [], (existing_desc or '')
    base = (existing_desc or '').strip().rstrip(',;').rstrip()
    joined = ', '.join(missing)
    new_desc = f'{base}, {joined}' if base else joined
    return missing, new_desc


def _snapshot_item(item: dict) -> dict:
    keys = (
        'id', 'name', 'mimeType', 'parents', 'driveId', 'trashed', 'size', 'md5Checksum',
        'description', 'createdTime', 'version', 'capabilities', 'shortcutDetails',
    )
    snap = {}
    for key in keys:
        if key in item and item[key] is not None:
            val = item[key]
            snap[key] = dict(val) if isinstance(val, dict) else list(val) if isinstance(val, list) else val
    return snap


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, 'resp', None)
    return getattr(resp, 'status', None)


class _AuditAPI(DriveCatalog):
    """Cliente Drive com paginação completa, campos explícitos de atalho/lixeira e escrita condicional."""

    def get(self, file_id: str) -> dict:
        headers = {}

        def build():
            headers.clear()
            req = self.service.files().get(
                fileId=file_id, supportsAllDrives=True, fields=SHORTCUT_AUDIT_FIELDS,
            )
            if hasattr(req, 'add_response_callback'):
                req.add_response_callback(
                    lambda h, *_: headers.update({k.lower(): v for k, v in (h or {}).items()})
                )
            return req

        raw = execute_drive_read(
            build,
            cancelled=self.cancelled,
            cancel_error=DedupCancelled,
            on_wait=self.on_wait,
        )
        if headers.get('etag'):
            raw['_etag'] = headers['etag']
        return raw


class ShortcutAuditJournal:
    """Diário durável SQLite em WAL com escrita síncrona para auditoria e retomada de atalhos."""

    def __init__(self, db_name: str):
        self.root = Path(db_name).resolve().parent / 'shortcut_audit'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'journal.db'
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    drive TEXT NOT NULL,
                    json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    job TEXT NOT NULL,
                    sc_id TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    folder_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    uncertain_phase TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (job, sc_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job TEXT NOT NULL,
                    sc_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
            """)

    def save_plan(self, plan: dict) -> None:
        header = {k: v for k, v in plan.items() if k != 'items'}
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                'INSERT OR REPLACE INTO plans(id, created_at, drive, json) VALUES (?, ?, ?, ?)',
                (plan['id'], plan['created_at'], plan['drive'], json.dumps(header, ensure_ascii=False)),
            )
            self.conn.execute('DELETE FROM operations WHERE job = ?', (plan['id'],))
            for idx, row in enumerate(plan['items']):
                self.conn.execute(
                    """INSERT INTO operations(
                           job, sc_id, sort_order, folder_id, target_id,
                           state, uncertain_phase, error, json, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        plan['id'],
                        row['source']['id'],
                        idx,
                        row.get('parent_id') or '',
                        row.get('target_id') or '',
                        row.get('state', 'pending'),
                        row.get('uncertain_phase') or '',
                        row.get('error') or '',
                        json.dumps(row, ensure_ascii=False),
                        now,
                    ),
                )
            self._log_event(plan['id'], '*', 'plan_saved', {'total': len(plan['items'])}, now)

    def load(self, job: str) -> dict:
        row = self.conn.execute('SELECT json FROM plans WHERE id = ?', (job,)).fetchone()
        if not row:
            raise ValueError('Plano de pente-fino não encontrado: ' + job)
        plan = json.loads(row[0])
        op_rows = self.conn.execute(
            'SELECT state, uncertain_phase, error, json FROM operations WHERE job = ? ORDER BY sort_order',
            (job,),
        ).fetchall()
        items = []
        for state, phase, error, raw_json in op_rows:
            item = json.loads(raw_json)
            item['state'] = state
            item['uncertain_phase'] = phase
            item['error'] = error
            items.append(item)
        plan['items'] = items
        return plan

    def jobs(self) -> list[tuple[str, int, str]]:
        return self.conn.execute(
            'SELECT id, created_at, drive FROM plans ORDER BY created_at DESC LIMIT 20'
        ).fetchall()

    def uncertain_operation_ids(self, job: str | None = None) -> list[str]:
        placeholders = ','.join('?' for _ in _UNCERTAIN_STATES)
        if job:
            rows = self.conn.execute(
                f'SELECT sc_id FROM operations WHERE job = ? AND state IN ({placeholders}) ORDER BY sort_order',
                (job, *_UNCERTAIN_STATES),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f'SELECT sc_id FROM operations WHERE state IN ({placeholders}) ORDER BY updated_at, sort_order',
                _UNCERTAIN_STATES,
            ).fetchall()
        return [r[0] for r in rows]

    def get_operation(self, job: str, sc_id: str) -> dict | None:
        row = self.conn.execute(
            'SELECT state, uncertain_phase, error, json FROM operations WHERE job = ? AND sc_id = ?',
            (job, sc_id),
        ).fetchone()
        if not row:
            return None
        item = json.loads(row[3])
        item['state'] = row[0]
        item['uncertain_phase'] = row[1]
        item['error'] = row[2]
        return item

    def save_operation(
        self,
        job: str,
        row: dict,
        event: str,
        payload: dict | None = None,
    ) -> None:
        now = int(time.time())
        sc_id = row['source']['id']
        with self.conn:
            self.conn.execute(
                """UPDATE operations
                      SET state = ?, uncertain_phase = ?, error = ?, json = ?, updated_at = ?
                    WHERE job = ? AND sc_id = ?""",
                (
                    row['state'],
                    row.get('uncertain_phase') or '',
                    row.get('error') or '',
                    json.dumps(row, ensure_ascii=False),
                    now,
                    job,
                    sc_id,
                ),
            )
            self._log_event(job, sc_id, event, payload or {}, now)

    def event(self, job: str, sc_id: str, event: str, payload: dict | None = None) -> None:
        with self.conn:
            self._log_event(job, sc_id, event, payload or {}, int(time.time()))

    def _log_event(self, job: str, sc_id: str, event: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            'INSERT INTO events(job, sc_id, event, payload, created_at) VALUES (?, ?, ?, ?, ?)',
            (job, sc_id, event, json.dumps(payload, ensure_ascii=False), ts),
        )

    def close(self) -> None:
        self.conn.close()


class ShortcutAudit:
    """Análise somente leitura e exclusão segura (via Lixeira) de atalhos no Banco de Imagens."""

    def __init__(
        self,
        service,
        db_name: str,
        cfg: ConfigManager | None = None,
        cancelled=lambda: False,
        progress=lambda pct, text: None,
    ):
        self.db_name = db_name
        self.cfg = cfg or ConfigManager()
        self.drive = self.cfg.get('production_drive_id') or ''
        if not self.drive:
            raise ValueError('Configure o ID do Banco de Imagens antes de auditar atalhos.')
        self.cancelled = cancelled
        self.progress = progress
        self._last_pct = -1
        self.api = _AuditAPI(
            private_service(service),
            self.drive,
            cancelled=cancelled,
            on_wait=lambda msg: self.progress(self._last_pct, msg),
        )

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise DedupCancelled('Operação cancelada.')

    def _acquire_lock(self, journal: ShortcutAuditJournal) -> QLockFile:
        lock = QLockFile(str(journal.root / 'execution.lock'))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError('Já existe uma auditoria/limpeza de atalhos em andamento.')
        return lock

    def scan(self) -> dict:
        """Lista todos os itens ativos do Banco de Imagens sem alterar Drive nem banco local."""
        self.check_cancelled()
        self._last_pct = -1
        self.progress(-1, 'Listando itens e atalhos do Banco de Imagens...')
        items = self.api.list('trashed = false', fields=SHORTCUT_AUDIT_FIELDS, drive_id=self.drive)

        drive_items = {item['id']: item for item in items}
        folders = {fid: item for fid, item in drive_items.items() if item.get('mimeType') == FOLDER_MIME}
        folder_names = defaultdict(list)
        for item in items:
            if item.get('mimeType') != SHORTCUT:
                for parent in item.get('parents') or []:
                    folder_names[(parent, (item.get('name') or '').casefold())].append(item['id'])

        try:
            root_folder = self.api.get(self.drive)
        except HttpError:
            root_folder = {'id': self.drive, 'name': 'Banco de Imagens'}
        root_name = root_folder.get('name') or 'Banco de Imagens'

        folder_cache: dict[str, tuple[str, str | None]] = {self.drive: (root_name, None)}
        folder_issues: list[str] = []

        def resolve_folder_path(folder_id: str) -> tuple[str, str | None]:
            if folder_id in folder_cache:
                return folder_cache[folder_id]
            chain = []
            visited = set()
            curr = folder_id
            while curr and curr != self.drive:
                if curr in folder_cache:
                    prefix, err = folder_cache[curr]
                    if err:
                        return '/'.join([prefix] + [c.get('name') or c['id'] for c in reversed(chain)]), err
                    names = [prefix] + [c.get('name') or c['id'] for c in reversed(chain)]
                    path = '/'.join(names)
                    folder_cache[folder_id] = (path, None)
                    return path, None
                if curr in visited:
                    err = 'Loop na hierarquia de pastas do Drive.'
                    folder_issues.append(f'Loop de pastas detectado próximo a "{folder_id}".')
                    path = '/'.join([c.get('name') or c['id'] for c in reversed(chain)]) or folder_id
                    folder_cache[folder_id] = (path, err)
                    return path, err
                visited.add(curr)
                meta = folders.get(curr)
                if not meta or meta.get('trashed'):
                    err = 'Hierarquia de pastas incompleta ou fora do Banco de Imagens.'
                    fallback_names = [c.get('name') or c['id'] for c in reversed(chain)]
                    path = '/'.join(fallback_names) if fallback_names else folder_id
                    folder_cache[folder_id] = (path, err)
                    return path, err
                parents = meta.get('parents') or []
                if len(parents) != 1:
                    err = 'Pasta sem pai único verificável.'
                    names = [c.get('name') or c['id'] for c in reversed(chain)] + [meta.get('name') or curr]
                    path = '/'.join(reversed(names))
                    folder_cache[folder_id] = (path, err)
                    return path, err
                chain.append(meta)
                curr = parents[0]
            path = '/'.join([root_name] + [c.get('name') or c['id'] for c in reversed(chain)])
            folder_cache[folder_id] = (path, None)
            return path, None

        shortcuts = [item for item in items if item.get('mimeType') == SHORTCUT]
        external_targets: dict[str, tuple[str, dict | None]] = {}

        def resolve_target(target_id: str) -> tuple[str, dict | None]:
            if not target_id:
                return 'missing_target_id', None
            if target_id in drive_items:
                return 'ok_in_drive', drive_items[target_id]
            if target_id not in external_targets:
                try:
                    fetched = self.api.get(target_id)
                except HttpError as exc:
                    status = _http_status(exc)
                    if status == 404:
                        external_targets[target_id] = ('not_found', None)
                    elif status == 403:
                        external_targets[target_id] = ('no_access', None)
                    else:
                        external_targets[target_id] = ('lookup_error', None)
                else:
                    if fetched.get('trashed'):
                        external_targets[target_id] = ('trashed', fetched)
                    elif fetched.get('driveId') != self.drive:
                        external_targets[target_id] = ('outside_drive', fetched)
                    else:
                        external_targets[target_id] = ('ok_in_drive', fetched)
            return external_targets[target_id]

        rows = []
        total = len(shortcuts)
        for index, sc in enumerate(shortcuts):
            self.check_cancelled()
            if total:
                pct = int(index * 100 / total)
                if pct != self._last_pct:
                    self._last_pct = pct
                    self.progress(pct, f'Analisando atalho {index + 1:,}/{total:,}: {sc.get("name", sc["id"])}')

            sc_tags = parse_tags(sc.get('description'))
            parents = sc.get('parents') or []
            if len(parents) != 1:
                parent_id = parents[0] if parents else ''
                folder_path = '(Sem pasta única)'
                folder_err = 'Atalho sem pasta única verificável.'
            else:
                parent_id = parents[0]
                folder_path, folder_err = resolve_folder_path(parent_id)

            details = sc.get('shortcutDetails') or {}
            target_id = details.get('targetId') or ''
            target_status, target = resolve_target(target_id)
            target_path = ''
            if target and target_status == 'ok_in_drive':
                t_parents = target.get('parents') or []
                if len(t_parents) == 1:
                    target_path, _ = resolve_folder_path(t_parents[0])

            existing_target_tags = parse_tags(target.get('description')) if target else []
            has_conflict = bool(parent_id and folder_names.get((parent_id, (sc.get('name') or '').casefold())))
            can_trash = bool(sc.get('capabilities', {}).get('canTrash'))

            error = ''
            missing: list[str] = []
            planned_desc = target.get('description', '') if target else ''
            action = 'trash_only'

            if folder_err:
                error = folder_err
            elif not can_trash:
                error = 'Sem permissão para enviar este atalho à Lixeira.'
            elif sc_tags:
                if target_status == 'missing_target_id':
                    error = 'Atalho com tags, mas sem ID de arquivo de destino.'
                elif target_status == 'not_found':
                    error = 'Atalho com tags: arquivo de destino não encontrado (404).'
                elif target_status == 'no_access':
                    error = 'Atalho com tags: sem permissão para ler o arquivo de destino.'
                elif target_status == 'trashed':
                    error = 'Atalho com tags: arquivo de destino está na Lixeira.'
                elif target_status == 'outside_drive':
                    error = 'Atalho com tags: arquivo de destino está fora do Banco de Imagens.'
                elif target_status != 'ok_in_drive' or not target:
                    error = 'Atalho com tags: não foi possível verificar o arquivo de destino.'
                elif target.get('mimeType') == FOLDER_MIME:
                    error = 'Atalho com tags aponta para uma pasta; revise manualmente antes de excluir.'
                elif target.get('mimeType') == SHORTCUT:
                    error = 'Atalho com tags aponta para outro atalho; revise a cadeia manualmente.'
                elif not target.get('capabilities', {}).get('canEdit'):
                    error = 'Atalho com tags: sem permissão de edição no arquivo de destino.'
                else:
                    missing, planned_desc = additive_missing_tags(target.get('description', ''), sc.get('description', ''))
                    if missing and len(planned_desc.encode('utf-8')) > DESCRIPTION_BYTE_LIMIT:
                        error = f'Descrição final no destino excederia {DESCRIPTION_BYTE_LIMIT} bytes.'
                    else:
                        action = 'transfer_and_trash' if missing else 'trash_only'
            else:
                action = 'trash_only'

            if error:
                action = 'blocked'

            rows.append({
                'source': _snapshot_item(sc),
                'parent_id': parent_id,
                'folder_id': parent_id,
                'folder_path': folder_path,
                'folder_url': DRIVE_FOLDER_URL.format(parent_id) if parent_id else '',
                'shortcut_tags': sc_tags,
                'target_id': target_id,
                'target_mime': details.get('targetMimeType') or (target.get('mimeType') if target else ''),
                'target_status': target_status,
                'target': _snapshot_item(target) if target else None,
                'target_path': target_path,
                'existing_target_tags': existing_target_tags,
                'missing_tags': missing,
                'planned_target_description': planned_desc,
                'has_name_conflict': has_conflict,
                'action': action,
                'state': 'blocked' if error else 'pending',
                'error': error,
                'uncertain_phase': '',
                'confirmed_target_description': '',
                'confirmed_target_version': '',
            })

        rows.sort(key=lambda r: (
            (r['folder_path'] or '').casefold(),
            (r['source'].get('name') or '').casefold(),
            r['source']['id'],
        ))

        plan = {
            'id': uuid.uuid4().hex,
            'created_at': int(time.time()),
            'drive': self.drive,
            'drive_name': root_name,
            'folder_issues': folder_issues,
            'total_shortcuts': len(rows),
            'with_tags': sum(1 for r in rows if r['shortcut_tags']),
            'tags_to_add': sum(1 for r in rows if r['state'] != 'blocked' and r['missing_tags']),
            'already_on_target': sum(
                1 for r in rows if r['state'] != 'blocked' and r['shortcut_tags'] and not r['missing_tags']
            ),
            'without_tags': sum(1 for r in rows if not r['shortcut_tags']),
            'blocked': sum(1 for r in rows if r['state'] == 'blocked'),
            'name_conflicts': sum(1 for r in rows if r['has_name_conflict']),
            'items': rows,
        }
        journal = ShortcutAuditJournal(self.db_name)
        try:
            journal.save_plan(plan)
        finally:
            journal.close()
        self._last_pct = 100
        self.progress(100, f'{len(rows):,} atalho(s) analisado(s).')
        return plan

    def _validate_target_live(self, target: dict, *, require_edit: bool = True) -> None:
        if target.get('trashed'):
            raise ValueError('O arquivo de destino foi enviado para a Lixeira no Drive.')
        if target.get('driveId') != self.drive:
            raise ValueError('O arquivo de destino saiu do Banco de Imagens.')
        if target.get('mimeType') in (FOLDER_MIME, SHORTCUT):
            raise ValueError('O destino do atalho com tags não é um arquivo de mídia comum.')
        if require_edit and not target.get('capabilities', {}).get('canEdit'):
            raise ValueError('Sem permissão de edição no arquivo de destino.')

    def _assert_target_ready_for_trash(
        self,
        journal: ShortcutAuditJournal,
        job: str,
        row: dict,
        target: dict,
        shortcut_desc: str,
        *,
        check_tags: bool,
        event: str,
    ) -> None:
        """Revalida destino ativo/local e preserva o atalho com pendência em caso de divergência."""
        try:
            self._validate_target_live(target, require_edit=False)
            if check_tags:
                missing, _ = additive_missing_tags(target.get('description', ''), shortcut_desc)
                if missing:
                    raise ValueError(
                        'As tags do atalho não constam mais no arquivo de destino antes da exclusão.'
                    )
        except ValueError as exc:
            row['state'] = 'error'
            row['uncertain_phase'] = ''
            row['error'] = f'{exc} Atalho preservado para revisão.'
            journal.save_operation(
                job,
                row,
                event,
                {
                    'target_id': row.get('target_id', ''),
                    'trashed': bool(target.get('trashed')),
                    'driveId': target.get('driveId'),
                    'error': str(exc),
                },
            )
            raise ValueError(row['error']) from exc

    def _validate_shortcut_live(self, fresh_sc: dict, row: dict) -> None:
        if fresh_sc.get('mimeType') != SHORTCUT or fresh_sc.get('trashed'):
            raise ValueError('O atalho foi excluído ou convertido no Drive após a análise.')
        if fresh_sc.get('driveId') != self.drive:
            raise ValueError('O atalho não está mais no Banco de Imagens.')
        if (fresh_sc.get('parents') or []) != (row['source'].get('parents') or []):
            raise ValueError('O atalho mudou de pasta no Drive após a análise; reanalise.')
        if (fresh_sc.get('shortcutDetails') or {}) != (row['source'].get('shortcutDetails') or {}):
            raise ValueError('O destino do atalho mudou no Drive após a análise; reanalise.')
        if parse_tags(fresh_sc.get('description')) != row['shortcut_tags']:
            raise ValueError('As tags do atalho mudaram no Drive após a análise; reanalise.')
        if str(fresh_sc.get('version', '')) != str(row['source'].get('version', '')):
            raise ValueError('Metadados do atalho mudaram no Drive após a análise; reanalise.')
        if not fresh_sc.get('capabilities', {}).get('canTrash'):
            raise ValueError('Sem permissão atual para enviar este atalho à Lixeira.')

    def _execute_one(
        self,
        journal: ShortcutAuditJournal,
        indexer: FileIndexer,
        job: str,
        row: dict,
        result: dict,
    ) -> None:
        sc_id = row['source']['id']
        fresh_sc = self.api.get(sc_id)
        self._validate_shortcut_live(fresh_sc, row)

        did_write_target = False
        if row['shortcut_tags']:
            target_id = row['target_id']
            fresh_target = self.api.get(target_id)
            self._validate_target_live(fresh_target)
            missing, new_desc = additive_missing_tags(
                fresh_target.get('description', ''),
                fresh_sc.get('description', ''),
            )
            if row['state'] == 'tags_done':
                if missing:
                    raise ValueError(
                        'As tags do atalho desapareceram do arquivo de destino após a etapa anterior; revise antes de excluir.'
                    )
            elif missing:
                if len(new_desc.encode('utf-8')) > DESCRIPTION_BYTE_LIMIT:
                    raise ValueError(f'Descrição final no destino excederia {DESCRIPTION_BYTE_LIMIT} bytes.')
                row['state'] = 'writing_tags'
                row['uncertain_phase'] = 'tags'
                row['error'] = ''
                journal.save_operation(
                    job,
                    row,
                    'writing_tags',
                    {
                        'target_id': target_id,
                        'before_desc': fresh_target.get('description', ''),
                        'before_version': fresh_target.get('version', ''),
                        'added_tags': missing,
                        'planned_desc': new_desc,
                    },
                )
                try:
                    self.api.update(fresh_target, body={'description': new_desc})
                except Exception as exc:
                    row['state'] = 'uncertain'
                    row['uncertain_phase'] = 'tags'
                    row['error'] = f'Resposta incerta ao gravar tags no destino: {exc}'
                    journal.save_operation(job, row, 'uncertain_tags', {'error': str(exc)})
                    raise RuntimeError(row['error']) from exc

                try:
                    confirmed_target = self.api.get(target_id)
                except Exception as exc:
                    row['state'] = 'uncertain'
                    row['uncertain_phase'] = 'tags'
                    row['error'] = f'Falha ao confirmar gravação de tags no destino: {exc}'
                    journal.save_operation(job, row, 'uncertain_tags_verify', {'error': str(exc)})
                    raise RuntimeError(row['error']) from exc

                self._assert_target_ready_for_trash(
                    journal,
                    job,
                    row,
                    confirmed_target,
                    fresh_sc.get('description', ''),
                    check_tags=False,
                    event='target_invalid_after_tags',
                )

                remaining, _ = additive_missing_tags(
                    confirmed_target.get('description', ''),
                    fresh_sc.get('description', ''),
                )
                if remaining:
                    row['state'] = 'uncertain'
                    row['uncertain_phase'] = 'tags'
                    row['error'] = 'Falha ao confirmar gravação de tags no destino: tags ausentes na leitura de retorno.'
                    journal.save_operation(job, row, 'uncertain_tags_verify', {'remaining': remaining})
                    raise RuntimeError(row['error'])

                did_write_target = True
                confirmed_desc = confirmed_target.get('description', '')
                local_updated = update_local_description_by_drive_id(indexer, target_id, confirmed_desc)
                row['state'] = 'tags_done'
                row['uncertain_phase'] = ''
                row['error'] = ''
                row['confirmed_target_description'] = confirmed_desc
                row['confirmed_target_version'] = str(confirmed_target.get('version', ''))
                row['missing_tags'] = []
                journal.save_operation(
                    job,
                    row,
                    'tags_done',
                    {
                        'target_id': target_id,
                        'added_tags': missing,
                        'confirmed_desc': confirmed_desc,
                        'confirmed_version': row['confirmed_target_version'],
                        'local_updated': local_updated,
                    },
                )
                result['tags_updated'] += 1
                result['tags_added'] += len(missing)
                result['local_updated'] += local_updated
            else:
                confirmed_desc = fresh_target.get('description', '')
                row['state'] = 'tags_done'
                row['uncertain_phase'] = ''
                row['error'] = ''
                row['confirmed_target_description'] = confirmed_desc
                row['confirmed_target_version'] = str(fresh_target.get('version', ''))
                journal.save_operation(
                    job,
                    row,
                    'tags_already_present',
                    {'target_id': target_id, 'confirmed_version': row['confirmed_target_version']},
                )
                result['already_tagged'] += 1

        self.check_cancelled()
        if did_write_target:
            fresh_sc = self.api.get(sc_id)
            self._validate_shortcut_live(fresh_sc, row)
        if row['shortcut_tags']:
            pre_trash_target = self.api.get(row['target_id'])
            self._assert_target_ready_for_trash(
                journal,
                job,
                row,
                pre_trash_target,
                fresh_sc.get('description', ''),
                check_tags=True,
                event='target_invalid_before_trash',
            )

        row['state'] = 'writing_trash'
        row['uncertain_phase'] = 'trash'
        row['error'] = ''
        journal.save_operation(
            job,
            row,
            'writing_trash',
            {'before_version': fresh_sc.get('version', ''), 'target_id': row['target_id']},
        )
        try:
            self.api.update(fresh_sc, body={'trashed': True})
        except Exception as exc:
            row['state'] = 'uncertain'
            row['uncertain_phase'] = 'trash'
            row['error'] = f'Resposta incerta ao enviar atalho à Lixeira: {exc}'
            journal.save_operation(job, row, 'uncertain_trash', {'error': str(exc)})
            raise RuntimeError(row['error']) from exc

        try:
            confirmed_sc = self.api.get(sc_id)
            if not confirmed_sc.get('trashed'):
                raise RuntimeError('Atalho não consta como movido para a Lixeira na leitura de retorno.')
        except Exception as exc:
            row['state'] = 'uncertain'
            row['uncertain_phase'] = 'trash'
            row['error'] = f'Falha ao confirmar envio do atalho à Lixeira: {exc}'
            journal.save_operation(job, row, 'uncertain_trash_verify', {'error': str(exc)})
            raise RuntimeError(row['error']) from exc

        row['state'] = 'trashed_waiting_local'
        row['uncertain_phase'] = 'trash'
        journal.save_operation(job, row, 'trash_confirmed', {'version': confirmed_sc.get('version', '')})

        cleaned = clean_local_by_drive_id(indexer, sc_id)
        row['state'] = 'completed'
        row['uncertain_phase'] = ''
        row['error'] = ''
        journal.save_operation(job, row, 'completed', {'local_cleaned': len(cleaned), 'cleaned_ids': cleaned})
        result['completed'] += 1
        result['shortcuts_trashed'] += 1
        result['local_cleaned'] += len(cleaned)

    def execute(self, job: str, selected) -> dict:
        """Executa transferência aditiva de tags e exclusão segura (Lixeira) dos atalhos marcados."""
        if self.cfg.is_read_only() or self.cfg.is_sandbox():
            raise ValueError('A execução exige modo Produção e permissão de escrita.')
        assert_queue_empty(self.db_name)

        journal = ShortcutAuditJournal(self.db_name)
        result = {
            'completed': 0,
            'tags_updated': 0,
            'tags_added': 0,
            'already_tagged': 0,
            'shortcuts_trashed': 0,
            'local_updated': 0,
            'local_cleaned': 0,
            'uncertain': [],
            'errors': [],
            'cancelled': False,
            'journal': str(journal.path),
        }
        lock = None
        indexer = None
        try:
            lock = self._acquire_lock(journal)
            plan = journal.load(job)
            if plan['drive'] != self.drive:
                raise ValueError('O ID do Banco de Imagens configurado mudou após a análise.')
            uncertain = journal.uncertain_operation_ids(job)
            if uncertain:
                raise ValueError(
                    'Há operação(ões) com resposta incerta na nuvem; use Conferir / liberar incertas antes de continuar.'
                )
            selected_ids = set(selected)
            rows = [
                r for r in plan['items']
                if r['source']['id'] in selected_ids and r['state'] not in ('completed', 'blocked')
            ]
            if not rows:
                return result
            if Path(self.db_name).exists():
                backup = local_backup(self.db_name)
                if backup:
                    journal.event(job, '*', 'backup', {'path': backup})

            indexer = FileIndexer(self.db_name)
            total = len(rows)
            for idx, row in enumerate(rows):
                self.check_cancelled()
                self._last_pct = int(idx * 100 / total)
                self.progress(
                    self._last_pct,
                    f'Processando {idx + 1:,}/{total:,}: {row["source"].get("name", row["source"]["id"])}',
                )
                try:
                    self._execute_one(journal, indexer, job, row, result)
                except DedupCancelled:
                    raise
                except Exception as exc:
                    if row.get('state') not in _UNCERTAIN_STATES:
                        row['state'] = 'error'
                        row['error'] = str(exc)
                        journal.save_operation(job, row, 'error', {'error': str(exc)})
                    else:
                        result['uncertain'].append(row['source']['id'])
                    result['errors'].append(f'{row["source"].get("name", row["source"]["id"])}: {exc}')
                    break
        except DedupCancelled:
            result['cancelled'] = True
        finally:
            if indexer is not None:
                indexer.close()
            if lock is not None:
                lock.unlock()
            journal.close()
        self._last_pct = 100
        self.progress(100, 'Execução encerrada.')
        return result

    def reconcile_uncertain(self, job: str, selected=None) -> dict:
        """Consulta apenas por get() o estado real na nuvem das operações incertas e concilia o diário/índice."""
        if self.cfg.is_read_only() or self.cfg.is_sandbox():
            raise ValueError('A conciliação exige modo Produção e permissão de escrita.')
        assert_queue_empty(self.db_name)

        journal = ShortcutAuditJournal(self.db_name)
        result = {
            'reconciled': 0,
            'completed': 0,
            'tags_updated': 0,
            'local_updated': 0,
            'local_cleaned': 0,
            'unresolved': 0,
            'errors': [],
            'cancelled': False,
            'journal': str(journal.path),
        }
        lock = None
        indexer = None
        try:
            lock = self._acquire_lock(journal)
            plan = journal.load(job)
            if plan['drive'] != self.drive:
                raise ValueError('O ID do Banco de Imagens configurado mudou após a análise.')
            uncertain_ids = journal.uncertain_operation_ids(job)
            if selected:
                wanted = set(selected)
                uncertain_ids = [sid for sid in uncertain_ids if sid in wanted]
            if not uncertain_ids:
                return result
            if Path(self.db_name).exists():
                backup = local_backup(self.db_name)
                if backup:
                    journal.event(job, '*', 'reconcile_backup', {'path': backup})

            indexer = FileIndexer(self.db_name)
            total = len(uncertain_ids)
            for idx, sc_id in enumerate(uncertain_ids):
                self.check_cancelled()
                self._last_pct = int(idx * 100 / total)
                row = journal.get_operation(job, sc_id)
                if not row:
                    continue
                self.progress(
                    self._last_pct,
                    f'Conferindo {idx + 1:,}/{total:,}: {row["source"].get("name", sc_id)}',
                )
                try:
                    self._reconcile_one(journal, indexer, job, row, result)
                except DedupCancelled:
                    raise
                except Exception as exc:
                    result['unresolved'] += 1
                    result['errors'].append(f'{row["source"].get("name", sc_id)}: {exc}')
        except DedupCancelled:
            result['cancelled'] = True
        finally:
            if indexer is not None:
                indexer.close()
            if lock is not None:
                lock.unlock()
            journal.close()
        self._last_pct = 100
        self.progress(100, 'Conciliação concluída.')
        return result

    def _reconcile_one(
        self,
        journal: ShortcutAuditJournal,
        indexer: FileIndexer,
        job: str,
        row: dict,
        result: dict,
    ) -> None:
        sc_id = row['source']['id']
        state = row.get('state')
        phase = row.get('uncertain_phase') or ''

        if state == 'trashed_waiting_local' or (phase == 'trash' or state == 'writing_trash'):
            try:
                sc = self.api.get(sc_id)
            except HttpError as exc:
                if _http_status(exc) == 404:
                    row['state'] = 'blocked'
                    row['uncertain_phase'] = ''
                    row['error'] = 'Atalho não encontrado no Drive (404) ao conferir resposta incerta.'
                    journal.save_operation(job, row, 'reconcile_trash_404', {})
                    result['unresolved'] += 1
                    result['errors'].append(row['error'])
                    return
                raise
            if sc.get('trashed'):
                cleaned = clean_local_by_drive_id(indexer, sc_id)
                row['state'] = 'completed'
                row['uncertain_phase'] = ''
                row['error'] = ''
                journal.save_operation(job, row, 'reconciled_completed', {'local_cleaned': len(cleaned), 'cleaned_ids': cleaned})
                result['reconciled'] += 1
                result['completed'] += 1
                result['local_cleaned'] += len(cleaned)
                return
            row['state'] = 'tags_done' if row['shortcut_tags'] else 'pending'
            row['uncertain_phase'] = ''
            row['error'] = ''
            journal.save_operation(job, row, 'reconciled_trash_not_applied', {'state': row['state']})
            result['reconciled'] += 1
            return

        if phase == 'tags' or state == 'writing_tags':
            target_id = row['target_id']
            try:
                target = self.api.get(target_id)
            except HttpError as exc:
                if _http_status(exc) == 404:
                    row['state'] = 'blocked'
                    row['uncertain_phase'] = ''
                    row['error'] = 'Arquivo de destino não encontrado (404) ao conferir gravação de tags.'
                    journal.save_operation(job, row, 'reconcile_tags_404', {'target_id': target_id})
                    result['unresolved'] += 1
                    result['errors'].append(row['error'])
                    return
                raise
            try:
                self._validate_target_live(target, require_edit=False)
            except ValueError as exc:
                row['state'] = 'blocked'
                row['uncertain_phase'] = ''
                row['error'] = str(exc)
                journal.save_operation(job, row, 'reconcile_tags_invalid_target', {'target_id': target_id})
                result['unresolved'] += 1
                result['errors'].append(row['error'])
                return

            missing, _ = additive_missing_tags(
                target.get('description', ''),
                ', '.join(row['shortcut_tags']),
            )
            desc = target.get('description', '')
            if not missing:
                local_updated = update_local_description_by_drive_id(indexer, target_id, desc)
                row['state'] = 'tags_done'
                row['uncertain_phase'] = ''
                row['error'] = ''
                row['confirmed_target_description'] = desc
                row['confirmed_target_version'] = str(target.get('version', ''))
                row['missing_tags'] = []
                journal.save_operation(
                    job,
                    row,
                    'reconciled_tags_confirmed',
                    {'target_id': target_id, 'confirmed_version': row['confirmed_target_version'], 'local_updated': local_updated},
                )
                result['reconciled'] += 1
                result['tags_updated'] += 1
                result['local_updated'] += local_updated
                return

            local_updated = update_local_description_by_drive_id(indexer, target_id, desc)
            row['state'] = 'pending'
            row['uncertain_phase'] = ''
            row['error'] = ''
            row['missing_tags'] = missing
            journal.save_operation(
                job,
                row,
                'reconciled_tags_not_applied',
                {'target_id': target_id, 'missing': missing, 'local_updated': local_updated},
            )
            result['reconciled'] += 1
            result['local_updated'] += local_updated
            return

        row['state'] = 'pending'
        row['uncertain_phase'] = ''
        row['error'] = ''
        journal.save_operation(job, row, 'reconciled_reset', {})
        result['reconciled'] += 1


class ShortcutAuditWorker(QObject):
    """Thread trabalhadora para análise, execução e conciliação de atalhos."""

    progress_update = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, service, db_name: str, *, mode: str = 'scan', job: str | None = None, selected=()):
        super().__init__()
        self.service = service
        self.db_name = db_name
        self.mode = mode
        self.job = job
        self.selected = tuple(selected)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            engine = ShortcutAudit(
                self.service,
                self.db_name,
                ConfigManager(),
                lambda: self._cancelled,
                self.progress_update.emit,
            )
            if self.mode == 'execute':
                result = engine.execute(self.job, self.selected)
            elif self.mode == 'reconcile':
                result = engine.reconcile_uncertain(self.job, self.selected)
            else:
                result = engine.scan()
            self.finished.emit(result)
        except DedupCancelled:
            self.finished.emit({'cancelled': True})
        except Exception as exc:
            self.failed.emit(str(exc))
