"""Migração de usuários: API simulada e diário temporário, sem acervo real."""
import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from googleapiclient.errors import HttpError
from httplib2 import Response

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from scripts.test_hash_dedup import Request
from src.services.shortcut_migration import (
    ShortcutMigration, MigrationJournal, FOLDER_MIME, SHORTCUT, MARKER, folder_id,
    _is_uncertain_create_error,
)


class Config:
    readonly = False
    sandbox = False

    def get(self, key, default=None):
        return {'production_drive_id': 'bank', 'sandbox_drive_id': 'archive'}.get(key, default)

    def is_read_only(self):
        return self.readonly

    def is_sandbox(self):
        return self.sandbox


class Drive:
    def __init__(self):
        self.items, self.writes, self.reads = {}, [], []
        self.fail_create_after = False
        self.fail_move_after = False
        self.fail_move_before = False
        self.incomplete = False
        self.cancel_hook = None
        self.folder('bank', 'Banco de Imagens', None, 'bank')
        self.folder('archive', '_TestesBanco', None, 'archive')
        self.folder('destination', 'Recebidos', 'bank', 'bank')
        self.folder('myroot', 'Meu Drive', None, None)
        self.items['root'] = self.items['myroot']
        self.folder('source', 'Fotos Maria', 'myroot', None)
        self.folder('nested', 'Viagens', 'source', None)
        self.photo('photo', 'foto.jpg', 'source')
        self.photo('child', 'outra.jpg', 'nested', md5='b' * 32)

    def folder(self, fid, name, parent, drive):
        self.items[fid] = dict(id=fid, name=name, mimeType=FOLDER_MIME, parents=[parent] if parent else [],
                               driveId=drive, trashed=False, version='1', capabilities={'canAddChildren': True})

    def photo(self, fid, name, parent, md5='a' * 32):
        self.items[fid] = dict(id=fid, name=name, mimeType='image/jpeg', parents=[parent],
                               driveId=self.items[parent].get('driveId'), trashed=False, version='1',
                               size='100', md5Checksum=md5, sha256Checksum=md5 * 2, description='tags origem',
                               createdTime='2020-01-01T00:00:00Z',
                               capabilities={'canMoveItemIntoTeamDrive': True, 'canEdit': True})

    def files(self):
        return self

    def get(self, fileId, **kwargs):
        self.reads.append(('get', fileId))
        if fileId not in self.items:
            raise ValueError('File not found: ' + fileId)
        return Request(lambda _: copy.deepcopy(self.items[fileId]), '"' + self.items[fileId]['version'] + '"')

    def list(self, q, corpora=None, driveId=None, pageToken=None, **kwargs):
        self.reads.append(('list', q, driveId))
        parent = re.search(r"'([^']+)' in parents", q)
        marker = re.search(r"value='([^']+)'", q)
        rows = [v for v in self.items.values() if not v.get('trashed')
                and (not driveId or v.get('driveId') == driveId)
                and (not parent or parent.group(1) in v.get('parents', []))
                and (not marker or v.get('appProperties', {}).get(MARKER) == marker.group(1))]
        start = int(pageToken or 0)
        result = dict(files=copy.deepcopy(rows[start:start + 2]), incompleteSearch=self.incomplete)
        if start + 2 < len(rows):
            result['nextPageToken'] = str(start + 2)
        return Request(lambda _: result)

    def create(self, body, **kwargs):
        def perform(request):
            fid = 'created-' + str(len(self.items))
            self.items[fid] = dict(copy.deepcopy(body), id=fid, version='1', trashed=False,
                                   driveId=self.items[body['parents'][0]].get('driveId'),
                                   capabilities={'canAddChildren': True})
            self.writes.append(('create', fid, copy.deepcopy(body)))
            if self.fail_create_after:
                self.fail_create_after = False
                raise TimeoutError('criado, resposta perdida')
            return {'id': fid}
        return Request(perform)

    def update(self, fileId, addParents, removeParents, body, **kwargs):
        def perform(request):
            item = self.items[fileId]
            if self.fail_move_before:
                raise PermissionError('403: movimentação negada')
            if request.headers.get('If-Match') != '"' + item['version'] + '"':
                raise RuntimeError('412: editado por outra pessoa')
            if item['parents'] != [removeParents]:
                raise RuntimeError('pai mudou')
            item.update(body)
            item['parents'] = [addParents]
            item['driveId'] = self.items[addParents].get('driveId')
            item['version'] = str(int(item['version']) + 1)
            self.writes.append(('move', fileId, addParents))
            if self.fail_move_after:
                self.fail_move_after = False
                raise TimeoutError('movido, resposta perdida')
            if self.cancel_hook:
                self.cancel_hook()
            return copy.deepcopy(item)
        return Request(perform)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'data' / 'file_index.db')
        self.drive, self.cfg = Drive(), Config()
        self.cancelled = False
        self.engine = ShortcutMigration(self.drive, self.db, self.cfg, lambda: self.cancelled)
        self.queue = patch('src.services.shortcut_migration.assert_queue_empty')
        self.queue.start()
        self.addCleanup(self.queue.stop)

    def scan(self):
        return self.engine.scan('source', 'destination')

    def execute(self, plan, ids=None):
        return self.engine.execute(plan['id'], ids if ids is not None else [r['source']['id'] for r in plan['items']])

    def load(self, plan):
        journal = MigrationJournal(self.db)
        try:
            return journal.load(plan['id'])
        finally:
            journal.close()

    def shortcuts(self):
        return [v for v in self.drive.items.values() if v['mimeType'] == SHORTCUT]

    def test_analysis_read_only_and_recursive_hash_comparison(self):
        self.drive.photo('existing', 'nome diferente.jpg', 'destination')
        p = self.scan()
        self.assertEqual([], self.drive.writes)
        self.assertEqual(['duplicate', 'import'], [r['action'] for r in p['items']])
        self.assertEqual('existing', p['items'][0]['target_id'])
        self.assertEqual(['Fotos Maria', 'Viagens', 'outra.jpg'], p['items'][1]['parts'])

    def test_files_moved_same_id_shortcuts_left_and_tree_mirrored(self):
        p = self.scan()
        r = self.execute(p)
        self.assertEqual([], r['errors'])
        self.assertEqual(2, r['imported'])
        self.assertEqual(2, len(self.shortcuts()))
        for fid, origin in [('photo', 'source'), ('child', 'nested')]:
            self.assertEqual('bank', self.drive.items[fid]['driveId'])
            shortcut = next(s for s in self.shortcuts() if s['shortcutDetails']['targetId'] == fid)
            self.assertEqual([origin], shortcut['parents'])
            self.assertEqual('tags origem', self.drive.items[fid]['description'])
        parent = self.drive.items[self.drive.items['child']['parents'][0]]
        self.assertEqual('Viagens', parent['name'])
        self.assertEqual('Fotos Maria', self.drive.items[parent['parents'][0]]['name'])
        self.assertEqual('completed', self.load(p)['items'][0]['state'])

    def test_duplicate_goes_to_apagar_shortcut_points_to_bank(self):
        self.drive.photo('existing', 'original.jpg', 'destination')
        p = self.scan()
        r = self.execute(p)
        self.assertEqual([], r['errors'])
        self.assertEqual(1, r['duplicates'])
        archived = self.drive.items['photo']
        self.assertEqual('archive', archived['driveId'])
        parent = self.drive.items[archived['parents'][0]]
        self.assertEqual('APAGAR', parent['name'])
        self.assertEqual('Banco_Apagado', self.drive.items[parent['parents'][0]]['name'])
        shortcut = next(s for s in self.shortcuts() if s['parents'] == ['source'])
        self.assertEqual('existing', shortcut['shortcutDetails']['targetId'])
        self.assertFalse(any(w[0] == 'move' and w[1] == 'existing' for w in self.drive.writes))

    def test_existing_shortcut_and_folder_shortcut_not_followed(self):
        self.drive.items['old-shortcut'] = dict(id='old-shortcut', name='atalho', mimeType=SHORTCUT,
                                                parents=['source'], shortcutDetails={'targetId': 'nested'}, version='1')
        p = self.scan()
        self.assertEqual(1, p['skipped_shortcuts'])
        self.assertEqual(2, len(p['items']))
        self.execute(p)
        self.assertEqual(['source'], self.drive.items['old-shortcut']['parents'])

    def test_duplicate_within_source_depends_on_first_import(self):
        self.drive.items['child'].update(md5Checksum='a' * 32, sha256Checksum='a' * 64)
        p = self.scan()
        self.assertEqual('photo', p['items'][1]['target_id'])
        r = self.execute(p)
        self.assertEqual((1, 1), (r['imported'], r['duplicates']))
        self.assertEqual({'photo'}, {s['shortcutDetails']['targetId'] for s in self.shortcuts()})

    def test_unselected_dependency_never_archived(self):
        self.drive.items['child'].update(md5Checksum='a' * 32, sha256Checksum='a' * 64)
        p = self.scan()
        r = self.execute(p, ['child'])
        self.assertTrue(r['errors'])
        self.assertEqual(['nested'], self.drive.items['child']['parents'])

    def test_move_timeout_reconciles_without_second_move(self):
        p = self.scan()
        self.drive.fail_move_after = True
        r = self.execute(p)
        self.assertTrue(r['errors'])
        self.assertEqual('bank', self.drive.items['photo']['driveId'])
        r = self.execute(p)
        self.assertEqual([], r['errors'])
        self.assertEqual(2, len([w for w in self.drive.writes if w[0] == 'move']))
        self.assertEqual(2, len(self.shortcuts()))

    def test_create_timeout_reconciles_without_duplicate_folder(self):
        p = self.scan()
        self.drive.fail_create_after = True
        with self.assertRaises(TimeoutError):
            self.execute(p)
        r = self.execute(p)
        self.assertEqual([], r['errors'])
        self.assertEqual(1, len([v for v in self.drive.items.values()
                                if v['name'] == 'Fotos Maria' and v.get('driveId') == 'bank']))

    def test_resume_rechecks_destination_name_occupied_after_failure(self):
        plan = self.scan()
        self.drive.fail_move_before = True
        self.assertTrue(self.execute(plan)['errors'])
        row = self.load(plan)['items'][0]
        self.drive.photo('external', row['destination_name'], row['destination_parent'], md5='c' * 32)
        self.drive.fail_move_before = False
        result = self.execute(plan)
        self.assertIn('ocupado', result['errors'][0])
        self.assertEqual(['source'], self.drive.items['photo']['parents'])

    def test_archive_names_loaded_once_and_collisions_reserved_between_files(self):
        self.drive.photo('existing', 'preservada.jpg', 'destination')
        self.drive.items['child'].update(name='foto.jpg', md5Checksum='a' * 32, sha256Checksum='a' * 64)
        self.drive.folder('archived', 'Banco_Apagado', 'archive', 'archive')
        self.drive.folder('apagar', 'APAGAR', 'archived', 'archive')
        plan = self.scan()
        self.drive.reads.clear()
        result = self.execute(plan)
        self.assertEqual([], result['errors'])
        self.assertEqual(2, result['duplicates'])
        self.assertNotEqual(self.drive.items['photo']['name'], self.drive.items['child']['name'])
        listings = [r for r in self.drive.reads if r[0] == 'list'
                    and r[1] == "'apagar' in parents and trashed = false"]
        self.assertEqual(1, len(listings))

    def test_shortcut_create_timeout_keeps_original_and_resumes(self):
        p = self.scan()
        original_create = self.drive.create
        def create(body, **kw):
            if body['mimeType'] == SHORTCUT:
                self.drive.fail_create_after = True
            return original_create(body, **kw)
        with patch.object(self.drive, 'create', side_effect=create):
            r = self.execute(p)
        self.assertTrue(r['errors'])
        self.assertEqual(['source'], self.drive.items['photo']['parents'])
        self.assertEqual(1, len(self.shortcuts()))
        r = self.execute(p)
        self.assertEqual([], r['errors'])
        self.assertEqual(2, len(self.shortcuts()))

    def test_unknown_create_errors_remain_uncertain(self):
        for error in (ValueError('resposta inválida'), RuntimeError('falha desconhecida'),
                      json.JSONDecodeError('JSON inválido', '', 0), TimeoutError(),
                      ConnectionError(), PermissionError('origem da falha desconhecida')):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(_is_uncertain_create_error(error))
        for status in (200, 302, 408, 409, 429, 500, 503):
            with self.subTest(status=status):
                self.assertTrue(_is_uncertain_create_error(HttpError(Response({'status': status}), b'{}')))
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.assertFalse(_is_uncertain_create_error(HttpError(Response({'status': status}), b'{}')))

    def test_invalid_create_response_preserves_intent_and_prevents_duplicate(self):
        plan = self.scan()
        real_create = self.drive.create

        def invalid_response(body, **kwargs):
            request = real_create(body, **kwargs)
            if body['mimeType'] != SHORTCUT:
                return request

            def perform(_):
                request.execute()
                raise json.JSONDecodeError('Resposta inválida após criar', '', 0)
            return Request(perform)

        with patch.object(self.drive, 'create', side_effect=invalid_response):
            result = self.execute(plan, ['photo'])
        self.assertTrue(result['errors'])
        self.assertEqual(1, len(self.shortcuts()))
        key = self.engine.key('shortcut', 'photo', 'source', 'photo')
        journal = MigrationJournal(self.db)
        try:
            recorded = journal.object(key)
            self.assertIsNotNone(recorded)
            self.assertEqual((None, 'creating'), recorded[1:])
        finally:
            journal.close()

        real_listing = self.engine.api.listing

        def marker_not_visible(query, drive=None):
            if "'source' in parents" in query and 'appProperties has' in query:
                return iter([])
            return real_listing(query, drive)

        with patch.object(self.engine.api, 'listing', side_effect=marker_not_visible):
            result = self.execute(plan, ['photo'])
        self.assertEqual([key], result['uncertain_creates'])
        self.assertEqual(1, len(self.shortcuts()))
        self.assertEqual(['source'], self.drive.items['photo']['parents'])
        # Quando o marcador volta a ser encontrado, reutiliza o mesmo atalho.
        result = self.execute(plan, ['photo'])
        self.assertEqual([], result['errors'])
        self.assertEqual(1, result['completed'])
        self.assertEqual(1, len(self.shortcuts()))

    def test_explicit_http_rejection_allows_create_retry(self):
        plan = self.scan()
        real_create = self.drive.create

        def rejected(body, **kwargs):
            if body['mimeType'] != SHORTCUT:
                return real_create(body, **kwargs)

            def perform(_):
                raise HttpError(Response({'status': 403}), b'{"error":{"message":"Forbidden"}}')
            return Request(perform)

        with patch.object(self.drive, 'create', side_effect=rejected):
            result = self.execute(plan, ['photo'])
        self.assertTrue(result['errors'])
        journal = MigrationJournal(self.db)
        try:
            self.assertIsNone(journal.object(self.engine.key('shortcut', 'photo', 'source', 'photo')))
        finally:
            journal.close()
        self.assertEqual([], self.execute(plan, ['photo'])['errors'])
        self.assertEqual(1, len(self.shortcuts()))

    def test_ancestor_collection_covers_entire_selected_branch(self):
        self.drive.folder('deep', 'Europa', 'nested', None)
        self.drive.items['child']['parents'] = ['deep']
        plan = self.scan()
        rows = [r for r in plan['items'] if r['source']['id'] == 'child']
        writable, checked, mirrored = self.engine._folders_needed_for_rows(plan, rows)
        self.assertEqual({'deep'}, writable)
        self.assertEqual(['source', 'nested', 'deep'], [f['meta']['id'] for f in checked])
        self.assertEqual(['source', 'nested', 'deep'], [f['meta']['id'] for f in mirrored])

    def test_renamed_ancestor_blocks_selected_nested_file_before_writes(self):
        plan = self.scan()
        self.drive.items['source']['name'] = 'Renomeada depois da análise'
        with self.assertRaisesRegex(ValueError, 'pasta da origem mudou'):
            self.execute(plan, ['child'])
        self.assertEqual([], self.drive.writes)

    def test_moved_ancestor_blocks_duplicate_without_mirroring(self):
        self.drive.photo('existing', 'preservada.jpg', 'destination', md5='b' * 32)
        plan = self.scan()
        self.drive.folder('elsewhere', 'Outro local', 'myroot', None)
        self.drive.items['source']['parents'] = ['elsewhere']
        with self.assertRaisesRegex(ValueError, 'pasta da origem mudou'):
            self.execute(plan, ['child'])
        self.assertEqual([], self.drive.writes)

    def test_readonly_ancestor_does_not_require_shortcut_permission(self):
        plan = self.scan()
        self.drive.items['source']['capabilities']['canAddChildren'] = False
        result = self.execute(plan, ['child'])
        self.assertEqual([], result['errors'])
        self.assertEqual(1, result['imported'])

    def test_changed_source_is_not_moved(self):
        p = self.scan()
        self.drive.items['photo']['description'] = 'editada por outro usuário'
        r = self.execute(p)
        self.assertTrue(r['errors'])
        self.assertEqual([], self.shortcuts())
        self.assertEqual(['source'], self.drive.items['photo']['parents'])

    def test_changed_target_is_not_archived(self):
        self.drive.photo('existing', 'original.jpg', 'destination')
        p = self.scan()
        self.drive.items['existing']['trashed'] = True
        r = self.execute(p)
        self.assertTrue(r['errors'])
        self.assertEqual(['source'], self.drive.items['photo']['parents'])

    def test_new_bank_duplicate_after_analysis_blocks_import(self):
        p = self.scan()
        self.drive.photo('new-bank-file', 'novo.jpg', 'destination')
        r = self.execute(p)
        self.assertTrue(r['errors'])
        self.assertEqual(['source'], self.drive.items['photo']['parents'])

    def test_native_or_missing_hash_blocked(self):
        self.drive.items['photo'].pop('md5Checksum')
        p = self.scan()
        self.assertIn('Sem MD5', p['items'][0]['error'])
        r = self.execute(p)
        self.assertEqual(1, r['completed'])
        self.assertEqual(['source'], self.drive.items['photo']['parents'])

    def test_sha256_conflict_blocked(self):
        self.drive.photo('existing', 'original.jpg', 'destination')
        self.drive.items['existing']['sha256Checksum'] = 'f' * 64
        self.assertIn('SHA-256', self.scan()['items'][0]['error'])

    def test_incomplete_catalog_aborts_analysis(self):
        self.drive.incomplete = True
        with self.assertRaises(ValueError):
            self.scan()
        self.assertEqual([], self.drive.writes)

    def test_wrong_source_or_destination_blocked(self):
        with self.assertRaises(ValueError):
            self.engine.scan('destination', 'destination')
        with self.assertRaises(ValueError):
            self.engine.scan('source', 'archive')
        self.assertEqual([], self.drive.writes)

    def test_no_move_permission_blocked(self):
        self.drive.items['photo']['capabilities']['canMoveItemIntoTeamDrive'] = False
        p = self.scan()
        self.assertIn('permissão', p['items'][0]['error'])
        self.execute(p)
        self.assertEqual(['source'], self.drive.items['photo']['parents'])

    def test_readonly_prevents_all_writes(self):
        p = self.scan()
        self.cfg.readonly = True
        with self.assertRaises(ValueError):
            self.execute(p)
        self.assertEqual([], self.drive.writes)

    def test_cancel_keeps_completed_and_pending_for_resume(self):
        p = self.scan()
        self.drive.cancel_hook = lambda: setattr(self, 'cancelled', True)
        r = self.execute(p)
        self.assertTrue(r['cancelled'])
        self.assertEqual(1, r['completed'])
        self.assertEqual('completed', self.load(p)['items'][0]['state'])
        self.assertEqual(['nested'], self.drive.items['child']['parents'])

    def test_name_collision_never_overwrites(self):
        self.drive.folder('other-source', 'Fotos Maria', 'destination', 'bank')
        p = self.scan()
        r = self.execute(p)
        self.assertEqual([], r['errors'])
        self.assertEqual('Fotos Maria', self.drive.items['other-source']['name'])
        parent = self.drive.items[self.drive.items['photo']['parents'][0]]
        self.assertIn('[origem source]', parent['name'])

    def test_existing_archive_ambiguity_blocks(self):
        self.drive.photo('existing', 'original.jpg', 'destination')
        self.drive.folder('archive1', 'Banco_Apagado', 'archive', 'archive')
        self.drive.folder('archive2', 'Banco_Apagado', 'archive', 'archive')
        with self.assertRaises(ValueError):
            self.execute(self.scan())
        self.assertEqual([], self.shortcuts())

    def test_ids_not_local_paths(self):
        self.assertEqual('abc_123', folder_id('https://drive.google.com/drive/u/0/folders/abc_123'))
        for value in ('O:\\Fotos', 'root', '../file', 'https://example.org/folders/abc'):
            with self.assertRaises(ValueError):
                folder_id(value)


class MigrationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from types import SimpleNamespace
        from src.ui.shortcut_migration_dialog import ShortcutMigrationDialog
        self.fx = MigrationTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.patch = patch('src.ui.shortcut_migration_dialog.ConfigManager', return_value=self.fx.cfg)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.dialog = ShortcutMigrationDialog(indexer=SimpleNamespace(db_name=self.fx.db), drive_service=self.fx.drive)
        self.addCleanup(self.close)

    def close(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_review_requires_access_confirmation_and_same_scope(self):
        self.dialog._set_plan(self.fx.scan())
        self.assertFalse(self.dialog.execute.isEnabled())
        self.dialog.access.setChecked(True)
        self.assertTrue(self.dialog.execute.isEnabled())
        self.dialog.source.setText('different-source')
        self.assertFalse(self.dialog.execute.isEnabled())

    def test_blocked_files_not_selected(self):
        from PyQt6.QtCore import Qt
        self.fx.drive.items['photo'].pop('md5Checksum')
        self.dialog._set_plan(self.fx.scan())
        self.assertNotIn('photo', self.dialog.selected)
        self.assertFalse(self.dialog.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_large_review_is_paginated(self):
        plan = self.fx.scan()
        plan['items'] = [dict(copy.deepcopy(plan['items'][0]), source=dict(plan['items'][0]['source'], id=str(i))) for i in range(3001)]
        self.dialog._set_plan(plan)
        self.assertEqual(100, self.dialog.table.rowCount())
        self.dialog._change_page(1)
        self.assertEqual(100, self.dialog.table.rowCount())
        self.assertEqual(3001, len(self.dialog.selected))

    def test_saved_plan_can_be_reopened(self):
        p = self.fx.scan()
        self.fx.execute(p)
        self.dialog._histories()
        self.dialog._resume()
        self.assertEqual(p['id'], self.dialog.plan['id'])
        self.assertEqual(set(), self.dialog.selected)
        self.assertEqual('completed', self.dialog.plan['items'][0]['state'])

    def test_shared_top_level_folder_without_parents_allowed_but_own_root_blocked(self):
        self.fx.drive.items['source']['parents'] = []
        plan = self.fx.scan()
        self.assertEqual(2, len(plan['items']))
        res = self.fx.execute(plan, ['photo'])
        self.assertEqual([], res['errors'])
        self.assertEqual(1, res['imported'])

        # Raiz real do Meu Drive da própria conta autenticada continua bloqueada.
        with self.assertRaises(ValueError):
            self.fx.engine.scan('myroot', 'destination')

    def test_pre_create_permission_error_and_uncertain_create_unlock(self):
        from PyQt6.QtWidgets import QMessageBox
        p = self.fx.scan()
        orig_create = self.fx.drive.create

        def fail_perm(body, **kw):
            if body['mimeType'] == SHORTCUT:
                raise PermissionError('403')
            return orig_create(body, **kw)

        with patch.object(self.fx.drive, 'create', side_effect=fail_perm):
            r = self.fx.execute(p, ['photo'])
        self.assertTrue(r['errors'])
        # Após corrigir permissão, a criação não fica travada no diário.
        r2 = self.fx.execute(p, ['photo'])
        self.assertEqual([], r2['errors'])
        self.assertEqual(1, r2['imported'])

        # Erro de transporte durante execute(): não se pode provar se criou no Drive.
        # Bloqueia repetição cega
        # e oferece desbloqueio explícito na UI após confirmar ausência no Drive.
        def fail_net(body, **kw):
            if body['mimeType'] == SHORTCUT:
                def perform(_):
                    raise ConnectionError('conexão interrompida sem resposta')
                return Request(perform)
            return orig_create(body, **kw)

        with patch.object(self.fx.drive, 'create', side_effect=fail_net):
            r3 = self.fx.execute(p, ['child'])
        self.assertTrue(r3['errors'])
        r4 = self.fx.execute(p, ['child'])
        self.assertTrue(r4['uncertain_creates'])
        self.dialog._set_plan(p)
        with patch('src.ui.shortcut_migration_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog._finished(r4)
        r5 = self.fx.execute(p, ['child'])
        self.assertEqual([], r5['errors'])
        self.assertEqual(1, r5['imported'])

    def test_empty_readonly_subfolder_does_not_block_or_create_empty_mirror(self):
        self.fx.drive.folder('ro_sub', 'SomenteLeitura', 'source', None)
        self.fx.drive.items['ro_sub']['capabilities']['canAddChildren'] = False
        plan = self.fx.scan()
        res = self.fx.execute(plan, ['photo'])
        self.assertEqual([], res['errors'])
        self.assertEqual(1, res['imported'])
        self.assertFalse(any(v['name'] == 'SomenteLeitura' and v.get('driveId') == 'bank'
                             for v in self.fx.drive.items.values()))

    def test_scan_cancel_and_error_resume_guidance_in_ui(self):
        from PyQt6.QtWidgets import QMessageBox
        self.dialog._finished(dict(cancelled=True))
        self.assertEqual('Análise cancelada.', self.dialog.status.text())

        self.fx.drive.fail_move_after = True
        p = self.fx.scan()
        res = self.fx.execute(p, ['photo'])
        self.dialog._set_plan(p)
        with patch('src.ui.shortcut_migration_dialog.QMessageBox.warning'):
            self.dialog._finished(res)
        self.assertNotIn('photo', self.dialog.selected)
        self.assertIn('Marcar elegíveis', self.dialog.status.text())
        self.dialog._select(True)
        self.assertIn('photo', self.dialog.selected)
        res_ok = self.fx.execute(p, list(self.dialog.selected))
        self.assertEqual([], res_ok['errors'])
        self.assertEqual(2, res_ok['completed'])


if __name__ == '__main__':
    unittest.main()
