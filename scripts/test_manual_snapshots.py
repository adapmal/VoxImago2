"""Regressões de adoção, backups e isolamento do acervo real."""
import os
import sys
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.database.database import FileIndexer
from src.database.snapshot import sha256_file
from src.services.snapshot_management import (
    inspect_snapshot, prepare_adoption, apply_pending_adoption, local_backup,
    backup_directory, pending_path, cancel_adoption, check_path,
)
from src.utils.config_manager import ConfigManager
from src.utils.queue_store import QueueStore
from src.utils.staging_schema import StagingItem


class ManualSnapshotsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'data' / 'file_index.db'
        self.db.parent.mkdir()
        self.source = self.root / 'snapshot.db'
        for path in (self.db, self.source):
            indexer = FileIndexer(str(path))
            indexer.close()
        self.config_patch = patch('src.services.snapshot_management.persist_adoption_config')
        self.config_set = self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.tmp.cleanup()

    def prepare(self):
        prepare_adoption(self.db, inspect_snapshot(self.source), [])

    def test_no_automatic_shared_probe(self):
        with patch.object(ConfigManager, 'get_shared_cache_candidates', side_effect=AssertionError('cloud probe')):
            indexer = FileIndexer(str(self.root / 'new.db'))
            self.assertEqual(0, indexer.conn.execute('SELECT COUNT(*) FROM files').fetchone()[0])
            indexer.close()

    def test_preparation_and_cancel_do_not_replace_catalog(self):
        original = sha256_file(self.db)
        self.prepare()
        self.assertEqual(original, sha256_file(self.db))
        cancel_adoption(self.db)
        self.assertFalse(pending_path(self.db).exists())
        self.assertEqual(original, sha256_file(self.db))

    def test_tampered_preparation_rejected(self):
        self.prepare()
        original = sha256_file(self.db)
        stage = next(self.db.parent.glob('*.adopt-*.db'))
        with stage.open('ab') as stream:
            stream.write(b'tampered')
        with self.assertRaises(ValueError):
            apply_pending_adoption(self.db)
        self.assertEqual(original, sha256_file(self.db))

    def test_backup_failure_preserves_original(self):
        self.prepare()
        original = sha256_file(self.db)
        with patch('src.services.snapshot_management.local_backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                apply_pending_adoption(self.db)
        self.assertEqual(original, sha256_file(self.db))
        self.assertTrue(pending_path(self.db).exists())

    def test_new_queue_blocks_prepared_adoption(self):
        self.prepare()
        original = sha256_file(self.db)
        store = QueueStore(self.db.parent / 'staging_queue.db', self.root / 'absent.json')
        store.initialize()
        store.save([StagingItem('1', 'foto.jpg', 'L:/foto.jpg', 'set_description', '', 'teste').to_dict()])
        with self.assertRaises(ValueError):
            apply_pending_adoption(self.db)
        self.assertEqual(original, sha256_file(self.db))

    def test_corrupt_original_is_archived_only_on_explicit_adoption(self):
        self.db.write_bytes(b'corrupt original')
        self.prepare()
        backup = apply_pending_adoption(self.db)
        self.assertEqual(b'corrupt original', (Path(backup) / self.db.name).read_bytes())
        self.config_set.assert_called_once()

    def test_daily_retention_keeps_three_and_preserves_restore_backups(self):
        protected = local_backup(self.db)
        local_backup(self.db, automatic=True)
        self.assertIsNone(local_backup(self.db, automatic=True))
        for day in range(4):
            for path in backup_directory(self.db).glob('auto-*.db'):
                os.utime(path, (time.time() - 90000 * (day + 1),) * 2)
            local_backup(self.db, automatic=True)
        self.assertEqual(3, len(list(backup_directory(self.db).glob('auto-*.db'))))
        self.assertTrue(Path(protected).exists())

    def test_backup_includes_wal_transactions(self):
        conn = sqlite3.connect(self.db)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE test_wal (value TEXT)')
        conn.execute("INSERT INTO test_wal VALUES ('committed')")
        conn.commit()
        try:
            path = local_backup(self.db)
        finally:
            conn.close()
        copied = sqlite3.connect(path)
        try:
            self.assertEqual('committed', copied.execute('SELECT value FROM test_wal').fetchone()[0])
        finally:
            copied.close()

    def test_excluded_drive_rejected_before_probe(self):
        with patch('src.services.snapshot_management.Path.resolve', side_effect=AssertionError('probe')):
            with self.assertRaises(ValueError):
                check_path(r'O:\snapshot.db')


if __name__ == '__main__':
    unittest.main()
