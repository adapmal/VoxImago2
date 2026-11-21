"""Smoke test offline da construcao da janela principal."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contextlib import contextmanager
from PIL import Image
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu

from src.authentication import AuthWorker
from src.ui.list_update import list_update
from src.ui.staging_queue import StagingQueue
from src.ui.thumbnails import ThumbnailCache
from src.ui.ui import DriveFileGalleryApp
from src.utils.config_manager import ConfigManager


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @contextmanager
    def _offline_window(self):
        old_cwd = os.getcwd()
        old_config = ConfigManager._instance
        old_queue = StagingQueue._instance
        window = None
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    os.chdir(temp_dir)
                    ConfigManager._instance = None
                    StagingQueue._instance = None
                    config = ConfigManager()
                    config.set('sandbox_mode', True)
                    config.set('excluded_drive_letters', ['O:'])

                    with (
                        patch.object(AuthWorker, 'is_authenticated', return_value=False),
                        patch.object(AuthWorker, 'refresh_token', return_value=None),
                    ):
                        window = DriveFileGalleryApp()
                        self.app.processEvents()

                    yield window, Path(temp_dir)
                finally:
                    if window is not None:
                        window.details_panel.staging_queue.timer.stop()
                        window.close()
                        window.indexer.close()
                        window.deleteLater()
                        self.app.processEvents()
                        window = None
                    os.chdir(old_cwd)
        finally:
            ConfigManager._instance = old_config
            StagingQueue._instance = old_queue
            os.chdir(old_cwd)

    def test_main_window_opens_offline_with_temporary_database(self):
        with self._offline_window() as (window, _temp_dir):
            self.assertIsNotNone(window.indexer)
            self.assertTrue(window.indexer.search_index_is_consistent()[0])
            self.assertIsNotNone(window.details_panel)

    def test_delete_queue_preserves_page_scroll_and_selection(self):
        with (
            self._offline_window() as (window, temp_dir),
            patch.object(ThumbnailCache, 'get_existing_thumbnail_cache_path', return_value=str(temp_dir / 'missing.jpg')),
        ):
            root = temp_dir / 'Acervo'
            folder = root / '2025' / 'Evento'
            folder.mkdir(parents=True)
            window.config_mgr.set('sandbox_path', str(root))
            window.explorer_special_active = False
            items = []
            for i in range(60):
                p = folder / f'img_{i:03d}.jpg'
                Image.new('RGB', (20, 20), color=(40, 80, 120)).save(p, format='JPEG')
                items.append(dict(
                    id=os.path.normcase(str(p)),
                    path=os.path.normcase(str(p)),
                    name=p.name,
                    mimeType='image/jpeg',
                    source='local',
                    description='',
                    size=200,
                    modifiedTime=1000 + i,
                    createdTime=1000 + i,
                    parentId=os.path.normcase(str(folder)),
                ))
            window.indexer.save_files_in_batch(items, source='local')
            window.page_size = 20
            window.show()
            window.resize(1200, 500)
            window.folder_tree.set_root_directory(str(root))
            window.on_folder_tree_selected(str(folder))
            # Carrega segunda página e posiciona o scroll em uma miniatura distante do topo
            list_update.load_next_batch(window)
            self.assertEqual(2, window.current_page)
            self.assertEqual(40, window.file_list_model.rowCount())

            target_idx = window.file_list_model.index(30, 0)
            window.file_list_view.setCurrentIndex(target_idx)
            window.file_list_view.scrollTo(target_idx)
            self.app.processEvents()
            sb = window.file_list_view.verticalScrollBar()
            if sb.value() == 0 and sb.maximum() > 0:
                sb.setValue(sb.maximum() // 2)
                self.app.processEvents()
            scroll_before = sb.value()
            self.assertGreater(scroll_before, 0)

            # Enfileira exclusão pela tecla Delete
            QTest.keyClick(window.file_list_view, Qt.Key.Key_Delete)
            self.app.processEvents()

            deletes = [it for it in window.main_bar.staging_queue.items if it.action_type == 'delete']
            self.assertEqual(1, len(deletes))
            self.assertEqual(2, window.current_page)
            self.assertEqual(40, window.file_list_model.rowCount())
            self.assertEqual(scroll_before, sb.value())
            self.assertEqual([30], [i.row() for i in window.file_list_view.selectedIndexes()])

            # Desfaz exclusão pela mesma ação: continua no mesmo ponto sem reiniciar
            QTest.keyClick(window.file_list_view, Qt.Key.Key_Delete)
            self.app.processEvents()
            deletes_after = [it for it in window.main_bar.staging_queue.items if it.action_type == 'delete']
            self.assertEqual([], deletes_after)
            self.assertEqual(2, window.current_page)
            self.assertEqual(40, window.file_list_model.rowCount())
            self.assertEqual(scroll_before, sb.value())

    def test_context_menu_select_folder_selects_tree_and_filters_grid(self):
        with (
            self._offline_window() as (window, temp_dir),
            patch.object(ThumbnailCache, 'get_existing_thumbnail_cache_path', return_value=str(temp_dir / 'missing.jpg')),
        ):
            root = temp_dir / 'Acervo'
            dir_a = root / '2024' / 'Praia'
            dir_b = root / '2025' / 'Montanha'
            dir_a.mkdir(parents=True)
            dir_b.mkdir(parents=True)
            window.config_mgr.set('sandbox_path', str(root))
            window.explorer_special_active = False
            img_a = dir_a / 'praia.jpg'
            img_b = dir_b / 'montanha.jpg'
            for p in (img_a, img_b):
                Image.new('RGB', (20, 20), color=(20, 100, 180)).save(p, format='JPEG')

            window.indexer.save_files_in_batch([
                dict(id=os.path.normcase(str(img_a)), path=os.path.normcase(str(img_a)), name='praia.jpg',
                     mimeType='image/jpeg', source='local', description='', size=100,
                     modifiedTime=2000, createdTime=2000, parentId=os.path.normcase(str(dir_a))),
                dict(id=os.path.normcase(str(img_b)), path=os.path.normcase(str(img_b)), name='montanha.jpg',
                     mimeType='image/jpeg', source='local', description='', size=100,
                     modifiedTime=1000, createdTime=1000, parentId=os.path.normcase(str(dir_b))),
            ], source='local')

            window.folder_tree.set_root_directory(str(root))
            window.current_folder_path_filter = None
            window.current_page = 0
            window.all_files_loaded = False
            list_update.clear_display(window)
            list_update.load_next_batch(window)
            self.assertEqual(2, window.file_list_model.rowCount())

            window.show()
            self.app.processEvents()
            idx_a = window.file_list_model.index(0, 0)
            pos = window.file_list_view.visualRect(idx_a).center()

            captured_texts = []
            orig_exec = QMenu.exec
            def pick_select_folder(menu_self, _pos):
                for act in menu_self.actions():
                    captured_texts.append(act.text())
                    if 'selecionar pasta' in act.text().lower():
                        return act
                return None

            with patch.object(QMenu, 'exec', pick_select_folder):
                window.file_list_view.show_context_menu(pos)
            self.app.processEvents()

            self.assertTrue(any('selecionar pasta' in t.lower() for t in captured_texts))
            self.assertEqual(
                os.path.normcase(os.path.normpath(str(dir_a))),
                os.path.normcase(os.path.normpath(window.current_folder_path_filter or '')),
            )
            tree_idx = window.folder_tree.tree_view.currentIndex()
            self.assertTrue(tree_idx.isValid())
            self.assertEqual(
                os.path.normcase(os.path.normpath(str(dir_a))),
                os.path.normcase(os.path.normpath(window.folder_tree.model.filePath(tree_idx))),
            )
            self.assertTrue(window.folder_tree.btn_filter_toggle.isChecked())
            self.assertIn('Praia', window.folder_tree.btn_filter_toggle.text())
            self.assertEqual(['praia.jpg'], [f['name'] for f in window.file_list_model._files])

            # Com termo de busca ativo, selecionar pasta da segunda foto filtra a busca para aquela pasta
            window.folder_tree.clear_folder_selection()
            window.search_term = 'montanha'
            window.select_folder_for_file(dict(id=str(img_b), path=str(img_b), name='montanha.jpg', mimeType='image/jpeg', source='local'))
            self.app.processEvents()
            self.assertEqual(['montanha.jpg'], [f['name'] for f in window.file_list_model._files])
            self.assertEqual(
                os.path.normcase(os.path.normpath(str(dir_b))),
                os.path.normcase(os.path.normpath(window.current_folder_path_filter or '')),
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
