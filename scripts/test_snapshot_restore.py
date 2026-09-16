"""Teste de restauração exclusivamente manual e remapeamento de caminhos."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.database import FileIndexer
from src.database.snapshot import create_manifest, publish_generation
from src.utils.config_manager import ConfigManager
from src.services.snapshot_management import inspect_snapshot, prepare_adoption, apply_pending_adoption


class SnapshotRestoreTests(unittest.TestCase):
    def test_corrupt_database_is_preserved_when_snapshot_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'corrupt.db')
            original = b'not-a-sqlite-database'
            Path(db_path).write_bytes(original)
            with (
                patch.object(ConfigManager, 'is_sandbox', return_value=False),
                patch.object(
                    ConfigManager,
                    'get_shared_cache_candidates',
                    return_value=[],
                ),
            ):
                with self.assertRaises(RuntimeError):
                    FileIndexer(db_path)
            self.assertEqual(original, Path(db_path).read_bytes())

    def test_missing_database_requires_manual_adoption_and_rebase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed_db = os.path.join(temp_dir, 'seed.db')
            seed_csv = os.path.join(temp_dir, 'seed.csv')
            base_path = os.path.join(temp_dir, 'file_index_shared_v2.db')
            old_root = r'L:\Drives Compartilhados\Banco de Imagens'
            new_root = os.path.join(temp_dir, 'Image Bank')
            os.mkdir(new_root)
            old_path = old_root + r'\2010\foto.jpg'

            with patch.object(ConfigManager, 'is_sandbox', return_value=True):
                seed = FileIndexer(seed_db)
            seed.save_files_in_batch(
                [{
                    'id': old_path,
                    'path': old_path,
                    'name': 'foto.jpg',
                    'source': 'local',
                    'mimeType': 'image/jpeg',
                    'description': 'Brasil, Pará',
                    'size': 100,
                    'modifiedTime': 1,
                    'createdTime': 1,
                    'parentId': old_root + r'\2010',
                }],
                source='local',
            )
            seed.close()
            Path(seed_csv).write_text('file_id\n', encoding='utf-8')
            connection = sqlite3.connect(seed_db)
            manifest = create_manifest(
                connection,
                seed_db,
                seed_csv,
                scan_roots=[old_root],
                generation='restore-generation',
            )
            connection.close()
            publish_generation(base_path, seed_db, seed_csv, manifest)

            restored_db = os.path.join(temp_dir, 'restored.db')
            empty = FileIndexer(restored_db)
            self.assertEqual(0, empty.conn.execute('SELECT COUNT(*) FROM files').fetchone()[0])
            empty.close()
            with (
                patch.object(ConfigManager, 'is_sandbox', return_value=False),
                patch.object(
                    ConfigManager,
                    'get_shared_cache_candidates',
                    return_value=[base_path],
                ),
                patch.object(
                    ConfigManager,
                    'get_resolved_scan_paths',
                    return_value=[new_root],
                ),
                patch('src.services.snapshot_management.persist_adoption_config') as config_set,
            ):
                info = inspect_snapshot(base_path)
                prepare_adoption(restored_db, info, [(old_root, new_root)])
                backup = apply_pending_adoption(restored_db)
                self.assertTrue(os.path.isfile(backup))
                restored = FileIndexer(restored_db)

            row = restored.conn.execute(
                "SELECT file_id,path,description FROM files WHERE source='local'"
            ).fetchone()
            self.assertEqual(
                new_root.casefold() + r'\2010\foto.jpg', row[0].casefold()
            )
            self.assertEqual(row[0], row[1])
            self.assertEqual('Brasil, Pará', row[2])
            self.assertEqual('restore-generation', config_set.call_args.args[0]['generation'])
            restored.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
