"""Exercita a interface manual real, usando apenas bancos temporários."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QTimer, QEventLoop
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox
from src.database.database import FileIndexer
from src.ui.snapshot_dialog import SnapshotDialog
from src.services.snapshot_management import pending_path


class SnapshotDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'local.db'
        self.source = self.root / 'snapshot.db'
        for path in (self.db, self.source):
            indexer = FileIndexer(str(path))
            indexer.close()
        self.win = QMainWindow()
        self.win.begin_maintenance_operation = Mock(return_value=True)
        self.win.end_maintenance_operation = Mock()
        self.config = Mock()
        self.config.get.return_value = str(self.root / 'shared.db')
        self.config.is_read_only.return_value = False
        self.config.is_sandbox.return_value = False
        with patch('src.ui.snapshot_dialog.ConfigManager', return_value=self.config):
            self.dialog = SnapshotDialog(self.win, SimpleNamespace(db_name=str(self.db)))
        self.dialog.source.setText(str(self.source))
        self.dialog.show()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def wait(self):
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if self.dialog.worker is None else None)
        poll.start(1)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(5000)
        loop.exec()
        poll.stop()
        timeout.stop()
        self.assertIsNone(self.dialog.worker)

    def test_compare_then_prepare_is_explicit_and_does_not_replace_db(self):
        before = self.db.read_bytes()
        self.dialog._compare()
        self.assertFalse(self.dialog.compare.isEnabled())
        self.wait()
        self.assertTrue(self.dialog.adopt.isEnabled())
        self.assertIn('Nenhum dado foi importado', self.dialog.output.toPlainText())
        with patch('src.ui.snapshot_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog._adopt()
            self.wait()
        self.assertTrue(pending_path(self.db).exists())
        self.assertEqual(before, self.db.read_bytes())
        self.dialog._cancel_pending()
        self.assertFalse(pending_path(self.db).exists())
        self.assertEqual(2, self.win.end_maintenance_operation.call_count)

    def test_failed_comparison_releases_maintenance_and_blocks_adoption(self):
        self.dialog.source.setText(str(self.root / 'absent.db'))
        with patch('src.ui.snapshot_dialog.QMessageBox.warning') as warning:
            self.dialog._compare()
            self.wait()
        warning.assert_called_once()
        self.assertFalse(self.dialog.adopt.isEnabled())
        self.win.end_maintenance_operation.assert_called_once()

    def test_declining_publication_creates_nothing(self):
        with patch('src.ui.snapshot_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            self.dialog._publish_preview()
            self.wait()
        self.assertFalse(list(self.root.glob('shared_v2.*')))

    def test_confirmed_publication_creates_versioned_snapshot(self):
        from src.utils.config_manager import ConfigManager
        from src.database.snapshot import read_manifest
        with patch('src.ui.snapshot_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch.object(ConfigManager, 'get_resolved_scan_paths', return_value=[]), \
                patch.object(ConfigManager, 'set'):
            self.dialog._publish_preview()
            self.wait()
        manifest = read_manifest(str(self.root / 'shared_v2.db'))
        self.assertIsNotNone(manifest)
        self.assertTrue((self.root / manifest['database_file']).exists())
        self.assertTrue((self.root / manifest['csv_file']).exists())


if __name__ == '__main__':
    unittest.main()
