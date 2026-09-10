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

from PyQt6.QtWidgets import QApplication

from src.authentication import AuthWorker
from src.ui.staging_queue import StagingQueue
from src.ui.ui import DriveFileGalleryApp
from src.utils.config_manager import ConfigManager


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_opens_offline_with_temporary_database(self):
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

                    self.assertIsNotNone(window.indexer)
                    self.assertTrue(window.indexer.search_index_is_consistent()[0])
                    self.assertIsNotNone(window.details_panel)
                    window.close()
                    window.indexer.close()
                    window.deleteLater()
                    self.app.processEvents()
                    window = None
                finally:
                    os.chdir(old_cwd)
        finally:
            if window is not None:
                window.deleteLater()
            ConfigManager._instance = old_config
            StagingQueue._instance = old_queue
            os.chdir(old_cwd)


if __name__ == '__main__':
    unittest.main(verbosity=2)
