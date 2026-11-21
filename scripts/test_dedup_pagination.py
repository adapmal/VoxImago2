"""Regressão de memória limitada por grupo e seleção híbrida mouse/teclado."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from src.ui.hash_dedup_dialog import HashDedupDialog, _InstanceRowButton
from src.services.hash_deduplicator import DuplicateGroup
from src.ui.staging_queue import StagingQueue


class PaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_queue = StagingQueue._instance
        os.chdir(self.tmp.name)
        StagingQueue._instance = None
        self.cache = patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path', return_value='')
        self.cache.start()
        self.details = patch.object(HashDedupDialog, '_refresh_details_inspector')
        self.details.start()
        self.dialog = HashDedupDialog(indexer=SimpleNamespace(db_name='unused.db'))

    def tearDown(self):
        QTest.keyRelease(self.dialog.table, Qt.Key.Key_Shift)
        QTest.keyRelease(self.dialog.table, Qt.Key.Key_Control)
        self.dialog.details_panel.staging_queue.timer.stop()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.cache.stop()
        self.details.stop()
        StagingQueue._instance = self.old_queue
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def item(self, i):
        return dict(drive_id=str(i), name=f'{i}.jpg', path=f'X:\\unused\\{i}.jpg',
                    description='', mimeType='image/jpeg')

    def populate(self, groups):
        d = self.dialog
        d.groups = groups
        d._populate_table()
        while d._render_timer.isActive():
            self.app.processEvents()
        d.show()
        d.resize(1500, 1000)
        self.app.processEvents()

    def test_four_thousand_copies_have_twelve_cards_and_bounded_cache_reads(self):
        group = DuplicateGroup('a'*32, 100, self.item(0), [self.item(i) for i in range(1, 4001)])
        self.populate([group])
        d = self.dialog
        self.assertEqual(12, len(d.table.cellWidget(0, 3).findChildren(_InstanceRowButton)))
        d._navigate_to_instance(0, 'duplicate', 3999)
        cards = d.table.cellWidget(0, 3).findChildren(_InstanceRowButton)
        self.assertLessEqual(len(cards), 12)
        self.assertEqual(3999, cards[-1].property('duplicateIndex'))
        d._update_row_highlights(0)
        self.assertEqual('true', cards[-1].property('active'))
        with patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path', return_value='') as cache:
            d._find_group_cached_thumbnail(group, preferred=group.duplicates[-1])
            self.assertLessEqual(cache.call_count, 14)

    def test_page_buttons_preserve_all_members_and_check_state(self):
        group = DuplicateGroup('a'*32, 100, self.item(0), [self.item(i) for i in range(1, 31)], checked=False)
        self.populate([group])
        d = self.dialog
        d._show_copy_page(0, 12)
        cards = d.table.cellWidget(0, 3).findChildren(_InstanceRowButton)
        self.assertEqual(list(range(12, 24)), [c.property('duplicateIndex') for c in cards])
        self.assertEqual(30, len(group.duplicates))
        self.assertFalse(group.checked)
        d._show_copy_page(0, 0)
        self.assertEqual(0, d._active_dup_idx)

    def test_plain_arrow_then_shift_card_uses_new_anchor(self):
        self.populate([DuplicateGroup(str(i), 100, self.item(i*2), [self.item(i*2+1)]) for i in range(5)])
        d = self.dialog
        QTest.mouseClick(d.table.viewport(), Qt.MouseButton.LeftButton,
                         pos=d.table.visualRect(d.table.model().index(0, 4)).center())
        QTest.keyClick(d.table, Qt.Key.Key_Down)
        card = d.table.cellWidget(3, 3).findChildren(_InstanceRowButton)[0]
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual([1, 2, 3], [i.row() for i in d.table.selectionModel().selectedRows()])
        QTest.keyClick(d.table, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual([1, 2], [i.row() for i in d.table.selectionModel().selectedRows()])

    def test_page_focus_matches_inspector_after_cell_click_and_shift_arrow(self):
        groups = [DuplicateGroup(str(i), 100, self.item(i*100),
                                 [self.item(i*100+j) for j in range(1, 31)]) for i in range(2)]
        self.populate(groups)
        d = self.dialog
        self.details.stop()
        with patch.object(d.details_panel, 'update_details') as update:
            d._show_copy_page(1, 12)
            d._show_copy_page(0, 12)
            d._on_table_cell_clicked(0, 3)
            self.assertEqual(12, d._active_dup_idx)
            self.assertEqual(groups[0].duplicates[12]['path'], update.call_args.args[0]['path'])
            QTest.keyClick(d.table, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(12, d._active_dup_idx)
            self.assertEqual(groups[1].duplicates[12]['path'], update.call_args.args[0]['path'])
            cards = d.table.cellWidget(1, 3).findChildren(_InstanceRowButton)
            self.assertEqual([12], [c.property('duplicateIndex') for c in cards if c.property('active') == 'true'])

    def test_borrowed_thumbnail_rotates_in_memory_and_never_changes_cache(self):
        from pathlib import Path
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QPixmap, QPainter, QTransform
        base = QPixmap(80, 40)
        base.fill(Qt.GlobalColor.blue)
        painter = QPainter(base)
        painter.fillRect(0, 0, 40, 40, Qt.GlobalColor.red)
        painter.end()
        cached = Path(self.tmp.name) / 'donor.png'
        base.transformed(QTransform().rotate(90)).save(str(cached))
        unchanged = cached.read_bytes()
        d = self.dialog
        for angle in (0, 90, 180, 270):
            with self.subTest(angle=angle):
                keeper = dict(self.item(0), thumbnailRotation=angle)
                donor = dict(self.item(1), thumbnailRotation=90)
                group = DuplicateGroup(str(angle), 100, keeper, [donor])
                with patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                           side_effect=lambda item: str(cached) if item['drive_id'] == '1' else ''), \
                     patch('src.ui.details_panel.ThumbnailCache.is_thumbnail_cached', return_value=False), \
                     patch('src.ui.details_panel.ThumbnailManager.generate_local_thumbnail') as generate:
                    self.populate([group])
                    path, delta = d._find_group_cached_thumbnail(group, preferred=keeper)
                    self.assertEqual((str(cached), (angle-90) % 360), (path, delta))
                    # Cache hit retains both path and angular difference.
                    self.assertEqual((path, delta), d._find_group_cached_thumbnail(group, preferred=keeper))
                    expected = base.transformed(QTransform().rotate(angle))
                    icon = d.table.item(0, 1).icon().pixmap(QSize(56, 56))
                    d.details_panel._load_thumbnail(dict(keeper, fallback_thumbnail_path=path,
                                                        fallback_thumbnail_rotation=delta))
                    rendered = d.details_panel.thumbnail_label.pixmap()
                    for pix in (icon, rendered):
                        self.assertEqual(expected.width() > expected.height(), pix.width() > pix.height())
                        self.assertEqual(expected.toImage().pixelColor(0, 0), pix.toImage().pixelColor(0, 0))
                    # A própria miniatura já rotacionada não recebe o delta do fallback.
                    d.details_panel._load_thumbnail(dict(donor, thumbnailPath=str(cached),
                                                        fallback_thumbnail_path=path, fallback_thumbnail_rotation=270))
                    own = d.details_panel.thumbnail_label.pixmap()
                    self.assertLess(own.width(), own.height())
                    generate.assert_not_called()
                    self.assertEqual(unchanged, cached.read_bytes())


if __name__ == '__main__':
    unittest.main()
