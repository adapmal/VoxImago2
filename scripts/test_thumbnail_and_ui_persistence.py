"""Testes de preservacao do painel, cache e rotacao."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtWidgets import QApplication, QMainWindow, QStatusBar
from PIL import Image

from src.database.database import FileIndexer
from src.ui.details_panel import FileDetailsPanel
from src.ui.list_model import FileListModel
from src.ui.list_view import FileListView
from src.ui.thumbnails import ThumbnailCache
from src.utils.config_manager import ConfigManager


class ThumbnailAndUiPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_background_click_preserves_selection_context(self):
        view = FileListView()
        view.indexAt = Mock(return_value=QModelIndex())
        view.clearSelection = Mock()
        event = SimpleNamespace(
            button=lambda: Qt.MouseButton.LeftButton,
            pos=lambda: None,
            modifiers=lambda: Qt.KeyboardModifier.NoModifier,
            accept=Mock(),
        )
        view.mousePressEvent(event)
        view.clearSelection.assert_not_called()
        event.accept.assert_called_once()

    def test_rotation_survives_local_rescan_upsert(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                with patch.object(
                    ConfigManager, 'is_sandbox', return_value=True
                ):
                    indexer = FileIndexer(os.path.join(temp_dir, 'test.db'))
                path = os.path.join(temp_dir, 'foto.jpg')
                item = {
                    'id': path, 'path': path, 'name': 'foto.jpg',
                    'source': 'local', 'mimeType': 'image/jpeg',
                    'size': 10, 'modifiedTime': 1, 'createdTime': 1,
                    'parentId': temp_dir,
                }
                indexer.save_files_in_batch([item], source='local')
                indexer.update_thumbnail_rotation(path, path, 90)
                indexer.save_files_in_batch([item], source='local')
                rotation = indexer.conn.execute(
                    'SELECT thumbnailRotation FROM files WHERE file_id=?',
                    (os.path.normcase(os.path.normpath(path)),),
                ).fetchone()[0]
                self.assertEqual(90, rotation)
                indexer.close()
            finally:
                os.chdir(old_cwd)

    def test_rotation_has_its_own_cache_key(self):
        base = {
            'id': 'x', 'path': r'C:\Acervo\foto.jpg', 'source': 'local',
            'modifiedTime': 1, 'size': 2,
        }
        key_0 = ThumbnailCache.get_thumbnail_cache_key(base)
        key_90 = ThumbnailCache.get_thumbnail_cache_key(
            {**base, 'thumbnailRotation': 90}
        )
        self.assertNotEqual(key_0, key_90)

    def test_batch_rotation_uses_live_database_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(ConfigManager, 'is_sandbox', return_value=True):
                indexer = FileIndexer(os.path.join(temp_dir, 'batch.db'))
            items = []
            for number in (1, 2):
                path = os.path.join(temp_dir, f'foto-{number}.jpg')
                Image.new('RGB', (20, 10), 'red').save(path)
                item = {
                    'id': path, 'path': path,
                    'name': os.path.basename(path),
                    'source': 'local', 'mimeType': 'image/jpeg',
                    'size': os.path.getsize(path), 'modifiedTime': 1,
                    'createdTime': 1, 'parentId': temp_dir,
                    'thumbnailRotation': 0,
                }
                items.append(item)
            indexer.save_files_in_batch(items, source='local')
            refs = [(item['id'], item['path']) for item in items]

            indexer.rotate_thumbnail_rotations(refs)
            # Os objetos continuam deliberadamente em zero para provar que o
            # segundo clique usa o valor vivo do SQLite, nao o valor da tela.
            indexer.rotate_thumbnail_rotations(refs)

            rotations = indexer.conn.execute(
                'SELECT thumbnailRotation FROM files ORDER BY name'
            ).fetchall()
            self.assertEqual([(180,), (180,)], rotations)
            indexer.close()

    def test_batch_rotation_updates_grid_and_survives_refocus(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_cwd = os.getcwd()
            os.chdir(temp_dir)
            window = None
            indexer = None
            try:
                with patch.object(ConfigManager, 'is_sandbox', return_value=True):
                    indexer = FileIndexer(os.path.join(temp_dir, 'panel.db'))
                items = []
                for number in (1, 2):
                    path = os.path.join(temp_dir, f'item-{number}.jpg')
                    Image.new('RGB', (24, 12), 'blue').save(path)
                    items.append({
                        'id': path, 'path': path,
                        'name': os.path.basename(path),
                        'source': 'local', 'mimeType': 'image/jpeg',
                        'size': os.path.getsize(path), 'modifiedTime': 1,
                        'createdTime': 1, 'parentId': temp_dir,
                        'thumbnailRotation': 0,
                    })
                indexer.save_files_in_batch(items, source='local')

                window = QMainWindow()
                window.indexer = indexer
                window.file_list_model = FileListModel([dict(x) for x in items])
                window.file_list_view = FileListView(window)
                window.file_list_view.setModel(window.file_list_model)
                window.status_bar = QStatusBar(window)
                panel = FileDetailsPanel(window)

                with patch.object(ConfigManager, 'is_read_only', return_value=False):
                    panel.update_details_batch(window.file_list_model.getFiles())
                    panel._rotate_image_action()

                model_items = window.file_list_model.getFiles()
                self.assertEqual([90, 90], [
                    item['thumbnailRotation'] for item in model_items
                ])
                panel.update_details(model_items[0])
                self.assertEqual(
                    90, panel.current_file_item['thumbnailRotation']
                )
                stored = indexer.conn.execute(
                    'SELECT thumbnailRotation FROM files ORDER BY name'
                ).fetchall()
                self.assertEqual([(90,), (90,)], stored)
            finally:
                if window is not None:
                    window.close()
                if indexer is not None:
                    indexer.close()
                os.chdir(old_cwd)


if __name__ == '__main__':
    unittest.main(verbosity=2)
