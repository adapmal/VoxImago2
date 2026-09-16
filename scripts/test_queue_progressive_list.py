"""Teste da lista real Qt, sem abrir o banco, mídia ou Drive do usuário."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QObject, QEventLoop, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication
from src.ui.staging_queue import StagingQueueDialog
from src.utils.staging_schema import StagingItem


class Signals(QObject):
    changed = pyqtSignal(int)


class ProgressiveQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.signals = Signals()
        self.queue = SimpleNamespace(items=[], queueChanged=self.signals.changed)
        self.queue.count = lambda: len(self.queue.items)
        self.config = Mock()
        self.config.is_read_only.return_value = False
        self.config.is_sandbox.return_value = False
        with patch('src.ui.staging_queue.StagingQueue', return_value=self.queue), \
                patch('src.ui.staging_queue.ConfigManager', return_value=self.config):
            self.dialog = StagingQueueDialog()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def items(self, count):
        return [StagingItem(str(i), f'{i}.jpg', f'X:/online/{i}.jpg',
                            'set_description', '', 'Teste') for i in range(count)]

    def wait_until(self, condition):
        loop = QEventLoop()
        timer = QTimer()
        ticks = []
        def check():
            ticks.append(True)
            if condition():
                loop.quit()
        timer.timeout.connect(check)
        timer.start(1)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(10000)
        loop.exec()
        timer.stop()
        timeout.stop()
        self.assertTrue(condition(), 'Carregamento não terminou dentro de 10 s')
        return len(ticks)

    def test_ten_thousand_rows_are_progressive_and_keep_event_loop_alive(self):
        self.queue.items = self.items(10000)
        original = list(self.queue.items)
        focus = Mock()
        self.dialog.itemFocusRequested.connect(focus)
        with patch('src.ui.staging_queue.os.path.isfile', side_effect=AssertionError('media probe')):
            self.dialog.show()
            self.assertEqual(0, self.dialog.list_widget.count())
            self.assertTrue(self.dialog.list_progress.isVisible())
            self.assertFalse(self.dialog.btn_execute.isEnabled())
            ticks = self.wait_until(lambda: not self.dialog._list_loading)
        self.assertGreater(ticks, 2)
        self.assertEqual(10000, self.dialog.list_widget.count())
        self.assertTrue(self.dialog.btn_execute.isEnabled())
        self.assertFalse(self.dialog.list_progress.isVisible())
        self.assertEqual(original, self.queue.items)
        for index in (0, 4999, 9999):
            row = self.dialog.list_widget.item(index)
            self.assertIs(original[index], row.data(Qt.ItemDataRole.UserRole))
            self.assertTrue(row.text().startswith(f'{index + 1}.'))
        focus.assert_not_called()

    def test_change_during_load_discards_stale_rows(self):
        self.queue.items = self.items(3000)
        self.dialog.show()
        self.wait_until(lambda: self.dialog.list_widget.count() > 0)
        self.queue.items = self.items(7)
        self.signals.changed.emit(7)
        self.wait_until(lambda: not self.dialog._list_loading)
        self.assertEqual(7, self.dialog.list_widget.count())
        self.assertIs(self.queue.items[-1], self.dialog.list_widget.item(6).data(Qt.ItemDataRole.UserRole))

    def test_close_stops_loading_and_reopen_uses_current_queue(self):
        self.queue.items = self.items(3000)
        self.dialog.show()
        self.wait_until(lambda: self.dialog.list_widget.count() > 0)
        self.dialog.close()
        self.assertFalse(self.dialog._list_timer.isActive())
        self.queue.items = self.items(3)
        self.dialog.show()
        self.wait_until(lambda: not self.dialog._list_loading)
        self.assertEqual(3, self.dialog.list_widget.count())

    def test_empty_and_read_only_keep_execution_disabled(self):
        self.dialog.show()
        self.wait_until(lambda: not self.dialog._list_loading)
        self.assertFalse(self.dialog.btn_execute.isEnabled())
        self.assertTrue(self.dialog.btn_import.isEnabled())
        self.config.is_read_only.return_value = True
        self.queue.items = self.items(3)
        self.signals.changed.emit(3)
        self.wait_until(lambda: not self.dialog._list_loading)
        self.assertFalse(self.dialog.btn_execute.isEnabled())
        self.assertFalse(self.dialog.btn_execute_next.isEnabled())

    def test_later_move_is_shown_on_earlier_tag_row(self):
        self.queue.items = self.items(500)
        self.queue.items.append(StagingItem('0', '0.jpg', 'X:/online/0.jpg',
                                            'move', 'X:/online/0.jpg', 'X:/dest/0.jpg'))
        self.dialog.show()
        self.wait_until(lambda: not self.dialog._list_loading)
        self.assertIn('Movido para: dest', self.dialog.list_widget.item(0).text())


if __name__ == '__main__':
    unittest.main()
