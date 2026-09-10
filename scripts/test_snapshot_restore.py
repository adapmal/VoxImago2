"""Teste de restauracao automatica e remapeamento entre letras de unidade."""

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
                indexer = FileIndexer(db_path)

            self.assertTrue(indexer.search_index_is_consistent()[0])
            indexer.close()
            preserved = list(Path(temp_dir).glob('corrupt.db.corrupt-*'))
            self.assertEqual(1, len(preserved))
            self.assertEqual(original, preserved[0].read_bytes())

    def test_missing_database_is_restored_and_rebased(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed_db = os.path.join(temp_dir, 'seed.db')
            seed_csv = os.path.join(temp_dir, 'seed.csv')
            base_path = os.path.join(temp_dir, 'file_index_shared_v2.db')
            old_root = r'L:\Drives Compartilhados\Banco de Imagens'
            new_root = r'G:\Shared drives\Image Bank'
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
                patch.object(ConfigManager, 'set') as config_set,
            ):
                restored = FileIndexer(restored_db)

            row = restored.conn.execute(
                "SELECT file_id,path,description FROM files WHERE source='local'"
            ).fetchone()
            self.assertEqual(
                new_root.casefold() + r'\2010\foto.jpg', row[0].casefold()
            )
            self.assertEqual(row[0], row[1])
            self.assertEqual('Brasil, Pará', row[2])
            config_set.assert_called_with(
                'shared_snapshot_generation', 'restore-generation'
            )
            restored.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
