"""Testes do matching conservador Drive/local (sem PyQt6 ou acesso a rede)."""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database.search import SearchEngine
from src.drive.match import (
    build_local_link_index,
    extract_drive_file_id,
    find_local_matches,
    match_drive_to_local,
    select_unique_drive_candidate_by_size,
)
from src.drive.link_validation import ensure_validation_table


class MatchingSecurityTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            """
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                name TEXT,
                path TEXT,
                size INTEGER,
                mimeType TEXT,
                source TEXT,
                name_normalized TEXT,
                webContentLink TEXT,
                parentId TEXT
            )
            """
        )

    def tearDown(self):
        self.connection.close()

    def add_local(self, path, size, link=''):
        name = os.path.basename(path)
        self.cursor.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, 'local', ?, ?, '')",
            (
                path,
                name,
                path,
                size,
                'image/jpeg',
                SearchEngine(None).normalize_text(name),
                link,
            ),
        )

    def drive_file(self, *, name='foto.jpg', size=100, parent_path='Acervo/2026'):
        return {
            'id': 'drive_ABC123',
            'name': name,
            'size': size,
            'mimeType': 'image/jpeg',
            'parent_path': parent_path,
        }

    def test_unique_name_requires_exact_size(self):
        self.add_local(r'C:\Acervo\2026\foto.jpg', 101)
        result = match_drive_to_local(self.drive_file(), self.cursor)
        self.assertFalse(result.matched)
        self.assertEqual(result.reason, 'size_mismatch')

    def test_unique_exact_name_and_size_matches(self):
        path = r'C:\Acervo\2026\foto.jpg'
        self.add_local(path, 100)
        result = match_drive_to_local(self.drive_file(), self.cursor)
        self.assertTrue(result.matched)
        self.assertEqual(result.local_id, path)
        self.assertEqual(result.reason, 'exact_name_size')

    def test_size_disambiguates_same_name(self):
        expected = r'C:\Acervo\2026\foto.jpg'
        self.add_local(expected, 100)
        self.add_local(r'C:\Backup\foto.jpg', 200)
        self.assertEqual(find_local_matches(self.drive_file(), self.cursor), [expected])

    def test_hierarchy_disambiguates_equal_name_and_size(self):
        expected = r'C:\Acervo\2026\Evento\foto.jpg'
        self.add_local(expected, 100)
        self.add_local(r'C:\Backup\Evento\foto.jpg', 100)
        drive = self.drive_file(parent_path='Acervo/2026/Evento')
        result = match_drive_to_local(drive, self.cursor)
        self.assertTrue(result.matched)
        self.assertEqual(result.local_id, expected)
        self.assertEqual(result.hierarchy_score, 3)

    def test_hierarchy_tie_is_rejected(self):
        self.add_local(r'C:\Acervo\A\Evento\foto.jpg', 100)
        self.add_local(r'C:\Acervo\B\Evento\foto.jpg', 100)
        result = match_drive_to_local(
            self.drive_file(parent_path='Evento'), self.cursor
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.status, 'ambiguous')

    def test_normalized_name_needs_hierarchy(self):
        expected = r'C:\Acervo\Brasil\ação.jpg'
        self.add_local(expected, 100)
        drive = self.drive_file(
            name='acao.jpg', size=100, parent_path='Acervo/Brasil'
        )
        result = match_drive_to_local(drive, self.cursor)
        self.assertTrue(result.matched)
        self.assertEqual(result.local_id, expected)
        self.assertEqual(result.reason, 'normalized_name_size_hierarchy')

    def test_duplicate_legacy_link_is_not_selected_arbitrarily(self):
        link = 'https://drive.google.com/file/d/drive_ABC123/view'
        self.add_local(r'C:\Acervo\A\foto.jpg', 100, link)
        self.add_local(r'C:\Acervo\B\foto.jpg', 100, link)
        result = match_drive_to_local(
            self.drive_file(parent_path='Acervo'), self.cursor
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.status, 'ambiguous')

    def test_legacy_link_is_revalidated_by_size(self):
        link = 'https://drive.google.com/file/d/drive_ABC123/view'
        self.add_local(r'C:\Acervo\foto.jpg', 999, link)
        result = match_drive_to_local(self.drive_file(), self.cursor)
        self.assertFalse(result.matched)
        self.assertEqual(result.reason, 'size_mismatch')

    def test_prebuilt_link_index_preserves_safe_decision(self):
        path = r'C:\Acervo\foto.jpg'
        link = 'https://drive.google.com/file/d/drive_ABC123/view'
        self.add_local(path, 100, link)
        link_index = build_local_link_index(self.cursor)
        self.assertEqual(
            find_local_matches(
                self.drive_file(parent_path='Acervo'),
                self.cursor,
                link_index=link_index,
            ),
            [path],
        )

    def test_quarantined_legacy_link_cannot_fall_back_to_name_match(self):
        path = r'C:\Acervo\foto.jpg'
        link = 'https://drive.google.com/file/d/drive_ABC123/view'
        self.add_local(path, 100, link)
        ensure_validation_table(self.cursor)
        self.cursor.execute(
            """
            INSERT INTO drive_link_validation (
                local_file_id, drive_id, status, validated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (path, 'drive_ABC123', 'hash_mismatch', 1),
        )
        result = match_drive_to_local(self.drive_file(), self.cursor)
        self.assertFalse(result.matched)
        self.assertEqual(result.status, 'conflict')
        self.assertEqual(result.reason, 'quarantined_existing_link')

    def test_validated_legacy_link_remains_eligible(self):
        path = r'C:\Acervo\foto.jpg'
        link = 'https://drive.google.com/file/d/drive_ABC123/view'
        self.add_local(path, 100, link)
        ensure_validation_table(self.cursor)
        self.cursor.execute(
            """
            INSERT INTO drive_link_validation (
                local_file_id, drive_id, status, validated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (path, 'drive_ABC123', 'valid_hash', 1),
        )
        result = match_drive_to_local(self.drive_file(), self.cursor)
        self.assertTrue(result.matched)
        self.assertEqual(result.local_id, path)

    def test_candidate_linked_to_other_drive_is_not_reused(self):
        link = 'https://drive.google.com/file/d/outro_ID/view'
        self.add_local(r'C:\Acervo\2026\foto.jpg', 100, link)
        result = match_drive_to_local(self.drive_file(), self.cursor)
        self.assertFalse(result.matched)

    def test_candidates_outside_configured_roots_are_ignored(self):
        expected = r'C:\Acervo\2026\foto.jpg'
        self.add_local(expected, 100)
        self.add_local(r'D:\OutroAcervo\2026\foto.jpg', 100)
        result = match_drive_to_local(
            self.drive_file(),
            self.cursor,
            allowed_roots=[r'C:\Acervo'],
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.local_id, expected)

    def test_drive_id_parser_accepts_known_urls_only(self):
        self.assertEqual(
            extract_drive_file_id('https://drive.google.com/file/d/abc_DEF-123/view'),
            'abc_DEF-123',
        )
        self.assertEqual(
            extract_drive_file_id('https://docs.google.com/document/d/doc_123/edit'),
            'doc_123',
        )
        self.assertEqual(
            extract_drive_file_id('https://drive.google.com/open?id=query_123'),
            'query_123',
        )
        self.assertIsNone(extract_drive_file_id('https://example.com/file/d/abc/view'))

    def test_drive_candidate_selection_requires_unique_exact_size(self):
        candidates = [
            {'id': 'a', 'size': '100'},
            {'id': 'b', 'size': '100'},
            {'id': 'c', 'size': '101'},
        ]
        self.assertIsNone(select_unique_drive_candidate_by_size(candidates, 100))
        self.assertEqual(
            select_unique_drive_candidate_by_size(candidates, 101)['id'], 'c'
        )
        self.assertIsNone(select_unique_drive_candidate_by_size([candidates[0]], 99))


if __name__ == '__main__':
    unittest.main(verbosity=2)
