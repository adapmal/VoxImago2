"""Isolated queue crash/recovery and large-batch regression tests."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QStatusBar
from src.utils.queue_store import QueueStore, atomic_json
from src.utils.staging_schema import StagingItem
from src.services.batch_tags import prepare_batch, BatchTagsWorker


def record(fid, value='anterior'):
    return StagingItem(str(fid), f'{fid}.jpg', f'X:/online/{fid}.jpg', 'set_description', '', value).to_dict()


class QueueDurabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.legacy = self.root / 'legacy.json'
        self.store = QueueStore(self.root / 'staging.db', self.legacy)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_preserves_legacy_and_restarts_from_sqlite(self):
        old = record(1)
        old.pop('operation_id')
        atomic_json(self.legacy, [old])
        before = self.legacy.read_bytes()
        records = self.store.initialize()
        self.assertEqual(1, len(records))
        self.store.save(records + [record(2)])
        self.assertEqual(before, self.legacy.read_bytes())
        self.assertEqual(2, len(QueueStore(self.store.path, self.legacy).initialize()))

    def test_invalid_legacy_never_becomes_initialized_empty_queue(self):
        atomic_json(self.legacy, {'unexpected': []})
        for _ in range(2):
            with self.assertRaises(KeyError):
                self.store.initialize()

    def test_invalid_batch_leaves_old_queue_untouched(self):
        self.store.initialize()
        original = [record(1)]
        self.store.save(original)
        invalid = record(2)
        invalid['action_type'] = 'execute_command'
        with self.assertRaises(ValueError):
            self.store.save(original + [invalid])
        self.assertEqual(original, self.store.load())

    def test_disk_failure_rolls_back_entire_transaction(self):
        self.store.initialize()
        original = [record(1)]
        self.store.save(original)
        with self.store.connect() as connection:
            connection.execute("CREATE TRIGGER fail_write BEFORE INSERT ON items WHEN NEW.payload LIKE '%failme%' BEGIN SELECT RAISE(ABORT,'simulated write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save([record(2), record(3, 'failme')])
        self.assertEqual(original, self.store.load())

    def test_exactly_three_backups_and_no_rotation_without_changes(self):
        self.store.initialize()
        for i in range(5):
            self.store.save([record(i)])
            self.assertTrue(self.store.autosave())
        backups = list(self.store.backup_dir.glob('*.json'))
        self.assertEqual(3, len(backups))
        names = {json.loads(p.read_text(encoding='utf-8'))['items'][0]['file_id'] for p in backups}
        self.assertEqual({'2', '3', '4'}, names)
        self.assertFalse(self.store.autosave())

    def test_failed_atomic_backup_preserves_previous_slot(self):
        target = self.root / 'copy.json'
        atomic_json(target, [record(1)])
        before = target.read_bytes()
        with patch('src.utils.queue_store.os.replace', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):
                atomic_json(target, [record(2)])
        self.assertEqual(before, target.read_bytes())

    def test_completed_and_uncertain_operations_cannot_be_replayed(self):
        self.store.initialize()
        records = [record(1), record(2)]
        self.store.save(records)
        self.store.autosave()
        self.store.begin_execution(records[0]['operation_id'])
        self.store.complete_execution(records[0]['operation_id'])
        self.store.begin_execution(records[1]['operation_id'])
        recovered = self.store.recovery_records(next(self.store.backup_dir.glob('*.json')))
        self.assertEqual([records[1]], recovered)
        with self.assertRaises(ValueError):
            self.store.begin_execution(records[1]['operation_id'])

    def test_other_instance_cannot_silently_overwrite_queue(self):
        self.store.initialize()
        other = QueueStore(self.store.path, self.legacy)
        other.initialize()
        self.store.save([record(1)])
        with self.assertRaisesRegex(ValueError, 'outra instância'):
            other.save([record(2)])
        self.assertEqual('1', self.store.load()[0]['file_id'])

    def test_corrupt_database_recovery_preserves_original_and_blocks_replay(self):
        self.store.initialize()
        self.store.save([record(1)])
        self.store.autosave()
        # Test fixture, never the application catalogue/queue.
        self.store.path.write_bytes(b'broken database')
        recovered = self.store.restore(next(self.store.backup_dir.glob('*.json')))
        self.assertEqual(1, len(recovered))
        self.assertEqual(b'broken database', next(self.root.glob('*.damaged-*')).read_bytes())
        with self.assertRaises(ValueError):
            self.store.begin_execution(recovered[0]['operation_id'])

    def test_restoration_uses_selected_copy_even_if_rotation_replaces_slot(self):
        self.store.initialize()
        original = [record(1)]
        self.store.save(original)
        self.store.autosave()
        slot = next(self.store.backup_dir.glob('*.json'))
        atomic_json(slot, {'items': [record(2)]})
        self.store.restore(slot, records=original)
        self.assertEqual(original, self.store.load())

    def test_autosave_is_deferred_during_queue_work(self):
        from src.ui.staging_queue import StagingQueue
        queue = SimpleNamespace(busy=True, load_error=None, _autosave_worker=None, _autosave_pending=False)
        with patch('src.ui.staging_queue.QueueAutosaveWorker') as worker:
            StagingQueue.request_autosave(queue)
            worker.assert_not_called()
        self.assertTrue(queue._autosave_pending)

    def create_catalog(self, count):
        path = self.root / 'catalog.db'
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE files(file_id TEXT PRIMARY KEY, description TEXT)')
            conn.executemany('INSERT INTO files VALUES (?,?)', [(str(i), 'Brasil, antiga') for i in range(count)])
        conn.close()
        return path, [{'file_id': str(i), 'name': f'{i}.jpg', 'path': f'X:/online/{i}.jpg'} for i in range(count)]

    def test_3000_online_files_preserve_previous_tags_and_cancel(self):
        path, files = self.create_catalog(3000)
        self.store.initialize()
        original = [record(0, 'Brasil, particular'), record('unrelated', 'manter')]
        self.store.save(original)
        progress = []
        with patch('os.path.isfile', side_effect=AssertionError('Must not inspect media')):
            result = prepare_batch(path, files, original, ['Nova'], {'antiga'}, lambda: False, lambda a,b: progress.append((a,b)))
        self.assertEqual(3001, len(result))
        by_id = {r['file_id']: r for r in result}
        self.assertEqual('Brasil, particular, Nova', by_id['0']['new_value'])
        self.assertEqual('Brasil, Nova', by_id['2999']['new_value'])
        self.assertEqual('manter', by_id['unrelated']['new_value'])
        self.assertEqual((3000, 3000), progress[-1])
        self.assertIsNone(prepare_batch(path, files, original, ['Nova'], set(), lambda: True, lambda *a: None))
        self.assertEqual(original, self.store.load())

    def test_more_than_old_10mb_limit_can_be_reopened(self):
        self.store.initialize()
        records = [record(i, 'x' * 4000) for i in range(3000)]
        self.store.save(records)
        self.assertEqual(3000, len(self.store.load()))

    def test_worker_keeps_event_loop_alive_and_commits_once(self):
        app = QApplication.instance() or QApplication([])
        self.store.initialize()
        path, files = self.create_catalog(10000)
        worker = BatchTagsWorker(str(path), files, [], ['Nova'], set(), self.store)
        loop = QEventLoop()
        ticks, results, failures = [], [], []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        worker.succeeded.connect(results.append)
        worker.failed.connect(failures.append)
        worker.finished.connect(loop.quit)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(30000)
        timer.start()
        with patch.object(self.store, 'save', wraps=self.store.save) as save:
            worker.start()
            loop.exec()
            worker.wait(30000)
            self.assertEqual(1, save.call_count)
        timer.stop()
        deadline.stop()
        self.assertFalse(failures)
        self.assertEqual(1, len(results))
        self.assertGreater(len(ticks), 2)
        self.assertEqual(10000, len(self.store.load()))

    def test_details_panel_applies_large_batch_with_one_notification(self):
        from src.ui.details_panel import FileDetailsPanel
        from src.ui.staging_queue import StagingQueue
        app = QApplication.instance() or QApplication([])
        path, files = self.create_catalog(3000)
        previous_cwd, previous_queue = os.getcwd(), StagingQueue._instance
        window, queue, panel = None, None, None
        os.chdir(self.root)
        StagingQueue._instance = None
        try:
            window = QMainWindow()
            window.status_bar = QStatusBar(window)
            window.setStatusBar(window.status_bar)
            window.indexer = SimpleNamespace(db_name=str(path))
            window.begin_maintenance_operation = lambda: True
            window.end_maintenance_operation = lambda: None
            panel = FileDetailsPanel(window)
            queue = panel.staging_queue
            notifications = []
            queue.queueChanged.connect(notifications.append)
            with patch('os.path.isfile', side_effect=AssertionError('Online media must not be probed')):
                panel.update_details_batch(files)
            panel._start_batch_tags(['Nova'], set())
            worker = panel._batch_worker
            loop = QEventLoop()
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            deadline.start(30000)
            worker.finished.connect(loop.quit)
            loop.exec()
            deadline.stop()
            self.assertIsNone(panel._batch_worker)
            self.assertFalse(queue.busy)
            self.assertEqual([3000], notifications)
            self.assertEqual(3000, len(queue.store.load()))
        finally:
            if panel and panel._batch_worker and panel._batch_worker.isRunning():
                panel._batch_worker.requestInterruption()
                panel._batch_worker.wait(30000)
            if queue:
                queue.timer.stop()
            if window:
                window.close()
            StagingQueue._instance = previous_queue
            os.chdir(previous_cwd)


if __name__ == '__main__':
    unittest.main()
