"""Regressões da auditoria: somente mocks, imagens em memória e fixtures temporárias."""
import os
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class MediaGuardTests(unittest.TestCase):
    def test_only_tiff_cache_version_changes_and_rotation_is_retained(self):
        import hashlib
        from src.ui.thumbnails import ThumbnailCache
        for ext in ('.jpg', '.tif', '.tiff'):
            item = dict(source='local', path='image' + ext, modifiedTime=1, size=2, thumbnailRotation=90)
            old = hashlib.sha1(f"image{ext}|1|2|rotation=90".encode()).hexdigest()
            current = ThumbnailCache.get_thumbnail_cache_key(item)
            self.assertEqual(old == current, ext == '.jpg')
            self.assertNotEqual(current, ThumbnailCache.get_thumbnail_cache_key(dict(item, thumbnailRotation=0)))

    def test_placeholder_flags_never_open_media(self):
        from src.utils.exif_utils import extract_exif_summary
        for flag in (0x1000, 0x40000, 0x400000):
            with self.subTest(flag=flag), \
                    patch('src.utils.exif_utils.os.stat', return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=flag)), \
                    patch('src.utils.exif_utils.Image.open') as opened:
                self.assertEqual((None, None), extract_exif_summary(r'C:\online.jpg'))
                opened.assert_not_called()

    def test_excluded_drive_never_even_stats_media(self):
        from src.utils.exif_utils import extract_exif_summary
        with patch('src.utils.exif_utils.os.stat') as stat_call:
            self.assertEqual((None, None), extract_exif_summary(r'O:\photo.jpg'))
        stat_call.assert_not_called()

    def test_numeric_thumbnail_ranges(self):
        from src.ui.thumbnails import _numeric_thumbnail
        cases = [('F', 1.0, 255), ('F', 0.5, 128), ('F', 0, 0),
                 ('I', 255, 255), ('I', 65535, 255), ('I;16', 65535, 255)]
        for mode, value, expected in cases:
            with self.subTest(mode=mode, value=value):
                source = Image.new(mode, (4, 4), value)
                original = source.tobytes()
                result = _numeric_thumbnail(source, 256)
                self.assertEqual((expected,) * 3, result.getpixel((0, 0)))
                self.assertEqual(original, source.tobytes())

    def test_signed_and_nonfinite_numeric_thumbnail(self):
        import numpy as np
        from src.ui.thumbnails import _numeric_thumbnail
        for values in ([-5, 0, 5], [float('nan'), float('inf'), float('-inf')]):
            source = Image.fromarray(np.array([values], dtype=np.float32))
            result = _numeric_thumbnail(source, 256)
            self.assertEqual('RGB', result.mode)
            if values[0] == -5:
                self.assertEqual([(0, 0, 0), (128, 128, 128), (255, 255, 255)], list(result.getdata()))
            else:
                self.assertEqual([(0, 0, 0)] * 3, list(result.getdata()))


class ExecutionPlanTests(unittest.TestCase):
    def test_worker_copies_entire_plan(self):
        from src.services.hash_deduplicator import HashDedupExecuteWorker, DuplicateGroup
        group = DuplicateGroup('abc', 1, {'drive_id': 'keep', 'description': 'old'}, [{'drive_id': 'dup'}])
        worker = HashDedupExecuteWorker('unused.db', [group], r'C:\dest', None)
        group.keeper['description'] = 'new'
        group.duplicates.clear()
        group.edited_description = 'changed'
        self.assertEqual('old', worker.groups[0].keeper['description'])
        self.assertEqual(1, len(worker.groups[0].duplicates))
        self.assertIsNone(worker.groups[0].edited_description)

    def test_busy_inspector_and_mutators_cannot_change_plan(self):
        from scripts.test_hash_dedup import HashDedupDialogTests
        from PyQt6.QtWidgets import QPushButton
        fixture = HashDedupDialogTests()
        fixture.setUpClass()
        with patch('src.ui.details_panel.extract_exif_summary', return_value=(None, None)):
            fixture.setUp()
            try:
                dialog = fixture.dialog
                dialog._start_scan()
                fixture.wait()
                before = dialog.groups[0].keeper['drive_id']
                dialog._set_busy(True)
                buttons = [b for b in dialog.findChildren(QPushButton) if 'Tornar este' in b.text()]
                self.assertTrue(buttons)
                self.assertTrue(all(not b.isEnabled() for b in buttons))
                dialog._promote_duplicate_to_keeper(0, 0)
                dialog._swap_selected_keeper()
                self.assertEqual(before, dialog.groups[0].keeper['drive_id'])
            finally:
                dialog._set_busy(False)
                fixture.tearDown()


class VirtualFolderTests(unittest.TestCase):
    def test_pending_move_selects_virtual_destination_not_physical_origin(self):
        from src.ui.ui import DriveFileGalleryApp
        src, dst = r'C:\acervo\origem\foto.jpg', r'C:\acervo\destino\foto.jpg'
        for overlay in (False, True):
            with self.subTest(overlay=overlay):
                tree = SimpleNamespace(select_and_expand_folder=Mock(return_value=True))
                app = SimpleNamespace(main_bar=SimpleNamespace(staging_queue=SimpleNamespace(items=[
                    SimpleNamespace(file_id=src, path=src, action_type='move', new_value=dst)])), folder_tree=tree)
                with patch('os.path.exists', side_effect=lambda p: p == src), \
                        patch('os.path.isdir', side_effect=lambda p: not p.endswith('.jpg')), \
                        patch('src.services.path_reconciler.resolve_and_heal_local_path') as heal:
                    DriveFileGalleryApp.select_folder_for_file(app, dict(id=src, path=dst if overlay else src,
                        name='foto.jpg', source='local', mimeType='image/jpeg', is_staged_move=overlay))
                heal.assert_not_called()
                tree.select_and_expand_folder.assert_called_once_with(r'C:\acervo\destino')


if __name__ == '__main__':
    unittest.main()
