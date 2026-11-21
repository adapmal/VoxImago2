"""Persistência e progresso parcial: apenas bancos temporários e Drive simulado."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox
from scripts import test_dedup_pagination as ui_fixture
from scripts import test_hash_dedup as fixture
from src.services.dedup_review import load_review, review_path, save_review
from src.services.hash_deduplicator import DuplicateGroup, HashDedupExecuteWorker, DedupCancelled
from src.services import hash_deduplicator as dedup
from src.ui.hash_dedup_dialog import HashDedupDialog, _InstanceRowButton


class ReviewTests(unittest.TestCase):
    setUpClass = classmethod(ui_fixture.PaginationTests.setUpClass.__func__)
    setUp = ui_fixture.PaginationTests.setUp
    tearDown = ui_fixture.PaginationTests.tearDown
    item = ui_fixture.PaginationTests.item
    populate = ui_fixture.PaginationTests.populate

    def group(self, base=0):
        return DuplicateGroup('a' * 32, 100, self.item(base),
                              [self.item(base + 1), self.item(base + 2)],
                              eligible_ids={str(i) for i in range(base, base + 3)})

    def finish_render(self):
        while self.dialog._render_timer.isActive():
            self.app.processEvents()

    def reopen(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.dialog = HashDedupDialog(indexer=SimpleNamespace(db_name='unused.db'))
        self.finish_render()

    def test_close_reopen_keeps_manual_tags_selection_scope_and_members(self):
        group = self.group()
        group.edited_description = 'tags manuais, Pará'
        group.checked = False
        self.dialog.custom_list.addItems(['X:\\A', 'X:\\B'])
        self.populate([group])
        self.reopen()
        restored = self.dialog.groups[0]
        self.assertEqual(group, restored)
        self.assertEqual(2, self.dialog.custom_list.count())
        self.assertFalse(self.dialog._plan_stale)
        self.assertFalse(self.dialog._review_load_failed)

    def test_remove_only_confirmed_copies_keep_pending_and_manual_edits(self):
        done, partial, untouched = self.group(), self.group(10), self.group(20)
        partial.edited_description = 'manuais'
        untouched.checked = False
        self.populate([done, partial, untouched])
        self.dialog._execution_in_flight = ['0', '10', '20']
        self.dialog._retain_pending_results(dict(
            completed_groups=['0'], completed_moves=['1', '2', '11'],
            review_groups={'10': 'Cancelado'}, keeper_updates={'10': {'description': 'manuais'}}))
        self.finish_render()
        self.assertEqual(['10', '20'], [g.keeper['drive_id'] for g in self.dialog.groups])
        self.assertEqual(['12'], [d['drive_id'] for d in partial.duplicates])
        self.assertEqual('manuais', partial.edited_description)
        self.assertFalse(partial.checked)
        self.assertTrue(partial.plan_error)
        self.assertEqual(untouched, self.dialog.groups[1])
        self.reopen()
        self.assertEqual(['12'], [d['drive_id'] for d in self.dialog.groups[0].duplicates])
        self.assertTrue(self.dialog.groups[0].plan_error)

    def test_crash_marker_restores_blocked_not_executable(self):
        group = self.group()
        save_review('unused.db', [group], self.dialog._review_settings(), in_flight=['0']).result()
        # Simular outra abertura sem o fechamento normal sobrescrever o marcador.
        self.dialog._restore_review()
        self.finish_render()
        self.assertFalse(self.dialog.groups[0].checked)
        self.assertIn('interrompida', self.dialog.groups[0].plan_error)
        self.assertFalse(self.dialog.btn_execute.isEnabled())

    def test_fatal_error_blocks_submitted_but_keeps_untouched(self):
        self.populate([self.group(), self.group(10)])
        self.dialog._execution_in_flight = ['0']
        self.dialog._retain_pending_results({}, uncertain=True)
        self.finish_render()
        self.assertTrue(self.dialog.groups[0].plan_error)
        self.assertFalse(self.dialog.groups[1].plan_error)

    def test_scope_change_preserved_and_stale_after_reopen(self):
        self.populate([self.group()])
        self.dialog.dest_edit.setText('X:\\Outro')
        self.reopen()
        self.assertEqual(1, len(self.dialog.groups))
        self.assertTrue(self.dialog._plan_stale)
        self.assertFalse(self.dialog.btn_execute.isEnabled())

    def test_clear_is_explicit_and_persisted(self):
        self.populate([self.group()])
        with patch('src.ui.hash_dedup_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog.btn_clear_results.click()
        self.finish_render()
        self.reopen()
        self.assertEqual([], self.dialog.groups)

    def test_failed_scan_does_not_discard_previous_review(self):
        self.populate([self.group()])
        with patch('src.ui.hash_dedup_dialog.QMessageBox.warning'):
            self.dialog._on_scan_failed('rede indisponível')
        self.assertEqual(1, len(self.dialog.groups))

    def test_red_cells_and_cards_only_for_discrepancy(self):
        root = r'X:\Banco de Imagens'
        disc_keeper = dict(self.item(0), path=root + r'\2005\0.jpg',
                           imageMediaMetadata={'time': '2008:01:01 10:00:00'})
        disc_dup = dict(self.item(1), name='festa_2012.jpg', path=root + r'\2012\festa_2012.jpg')
        group = DuplicateGroup('a' * 32, 100, disc_keeper, [disc_dup],
                               eligible_ids={'0', '1'}, plan_root=root,
                               year_context=[disc_keeper, disc_dup])
        ok_keeper = dict(self.item(10), name='foto_2005.jpg', path=root + r'\2005\foto_2005.jpg',
                         imageMediaMetadata={'time': '2005:01:01 10:00:00'})
        ok_dup = dict(self.item(11), name='copia_ate_2010.jpg', path=root + r'\Organizar\copia_ate_2010.jpg')
        ok_group = DuplicateGroup('b' * 32, 100, ok_keeper, [ok_dup],
                                  eligible_ids={'10', '11'}, plan_root=root,
                                  year_context=[ok_keeper, ok_dup])
        self.populate([group, ok_group])
        for column in range(6):
            self.assertEqual('#642b31', self.dialog.table.item(0, column).background().color().name())
        self.dialog._ensure_row_hydrated(0)
        for column in (2, 3):
            cards = self.dialog.table.cellWidget(0, column).findChildren(_InstanceRowButton)
            self.assertTrue(cards)
            self.assertTrue(all(c.property('discrepancy') == 'true' for c in cards))
        self.assertNotEqual('#642b31', self.dialog.table.item(1, 0).background().color().name())

    def test_col0_rapid_double_click_toggles_outside_checkbox(self):
        from PyQt6.QtCore import QEvent, QPointF
        from PyQt6.QtGui import QMouseEvent
        self.populate([self.group()])
        self.finish_render()
        vp = self.dialog.table.viewport()
        rect = self.dialog.table.visualItemRect(self.dialog.table.item(0, 0))
        outside = QPoint(rect.right() - 4, rect.center().y())
        QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, outside)
        QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, outside)
        self.assertEqual(Qt.CheckState.Unchecked, self.dialog.table.item(0, 0).checkState())
        dbl = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(outside), QPointF(vp.mapToGlobal(outside)),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, dbl)
        QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, outside)
        self.assertEqual(Qt.CheckState.Checked, self.dialog.table.item(0, 0).checkState())

    def test_invalid_review_is_not_overwritten_on_close(self):
        path = review_path('unused.db')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken json', encoding='utf-8')
        self.dialog._restore_review()
        self.assertTrue(self.dialog._review_load_failed)
        self.dialog.close()
        self.assertEqual('{broken json', path.read_text(encoding='utf-8'))

    def test_writer_failure_keeps_previous_atomic_save(self):
        self.populate([self.group()])
        self.dialog._save_review(wait=True)
        previous = review_path('unused.db').read_bytes()
        with patch('src.services.dedup_review.atomic_json', side_effect=OSError('disco cheio')):
            with self.assertRaises(OSError):
                save_review('unused.db', [], self.dialog._review_settings()).result()
        self.assertEqual(previous, review_path('unused.db').read_bytes())

    def test_invalid_settings_rejected_before_widgets_change(self):
        self.populate([self.group()])
        self.dialog._save_review(wait=True)
        path = review_path('unused.db')
        data = json.loads(path.read_text(encoding='utf-8'))
        data['settings']['mode'] = -1
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_review('unused.db')

    def test_col0_click_cannot_check_blocked_plan_error_row(self):
        blocked = self.group()
        blocked.plan_error = 'Conflito de ano.'
        blocked.checked = True
        self.populate([blocked])
        self.finish_render()
        item = self.dialog.table.item(0, 0)
        self.assertFalse(blocked.checked)
        self.assertEqual(Qt.CheckState.Unchecked, item.checkState())
        rect = self.dialog.table.visualItemRect(item)
        vp = self.dialog.table.viewport()
        for pos in (rect.center(), QPoint(rect.right() - 2, rect.center().y())):
            QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
            self.assertFalse(blocked.checked)
            self.assertEqual(Qt.CheckState.Unchecked, item.checkState())
            self.assertFalse(self.dialog.btn_execute.isEnabled())
        item.setCheckState(Qt.CheckState.Checked)
        self.assertFalse(blocked.checked)
        self.assertEqual(Qt.CheckState.Unchecked, item.checkState())
        self.assertFalse(self.dialog.btn_execute.isEnabled())

    def test_initial_folders_preserved_without_losing_saved_review(self):
        group = self.group()
        group.edited_description = 'tags salvas'
        self.dialog.radio_all.setChecked(True)
        self.populate([group])
        self.finish_render()
        self.dialog.details_panel.staging_queue.timer.stop()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

        self.dialog = HashDedupDialog(indexer=SimpleNamespace(db_name='unused.db'),
                                      initial_folders=[r'X:\NovaPasta'])
        self.finish_render()
        self.assertTrue(self.dialog.radio_selected.isChecked())
        self.assertEqual([r'X:\NovaPasta'],
                         [self.dialog.custom_list.item(i).text() for i in range(self.dialog.custom_list.count())])
        self.assertEqual(1, len(self.dialog.groups))
        self.assertEqual('tags salvas', self.dialog.groups[0].edited_description)
        self.assertTrue(self.dialog._plan_stale)
        self.assertFalse(self.dialog.btn_execute.isEnabled())

        # JSON corrompido continua protegido mesmo ao abrir com pastas iniciais.
        self.dialog.details_panel.staging_queue.timer.stop()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        path = review_path('unused.db')
        path.write_text('{broken json', encoding='utf-8')
        self.dialog = HashDedupDialog(indexer=SimpleNamespace(db_name='unused.db'),
                                      initial_folders=[r'X:\OutraPasta'])
        self.finish_render()
        self.assertTrue(self.dialog._review_load_failed)
        self.assertTrue(self.dialog.radio_selected.isChecked())
        self.assertEqual([r'X:\OutraPasta'],
                         [self.dialog.custom_list.item(i).text() for i in range(self.dialog.custom_list.count())])
        self.dialog.close()
        self.assertEqual('{broken json', path.read_text(encoding='utf-8'))

    def test_swap_keeper_updates_red_discrepancy_and_tags_live(self):
        root = r'X:\Banco de Imagens'
        k_2005 = dict(self.item(0), name='foto.jpg', path=root + r'\2005\foto.jpg',
                      rel_path=r'2005\foto.jpg', imageMediaMetadata={'time': '2008:05:01 10:00:00'})
        d_2008 = dict(self.item(1), name='foto.jpg', path=root + r'\2008\foto.jpg',
                      rel_path=r'2008\foto.jpg', imageMediaMetadata={'time': '2008:05:01 10:00:00'})
        group = DuplicateGroup('c' * 32, 100, k_2005, [d_2008],
                               eligible_ids={'0', '1'}, plan_root=root,
                               year_context=[k_2005, d_2008])
        group.recompute_merged()
        self.populate([group])
        self.finish_render()
        self.assertEqual('#642b31', self.dialog.table.item(0, 0).background().color().name())
        self.assertIn('DateTaken', self.dialog.table.item(0, 5).text())

        self.dialog.table.selectRow(0)
        self.dialog._swap_selected_keeper()
        self.assertEqual('1', self.dialog.groups[0].keeper['drive_id'])
        self.assertNotEqual('#642b31', self.dialog.table.item(0, 0).background().color().name())
        self.assertNotIn('DateTaken', self.dialog.table.item(0, 5).text())


class ExecutionProgressTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.HashDedupTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def test_success_identifies_confirmed_group_and_move(self):
        result = self.fx.execute(self.fx.scan())
        self.assertEqual(['keep'], result['completed_groups'])
        self.assertEqual(['dup'], result['completed_moves'])
        self.assertIn('keep', result['keeper_updates'])

    def test_uncertain_remote_response_not_marked_completed(self):
        self.fx.drive.fail_move_after = True
        result = self.fx.execute(self.fx.scan())
        self.assertEqual([], result['completed_moves'])
        self.assertEqual([], result['completed_groups'])
        self.assertIn('keep', result['review_groups'])

    def test_cancellation_is_not_reported_as_upload_failure(self):
        groups = self.fx.scan()
        with patch.object(HashDedupExecuteWorker, 'execute_group', side_effect=DedupCancelled()):
            result = self.fx.execute(groups)
        self.assertTrue(result['cancelled'])
        self.assertEqual([], result['errors'])
        self.assertIn('keep', result['review_groups'])
        self.assertTrue(result['notes'])
        self.assertEqual([], self.fx.drive.writes)

    def test_local_cleanup_failure_not_safe_to_remove_from_review(self):
        groups = self.fx.scan()
        with patch('src.services.hash_deduplicator.clean_local', side_effect=OSError('falha local')):
            result = self.fx.execute(groups)
        self.assertEqual(1, result['files_moved'])
        self.assertEqual([], result['completed_moves'])
        self.assertIn('keep', result['review_groups'])

    def test_cancel_after_first_move_reports_only_cleaned_copy(self):
        groups = self.fx.scan(dedup.MODE_ALL_VS_ALL, references=[])
        self.assertEqual(2, len(groups[0].duplicates))
        worker = HashDedupExecuteWorker(str(self.fx.db), groups, fixture.DEST, self.fx.drive)
        results, errors = [], []
        worker.execution_finished.connect(results.append)
        worker.execution_failed.connect(errors.append)
        original_clean = dedup.clean_local

        def clean_and_cancel(indexer, payload):
            original_clean(indexer, payload)
            worker.cancel()

        with patch('src.services.hash_deduplicator.clean_local', side_effect=clean_and_cancel):
            worker.run()
        self.assertEqual([], errors)
        result = results[0]
        self.assertTrue(result['cancelled'])
        self.assertEqual([], result['errors'])
        self.assertEqual([], result['completed_groups'])
        self.assertEqual(1, len(result['completed_moves']))
        moved = result['completed_moves'][0]
        self.assertEqual('dst', self.fx.drive.items[moved]['driveId'])
        remaining = next(d['drive_id'] for d in groups[0].duplicates if d['drive_id'] != moved)
        self.assertEqual('src', self.fx.drive.items[remaining]['driveId'])
        self.assertIn('keep', result['review_groups'])


if __name__ == '__main__':
    unittest.main()
