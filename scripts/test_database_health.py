"""Testes offline do diagnostico e do reparo determinista do banco."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import database_health
from src.database.database import FileIndexer
from src.utils.config_manager import ConfigManager


class DatabaseHealthTests(unittest.TestCase):
    def test_full_check_never_probes_excluded_drive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'excluded.db')
            with patch.object(ConfigManager, 'is_sandbox', return_value=True):
                indexer = FileIndexer(db_path)
            excluded_path = r'O:\Banco de Imagens\foto.jpg'
            indexer.save_files_in_batch(
                [{
                    'id': excluded_path,
                    'path': excluded_path,
                    'name': 'foto.jpg',
                    'source': 'local',
                    'mimeType': 'image/jpeg',
                    'size': 4,
                    'modifiedTime': 1,
                    'createdTime': 1,
                    'parentId': r'O:\Banco de Imagens',
                }],
                source='local',
            )
            indexer.close()

            real_exists = os.path.exists

            def guarded_exists(path):
                if str(path).upper().startswith('O:'):
                    raise AssertionError('A unidade excluida foi consultada.')
                return real_exists(path)

            with (
                patch.object(database_health, 'PROJECT_ROOT', Path(temp_dir)),
                patch.object(
                    ConfigManager, 'get_resolved_scan_paths',
                    return_value=[temp_dir],
                ),
                patch.object(
                    ConfigManager, 'get_excluded_drive_letters',
                    return_value={'O:'},
                ),
                patch.object(
                    ConfigManager, 'get_shared_cache_candidates',
                    return_value=[],
                ),
                patch.object(
                    database_health.os.path, 'exists',
                    side_effect=guarded_exists,
                ),
            ):
                report = database_health.diagnose(db_path, full=True)

            disk_check = next(
                item for item in report
                if item['check'] == 'arquivos_no_disco'
            )
            self.assertEqual(0, disk_check['checked'])
            self.assertEqual(1, disk_check['excluded'])

    def test_inconsistent_search_index_is_backed_up_and_repaired(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = os.path.join(temp_dir, 'foto.jpg')
            Path(media_path).write_bytes(b'test')
            db_path = os.path.join(temp_dir, 'health.db')
            with patch.object(ConfigManager, 'is_sandbox', return_value=True):
                indexer = FileIndexer(db_path)
            indexer.save_files_in_batch(
                [{
                    'id': media_path,
                    'path': media_path,
                    'name': 'foto.jpg',
                    'source': 'local',
                    'mimeType': 'image/jpeg',
                    'size': 4,
                    'modifiedTime': 1,
                    'createdTime': 1,
                    'parentId': temp_dir,
                }],
                source='local',
            )
            indexer.conn.execute('DELETE FROM search_index')
            indexer.conn.commit()
            self.assertFalse(indexer.search_index_is_consistent()[0])
            indexer.close()

            with (
                patch.object(database_health, 'PROJECT_ROOT', Path(temp_dir)),
                patch.object(
                    ConfigManager, 'get_resolved_scan_paths',
                    return_value=[temp_dir],
                ),
                patch.object(
                    ConfigManager, 'get_shared_cache_candidates',
                    return_value=[],
                ),
            ):
                report = database_health.diagnose(
                    db_path, full=True, repair_indexes=True
                )

            self.assertTrue(any(
                item['check'] == 'reparo_indice'
                and item['severity'] == 'ok'
                for item in report
            ))
            repaired = FileIndexer(db_path)
            self.assertTrue(repaired.search_index_is_consistent()[0])
            self.assertEqual(
                repaired.conn.execute('SELECT COUNT(*) FROM files').fetchone(),
                repaired.conn.execute(
                    'SELECT COUNT(*) FROM search_index'
                ).fetchone(),
            )
            repaired.close()
            backups = list(
                (Path(temp_dir) / 'config' / 'backups' / 'database_health')
                .glob('*.db')
            )
            self.assertEqual(1, len(backups))


if __name__ == '__main__':
    unittest.main(verbosity=2)
