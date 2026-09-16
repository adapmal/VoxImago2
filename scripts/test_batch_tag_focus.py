"""Regressões de foco ao terminar a preparação de tags em lote (Qt real)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QLineEdit, QStatusBar
from src.ui.details_panel import FileDetailsPanel
from src.ui.staging_queue import StagingQueue


class ControlledWorker(QObject):
    progress = pyqtSignal(int, int)
    saving = pyqtSignal()
    succeeded = pyqtSignal(object)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, *args):
        super().__init__(args[-1])

    def start(self):
        pass

    def requestInterruption(self):
        self.cancelled.emit()
        self.finished.emit()


class BatchTagFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd, self.old_queue = os.getcwd(), StagingQueue._instance
        os.chdir(self.tmp.name)
        StagingQueue._instance = None
        self.worker_patch = patch('src.services.batch_tags.BatchTagsWorker', ControlledWorker)
        self.worker_patch.start()
        self.win = QMainWindow()
        self.win.resize(700, 1000)
        self.win.status_bar = QStatusBar(self.win)
        self.win.setStatusBar(self.win.status_bar)
        self.win.indexer = SimpleNamespace(db_name='unused.db')
        self.win.begin_maintenance_operation = lambda: True
        self.win.end_maintenance_operation = lambda: None
        container = QWidget()
        layout = QVBoxLayout(container)
        self.search = QLineEdit()
        layout.addWidget(self.search)
        self.panel = FileDetailsPanel(self.win)
        layout.addWidget(self.panel)
        self.win.setCentralWidget(container)
        self.files = [dict(id=str(i), path=f'X:/online/{i}.jpg', name=f'{i}.jpg',
                           source='local', mimeType='image/jpeg', description='') for i in range(2)]
        self.panel.update_details_batch(self.files)
        self.win.show()
        self.win.activateWindow()
        self.editor = self.panel.tag_chips_widget.input_field
        self.panel.scroll_area.ensureWidgetVisible(self.editor)
        self.app.processEvents()
        self.editor.setFocus()
        self.app.processEvents()
        self.assertTrue(self.editor.hasFocus())

    def tearDown(self):
        if self.panel._batch_worker:
            self.panel._batch_worker.finished.emit()
        self.panel.staging_queue.timer.stop()
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()
        self.worker_patch.stop()
        StagingQueue._instance = self.old_queue
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def start_tag(self):
        self.editor.setText('Nova')
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertIsNotNone(self.panel._batch_worker)
        self.assertFalse(self.panel.content_widget.isEnabled())

    def finish_tag(self):
        worker = self.panel._batch_worker
        worker.succeeded.emit([])
        worker.finished.emit()
        self.app.processEvents()
        self.assertTrue(self.panel.content_widget.isEnabled())

    def test_focus_returns_after_each_tag_without_clicking_again(self):
        for _ in range(3):
            self.start_tag()
            self.finish_tag()
            self.assertTrue(self.editor.hasFocus())
            self.assertEqual('', self.editor.text())

    def test_click_in_search_during_batch_keeps_focus_there(self):
        self.start_tag()
        QTest.mouseClick(self.search, Qt.MouseButton.LeftButton)
        self.finish_tag()
        self.assertTrue(self.search.hasFocus())
        self.assertFalse(self.editor.hasFocus())

    def test_keyboard_navigation_during_batch_does_not_reclaim_focus(self):
        self.start_tag()
        self.search.setFocus()
        QTest.keyClicks(self.search, 'busca')
        self.finish_tag()
        self.assertTrue(self.search.hasFocus())
        self.assertEqual('busca', self.search.text())

    def test_changed_selection_does_not_reclaim_focus(self):
        self.start_tag()
        self.panel.update_details_batch(self.files[:1])
        self.search.setFocus()
        self.finish_tag()
        self.assertTrue(self.search.hasFocus())


if __name__ == '__main__':
    unittest.main()
