"""Testes offline do auditor de vinculos Drive/local."""

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.revalidate_drive_links import (
    HashBudget,
    apply_results,
    base_audit_rows,
    build_local_candidate_index,
    read_local_links,
    remote_parent_paths,
    resolve_suspects,
    stale_drive_cache_ids,
)


class LinkRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.wrong = root / 'Antiga' / 'foto.jpg'
        self.correct = root / 'Evento' / 'foto.jpg'
        self.wrong.parent.mkdir(parents=True)
        self.correct.parent.mkdir(parents=True)
        self.wrong.write_bytes(b'conteudo-incorreto')
        self.correct.write_bytes(b'conteudo-correto--')
        self.assertEqual(self.wrong.stat().st_size, self.correct.stat().st_size)

        self.connection = sqlite3.connect(':memory:')
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            """
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, name TEXT, path TEXT, size INTEGER,
                source TEXT, webContentLink TEXT
            )
            """
        )
        self.cursor.execute(
            """
            CREATE TABLE search_index (
                file_id TEXT, source TEXT
            )
            """
        )
        link = 'https://drive.google.com/file/d/drive_123/view'
        self.cursor.executemany(
            "INSERT INTO files VALUES (?, 'foto.jpg', ?, ?, 'local', ?)",
            [
                (str(self.wrong), str(self.wrong), self.wrong.stat().st_size, link),
                (str(self.correct), str(self.correct), self.correct.stat().st_size, ''),
            ],
        )
        self.cursor.execute(
            "INSERT INTO files VALUES ('stale_drive', 'velho.jpg', NULL, 10, 'drive', '')"
        )
        self.cursor.execute(
            "INSERT INTO search_index VALUES ('stale_drive', 'drive')"
        )

        digest = hashlib.md5(self.correct.read_bytes()).hexdigest()
        self.items = [
            {'id': 'folder_1', 'name': 'Evento', 'mimeType': 'application/vnd.google-apps.folder'},
            {
                'id': 'drive_123', 'name': 'foto.jpg',
                'size': str(self.correct.stat().st_size),
                'md5Checksum': digest, 'mimeType': 'image/jpeg',
                'parents': ['folder_1'],
                'webViewLink': 'https://drive.google.com/file/d/drive_123/view',
            },
        ]

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_unique_hash_repairs_wrong_legacy_link_and_prunes_cache(self):
        links, malformed = read_local_links(self.cursor)
        remote = {item['id']: item for item in self.items}
        resolver = remote_parent_paths(self.items)
        rows, suspects, grouped = base_audit_rows(
            links, malformed, remote, resolver
        )
        self.assertEqual(suspects, {'drive_123'})

        candidates = build_local_candidate_index(self.cursor)
        budget = HashBudget(1024 * 1024, 1024 * 1024)
        rows, relinks = resolve_suspects(
            rows, suspects, grouped, remote, candidates, budget
        )
        self.assertEqual(relinks['drive_123'].file_id, str(self.correct))

        stale = stale_drive_cache_ids(self.cursor, set(remote))
        self.assertEqual(stale, ['stale_drive'])
        apply_results(
            self.connection, rows, relinks, remote, stale, prune_stale=True
        )

        wrong_link = self.cursor.execute(
            'SELECT webContentLink FROM files WHERE file_id=?', (str(self.wrong),)
        ).fetchone()[0]
        correct_link = self.cursor.execute(
            'SELECT webContentLink FROM files WHERE file_id=?', (str(self.correct),)
        ).fetchone()[0]
        self.assertIsNone(wrong_link)
        self.assertIn('drive_123', correct_link)
        self.assertIsNone(self.cursor.execute(
            "SELECT 1 FROM files WHERE file_id='stale_drive'"
        ).fetchone())
        validation = self.cursor.execute(
            "SELECT drive_id,status FROM drive_link_validation WHERE local_file_id=?",
            (str(self.correct),),
        ).fetchone()
        self.assertEqual(validation, ('drive_123', 'valid_hash_relinked'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
