"""Testes offline de snapshot versionado e caminhos portateis."""

from __future__ import annotations

import json
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

from src.database.snapshot import (
    build_root_mapping,
    create_manifest,
    manifest_path,
    publish_generation,
    rebase_local_paths,
    resolve_snapshot_database,
    select_snapshot_database,
)
from src.utils.portable_paths import (
    available_drive_letters,
    discover_shared_folder,
    relocation_candidates,
)
from src.utils.config_manager import ConfigManager


class SnapshotPortabilityTests(unittest.TestCase):
    def test_excluded_drive_root_is_not_probed(self):
        probed = []

        def remember(path):
            probed.append(path)
            return False

        with (
            patch('src.utils.portable_paths.os.name', 'nt'),
            patch(
                'src.utils.portable_paths.os.path.exists',
                side_effect=remember,
            ),
        ):
            available_drive_letters(excluded={'O:'})

        self.assertFalse(any(path.upper().startswith('O:') for path in probed))

    def test_excluded_drive_is_never_a_snapshot_candidate(self):
        config = ConfigManager()

        def fake_get(key, default=None):
            values = {
                'excluded_drive_letters': ['O:'],
                'shared_cache_path': (
                    r'L:\Drives Compartilhados\zRecursos_VoxImago'
                    r'\file_index_shared.db'
                ),
            }
            return values.get(key, default)

        with (
            patch.object(config, 'get', side_effect=fake_get),
            patch(
                'src.utils.config_manager.available_drive_letters',
                return_value=['L:'],
            ) as available,
            patch('src.utils.config_manager.os.path.isfile', return_value=False),
        ):
            candidates = config.get_shared_cache_candidates(2)

        available.assert_called_with(excluded={'O:'})
        self.assertTrue(candidates)
        self.assertFalse(any(path.upper().startswith('O:') for path in candidates))

    def test_drive_letter_candidates_preserve_path_tail(self):
        candidates = relocation_candidates(
            r'L:\Drives Compartilhados\Banco de Imagens',
            drive_letters=['G:'],
        )
        self.assertIn(
            r'G:\Drives Compartilhados\Banco de Imagens', candidates
        )
        self.assertIn(r'G:\Shared drives\Banco de Imagens', candidates)

    def test_discovery_builds_absolute_drive_path(self):
        expected = r'G:\Shared drives\Image Bank'
        with patch(
            'src.utils.portable_paths.os.path.isdir',
            side_effect=lambda path: path == expected,
        ):
            self.assertEqual(
                expected,
                discover_shared_folder(
                    ['Image Bank'], drive_letters=['G:'],
                ),
            )

    def test_discovery_treats_language_aliases_as_same_folder(self):
        portuguese = r'G:\Drives compartilhados\Banco de Imagens'
        english = r'G:\Shared drives\Banco de Imagens'
        with (
            patch(
                'src.utils.portable_paths.os.path.isdir',
                side_effect=lambda path: path in (portuguese, english),
            ),
            patch(
                'src.utils.portable_paths.os.path.samefile',
                side_effect=lambda left, right: {left, right} == {
                    portuguese, english,
                },
            ),
        ):
            self.assertEqual(
                portuguese,
                discover_shared_folder(
                    ['Banco de Imagens'], drive_letters=['G:'],
                ),
            )

    def test_root_mapping_rejects_ambiguous_target(self):
        self.assertEqual(
            [],
            build_root_mapping(
                [r'L:\Drives Compartilhados\Banco de Imagens'],
                [
                    r'G:\Drives Compartilhados\Banco de Imagens',
                    r'H:\Shared drives\Banco de Imagens',
                ],
            ),
        )

    def test_root_mapping_accepts_portuguese_english_alias(self):
        self.assertEqual(
            [
                (
                    r'L:\Drives Compartilhados\Banco de Imagens',
                    r'G:\Shared drives\Image Bank',
                )
            ],
            build_root_mapping(
                [r'L:\Drives Compartilhados\Banco de Imagens'],
                [r'G:\Shared drives\Image Bank'],
            ),
        )

    def test_rebase_updates_files_search_and_validation(self):
        connection = sqlite3.connect(':memory:')
        connection.executescript(
            '''
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, path TEXT, parentId TEXT, source TEXT
            );
            CREATE TABLE search_index (file_id TEXT, source TEXT);
            CREATE TABLE drive_link_validation (
                local_file_id TEXT PRIMARY KEY, drive_id TEXT
            );
            '''
        )
        old_path = r'l:\drives compartilhados\banco de imagens\2010\foto.jpg'
        old_parent = r'l:\drives compartilhados\banco de imagens\2010'
        connection.execute(
            'INSERT INTO files VALUES (?,?,?,?)',
            (old_path, old_path, old_parent, 'local'),
        )
        connection.execute(
            'INSERT INTO search_index VALUES (?,?)', (old_path, 'local')
        )
        connection.execute(
            'INSERT INTO drive_link_validation VALUES (?,?)',
            (old_path, 'drive-id'),
        )
        connection.commit()

        changed = rebase_local_paths(
            connection,
            r'L:\Drives Compartilhados\Banco de Imagens',
            r'G:\Drives Compartilhados\Banco de Imagens',
        )
        self.assertEqual(1, changed)
        expected = r'g:\drives compartilhados\banco de imagens\2010\foto.jpg'
        self.assertEqual(
            expected, connection.execute('SELECT file_id FROM files').fetchone()[0]
        )
        self.assertEqual(
            expected,
            connection.execute('SELECT file_id FROM search_index').fetchone()[0],
        )
        self.assertEqual(
            expected,
            connection.execute(
                'SELECT local_file_id FROM drive_link_validation'
            ).fetchone()[0],
        )
        connection.close()

    def test_manifest_points_only_to_verified_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'local.db')
            csv_path = os.path.join(temp_dir, 'local.csv')
            base_path = os.path.join(temp_dir, 'file_index_shared_v2.db')
            connection = sqlite3.connect(db_path)
            connection.executescript(
                '''
                CREATE TABLE files (source TEXT);
                CREATE TABLE drive_link_validation (local_file_id TEXT);
                INSERT INTO files VALUES ('local');
                '''
            )
            connection.commit()
            Path(csv_path).write_text('source\nlocal\n', encoding='utf-8')
            manifest = create_manifest(
                connection, db_path, csv_path,
                scan_roots=[r'L:\Drives Compartilhados\Banco de Imagens'],
                generation='test-generation',
            )
            _db, _csv, pointer = publish_generation(
                base_path, db_path, csv_path, manifest
            )
            self.assertTrue(os.path.isfile(pointer))
            resolved, loaded = resolve_snapshot_database(base_path)
            self.assertTrue(os.path.isfile(resolved))
            self.assertEqual('test-generation', loaded['generation'])
            with open(resolved, 'ab') as stream:
                stream.write(b'corruption')
            self.assertEqual((None, loaded), resolve_snapshot_database(base_path))
            connection.close()

    def test_publish_refuses_active_lock_from_another_machine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'local.db')
            csv_path = os.path.join(temp_dir, 'local.csv')
            base_path = os.path.join(temp_dir, 'file_index_shared_v2.db')
            connection = sqlite3.connect(db_path)
            connection.execute('CREATE TABLE files (source TEXT)')
            connection.commit()
            Path(csv_path).write_text('source\n', encoding='utf-8')
            manifest = create_manifest(
                connection, db_path, csv_path,
                scan_roots=[], generation='locked-generation',
            )
            lock = manifest_path(base_path) + '.lock'
            Path(lock).write_text(
                json.dumps({'token': 'another-writer'}), encoding='utf-8'
            )
            with self.assertRaisesRegex(RuntimeError, 'Outro computador'):
                publish_generation(base_path, db_path, csv_path, manifest)
            self.assertFalse(os.path.isfile(manifest_path(base_path)))
            self.assertTrue(os.path.isfile(lock))
            connection.close()

    def test_stale_machine_cannot_replace_newer_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'local.db')
            csv_path = os.path.join(temp_dir, 'local.csv')
            base_path = os.path.join(temp_dir, 'file_index_shared_v2.db')
            connection = sqlite3.connect(db_path)
            connection.execute('CREATE TABLE files (source TEXT)')
            connection.commit()
            Path(csv_path).write_text('source\n', encoding='utf-8')
            first = create_manifest(
                connection, db_path, csv_path,
                scan_roots=[], generation='generation-1',
            )
            publish_generation(base_path, db_path, csv_path, first)
            second = dict(first, generation='generation-2')

            with self.assertRaisesRegex(RuntimeError, 'mudou'):
                publish_generation(
                    base_path, db_path, csv_path, second,
                    expected_previous_generation='outdated-generation',
                )
            self.assertEqual(
                'generation-1',
                resolve_snapshot_database(base_path)[1]['generation'],
            )
            connection.close()

    def test_selection_rejects_divergent_versioned_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = sqlite3.connect(':memory:')
            connection.execute('CREATE TABLE files (source TEXT)')
            connection.commit()
            bases = []
            for suffix in ('a', 'b'):
                folder = os.path.join(temp_dir, suffix)
                os.makedirs(folder)
                db_path = os.path.join(folder, 'local.db')
                csv_path = os.path.join(folder, 'local.csv')
                backup = sqlite3.connect(db_path)
                connection.backup(backup)
                backup.close()
                Path(csv_path).write_text('source\n', encoding='utf-8')
                base_path = os.path.join(folder, 'file_index_shared_v2.db')
                manifest = create_manifest(
                    connection, db_path, csv_path,
                    scan_roots=[], generation=f'generation-{suffix}',
                )
                publish_generation(base_path, db_path, csv_path, manifest)
                bases.append(base_path)

            selected, manifest, reason = select_snapshot_database(bases)
            self.assertIsNone(selected)
            self.assertIsNone(manifest)
            self.assertIn('divergentes', reason)
            connection.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
