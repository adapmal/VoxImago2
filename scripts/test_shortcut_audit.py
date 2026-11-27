"""Testes do pente-fino de atalhos no Banco de Imagens (somente Drive simulado)."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from googleapiclient.errors import HttpError
from httplib2 import Response
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QApplication

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.test_hash_dedup import Request
from scripts.test_name_disambiguation import RenameDrive
from scripts.test_shortcut_migration import Config
from src.database.database import FileIndexer
from src.services.hash_deduplicator import DriveCatalog
from src.services.shortcut_audit import (
    DRIVE_FILE_URL,
    DRIVE_FOLDER_URL,
    FOLDER_MIME,
    SHORTCUT,
    ShortcutAudit,
    ShortcutAuditJournal,
    additive_missing_tags,
)
from src.ui.shortcut_audit_dialog import ShortcutAuditDialog, _FolderLinkLabel


def _http_error(status: int, reason: str = 'forbidden') -> HttpError:
    payload = json.dumps({'error': {'code': status, 'errors': [{'reason': reason}]}}).encode()
    return HttpError(Response({'status': str(status)}), payload)


def _split_field_tokens(spec: str) -> list[str]:
    tokens, buf, depth = [], [], 0
    for ch in spec:
        if ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            part = ''.join(buf).strip()
            if part:
                tokens.append(part)
            buf.clear()
        else:
            buf.append(ch)
    tail = ''.join(buf).strip()
    if tail:
        tokens.append(tail)
    return tokens


def _filter_fields(value, fields: str | None):
    if not fields or fields.strip() == '*':
        return copy.deepcopy(value)
    if isinstance(value, list):
        return [_filter_fields(elem, fields) for elem in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    filtered = {}
    for token in _split_field_tokens(fields):
        if '(' in token and token.endswith(')'):
            name, sub = token[:-1].split('(', 1)
            name = name.strip()
            if name in value:
                filtered[name] = _filter_fields(value[name], sub)
        else:
            key = token.strip()
            if key in value:
                filtered[key] = copy.deepcopy(value[key])
    return filtered


class AuditDrive(RenameDrive):
    def __init__(self):
        super().__init__()
        self.v2_mode = False
        self.after_write_hook = None
        self.after_get_hook = None
        for item in self.items.values():
            item.setdefault('capabilities', {})['canTrash'] = True
            item['capabilities'].setdefault('canEdit', True)

    def photo(self, fid, name, parent, md5='a' * 32, description='tags origem', *, can_edit=True, can_trash=True):
        super().photo(fid, name, parent, md5=md5)
        self.items[fid]['description'] = description
        self.items[fid]['capabilities']['canEdit'] = can_edit
        self.items[fid]['capabilities']['canTrash'] = can_trash

    def shortcut(
        self,
        fid: str,
        name: str,
        parent: str,
        target_id: str,
        description: str = '',
        *,
        can_trash: bool = True,
        target_mime: str = 'image/jpeg',
    ):
        parent_drive = self.items[parent].get('driveId') if parent in self.items else 'bank'
        self.items[fid] = {
            'id': fid,
            'name': name,
            'mimeType': SHORTCUT,
            'parents': [parent] if parent else [],
            'driveId': parent_drive,
            'trashed': False,
            'description': description,
            'createdTime': '2022-05-01T00:00:00Z',
            'version': '1',
            'capabilities': {'canTrash': can_trash, 'canEdit': True},
            'shortcutDetails': {'targetId': target_id, 'targetMimeType': target_mime},
        }

    def get(self, fileId, fields=None, **kwargs):
        self.reads.append(('get', fileId, fields))
        if fileId not in self.items:
            def raise_404(_req):
                raise _http_error(404, 'notFound')
            return Request(raise_404)
        etag = None if self.v2_mode else ('"' + self.items[fileId]['version'] + '"')

        def perform(_req):
            out = _filter_fields(self.items[fileId], fields)
            if self.after_get_hook:
                self.after_get_hook(fileId, out)
            return out

        return Request(perform, etag)

    def list(self, q, corpora=None, driveId=None, pageToken=None, fields=None, **kwargs):
        inner_req = super().list(q, corpora=corpora, driveId=driveId, pageToken=pageToken, **kwargs)
        return Request(lambda req: _filter_fields(inner_req.execute(), fields))

    def v2_service(self):
        drive = self

        class _V2Files:
            def get(self, fileId, fields=None, **kwargs):
                drive.reads.append(('v2_get', fileId, fields))

                def perform(_req):
                    item = drive.items[fileId]
                    out = {
                        'id': item['id'],
                        'version': item['version'],
                        'title': item['name'],
                        'parents': [{'id': p} for p in item.get('parents', [])],
                        'driveId': item.get('driveId'),
                        'labels': {'trashed': bool(item.get('trashed'))},
                        'description': item.get('description', ''),
                        'etag': '"' + item['version'] + '"',
                    }
                    if 'size' in item:
                        out['fileSize'] = item['size']
                    if 'md5Checksum' in item:
                        out['md5Checksum'] = item['md5Checksum']
                    return _filter_fields(out, fields)

                return Request(perform)

            def patch(self, fileId, body=None, fields=None, **kwargs):
                return drive.update(fileId, body=body, fields=fields, **kwargs)

        class _V2Svc:
            def files(self):
                return _V2Files()

        return _V2Svc()

    def update(self, fileId, body=None, addParents=None, removeParents=None, fields=None, **kwargs):
        body = dict(body or {})
        if body.get('trashed') is True or (body.get('labels') or {}).get('trashed') is True:
            op = 'trash'
        elif 'description' in body and 'name' not in body:
            op = 'tags'
        else:
            op = 'other'

        def perform(request):
            if self.v2_mode and not body.get('labels') and 'trashed' in body:
                raise _http_error(403, 'insufficientFilePermissions')
            item = self.items[fileId]
            should_fail_before = self.fail_before and (self.fail_on_op is None or self.fail_on_op == op)
            should_fail_after = self.fail_after and (self.fail_on_op is None or self.fail_on_op == op)
            if should_fail_before:
                self.fail_before = False
                raise TimeoutError('Sem confirmação')
            if request.headers.get('If-Match') != '"' + item['version'] + '"':
                raise _http_error(412, 'conditionNotMet')
            if 'labels' in body and isinstance(body['labels'], dict) and 'trashed' in body['labels']:
                item['trashed'] = bool(body['labels']['trashed'])
            for k, v in body.items():
                if k != 'labels':
                    item[k] = v
            item['version'] = str(int(item['version']) + 1)
            self.writes.append((op, fileId, copy.deepcopy(body), None))
            if self.after_write_hook:
                self.after_write_hook(op, fileId)
            if should_fail_after:
                self.fail_after = False
                raise TimeoutError('Gravado, resposta perdida')
            if self.cancel_hook:
                self.cancel_hook()
            return _filter_fields(item, fields)

        return Request(perform)


class ShortcutAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'file_index.db')
        self.drive = AuditDrive()
        self.cfg = Config()
        # Clean initial bank: keep bank/destination and create organized folders
        self.drive.folder('folder_efatha', 'Efatha', 'bank', 'bank')
        self.drive.folder('folder_brasil', 'Brasil', 'bank', 'bank')
        self.cancelled = False
        self.engine = ShortcutAudit(self.drive, self.db, self.cfg, lambda: self.cancelled)

    def _seed_indexer(self, rows: list[tuple[str, str, str, str, str]]) -> None:
        """rows: (file_id, name, source, desc, drive_id_or_empty)."""
        indexer = FileIndexer(self.db)
        try:
            with indexer.conn:
                for fid, name, source, desc, drive_id in rows:
                    web_link = f'https://drive.google.com/file/d/{drive_id}/view' if (source == 'local' and drive_id) else ''
                    indexer.cursor.execute(
                        'INSERT INTO files(file_id, name, path, mimeType, source, description, webContentLink) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (fid, name, rf'L:\Banco\{name}' if source == 'local' else drive_id, 'image/jpeg', source, desc, web_link),
                    )
                    indexer.cursor.execute(
                        'INSERT INTO search_index(name, description, normalized_name, normalized_description, file_id, source) VALUES (?, ?, ?, ?, ?, ?)',
                        (name, desc, name.lower(), desc.lower(), fid, source),
                    )
                    if source == 'local' and drive_id:
                        indexer.cursor.execute(
                            'INSERT INTO drive_link_validation(local_file_id, drive_id, status, validated_at) VALUES (?, ?, ?, ?)',
                            (fid, drive_id, 'valid_hash', 1),
                        )
        finally:
            indexer.close()

    def test_additive_missing_tags_preserves_existing_and_deduplicates_case_insensitively(self):
        missing, merged = additive_missing_tags('Menino Jesus, 2020', 'menino jesus, Natal, natal, Procissão')
        self.assertEqual(['Natal', 'Procissão'], missing)
        self.assertEqual('Menino Jesus, 2020, Natal, Procissão', merged)

        missing2, merged2 = additive_missing_tags('Menino Jesus, 2020', '2020, MENINO JESUS')
        self.assertEqual([], missing2)
        self.assertEqual('Menino Jesus, 2020', merged2)

        missing3, merged3 = additive_missing_tags('', 'Natal; Coral')
        self.assertEqual(['Natal', 'Coral'], missing3)
        self.assertEqual('Natal, Coral', merged3)

    def test_scan_reads_only_isolates_broken_branch_detects_conflicts_and_sorts_by_folder(self):
        self.drive.photo('real_festa', 'Festa.jpg', 'folder_efatha', description='Antiga')
        self.drive.shortcut('sc_z', 'z_atalho.jpg', 'folder_efatha', 'real_festa', description='Nova')
        self.drive.shortcut('sc_a', 'Festa.jpg', 'folder_efatha', 'real_festa', description='')
        self.drive.shortcut('sc_b', 'b_brasil.jpg', 'folder_brasil', 'real_festa', description='')
        self.drive.shortcut('sc_broken', 'orfao.jpg', 'missing_parent', 'real_festa', description='')

        plan = self.engine.scan()
        self.assertEqual([], self.drive.writes)
        ids = [r['source']['id'] for r in plan['items']]
        self.assertEqual(['sc_b', 'sc_a', 'sc_z', 'sc_broken'], ids)

        by_id = {r['source']['id']: r for r in plan['items']}
        self.assertEqual('blocked', by_id['sc_broken']['state'])
        self.assertTrue(by_id['sc_a']['has_name_conflict'])
        self.assertFalse(by_id['sc_z']['has_name_conflict'])
        self.assertEqual(['Nova'], by_id['sc_z']['missing_tags'])
        self.assertEqual('transfer_and_trash', by_id['sc_z']['action'])
        self.assertEqual(DRIVE_FOLDER_URL.format('folder_efatha'), by_id['sc_z']['folder_url'])

    def test_untagged_shortcut_trashes_and_cleans_local_db_by_drive_id_only(self):
        self.drive.shortcut('sc_no_tag_ext', 'Festa.jpg', 'folder_efatha', 'photo', description='')
        self.drive.shortcut('sc_no_tag_404', 'Sumido.jpg', 'folder_efatha', 'nao_existe', description='')
        self._seed_indexer([
            ('local_sc', 'Festa.jpg', 'local', '', 'sc_no_tag_ext'),
            ('local_other', 'Festa.jpg', 'local', 'Manter', 'real_festa_outro'),
            ('sc_no_tag_ext', 'Festa.jpg', 'drive', '', ''),
        ])

        plan = self.engine.scan()
        self.assertEqual(0, plan['blocked'])
        result = self.engine.execute(plan['id'], ['sc_no_tag_ext', 'sc_no_tag_404'])
        self.assertEqual(2, result['completed'])
        self.assertEqual(2, result['shortcuts_trashed'])
        self.assertEqual(0, result['tags_updated'])
        self.assertTrue(self.drive.items['sc_no_tag_ext']['trashed'])
        self.assertTrue(self.drive.items['sc_no_tag_404']['trashed'])

        indexer = FileIndexer(self.db)
        try:
            remaining = indexer.cursor.execute('SELECT file_id, description FROM files').fetchall()
        finally:
            indexer.close()
        self.assertEqual([('local_other', 'Manter')], remaining)

        plan_after = self.engine.scan()
        self.assertEqual(0, plan_after['total_shortcuts'])

    def test_tagged_shortcut_adds_only_missing_tags_updates_local_by_drive_id_and_skips_when_already_present(self):
        self.drive.photo('real_target', 'Renata.jpg', 'folder_brasil', description='Menino Jesus, 2020')
        self.drive.photo('real_done', 'Done.jpg', 'folder_brasil', description='Coral, 2021')
        self.drive.shortcut('sc_add', 'Renata.jpg', 'folder_efatha', 'real_target', description='2020, Natal, natal')
        self.drive.shortcut('sc_already', 'Done.jpg', 'folder_efatha', 'real_done', description='2021, coral')
        self._seed_indexer([
            ('local_target', 'Renata.jpg', 'local', 'Menino Jesus, 2020', 'real_target'),
            ('local_same_name', 'Renata.jpg', 'local', 'Outro arquivo nao vinculado', 'other_id'),
            ('real_target', 'Renata.jpg', 'drive', 'Menino Jesus, 2020', ''),
            ('sc_add', 'Renata.jpg', 'drive', '2020, Natal, natal', ''),
        ])

        plan = self.engine.scan()
        by_id = {r['source']['id']: r for r in plan['items']}
        self.assertEqual('transfer_and_trash', by_id['sc_add']['action'])
        self.assertEqual(['Natal'], by_id['sc_add']['missing_tags'])
        self.assertEqual('trash_only', by_id['sc_already']['action'])
        self.assertEqual([], by_id['sc_already']['missing_tags'])

        result = self.engine.execute(plan['id'], ['sc_add', 'sc_already'])
        self.assertEqual(2, result['completed'])
        self.assertEqual(1, result['tags_updated'])
        self.assertEqual(1, result['tags_added'])
        self.assertEqual(1, result['already_tagged'])
        self.assertEqual('Menino Jesus, 2020, Natal', self.drive.items['real_target']['description'])
        self.assertEqual('Coral, 2021', self.drive.items['real_done']['description'])
        # Only one 'tags' write happened (for real_target), zero on real_done.
        tag_writes = [w for w in self.drive.writes if w[0] == 'tags']
        self.assertEqual([('tags', 'real_target', {'description': 'Menino Jesus, 2020, Natal'}, None)], tag_writes)

        indexer = FileIndexer(self.db)
        try:
            rows = dict(indexer.cursor.execute('SELECT file_id, description FROM files').fetchall())
        finally:
            indexer.close()
        self.assertEqual('Menino Jesus, 2020, Natal', rows['local_target'])
        self.assertEqual('Menino Jesus, 2020, Natal', rows['real_target'])
        self.assertEqual('Outro arquivo nao vinculado', rows['local_same_name'])
        self.assertNotIn('sc_add', rows)

    def test_multiple_shortcuts_to_same_target_accumulate_distinct_tags_sequentially(self):
        self.drive.photo('shared_target', 'Foto.jpg', 'folder_brasil', description='Orig')
        self.drive.shortcut('sc1', 'Atalho1.jpg', 'folder_efatha', 'shared_target', description='Natal, Coral')
        self.drive.shortcut('sc2', 'Atalho2.jpg', 'folder_brasil', 'shared_target', description='coral, Procissão')

        plan = self.engine.scan()
        result = self.engine.execute(plan['id'], ['sc1', 'sc2'])
        self.assertEqual(2, result['completed'])
        self.assertEqual(2, result['tags_updated'])
        self.assertEqual(3, result['tags_added'])
        self.assertEqual('Orig, coral, Procissão, Natal', self.drive.items['shared_target']['description'])
        self.assertTrue(self.drive.items['sc1']['trashed'])
        self.assertTrue(self.drive.items['sc2']['trashed'])

    def test_blocking_rules_for_tagged_uneditable_external_trashed_404_folder_cantrash_and_byte_limit(self):
        self.drive.photo('p_ok', 'Ok.jpg', 'folder_efatha', description='')
        self.drive.photo('p_ro', 'RO.jpg', 'folder_efatha', description='Orig', can_edit=False)
        self.drive.photo('p_trashed', 'Trashed.jpg', 'folder_efatha', description='Orig')
        self.drive.items['p_trashed']['trashed'] = True
        self.drive.photo('p_big', 'Big.jpg', 'folder_efatha', description='x' * 29995)

        self.drive.shortcut('sc_404', 's1.jpg', 'folder_efatha', 'does_not_exist', description='Tag1')
        self.drive.shortcut('sc_trashed', 's2.jpg', 'folder_efatha', 'p_trashed', description='Tag2')
        self.drive.shortcut('sc_ext', 's3.jpg', 'folder_efatha', 'photo', description='Tag3')
        self.drive.shortcut('sc_dir', 's4.jpg', 'folder_efatha', 'folder_brasil', description='Tag4', target_mime=FOLDER_MIME)
        self.drive.shortcut('sc_ro', 's5.jpg', 'folder_efatha', 'p_ro', description='Tag5')
        self.drive.shortcut('sc_no_trash', 's6.jpg', 'folder_efatha', 'p_ok', description='', can_trash=False)
        self.drive.shortcut('sc_big', 's7.jpg', 'folder_efatha', 'p_big', description='TagMuitoLonga')

        plan = self.engine.scan()
        by_id = {r['source']['id']: r for r in plan['items']}
        for sid in ('sc_404', 'sc_trashed', 'sc_ext', 'sc_dir', 'sc_ro', 'sc_no_trash', 'sc_big'):
            self.assertEqual('blocked', by_id[sid]['state'], sid)
            self.assertTrue(by_id[sid]['error'], sid)

        res = self.engine.execute(plan['id'], list(by_id.keys()))
        self.assertEqual(0, res['completed'])
        self.assertEqual([], self.drive.writes)

    def test_timeout_before_and_after_on_tag_write_and_reconcile(self):
        self.drive.photo('t_real', 'Alvo.jpg', 'folder_efatha', description='Existente')
        self.drive.shortcut('sc_t', 'Alvo.jpg', 'folder_brasil', 't_real', description='NovaTag')
        self._seed_indexer([('local_t', 'Alvo.jpg', 'local', 'Existente', 't_real')])

        plan = self.engine.scan()

        # 1) Timeout BEFORE tag write succeeds
        self.drive.fail_on_op = 'tags'
        self.drive.fail_before = True
        res1 = self.engine.execute(plan['id'], ['sc_t'])
        self.assertEqual(['sc_t'], res1['uncertain'])
        self.assertEqual('Existente', self.drive.items['t_real']['description'])
        self.assertFalse(self.drive.items['sc_t']['trashed'])
        with self.assertRaises(ValueError):
            self.engine.execute(plan['id'], ['sc_t'])

        rec1 = self.engine.reconcile_uncertain(plan['id'])
        self.assertEqual(1, rec1['reconciled'])
        self.assertEqual(0, rec1['tags_updated'])

        # 2) Timeout AFTER tag write succeeded on Drive
        self.drive.fail_on_op = 'tags'
        self.drive.fail_after = True
        res2 = self.engine.execute(plan['id'], ['sc_t'])
        self.assertEqual(['sc_t'], res2['uncertain'])
        self.assertEqual('Existente, NovaTag', self.drive.items['t_real']['description'])
        self.assertFalse(self.drive.items['sc_t']['trashed'])

        rec2 = self.engine.reconcile_uncertain(plan['id'])
        self.assertEqual(1, rec2['reconciled'])
        self.assertEqual(1, rec2['tags_updated'])
        indexer = FileIndexer(self.db)
        try:
            desc = indexer.cursor.execute("SELECT description FROM files WHERE file_id='local_t'").fetchone()[0]
        finally:
            indexer.close()
        self.assertEqual('Existente, NovaTag', desc)

        # Resume after tags_done finishes by trashing shortcut without re-writing tags
        self.drive.fail_on_op = None
        writes_before = len(self.drive.writes)
        res3 = self.engine.execute(plan['id'], ['sc_t'])
        self.assertEqual(1, res3['completed'])
        self.assertTrue(self.drive.items['sc_t']['trashed'])
        self.assertEqual(['trash'], [w[0] for w in self.drive.writes[writes_before:]])

    def test_timeout_after_trash_reconcile_and_tags_done_tamper_guard(self):
        self.drive.photo('t_real', 'Alvo.jpg', 'folder_efatha', description='Orig')
        self.drive.shortcut('sc_1', 'Atalho1.jpg', 'folder_brasil', 't_real', description='Tag1')
        self.drive.shortcut('sc_2', 'Atalho2.jpg', 'folder_brasil', 't_real', description='Tag2')
        self._seed_indexer([('sc_1', 'Atalho1.jpg', 'drive', '', '')])

        plan = self.engine.scan()

        # Timeout after trash on sc_1 -> reconcile sees trashed=True, cleans local DB & completes
        self.drive.fail_on_op = 'trash'
        self.drive.fail_after = True
        res1 = self.engine.execute(plan['id'], ['sc_1'])
        self.assertEqual(['sc_1'], res1['uncertain'])
        self.assertTrue(self.drive.items['sc_1']['trashed'])

        rec = self.engine.reconcile_uncertain(plan['id'])
        self.assertEqual(1, rec['reconciled'])
        self.assertEqual(1, rec['completed'])
        self.assertEqual(1, rec['local_cleaned'])

        # Simulate tags_done on sc_2, followed by external deletion of Tag2 on target before trashing sc_2
        self.drive.fail_on_op = 'trash'
        self.drive.fail_before = True
        self.engine.execute(plan['id'], ['sc_2'])
        self.engine.reconcile_uncertain(plan['id'])
        self.drive.items['t_real']['description'] = 'Orig'  # Tag2 vanished externally!
        res2 = self.engine.execute(plan['id'], ['sc_2'])
        self.assertEqual(0, res2['completed'])
        self.assertTrue(res2['errors'])
        self.assertFalse(self.drive.items['sc_2']['trashed'])

    def test_v2_conditional_fallback_for_tag_update_and_trash(self):
        self.drive.v2_mode = True
        self.drive.photo('real_v2', 'V2.jpg', 'folder_efatha', description='Orig')
        self.drive.shortcut('sc_v2', 'V2_sc.jpg', 'folder_efatha', 'real_v2', description='Extra')

        # Enforce that Drive fields filtering omits unrequested fields (e.g. sha256Checksum)
        # while returning size and md5Checksum required by DriveCatalog.prepare_write().
        fetched = self.engine.api.get('real_v2')
        self.assertEqual('100', fetched.get('size'))
        self.assertEqual('a' * 32, fetched.get('md5Checksum'))
        self.assertNotIn('sha256Checksum', fetched)

        plan = self.engine.scan()
        with patch('src.services.hash_deduplicator.conditional_service', return_value=self.drive.v2_service()):
            result = self.engine.execute(plan['id'], ['sc_v2'])
        self.assertEqual(1, result['completed'])
        self.assertEqual([], result['uncertain'])
        self.assertEqual('Orig, Extra', self.drive.items['real_v2']['description'])
        self.assertTrue(self.drive.items['sc_v2']['trashed'])

    def test_destination_trashed_right_after_tag_write_preserves_shortcut_and_records_pending(self):
        self.drive.photo('t_real', 'Foto.jpg', 'folder_efatha', description='Orig')
        self.drive.shortcut('sc_keep', 'Foto.jpg', 'folder_brasil', 't_real', description='Nova')
        plan = self.engine.scan()

        def trash_target_after_tag_write(op, file_id):
            if op == 'tags' and file_id == 't_real':
                self.drive.items['t_real']['trashed'] = True

        self.drive.after_write_hook = trash_target_after_tag_write
        result = self.engine.execute(plan['id'], ['sc_keep'])
        self.assertEqual(0, result['completed'])
        self.assertEqual(0, result['shortcuts_trashed'])
        self.assertEqual([], result['uncertain'])
        self.assertTrue(result['errors'])
        self.assertFalse(self.drive.items['sc_keep']['trashed'])

        journal = ShortcutAuditJournal(self.db)
        try:
            row = journal.get_operation(plan['id'], 'sc_keep')
            events = [
                r[0] for r in journal.conn.execute(
                    'SELECT event FROM events WHERE job = ? AND sc_id = ?',
                    (plan['id'], 'sc_keep'),
                ).fetchall()
            ]
        finally:
            journal.close()
        self.assertEqual('error', row['state'])
        self.assertIn('Lixeira', row['error'])
        self.assertIn('target_invalid_after_tags', events)

    def test_destination_moved_outside_drive_before_trash_preserves_shortcut_and_records_pending(self):
        self.drive.photo('t_real', 'Foto.jpg', 'folder_efatha', description='Orig')
        self.drive.shortcut('sc_keep', 'Foto.jpg', 'folder_brasil', 't_real', description='Orig')
        plan = self.engine.scan()

        # Simulate destination leaving Banco de Imagens after initial target validation
        # and right before authorizing shortcut trash
        t_gets = [0]

        def move_target_outside(file_id, _payload):
            if file_id == 't_real':
                t_gets[0] += 1
                if t_gets[0] == 1:
                    self.drive.items['t_real']['driveId'] = 'archive'

        self.drive.after_get_hook = move_target_outside
        result = self.engine.execute(plan['id'], ['sc_keep'])
        self.assertEqual(0, result['completed'])
        self.assertEqual(0, result['shortcuts_trashed'])
        self.assertTrue(result['errors'])
        self.assertFalse(self.drive.items['sc_keep']['trashed'])

        journal = ShortcutAuditJournal(self.db)
        try:
            row = journal.get_operation(plan['id'], 'sc_keep')
            events = [
                r[0] for r in journal.conn.execute(
                    'SELECT event FROM events WHERE job = ? AND sc_id = ?',
                    (plan['id'], 'sc_keep'),
                ).fetchall()
            ]
        finally:
            journal.close()
        self.assertEqual('error', row['state'])
        self.assertIn('Banco de Imagens', row['error'])
        self.assertIn('target_invalid_before_trash', events)

    def test_dialog_merges_folder_cells_renders_hyperlink_and_tracks_uncertain_state(self):
        self.drive.photo('target_a', 'ImgA.jpg', 'folder_efatha', description='Orig')
        self.drive.shortcut('sc_1', 'A1.jpg', 'folder_efatha', 'target_a', description='T1')
        self.drive.shortcut('sc_2', 'A2.jpg', 'folder_efatha', 'target_a', description='')
        self.drive.shortcut('sc_3', 'B1.jpg', 'folder_brasil', 'target_a', description='')

        plan = self.engine.scan()
        dlg = ShortcutAuditDialog(drive_service=self.drive)
        dlg._db_dummy = None
        dlg.db_name = self.db
        dlg._set_plan(plan)

        self.assertEqual(3, dlg.table.rowCount())
        # First two shortcuts are in folder_brasil? Wait: alphabetical by folder_path!
        # 'Banco de Imagens/Brasil' comes before 'Banco de Imagens/Efatha'!
        self.assertEqual(1, dlg.table.rowSpan(0, 1))
        self.assertEqual(2, dlg.table.rowSpan(1, 1))

        label = dlg.table.cellWidget(1, 1)
        self.assertIsInstance(label, _FolderLinkLabel)
        expected_url = DRIVE_FOLDER_URL.format('folder_efatha')
        self.assertIn(expected_url, label.text())

        opened = []
        with patch('src.ui.shortcut_audit_dialog.QDesktopServices.openUrl', side_effect=opened.append):
            label.linkActivated.emit(expected_url)
            dlg._open_link(1, 2)
            dlg._open_link(1, 4)
        self.assertEqual([
            QUrl(expected_url),
            QUrl(DRIVE_FILE_URL.format('sc_1')),
            QUrl(DRIVE_FILE_URL.format('target_a')),
        ], opened)

        dlg.select_none.click()
        self.assertEqual(set(), dlg.selected)
        dlg.select_all.click()
        self.assertEqual({'sc_1', 'sc_2', 'sc_3'}, dlg.selected)

        # Mark sc_1 uncertain and ensure reconcile button enables while execute disables
        journal = ShortcutAuditJournal(self.db)
        try:
            row = journal.get_operation(plan['id'], 'sc_1')
            row['state'] = 'uncertain'
            row['uncertain_phase'] = 'tags'
            journal.save_operation(plan['id'], row, 'test_uncertain', {})
        finally:
            journal.close()

        dlg._set_plan(plan)
        self.assertTrue(dlg.reconcile_button.isEnabled())
        self.assertFalse(dlg.execute.isEnabled())
        dlg.close()
        dlg.deleteLater()

    def test_scan_list_respects_DriveCatalog_type(self):
        self.assertIsInstance(self.engine.api, DriveCatalog)


if __name__ == '__main__':
    unittest.main()
