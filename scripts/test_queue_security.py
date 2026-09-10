import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.staging_schema import StagingItem
from src.utils.path_validation import validate_path_component, validate_path_in_roots


class QueueCompatibilityTests(unittest.TestCase):
    def test_legacy_item_without_schema_version_is_accepted(self):
        legacy = {
            'file_id': r'C:\Acervo\foto.jpg',
            'file_name': 'foto.jpg',
            'path': r'C:\Acervo\foto.jpg',
            'action_type': 'rename',
            'old_value': 'foto.jpg',
            'new_value': 'foto_final.jpg',
            'timestamp': 123.0,
        }
        item = StagingItem.from_dict(legacy)
        self.assertEqual(item.action_type, 'rename')
        self.assertEqual(item.new_value, 'foto_final.jpg')

    def test_historical_action_shapes_are_accepted(self):
        records = [
            {
                'file_id': r'C:\Acervo\foto.jpg',
                'file_name': 'foto.jpg',
                'path': r'C:\Acervo\foto.jpg',
                'action_type': 'delete',
                'old_value': '',
                'new_value': '',
            },
            {
                'file_id': r'C:\Acervo\foto.jpg',
                'file_name': 'foto.jpg',
                'path': r'C:\Acervo\foto.jpg',
                'action_type': 'move',
                'old_value': r'C:\Acervo\foto.jpg',
                'new_value': r'C:\Acervo\2026\foto.jpg',
            },
            {
                'file_id': 'drive-file-id',
                'file_name': 'foto.jpg',
                'path': '',
                'action_type': 'set_description',
                'old_value': 'antiga',
                'new_value': 'nova',
            },
        ]
        self.assertEqual(
            [StagingItem.from_dict(record).action_type for record in records],
            ['delete', 'move', 'set_description'],
        )

    def test_current_output_keeps_legacy_fields(self):
        item = StagingItem('id', 'foto.jpg', '', 'set_description', '', 'tag')
        serialized = item.to_dict()
        for field in ('file_id', 'file_name', 'path', 'action_type', 'old_value', 'new_value', 'timestamp'):
            self.assertIn(field, serialized)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            StagingItem.from_dict({
                'file_id': 'x',
                'action_type': 'execute_program',
            })

    def test_rename_with_parent_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            StagingItem.from_dict({
                'file_id': r'C:\Acervo\foto.jpg',
                'path': r'C:\Acervo\foto.jpg',
                'action_type': 'rename',
                'new_value': r'..\fora.jpg',
            })


class PathValidationTests(unittest.TestCase):
    def test_windows_reserved_name_is_rejected(self):
        valid, _ = validate_path_component('CON.txt')
        self.assertFalse(valid)

    def test_path_must_remain_under_root(self):
        with tempfile.TemporaryDirectory() as temp_root:
            allowed = os.path.join(temp_root, 'acervo')
            inside = os.path.join(allowed, '2026', 'foto.jpg')
            outside = os.path.join(temp_root, 'fora', 'foto.jpg')
            self.assertTrue(validate_path_in_roots(inside, [allowed])[0])
            self.assertFalse(validate_path_in_roots(outside, [allowed])[0])

    def test_configured_root_itself_cannot_be_targeted(self):
        with tempfile.TemporaryDirectory() as temp_root:
            self.assertFalse(validate_path_in_roots(temp_root, [temp_root])[0])


if __name__ == '__main__':
    unittest.main()
