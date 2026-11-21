"""Testes de layout/EXIF do Painel de Detalhes, inspeção/Barra de Espaço no Deduplicador e menu da Árvore."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMainWindow

from src.ui.details_panel import FileDetailsPanel
from src.ui.hash_dedup_dialog import HashDedupDialog
from src.ui.staging_queue import StagingQueue
from src.utils.exif_utils import extract_exif_summary


def _make_jpeg_with_exif(path: Path, *, date_str: str | None = None, with_gps: bool = False):
    img = Image.new('RGB', (80, 60), color=(40, 120, 200))
    exif = img.getexif()
    if date_str or with_gps:
        if date_str:
            exif[306] = date_str
            exif_ifd = exif.get_ifd(0x8769)
            exif_ifd[36867] = date_str
        if with_gps:
            from fractions import Fraction
            gps_ifd = exif.get_ifd(0x8825)
            gps_ifd[1] = 'S'
            gps_ifd[2] = (Fraction(23, 1), Fraction(33, 1), Fraction(187, 100))
            gps_ifd[3] = 'W'
            gps_ifd[4] = (Fraction(46, 1), Fraction(37, 1), Fraction(5991, 100))
    img.save(path, format='JPEG', exif=exif.tobytes())


class ExifAndDetailsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd, self.old_queue = os.getcwd(), StagingQueue._instance
        os.chdir(self.tmp.name)
        StagingQueue._instance = None
        self.win = QMainWindow()
        self.win.indexer = SimpleNamespace(db_name='unused.db')
        self.panel = FileDetailsPanel(self.win)
        self.win.setCentralWidget(self.panel)
        self.win.show()

    def tearDown(self):
        self._wait_exif()
        self.panel.staging_queue.timer.stop()
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()
        StagingQueue._instance = self.old_queue
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _wait_exif(self):
        from time import monotonic
        deadline = monotonic() + 5
        while self.panel._exif_task is not None and monotonic() < deadline:
            QTest.qWait(5)
        self.assertIsNone(self.panel._exif_task)

    def test_exif_is_background_coalesced_and_stale_results_ignored(self):
        import threading
        started, release = threading.Event(), threading.Event()
        calls, threads, heartbeat = [], [], []
        ui_thread = threading.get_ident()
        def read(path):
            calls.append(os.path.basename(path))
            threads.append(threading.get_ident())
            if path.endswith('a.jpg'):
                started.set()
                release.wait(3)
            return os.path.basename(path), None
        with patch('src.ui.details_panel.extract_exif_summary', side_effect=read), \
                patch.object(self.panel, '_apply_exif_result') as apply:
            try:
                self.panel._update_exif_section(str(Path(self.tmp.name) / 'a.jpg'))
                self.assertTrue(started.wait(1))
                self.panel._update_exif_section(str(Path(self.tmp.name) / 'b.jpg'))
                self.panel._update_exif_section(str(Path(self.tmp.name) / 'c.jpg'))
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                self.app.processEvents()
                self.assertTrue(heartbeat)
                apply.assert_not_called()
            finally:
                release.set()
                self._wait_exif()
            self.assertEqual(['a.jpg', 'c.jpg'], calls)
            self.assertTrue(all(t != ui_thread for t in threads))
            apply.assert_called_once_with('c.jpg', None)

    def test_clear_details_discards_pending_exif(self):
        import threading
        started, release = threading.Event(), threading.Event()
        def read(_):
            started.set()
            release.wait(3)
            return 'old result', None
        with patch('src.ui.details_panel.extract_exif_summary', side_effect=read), \
                patch.object(self.panel, '_apply_exif_result') as apply:
            try:
                self.panel._update_exif_section(str(Path(self.tmp.name) / 'old.jpg'))
                self.assertTrue(started.wait(1))
                self.panel.clear_details()
            finally:
                release.set()
                self._wait_exif()
            apply.assert_not_called()

    def test_inspection_never_generates_thumbnail_from_original(self):
        self.panel.set_inspection_only(True)
        with patch('src.ui.details_panel.ThumbnailCache.is_thumbnail_cached', return_value=False), \
                patch('src.ui.details_panel.ThumbnailManager.generate_local_thumbnail') as generate:
            self.panel._load_thumbnail(dict(path=str(Path(self.tmp.name) / 'online.tif'), mimeType='image/tiff'))
        generate.assert_not_called()

    def test_tags_and_exif_appear_right_below_preview(self):
        layout = self.panel.content_layout
        thumb_idx = layout.indexOf(self.panel.thumbnail_label)
        tags_idx = layout.indexOf(self.panel.tags_section_widget)
        exif_idx = layout.indexOf(self.panel.exif_container)
        sep_idx = layout.indexOf(self.panel.separator)
        form_idx = -1
        for i in range(layout.count()):
            if layout.itemAt(i).layout() is self.panel.form_layout:
                form_idx = i
                break
        self.assertGreaterEqual(thumb_idx, 0)
        self.assertLess(thumb_idx, tags_idx)
        self.assertEqual(tags_idx + 1, exif_idx)
        self.assertLess(exif_idx, sep_idx)
        self.assertLess(sep_idx, form_idx)

    def test_exif_shows_date_and_gps_or_only_date_or_hidden(self):
        p_both = Path(self.tmp.name) / 'both.jpg'
        p_date = Path(self.tmp.name) / 'date_only.jpg'
        p_none = Path(self.tmp.name) / 'plain.jpg'
        _make_jpeg_with_exif(p_both, date_str='2025:06:15 14:30:05', with_gps=True)
        _make_jpeg_with_exif(p_date, date_str='2025:06:15 14:30:05', with_gps=False)
        _make_jpeg_with_exif(p_none)

        d, g = extract_exif_summary(str(p_both))
        self.assertEqual('15/06/2025 14:30:05', d)
        self.assertIsNotNone(g)
        self.assertIn('-23.', g)
        self.assertIn('-46.', g)

        item_base = dict(source='local', mimeType='image/jpeg', description='', size=100, createdTime=1000)
        self.panel.update_details(dict(item_base, id=str(p_both), path=str(p_both), name='both.jpg'))
        self._wait_exif()
        self.assertTrue(self.panel.exif_container.isVisibleTo(self.panel))
        self.assertTrue(self.panel.exif_date_label.isVisibleTo(self.panel.exif_container))
        self.assertTrue(self.panel.exif_gps_label.isVisibleTo(self.panel.exif_container))

        self.panel.update_details(dict(item_base, id=str(p_date), path=str(p_date), name='date_only.jpg'))
        self._wait_exif()
        self.assertTrue(self.panel.exif_container.isVisibleTo(self.panel))
        self.assertTrue(self.panel.exif_date_label.isVisibleTo(self.panel.exif_container))
        self.assertFalse(self.panel.exif_gps_label.isVisibleTo(self.panel.exif_container))

        self.panel.update_details(dict(item_base, id=str(p_none), path=str(p_none), name='plain.jpg'))
        self._wait_exif()
        self.assertFalse(self.panel.exif_container.isVisibleTo(self.panel))


class HashDedupInspectionAndSpaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_initial_folders_and_inspection_and_space_preview(self):
        from scripts.test_hash_dedup import HashDedupTests, ROOT, SOURCE, REFERENCE
        from src.services.hash_deduplicator import MODE_SELECTED_FOLDERS

        fixture = HashDedupTests()
        fixture.setUp()
        try:
            with patch('src.ui.hash_dedup_dialog.ConfigManager', return_value=fixture.cfg), \
                 patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                       return_value=str(Path(fixture.tmp.name) / 'missing-cache.png')):
                dialog = HashDedupDialog(
                    None,
                    indexer=SimpleNamespace(db_name=str(fixture.db)),
                    drive_service=fixture.drive,
                    initial_folders=[SOURCE],
                )
                self.assertTrue(dialog.radio_selected.isChecked())
                self.assertEqual(1, dialog.custom_list.count())
                self.assertEqual(os.path.normpath(SOURCE), dialog.custom_list.item(0).text())

                dialog._add_folders_to_list([REFERENCE])
                self.assertEqual(2, dialog.custom_list.count())

                groups = fixture.scan(MODE_SELECTED_FOLDERS, [SOURCE, REFERENCE], [])
                dialog.groups = groups
                dialog._populate_table()
                # Processar o timer de renderização em lote
                while dialog._render_timer.isActive():
                    self.app.processEvents()

                dialog.table.selectRow(0)
                dialog._on_table_cell_clicked(0, 3)  # Coluna de duplicados
                self.assertTrue(dialog.inspector_widget.isVisibleTo(dialog))
                self.assertEqual(groups[0].duplicates[0]['name'], dialog.details_panel.name_edit.text())

                dialog._on_table_cell_clicked(0, 2)  # Coluna que fica
                self.assertEqual(groups[0].keeper['name'], dialog.details_panel.name_edit.text())

                called = []
                with patch('src.ui.hash_dedup_dialog.open_quick_preview_dialog', side_effect=lambda *a, **kw: called.append((a, kw))):
                    dialog.show()
                    dialog.table.setFocus()
                    QTest.keyClick(dialog.table, Qt.Key.Key_Space)
                    self.assertEqual(1, len(called))
                    items_arg = called[0][0][1]
                    start_idx = called[0][1]['start_index']
                    self.assertEqual(groups[0].keeper['name'], items_arg[start_idx]['name'])

                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
        finally:
            fixture.tearDown()

    def test_three_copies_swap_cycle_never_drops_items_and_each_instance_clickable(self):
        from scripts.test_hash_dedup import HashDedupTests, ROOT
        from src.services.hash_deduplicator import MODE_SELECTED_FOLDERS
        from src.ui.hash_dedup_dialog import _InstanceRowButton

        fixture = HashDedupTests()
        fixture.setUp()
        try:
            # 3 cópias em pastas selecionadas simétricas: ref, a, b
            folder_a = ROOT + r'\Organizar\A'
            folder_b = ROOT + r'\Organizar\B'
            folder_ref = ROOT + r'\Selecionadas'
            groups = fixture.scan(MODE_SELECTED_FOLDERS, [folder_ref, folder_a, folder_b], [])
            self.assertEqual(1, len(groups))
            self.assertEqual(2, len(groups[0].duplicates))

            with patch('src.ui.hash_dedup_dialog.ConfigManager', return_value=fixture.cfg), \
                 patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                       return_value=str(Path(fixture.tmp.name) / 'missing-cache.png')):
                dialog = HashDedupDialog(
                    None,
                    indexer=SimpleNamespace(db_name=str(fixture.db)),
                    drive_service=fixture.drive,
                    initial_folders=[folder_ref, folder_a, folder_b],
                )
                dialog.groups = groups
                dialog._populate_table()
                while dialog._render_timer.isActive():
                    self.app.processEvents()

                # Verificar que os 2 duplicados são botões clicáveis independentes na Coluna 3
                dup_container = dialog.table.cellWidget(0, 3)
                dup_cards = dup_container.findChildren(_InstanceRowButton)
                self.assertEqual(2, len(dup_cards))

                # Clicar na 2ª instância duplicada (other.jpg) exibe detalhes dela
                dup_cards[1].clicked.emit()
                self.assertEqual('other.jpg', dialog.details_panel.name_edit.text())

                # Clicar na 1ª instância duplicada (dup.jpg) exibe detalhes dela
                dup_cards[0].clicked.emit()
                self.assertEqual('dup.jpg', dialog.details_panel.name_edit.text())

                # Clicar na instância da coluna 2 (keep.jpg) exibe detalhes dela
                keep_cards = dialog.table.cellWidget(0, 2).findChildren(_InstanceRowButton)
                self.assertEqual(1, len(keep_cards))
                keep_cards[0].clicked.emit()
                self.assertEqual('keep.jpg', dialog.details_panel.name_edit.text())

                # Ciclar 'Inverter Quem Fica' 4x: NUNCA perde nenhuma das 3 cópias e mantém a seleção!
                dialog.table.selectRow(0)
                seen_keepers = []
                for _ in range(4):
                    dialog._swap_selected_keeper()
                    grp = dialog.groups[0]
                    all_ids = {grp.keeper['drive_id']} | {d['drive_id'] for d in grp.duplicates}
                    self.assertEqual({'keep', 'dup', 'other'}, all_ids)
                    self.assertEqual(2, len(grp.duplicates))
                    self.assertEqual(2, len(dialog.table.cellWidget(0, 3).findChildren(_InstanceRowButton)))
                    self.assertEqual([0], [i.row() for i in dialog.table.selectionModel().selectedRows()])
                    seen_keepers.append(grp.keeper['drive_id'])
                self.assertEqual(['dup', 'other', 'keep', 'dup'], seen_keepers)

                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
        finally:
            fixture.tearDown()

    def test_incremental_sync_uses_overlap_and_cancel_preserves_timestamp(self):
        from src.drive.incremental_sync import IncrementalSyncWorker, SYNC_OVERLAP_SECONDS
        from scripts.test_sync_progress import FakeConfig, FakeIndexer, FakeRequest

        class CancellingService:
            def __init__(self, worker_ref):
                self.worker_ref = worker_ref
                self.queries = []

            def files(self):
                return self

            def list(self, **kwargs):
                self.queries.append(kwargs['q'])
                # Simula 1 arquivo encontrado, cancelando na 2ª paginação
                if not kwargs.get('pageToken'):
                    return FakeRequest({'files': [{'id': 'f1', 'name': 'f1.jpg'}], 'nextPageToken': 'p2'})
                self.worker_ref[0].cancel()
                return FakeRequest({'files': []})

        cfg = FakeConfig()
        cfg.values['last_sync_timestamp'] = 1_700_000_000
        holder = [None]
        svc = CancellingService(holder)
        worker = IncrementalSyncWorker(svc, cfg, indexer=FakeIndexer(), force_window_days=None)
        holder[0] = worker
        cancelled = []
        finished = []
        worker.sync_cancelled.connect(cancelled.append)
        worker.sync_finished.connect(finished.append)

        with (
            patch('src.database.database.FileIndexer', return_value=FakeIndexer()),
            patch('src.drive.incremental_sync.configured_operation_roots', return_value=[]),
        ):
            worker.run()

        self.assertEqual([0], cancelled)
        self.assertEqual([], finished)
        # Cancelar no meio preserva o timestamp anterior inalterado
        self.assertEqual(1_700_000_000, cfg.values['last_sync_timestamp'])
        # E a consulta aplicou a margem de tolerância (last_sync - SYNC_OVERLAP_SECONDS)
        self.assertIn('2023-11-14T22:10:20', svc.queries[0])
        self.assertEqual(180, SYNC_OVERLAP_SECONDS)

    def test_per_instance_arrow_navigation_and_shift_selection(self):
        from src.services.hash_deduplicator import DuplicateGroup, MODE_SELECTED_FOLDERS

        def mk_entry(fid, name, rel):
            return dict(
                drive_id=fid,
                name=name,
                path=rf'L:\Drives Compartilhados\Banco de Imagens\{rel}',
                rel_path=rel,
                depth=rel.count('\\'),
                description=f'tag_{fid}',
                size=1024,
                mimeType='image/jpeg',
            )

        g0 = DuplicateGroup(
            md5='a' * 32,
            size=1024,
            keeper=mk_entry('k0', 'keep0.jpg', r'2002\keep0.jpg'),
            duplicates=[
                mk_entry('d0_0', 'dup0_a.jpg', r'Organizar\dup0_a.jpg'),
                mk_entry('d0_1', 'dup0_b.jpg', r'1999\dup0_b.jpg'),
                mk_entry('d0_2', 'dup0_c.jpg', r'1998\dup0_c.jpg'),
            ],
            eligible_ids={'k0', 'd0_0', 'd0_1', 'd0_2'},
            scope_mode=MODE_SELECTED_FOLDERS,
        )
        g0.recompute_merged()
        g1 = DuplicateGroup(
            md5='b' * 32,
            size=2048,
            keeper=mk_entry('k1', 'keep1.jpg', r'2003\keep1.jpg'),
            duplicates=[
                mk_entry('d1_0', 'dup1_a.jpg', r'Organizar\dup1_a.jpg'),
                mk_entry('d1_1', 'dup1_b.jpg', r'2001\dup1_b.jpg'),
            ],
            eligible_ids={'k1', 'd1_0', 'd1_1'},
            scope_mode=MODE_SELECTED_FOLDERS,
        )
        g1.recompute_merged()

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                   return_value=str(Path(tmpdir) / 'missing-cache.png')):
            dialog = HashDedupDialog(None, indexer=SimpleNamespace(db_name=str(Path(tmpdir) / 'x.db')))
            dialog.groups = [g0, g1]
            dialog._populate_table()
            while dialog._render_timer.isActive():
                self.app.processEvents()
            dialog.show()
            dialog.table.setFocus()
            self.app.processEvents()

            # Começa no 1º duplicado da linha 0
            self.assertEqual(0, dialog.table.currentRow())
            self.assertEqual(3, dialog.table.currentColumn())
            self.assertEqual('dup0_a.jpg', dialog.details_panel.name_edit.text())

            # Down passa por TODOS os duplicados da linha 0 antes de ir à linha 1
            QTest.keyClick(dialog.table, Qt.Key.Key_Down)
            self.assertEqual(0, dialog.table.currentRow())
            self.assertEqual(1, dialog._active_dup_idx)
            self.assertEqual('dup0_b.jpg', dialog.details_panel.name_edit.text())

            QTest.keyClick(dialog.table, Qt.Key.Key_Down)
            self.assertEqual(0, dialog.table.currentRow())
            self.assertEqual(2, dialog._active_dup_idx)
            self.assertEqual('dup0_c.jpg', dialog.details_panel.name_edit.text())

            # Próximo Down pula para a linha 1, 1º duplicado
            QTest.keyClick(dialog.table, Qt.Key.Key_Down)
            self.assertEqual(1, dialog.table.currentRow())
            self.assertEqual(0, dialog._active_dup_idx)
            self.assertEqual('dup1_a.jpg', dialog.details_panel.name_edit.text())

            # Up volta exatamente para o último duplicado da linha 0
            QTest.keyClick(dialog.table, Qt.Key.Key_Up)
            self.assertEqual(0, dialog.table.currentRow())
            self.assertEqual(2, dialog._active_dup_idx)
            self.assertEqual('dup0_c.jpg', dialog.details_panel.name_edit.text())

            # Left vai para Col 2 (Que Fica) e Right retorna à Col 3 (Duplicado #0)
            QTest.keyClick(dialog.table, Qt.Key.Key_Left)
            self.assertEqual(2, dialog.table.currentColumn())
            self.assertEqual('keeper', dialog._active_detail_role)
            self.assertEqual('keep0.jpg', dialog.details_panel.name_edit.text())

            QTest.keyClick(dialog.table, Qt.Key.Key_Right)
            self.assertEqual(3, dialog.table.currentColumn())
            self.assertEqual('duplicate', dialog._active_detail_role)
            self.assertEqual('dup0_a.jpg', dialog.details_panel.name_edit.text())

            # Shift+Down preserva seleção múltipla em lote entre linhas (e libera Shift ao final)
            QTest.keyPress(dialog.table, Qt.Key.Key_Shift)
            try:
                QTest.keyClick(dialog.table, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
            finally:
                QTest.keyRelease(dialog.table, Qt.Key.Key_Shift)
            selected_rows = sorted({i.row() for i in dialog.table.selectionModel().selectedRows()})
            self.assertEqual([0, 1], selected_rows)

            dialog.details_panel.staging_queue.timer.stop()
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_double_click_opens_default_app_and_multi_folder_picker(self):
        from PyQt6.QtWidgets import QDialog, QListView, QAbstractItemView
        from src.services.hash_deduplicator import DuplicateGroup, MODE_SELECTED_FOLDERS
        from src.ui.hash_dedup_dialog import _InstanceRowButton

        with tempfile.TemporaryDirectory() as tmpdir:
            p_dir1 = Path(tmpdir) / '2002'
            p_dir2 = Path(tmpdir) / '1999'
            p_dir1.mkdir()
            p_dir2.mkdir()
            p_keep = p_dir1 / 'keep.jpg'
            p_dup0 = p_dir2 / 'dup0.jpg'
            p_dup1 = p_dir2 / 'dup1.jpg'
            for p in (p_keep, p_dup0, p_dup1):
                Image.new('RGB', (40, 40), color=(30, 80, 160)).save(p, format='JPEG')

            grp = DuplicateGroup(
                md5='c' * 32,
                size=512,
                keeper=dict(drive_id='k', name='keep.jpg', path=str(p_keep), rel_path=r'2002\keep.jpg', depth=1, description='2002', mimeType='image/jpeg'),
                duplicates=[
                    dict(drive_id='d0', name='dup0.jpg', path=str(p_dup0), rel_path=r'1999\dup0.jpg', depth=1, description='1999', mimeType='image/jpeg'),
                    dict(drive_id='d1', name='dup1.jpg', path=str(p_dup1), rel_path=r'1999\dup1.jpg', depth=1, description='praia', mimeType='image/jpeg'),
                ],
                eligible_ids={'k', 'd0', 'd1'},
                scope_mode=MODE_SELECTED_FOLDERS,
            )
            grp.recompute_merged()

            class FakeMultiDirDialog:
                def __init__(self, *args, **kwargs):
                    self.options = []
                    self.file_mode = None
                    self._lv = QListView()
                def setOption(self, opt, on=True):
                    self.options.append((opt, on))
                def setFileMode(self, mode):
                    self.file_mode = mode
                def findChildren(self, types):
                    return [self._lv]
                def exec(self):
                    return QDialog.DialogCode.Accepted
                def selectedFiles(self):
                    return [str(p_dir1), str(p_dir2)]

            with patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                       return_value=str(Path(tmpdir) / 'missing.png')):
                dialog = HashDedupDialog(None, indexer=SimpleNamespace(db_name=str(Path(tmpdir) / 'x.db')))
                fake_holder = []
                def make_fake_dlg(*a, **kw):
                    fd = FakeMultiDirDialog(*a, **kw)
                    fake_holder.append(fd)
                    return fd
                with patch('src.ui.hash_dedup_dialog.QFileDialog', side_effect=make_fake_dlg):
                    dialog._add_custom_folder()
                self.assertEqual(1, len(fake_holder))
                self.assertEqual(QAbstractItemView.SelectionMode.ExtendedSelection, fake_holder[0]._lv.selectionMode())
                self.assertEqual([os.path.normpath(str(p_dir1)), os.path.normpath(str(p_dir2))],
                                 [dialog.custom_list.item(i).text() for i in range(dialog.custom_list.count())])

                dialog.groups = [grp]
                dialog._populate_table()
                while dialog._render_timer.isActive():
                    self.app.processEvents()
                dialog.show()
                self.app.processEvents()

                opened = []
                preview_calls = []
                with patch.object(os, 'startfile', side_effect=opened.append, create=True), \
                     patch('src.ui.hash_dedup_dialog.open_quick_preview_dialog', side_effect=lambda *a, **kw: preview_calls.append(a)):
                    # Duplo clique no cartão do 2º duplicado abre dup1 no aplicativo padrão
                    dup_cards = dialog.table.cellWidget(0, 3).findChildren(_InstanceRowButton)
                    dup_cards[1].doubleClicked.emit()
                    self.assertEqual([os.path.normpath(str(p_dup1))], opened)

                    # Duplo clique no cartão do arquivo que fica abre keep no app padrão
                    keep_cards = dialog.table.cellWidget(0, 2).findChildren(_InstanceRowButton)
                    keep_cards[0].doubleClicked.emit()
                    self.assertEqual([os.path.normpath(str(p_dup1)), os.path.normpath(str(p_keep))], opened)

                    # Duplo clique na coluna Miniatura (col 1) abre no app padrão
                    dialog._on_table_cell_double_clicked(0, 1)
                    self.assertEqual(3, len(opened))
                    self.assertEqual(os.path.normpath(str(p_keep)), opened[-1])

                    # Duplo clique na coluna Tags Finais (col 5) NÃO abre app padrão (fica livre para editar tags)
                    dialog._on_table_cell_double_clicked(0, 5)
                    self.assertEqual(3, len(opened))

                    # Duplo clique na miniatura do painel de detalhes abre a instância em foco no app padrão
                    dup_cards[0].clicked.emit()
                    QTest.mouseDClick(dialog.details_panel.thumbnail_label, Qt.MouseButton.LeftButton)
                    self.assertEqual(os.path.normpath(str(p_dup0)), opened[-1])

                    # Espaço continua ampliando no visualizador interno
                    dialog.table.setFocus()
                    QTest.keyClick(dialog.table, Qt.Key.Key_Space)
                    self.assertEqual(1, len(preview_calls))

                dialog.details_panel.staging_queue.timer.stop()
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()

    def test_tif_thumbnail_generation_8bit_and_16bit_and_category_support(self):
        import mimetypes
        from src.ui.thumbnails import ThumbnailManager

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cache_dir = tmp / 'cache'
            cache_dir.mkdir()
            tif8 = tmp / 'sample8.tif'
            tif16 = tmp / 'sample16.tif'
            Image.new('RGB', (64, 48), color=(30, 150, 210)).save(tif8, format='TIFF')
            Image.new('I;16', (64, 48), color=32768).save(tif16, format='TIFF')

            self.assertEqual('image/tiff', mimetypes.guess_type(str(tif8))[0])

            with patch('src.ui.thumbnails.THUMBNAIL_CACHE_DIR', str(cache_dir)):
                out8 = ThumbnailManager.generate_local_thumbnail({'path': str(tif8), 'name': 'sample8.tif', 'source': 'local'})
                out16 = ThumbnailManager.generate_local_thumbnail({'path': str(tif16), 'name': 'sample16.tif', 'source': 'local'})

            self.assertIsNotNone(out8)
            self.assertTrue(os.path.isfile(out8))
            self.assertIsNotNone(out16)
            self.assertTrue(os.path.isfile(out16))
            with Image.open(out16) as im16:
                px = im16.getpixel((im16.width // 2, im16.height // 2))
                # 16-bit meio-tom (~32768) não deve estourar para branco puro (255, 255, 255)
                self.assertLess(px[0], 180)
                self.assertGreater(px[0], 80)

    def test_col0_whole_cell_click_and_shift_ctrl_multiselection_and_only_make_keeper_btn(self):
        from PyQt6.QtWidgets import QPushButton
        from src.services.hash_deduplicator import DuplicateGroup, MODE_SELECTED_FOLDERS
        from src.ui.hash_dedup_dialog import _InstanceRowButton

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                   return_value=str(Path(tmpdir) / 'missing.png')):
            dialog = HashDedupDialog(None, indexer=SimpleNamespace(db_name=str(Path(tmpdir) / 'x.db')))
            groups = []
            for i in range(5):
                k = dict(drive_id=f'k{i}', name=f'k{i}.jpg', path=rf'L:\Drives Compartilhados\Banco de Imagens\2002\k{i}.jpg', rel_path=rf'2002\k{i}.jpg', depth=1, description='2002', mimeType='image/jpeg')
                d = dict(drive_id=f'd{i}', name=f'd{i}.jpg', path=rf'L:\Drives Compartilhados\Banco de Imagens\Organizar\d{i}.jpg', rel_path=rf'Organizar\d{i}.jpg', depth=1, description='', mimeType='image/jpeg')
                groups.append(DuplicateGroup(f'{i:032x}', 100 + i, k, [d], '2002', True, {f'k{i}', f'd{i}'}, MODE_SELECTED_FOLDERS))

            dialog.groups = groups
            dialog._populate_table()
            while dialog._render_timer.isActive():
                self.app.processEvents()
            dialog.show()
            dialog.resize(1300, 700)
            self.app.processEvents()

            # Apenas o botão "Este fica" fica acima do painel de detalhes (sem botões extras)
            top_btns = [b.text() for b in dialog.inspector_widget.findChildren(QPushButton) if 'Tornar este' in b.text() or 'Duplicado' in b.text() or 'Que Fica' in b.text() or 'Ampliar' in b.text()]
            self.assertEqual(['⭐ Tornar este o que FICA'], top_btns)

            # Clicar na extremidade esquerda/direita da célula Col 0 ('Aplicar') alterna o checkState mesmo fora do quadradinho
            vp = dialog.table.viewport()
            rect0 = dialog.table.visualRect(dialog.table.model().index(1, 0))
            edge_pos = rect0.topLeft() + (rect0.bottomRight() - rect0.topLeft()) * 0.9
            self.assertEqual(Qt.CheckState.Checked, dialog.table.item(1, 0).checkState())
            QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, edge_pos)
            self.app.processEvents()
            self.assertEqual(Qt.CheckState.Unchecked, dialog.table.item(1, 0).checkState())
            self.assertFalse(dialog.groups[1].checked)
            QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, edge_pos)
            self.app.processEvents()
            self.assertEqual(Qt.CheckState.Checked, dialog.table.item(1, 0).checkState())

            # Seleção múltipla via Ctrl+Click e Shift+Click nos cartões da coluna 3
            cards_row0 = dialog.table.cellWidget(0, 3).findChildren(_InstanceRowButton)
            cards_row2 = dialog.table.cellWidget(2, 3).findChildren(_InstanceRowButton)
            cards_row4 = dialog.table.cellWidget(4, 3).findChildren(_InstanceRowButton)

            QTest.mouseClick(cards_row0[0], Qt.MouseButton.LeftButton)
            self.assertEqual([0], sorted({i.row() for i in dialog.table.selectionModel().selectedRows()}))

            try:
                QTest.keyPress(dialog.table, Qt.Key.Key_Control)
                QTest.mouseClick(cards_row2[0], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
            finally:
                QTest.keyRelease(dialog.table, Qt.Key.Key_Control)
            self.assertEqual([0, 2], sorted({i.row() for i in dialog.table.selectionModel().selectedRows()}))

            try:
                QTest.keyPress(dialog.table, Qt.Key.Key_Shift)
                QTest.mouseClick(cards_row4[0], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
            finally:
                QTest.keyRelease(dialog.table, Qt.Key.Key_Shift)
            self.assertEqual([2, 3, 4], sorted({i.row() for i in dialog.table.selectionModel().selectedRows()}))

            dialog.details_panel.staging_queue.timer.stop()
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_large_table_uses_viewport_hydration_without_creating_all_widgets(self):
        from src.services.hash_deduplicator import DuplicateGroup, MODE_ALL_VS_ALL

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                   return_value=str(Path(tmpdir) / 'missing.png')):
            dialog = HashDedupDialog(None, indexer=SimpleNamespace(db_name=str(Path(tmpdir) / 'x.db')))
            groups = []
            for i in range(1200):
                k = dict(drive_id=f'k{i}', name=f'k{i}.jpg', path=rf'L:\Drives Compartilhados\Banco de Imagens\2002\k{i}.jpg', rel_path=rf'2002\k{i}.jpg', depth=1, description='2002', mimeType='image/jpeg')
                d = dict(drive_id=f'd{i}', name=f'd{i}.jpg', path=rf'L:\Drives Compartilhados\Banco de Imagens\Organizar\d{i}.jpg', rel_path=rf'Organizar\d{i}.jpg', depth=1, description='', mimeType='image/jpeg')
                groups.append(DuplicateGroup(f'{i:032x}', 100 + i, k, [d], '2002', True, {f'k{i}', f'd{i}'}, MODE_ALL_VS_ALL))

            dialog.groups = groups
            dialog._populate_table()
            while dialog._render_timer.isActive():
                self.app.processEvents()
            dialog.show()
            dialog.resize(1200, 600)
            self.app.processEvents()
            dialog._hydrate_visible_rows()

            self.assertEqual(1200, dialog.table.rowCount())
            self.assertLess(len(dialog._hydrated_rows), 80)

            dialog._navigate_to_instance(900, 'duplicate', 0, target_col=3)
            QTest.qWait(35)
            self.assertIn(900, dialog._hydrated_rows)
            self.assertNotIn(0, dialog._hydrated_rows)

            dialog.details_panel.staging_queue.timer.stop()
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_sibling_cached_thumbnail_used_for_col1_and_keeper_details_without_reading_original(self):
        from src.services.hash_deduplicator import DuplicateGroup, MODE_SELECTED_FOLDERS
        from src.ui.thumbnails import ThumbnailCache

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cache_dir = tmp / 'thumb_cache'
            cache_dir.mkdir()
            keeper = dict(
                drive_id='k0', name='k.jpg', path=r'C:\acervo\2002\k.jpg',
                rel_path=r'2002\k.jpg', depth=1, description='2002',
                mimeType='image/jpeg', source='local', modifiedTime=10, size=500,
            )
            dup = dict(
                drive_id='d0', name='d.jpg', path=r'C:\acervo\Organizar\d.jpg',
                rel_path=r'Organizar\d.jpg', depth=1, description='',
                mimeType='image/jpeg', source='local', modifiedTime=20, size=500,
            )
            grp = DuplicateGroup('f' * 32, 500, keeper, [dup], '2002', True, {'k0', 'd0'}, MODE_SELECTED_FOLDERS)

            with patch('src.ui.thumbnails.THUMBNAIL_CACHE_DIR', str(cache_dir)):
                # Apenas a cópia duplicada possui miniatura gerada no cache
                dup_thumb_path = Path(ThumbnailCache.get_thumbnail_cache_path(dup, 'png'))
                Image.new('RGB', (64, 64), color=(180, 60, 40)).save(dup_thumb_path, format='PNG')

                dialog = HashDedupDialog(None, indexer=SimpleNamespace(db_name=str(tmp / 'x.db')))
                dialog.groups = [grp]
                dialog._populate_table()
                while dialog._render_timer.isActive():
                    self.app.processEvents()

                # Coluna 1 (Miniatura) deve exibir a miniatura vinda da cópia em cache
                self.assertFalse(dialog.table.item(0, 1).icon().isNull())

                # Inspecionar o keeper (que não tem cache próprio) deve exibir a miniatura da cópia irmã sem gerar do original
                with patch('src.ui.details_panel.ThumbnailManager.generate_local_thumbnail') as generate_orig, \
                     patch('src.ui.details_panel.ThumbnailManager.get_generic_thumbnail') as generic_mock:
                    generic_mock.side_effect = lambda *a, **kw: ThumbnailCache and __import__('PyQt6.QtGui', fromlist=['QPixmap']).QPixmap(10, 10)
                    dialog._on_table_cell_clicked(0, 2)
                    self.app.processEvents()
                    generate_orig.assert_not_called()
                    pm = dialog.details_panel.thumbnail_label.pixmap()
                    self.assertIsNotNone(pm)
                    self.assertFalse(pm.isNull())
                    # Deve ser a miniatura real (~300x300 do cache), não o placeholder 10x10
                    self.assertGreater(pm.width(), 100)

                dialog.details_panel.staging_queue.timer.stop()
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
