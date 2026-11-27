"""Fila contextual: somente arquivos descartáveis e nuvem simulada."""
import copy
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from src.utils.staging_schema import StagingItem
from src.services.contextual_queue import ContextStore, QueueCatalog, ContextEngine, QueueCloud, norm, queue_conflict


class Config:
    def __init__(self, root):
        self.root = str(root)
    def is_read_only(self):
        return False
    def is_sandbox(self):
        return False
    def get(self, key, default=None):
        return [self.root] if key == 'scan_paths' else default


class Cloud:
    def __init__(self):
        self.files = {}
        self.writes = []
        self.offline = False
        self.fail_after_write = False
    def bind(self, item, catalog, saved_id=''):
        if self.offline:
            raise ValueError('offline')
        found = [r for r in self.files.values() if (r['id'] == saved_id if saved_id else norm(r['path']) == norm(item.path))]
        if len(found) != 1 or norm(found[0]['path']) != norm(item.path):
            raise ValueError('identidade não confirmada')
        return copy.deepcopy(found[0])
    def folder(self, path):
        if self.offline:
            raise ValueError('offline')
        return norm(path)
    def children(self, parent, name=None):
        return [r for r in self.files.values() if norm(os.path.dirname(r['path'])) == parent
                and (name is None or os.path.basename(r['path']).casefold() == name.casefold()) and not r.get('trashed')]
    def set_description(self, remote, text):
        self.writes.append((remote['id'], text))
        self.files[remote['id']]['description'] = text
        if self.fail_after_write:
            raise TimeoutError('resposta perdida')
    def confirm(self, payload):
        if self.offline:
            raise ValueError('offline')
        if payload['action'] == 'create_folder':
            found = [r for r in self.files.values() if norm(r['path']) == norm(payload['new_path'])]
            return bool(found), found[0]['id'] if found else ''
        remote = self.files[payload['drive_id']]
        if payload['action'] in ('set_description', 'add_tags', 'remove_tags'):
            return remote.get('description') == payload['description'], remote['id']
        if payload['action'] == 'delete':
            return bool(remote.get('trashed')), remote['id']
        return norm(remote['path']) == norm(payload['new_path']), remote['id']


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Banco'
        self.root.mkdir()
        self.destination = self.root / 'Destino'
        self.destination.mkdir()
        self.cfg, self.cloud = Config(self.root), Cloud()
        self.db = self.base / 'catalog.db'
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE files(file_id TEXT PRIMARY KEY,name TEXT,path TEXT,source TEXT,description TEXT,parentId TEXT,mimeType TEXT,webContentLink TEXT,size INTEGER,thumbnailRotation INTEGER,thumbnailPath TEXT)')
            conn.execute('CREATE TABLE search_index(name TEXT,description TEXT,normalized_name TEXT,normalized_description TEXT,file_id TEXT,source TEXT)')
        conn.close()
        self.store = ContextStore(self.base / 'queue.db', self.base / 'absent.json')
        self.store.initialize()
        self.catalog = QueueCatalog(self.db)
        self.addCleanup(self.catalog.close)
        self.photo = self.add_file('foto.jpg', 'drive-photo')

    def add_file(self, relative, fid):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test-media')
        with self.catalog.conn:
            self.catalog.conn.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (norm(str(path)), path.name, norm(str(path)), 'local', 'Brasil', norm(str(path.parent)), 'image/jpeg', '', 10, 90, 'cache.jpg'))
        self.cloud.files[fid] = dict(id=fid, path=str(path), name=path.name, description='Brasil', size='10')
        return str(path)

    def item(self, action, new='', path=None):
        path = path or self.photo
        old = 'Brasil' if action in ('set_description', 'add_tags', 'remove_tags') else path if action == 'move' else os.path.basename(path)
        return StagingItem(norm(path), os.path.basename(path), path, action, old, new)

    def save(self, *items):
        self.store.save([i.to_dict() for i in items])

    def run_items(self, *items, check=False):
        engine = ContextEngine(self.store, self.catalog, self.cloud, self.cfg)
        return engine.run([i.operation_id for i in items], check_only=check)

    def rows(self):
        return {r['operation_id']: r for r in self.store.load()}

    def test_rename_rebases_move_and_tags_but_waits_for_cloud(self):
        rename = self.item('rename', 'nova.jpg')
        move = self.item('move', str(self.destination / 'foto.jpg'))
        tags = self.item('add_tags', 'Nova')
        self.save(move, tags, rename)
        result = self.run_items(rename)
        new = str(self.root / 'nova.jpg')
        self.assertTrue(os.path.exists(new))
        self.assertFalse(os.path.exists(self.photo))
        self.assertEqual(1, result['waiting'])
        rows = self.rows()
        self.assertEqual(new, rows[tags.operation_id]['path'])
        self.assertEqual(str(self.destination / 'nova.jpg'), rows[move.operation_id]['new_value'])
        self.assertEqual(rows[rename.operation_id]['asset_id'], rows[tags.operation_id]['asset_id'])
        self.run_items(tags, move)
        self.assertEqual([], self.cloud.writes)
        self.assertFalse((self.destination / 'nova.jpg').exists())
        self.cloud.files['drive-photo']['path'] = new
        self.run_items(check=True)
        self.assertNotIn(rename.operation_id, self.rows())
        self.run_items(tags)
        self.assertEqual([('drive-photo', 'Brasil, Nova')], self.cloud.writes)
        self.run_items(move)
        self.assertTrue((self.destination / 'nova.jpg').exists())
        row = self.catalog.find(str(self.destination / 'nova.jpg'))
        self.assertEqual(90, row['thumbnailRotation'])
        self.assertEqual('cache.jpg', row['thumbnailPath'])

    def test_move_blocks_same_hash_different_name_in_destination(self):
        self.add_file('Destino/outra.jpg', 'other')
        self.cloud.files['drive-photo']['md5Checksum'] = 'a' * 32
        self.cloud.files['other']['md5Checksum'] = 'a' * 32
        move = self.item('move', str(self.destination / 'foto.jpg'))
        self.save(move)
        result = self.run_items(move)
        self.assertTrue(os.path.exists(self.photo))
        self.assertFalse((self.destination / 'foto.jpg').exists())
        self.assertEqual('blocked', self.rows()[move.operation_id]['execution_state'])

    def test_move_rechecks_destination_after_prepare(self):
        move = self.item('move', str(self.destination / 'foto.jpg'))
        engine = ContextEngine(self.store, self.catalog, self.cloud, self.cfg)
        payload = engine.prepare(move)
        self.cloud.files['new'] = dict(id='new', path=str(self.destination / 'foto.jpg'), name='foto.jpg')
        with self.assertRaises(ValueError):
            engine.apply_local(move, payload)
        self.assertTrue(os.path.exists(self.photo))

    def test_move_then_rename_tracks_current_directory(self):
        move = self.item('move', str(self.destination / 'foto.jpg'))
        rename = self.item('rename', 'final.jpg')
        self.save(move, rename)
        self.run_items(move)
        self.cloud.files['drive-photo']['path'] = str(self.destination / 'foto.jpg')
        self.run_items(check=True)
        self.run_items(rename)
        self.assertTrue((self.destination / 'final.jpg').exists())

    def test_waiting_rename_is_not_repeated_after_restart(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.run_items(rename)
        with patch('os.rename', side_effect=AssertionError('must not replay')):
            self.run_items(rename)
            self.cloud.files['drive-photo']['path'] = str(self.root / 'nova.jpg')
            self.run_items(check=True)
        self.assertEqual([], self.store.load())

    def test_unknown_move_response_reconciles_by_file_identity(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        original = os.rename
        def lost_response(src, dst):
            original(src, dst)
            raise TimeoutError('local result lost')
        with patch('os.rename', side_effect=lost_response):
            self.run_items(rename)
        self.assertEqual('applying', self.store.operation(rename.operation_id)['state'])
        self.cloud.files['drive-photo']['path'] = str(self.root / 'nova.jpg')
        with patch('os.rename', side_effect=AssertionError('must not replay')):
            self.run_items(check=True)
        self.assertEqual([], self.store.load())

    def test_unproven_local_failure_blocks_replay(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        with patch('os.rename', side_effect=PermissionError('locked')):
            self.run_items(rename)
        with patch('os.rename', side_effect=AssertionError('must not replay')):
            self.run_items(rename)
        self.assertTrue(os.path.exists(self.photo))
        self.assertEqual('applying', self.store.operation(rename.operation_id)['state'])

    def test_tags_read_cloud_live_and_preserve_other_users_tags(self):
        add = self.item('add_tags', 'Nova')
        self.save(add)
        self.cloud.files['drive-photo']['description'] = 'Brasil, Outra pessoa'
        self.run_items(add)
        self.assertEqual('Brasil, Outra pessoa, Nova', self.cloud.files['drive-photo']['description'])

    def test_replace_description_conflict_does_not_overwrite(self):
        replace = self.item('set_description', 'Substituir')
        self.save(replace)
        self.cloud.files['drive-photo']['description'] = 'Alterada externamente'
        self.run_items(replace)
        self.assertEqual([], self.cloud.writes)
        self.assertEqual('blocked', self.store.operation(replace.operation_id)['state'])

    def test_tag_timeout_readback_never_resends(self):
        add = self.item('add_tags', 'Nova')
        self.save(add)
        self.cloud.fail_after_write = True
        self.run_items(add)
        self.run_items(check=True)
        self.assertEqual(1, len(self.cloud.writes))
        self.assertEqual([], self.store.load())

    def test_unselected_folder_dependency_blocks_move(self):
        target = str(self.root / 'Pendente')
        mkdir = self.item('create_folder', target, target)
        move = self.item('move', os.path.join(target, 'foto.jpg'))
        self.save(mkdir, move)
        self.run_items(move)
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.exists(self.photo))
        self.run_items(mkdir)
        self.assertTrue(os.path.isdir(target))
        self.assertEqual('waiting_cloud', self.store.operation(mkdir.operation_id)['state'])
        self.run_items(move)
        self.assertTrue(os.path.exists(self.photo))

    def test_conflicting_delete_keeps_other_intentions(self):
        tags, delete = self.item('add_tags', 'Nova'), self.item('delete')
        self.save(tags, delete)
        self.run_items(delete, tags)
        self.assertTrue(os.path.exists(self.photo))
        self.assertEqual(2, len(self.store.load()))
        self.assertEqual([], self.cloud.writes)

    def test_collision_does_not_apply_unapproved_suffix(self):
        rename = self.item('rename', 'occupied.jpg')
        (self.root / 'occupied.jpg').write_bytes(b'other')
        self.save(rename)
        self.run_items(rename)
        self.assertTrue(os.path.exists(self.photo))
        self.assertFalse((self.root / 'occupied_1.jpg').exists())

    def test_cloud_offline_does_not_change_files(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.cloud.offline = True
        self.run_items(rename)
        self.assertTrue(os.path.exists(self.photo))
        self.assertEqual('blocked', self.store.operation(rename.operation_id)['state'])

    def test_cannot_discard_or_restore_over_applied_operation(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.store.autosave()
        self.run_items(rename)
        with self.assertRaises(ValueError):
            self.store.save([])
        with self.assertRaises(ValueError):
            self.store.restore(next(self.store.backup_dir.glob('*.json')))

    def test_unchanged_poll_does_not_grow_journal(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.run_items(rename)
        with self.store.connect() as conn:
            before = conn.execute('SELECT COUNT(*) FROM context_events').fetchone()[0]
        self.run_items(check=True)
        with self.store.connect() as conn:
            after = conn.execute('SELECT COUNT(*) FROM context_events').fetchone()[0]
        self.assertEqual(before, after)

    def test_check_only_never_executes_pending_operations(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.run_items(check=True)
        self.assertTrue(os.path.exists(self.photo))
        self.assertIsNone(self.store.operation(rename.operation_id))

    def test_no_media_open_when_moving(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        with patch('builtins.open', side_effect=AssertionError('No hydration')):
            self.run_items(rename)
        self.assertTrue((self.root / 'nova.jpg').exists())

    def test_parent_move_rebases_child_operations_and_catalog(self):
        child = self.add_file('Album/child.jpg', 'drive-child')
        album = str(self.root / 'Album')
        self.cloud.files['drive-album'] = dict(id='drive-album', path=album, name='Album')
        move = self.item('move', str(self.destination / 'Album'), album)
        tags = self.item('add_tags', 'Nova', child)
        self.save(move, tags)
        self.run_items(move)
        new_child = str(self.destination / 'Album' / 'child.jpg')
        self.assertEqual(new_child, self.rows()[tags.operation_id]['path'])
        self.assertIsNotNone(self.catalog.find(new_child))
        self.run_items(tags)
        self.assertEqual([], self.cloud.writes)

    def test_parent_move_waits_for_active_child_rename(self):
        child = self.add_file('Album/child.jpg', 'drive-child')
        album = str(self.root / 'Album')
        self.cloud.files['drive-album'] = dict(id='drive-album', path=album, name='Album')
        rename = self.item('rename', 'new.jpg', child)
        move = self.item('move', str(self.destination / 'Album'), album)
        self.save(rename, move)
        self.run_items(rename)
        self.run_items(move)
        self.assertTrue(os.path.isdir(album))
        self.assertFalse((self.destination / 'Album').exists())
        self.assertEqual('blocked', self.store.operation(move.operation_id)['state'])

    def test_legacy_unknown_execution_is_never_replayed(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.store.begin_execution(rename.operation_id)
        self.run_items(rename)
        self.assertTrue(os.path.exists(self.photo))

    def test_imported_active_operation_without_journal_cannot_be_replayed(self):
        rename = self.item('rename', 'nova.jpg')
        rename.execution_state = 'waiting_cloud'
        self.save(rename)
        self.run_items(rename)
        self.run_items(rename)
        self.run_items(check=True)
        self.assertTrue(os.path.exists(self.photo))
        self.assertEqual('uncertain', self.store.operation(rename.operation_id)['state'])

    def test_batch_tags_from_virtual_destination_preserve_physical_source(self):
        from src.services.batch_tags import prepare_batch
        move = self.item('move', str(self.destination / 'foto.jpg'))
        selected = dict(file_id=norm(self.photo), name='foto.jpg', path=move.new_value,
                        physical_path=self.photo)
        rows = prepare_batch(self.db, [selected], [move.to_dict()], ['Nova'], set(), lambda: False, lambda *a: None)
        tags = next(StagingItem.from_dict(row) for row in rows if row['action_type'] == 'set_description')
        self.assertEqual(self.photo, tags.path)
        self.save(move, tags)
        self.run_items(tags)
        self.assertEqual([('drive-photo', 'Brasil, Nova')], self.cloud.writes)

    def real_cloud_with_fake_api(self, rows, remote):
        cloud = QueueCloud.__new__(QueueCloud)
        cloud.drive_id = 'bank'
        cloud.roots = [str(self.root)]
        cloud.service = Mock()
        cloud.service.files.return_value.list.return_value.execute.return_value = dict(files=rows)
        cloud.service.drives.return_value.get.return_value.execute.return_value = dict(id='bank', name='Banco')
        cloud.api = Mock()
        cloud.api.get.side_effect = lambda fid: dict(id='bank', name='Drive') if fid == 'bank' else dict(remote)
        return cloud

    def test_drive_folder_uses_shared_drive_name_not_generic_file_root_name(self):
        from src.services.hash_deduplicator import FOLDER_MIME
        dest_folder = dict(id='dest-folder', name='Destino', mimeType=FOLDER_MIME)
        cloud = self.real_cloud_with_fake_api([dest_folder], {})
        self.assertEqual('dest-folder', cloud.folder(str(self.destination)))
        cloud.service.drives.return_value.get.assert_called_with(driveId='bank')
        cloud.api.get.assert_not_called()

        cloud.service.drives.return_value.get.return_value.execute.return_value = dict(id='bank', name='OutroBanco')
        with self.assertRaises(ValueError):
            cloud.folder(str(self.root))

    def test_drive_binding_accepts_normalized_windows_case_without_lossy_matching(self):
        remote = dict(id='drive-photo', name='FOTO.JPG', parents=['bank'], size='10', driveId='bank')
        cloud = self.real_cloud_with_fake_api([remote], remote)
        item = self.item('add_tags', 'Nova', norm(self.photo))
        self.assertEqual('drive-photo', cloud.bind(item, self.catalog)['id'])
        cloud.service.files.return_value.list.return_value.execute.return_value = dict(files=[dict(remote, name='FÓTO.JPG')])
        with self.assertRaises(ValueError):
            cloud.bind(item, self.catalog)

    def test_drive_duplicate_case_variants_block_even_with_saved_id(self):
        remote = dict(id='drive-photo', name='foto.jpg', parents=['bank'], size='10', driveId='bank')
        cloud = self.real_cloud_with_fake_api([remote, dict(remote, id='another', name='FOTO.JPG')], remote)
        with self.assertRaises(ValueError):
            cloud.bind(self.item('add_tags', 'Nova'), self.catalog, 'drive-photo')

    def test_drive_children_paginate_and_reject_incomplete_search(self):
        cloud = self.real_cloud_with_fake_api([], {})
        execute = cloud.service.files.return_value.list.return_value.execute
        execute.side_effect = [dict(files=[dict(id='one', name='FOTO.JPG')], nextPageToken='next'),
                               dict(files=[dict(id='two', name='foto.jpg')])]
        self.assertEqual(2, len(cloud.children('bank', 'foto.jpg')))
        execute.side_effect = [dict(files=[], incompleteSearch=True)]
        with self.assertRaises(ValueError):
            cloud.children('bank', 'foto.jpg')

    def test_explicit_release_verifies_original_and_allows_new_approval(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        with patch('os.rename', side_effect=PermissionError('locked')):
            self.run_items(rename)
        engine = ContextEngine(self.store, self.catalog, self.cloud, self.cfg)
        result = engine.release_unapplied([rename.operation_id])
        self.assertEqual(1, result['released'])
        self.assertTrue(os.path.exists(self.photo))
        self.run_items(rename)
        self.assertTrue((self.root / 'nova.jpg').exists())

    def test_explicit_release_cannot_reset_applied_or_changed_file(self):
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        self.run_items(rename)
        engine = ContextEngine(self.store, self.catalog, self.cloud, self.cfg)
        self.assertEqual(0, engine.release_unapplied([rename.operation_id])['released'])
        self.assertEqual('waiting_cloud', self.store.operation(rename.operation_id)['state'])

    def test_folder_creation_failure_never_recreates_without_explicit_review(self):
        target = str(self.root / 'Pendente')
        mkdir = self.item('create_folder', target, target)
        self.save(mkdir)
        original = os.mkdir
        def lost(path, *args, **kwargs):
            original(path, *args, **kwargs)
            raise TimeoutError('created but no return')
        with patch('os.mkdir', side_effect=lost):
            self.run_items(mkdir)
        with patch('os.mkdir', side_effect=AssertionError('must not repeat')):
            self.run_items(check=True)
        engine = ContextEngine(self.store, self.catalog, self.cloud, self.cfg)
        self.assertEqual(0, engine.release_unapplied([mkdir.operation_id])['released'])

    def test_delete_waits_for_trash_confirmation_not_404(self):
        delete = self.item('delete')
        self.save(delete)
        def fake_recycle(path):
            os.rename(path, str(self.base / 'temporary-trash'))
            return True
        with patch('src.utils.utils.send_to_recycle_bin', side_effect=fake_recycle):
            self.run_items(delete)
        self.assertEqual('waiting_cloud', self.store.operation(delete.operation_id)['state'])
        self.assertIsNone(self.catalog.find(self.photo))
        self.cloud.files['drive-photo']['trashed'] = True
        self.run_items(check=True)
        self.assertEqual([], self.store.load())

    def test_explicit_review_recognizes_interrupted_creation_once_confirmed(self):
        target = str(self.root / 'Pendente')
        mkdir = self.item('create_folder', target, target)
        self.save(mkdir)
        original = os.mkdir
        def lost(path):
            original(path)
            raise TimeoutError('created but no response')
        with patch('os.mkdir', side_effect=lost):
            self.run_items(mkdir)
        self.cloud.files['created-folder'] = dict(id='created-folder', path=target)
        with patch('os.mkdir', side_effect=AssertionError('must not repeat')):
            result = ContextEngine(self.store, self.catalog, self.cloud, self.cfg).release_unapplied([mkdir.operation_id])
        self.assertEqual(1, result['completed'])
        self.assertEqual([], self.store.load())

    def test_explicit_review_recognizes_interrupted_delete_once_trashed(self):
        delete = self.item('delete')
        self.save(delete)
        def lost(path):
            os.rename(path, str(self.base / 'temporary-trash'))
            raise TimeoutError('recycled but no response')
        with patch('src.utils.utils.send_to_recycle_bin', side_effect=lost):
            self.run_items(delete)
        self.cloud.files['drive-photo']['trashed'] = True
        result = ContextEngine(self.store, self.catalog, self.cloud, self.cfg).release_unapplied([delete.operation_id])
        self.assertEqual(1, result['completed'])
        self.assertEqual([], self.store.load())

    def test_worker_uses_private_catalog_and_keeps_ui_responsive(self):
        import time
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QTimer, QEventLoop
        from src.services.contextual_queue import ContextQueueWorker
        app = QApplication.instance() or QApplication([])
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        results, errors, ticks = [], [], []
        bind = self.cloud.bind
        def slow_bind(*args):
            time.sleep(0.15)
            return bind(*args)
        self.cloud.bind = slow_bind
        worker = ContextQueueWorker(self.store.path, self.store.legacy_path, self.db, None, [rename.operation_id])
        worker.result.connect(results.append)
        worker.failed.connect(errors.append)
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        worker.finished.connect(loop.quit)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        with patch('src.utils.config_manager.ConfigManager', return_value=self.cfg), \
                patch('src.services.contextual_queue.QueueCloud', return_value=self.cloud):
            deadline.start(10000)
            timer.start()
            worker.start()
            loop.exec()
            worker.wait(10000)
        timer.stop()
        deadline.stop()
        self.assertFalse(errors)
        self.assertTrue(results)
        self.assertGreater(len(ticks), 2)
        self.assertTrue((self.root / 'nova.jpg').exists())

    def test_dialog_runs_worker_and_keeps_waiting_item_and_report(self):
        import time
        from types import SimpleNamespace
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        from src.ui.staging_queue import StagingQueueDialog
        class Signals(QObject):
            changed = pyqtSignal(int)
        app = QApplication.instance() or QApplication([])
        signals = Signals()
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        queue = SimpleNamespace(items=[rename], store=self.store, busy=False, _autosave_worker=None,
                                queueChanged=signals.changed, can_edit=lambda: True)
        queue.count = lambda: len(queue.items)
        queue.release = lambda: setattr(queue, 'busy', False)
        config = Mock()
        config.is_read_only.return_value = False
        config.is_sandbox.return_value = False
        with patch('src.ui.staging_queue.StagingQueue', return_value=queue), \
             patch('src.ui.staging_queue.ConfigManager', return_value=config), \
             patch('src.utils.config_manager.ConfigManager', return_value=self.cfg), \
             patch('src.services.contextual_queue.QueueCloud', return_value=self.cloud):
            dialog = StagingQueueDialog(db_indexer=SimpleNamespace(db_name=self.db))
            try:
                dialog.show()
                deadline = time.monotonic() + 10
                while dialog._list_loading and time.monotonic() < deadline:
                    QTest.qWait(10)
                dialog._start_context_execution([rename.operation_id])
                self.assertTrue(queue.busy)
                self.assertFalse(dialog.btn_execute.isEnabled())
                while (dialog._execution_active or dialog._list_loading) and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertFalse(dialog._execution_active)
                self.assertFalse(queue.busy)
                self.assertEqual('waiting_cloud', queue.items[0].execution_state)
                self.assertIn('aguardando/conferência: 1', dialog.details_label.text())
            finally:
                if dialog._context_worker:
                    dialog._context_worker.requestInterruption()
                    dialog._context_worker.wait(10000)
                    app.processEvents()
                dialog.close()
                dialog.deleteLater()
                app.processEvents()

    def test_drag_exports_only_queue_payload_not_local_file_urls(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QStandardItemModel, QStandardItem
        from PyQt6.QtWidgets import QApplication
        from src.ui.list_view import FileListView
        from types import SimpleNamespace
        app = QApplication.instance() or QApplication([])
        view = FileListView()
        model = QStandardItemModel(view)
        item = QStandardItem('foto.jpg')
        item.setData(dict(id=norm(self.photo), path=self.photo, source='local'), Qt.ItemDataRole.UserRole)
        model.appendRow(item)
        view.setModel(model)
        view.setCurrentIndex(model.index(0, 0))
        with patch('src.ui.staging_queue.StagingQueue', return_value=SimpleNamespace(items=[])), \
             patch('src.ui.list_view.QDrag') as drag:
            view.startDrag(Qt.DropAction.MoveAction)
            mime = drag.return_value.setMimeData.call_args.args[0]
            self.assertFalse(mime.hasUrls())
            self.assertTrue(mime.hasFormat('application/x-voximago-file-items'))
        view.deleteLater()
        app.processEvents()

    def test_queue_cloud_rate_limit_cancels_promptly_without_blocking_pending_item(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response
        from scripts.test_hash_dedup import Request

        wait_messages = []
        cancelled = {'flag': False}
        attempts = {'count': 0}

        class FakeDrives:
            def get(self, driveId):
                def perform(_):
                    attempts['count'] += 1
                    raise HttpError(
                        Response({'status': 403}),
                        b'{"error":{"errors":[{"reason":"userRateLimitExceeded","message":"User rate limit exceeded."}]}}',
                    )
                return Request(perform)

        class FakeService:
            def drives(self):
                return FakeDrives()

        cfg = Config(self.root)
        cfg.get_current_drive_id = lambda: 'drive-id'
        rename = self.item('rename', 'nova.jpg')
        self.save(rename)
        cloud = QueueCloud(
            FakeService(), cfg,
            cancelled=lambda: cancelled['flag'],
            on_wait=wait_messages.append,
        )
        engine = ContextEngine(self.store, self.catalog, cloud, cfg, lambda: cancelled['flag'])

        def fake_sleep(_):
            cancelled['flag'] = True

        with patch('src.services.drive_retry.time.sleep', side_effect=fake_sleep):
            result = engine.run([rename.operation_id])

        self.assertEqual(1, attempts['count'])
        self.assertTrue(result['cancelled'])
        self.assertEqual([], result['errors'])
        self.assertTrue(any('Google limitou as consultas; nova tentativa em ' in m for m in wait_messages))
        self.assertEqual('pending', self.rows()[rename.operation_id]['execution_state'])


if __name__ == '__main__':
    unittest.main()
