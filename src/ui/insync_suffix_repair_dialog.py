"""Tela de revisão e execução para corrigir sufixos locais '(n)' deixados pelo InSync."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.services.insync_suffix_repair import (
    InsyncSuffixItem,
    InsyncSuffixRepairWorker,
    PendingTempJournal,
)
from src.ui.review_table import ReviewTable
from src.utils.config_manager import ConfigManager


class InsyncSuffixRepairDialog(QDialog):
    def __init__(self, parent=None, indexer=None, drive_service=None):
        super().__init__(parent)
        self.db_name = str(Path(indexer.db_name if indexer else 'data/file_index.db').resolve())
        self.indexer = indexer
        self.service = drive_service
        self.items: list[InsyncSuffixItem] = []
        self.selected: set[str] = set()
        self.thread: QThread | None = None
        self.worker: InsyncSuffixRepairWorker | None = None
        self.busy = False
        self.maintenance = False

        self.setWindowTitle('Corrigir sufixos (n) do InSync')
        self.resize(1160, 700)

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Localiza arquivos no computador que ficaram com sufixo "(2)", "(3)", etc. deixado pelo InSync '
            'mesmo quando no Google Drive só existe 1 arquivo com o nome limpo e conteúdo idêntico. '
            'Para cada item aprovado, o programa renomeia no Drive para um nome temporário, aguarda o InSync '
            'atualizar no HD e então devolve o nome original — preservando ID, tags e histórico. '
            'Se houver 2 ou mais arquivos com o mesmo nome na pasta do Drive, o item fica bloqueado para o Desambiguador.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        top_row = QHBoxLayout()
        self.analyze_btn = QPushButton('Analisar arquivos (n)')
        self.cancel_btn = QPushButton('Cancelar')
        self.cancel_btn.setEnabled(False)
        self.restore_btn = QPushButton('Restaurar temporários pendentes')
        self.timeout_label = QLabel('Espera máx. InSync (s):')
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(15, 300)
        self.timeout_spin.setValue(90)
        self.timeout_spin.setToolTip('Tempo máximo aguardando o InSync refletir cada renomeação no HD.')
        for w in (self.analyze_btn, self.cancel_btn, self.restore_btn):
            top_row.addWidget(w)
        top_row.addStretch()
        top_row.addWidget(self.timeout_label)
        top_row.addWidget(self.timeout_spin)
        layout.addLayout(top_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status_label = QLabel('A análise é somente leitura e não altera nada no Drive nem no HD.')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = ReviewTable(0, 5)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setHorizontalHeaderLabels([
            'Aplicar', 'Pasta', 'Nome atual no HD', 'Nome no Drive (final)', 'Estado / motivo',
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((60, 260, 250, 250, 280)):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)

        sel_row = QHBoxLayout()
        self.select_all_btn = QPushButton('Marcar elegíveis')
        self.select_none_btn = QPushButton('Desmarcar todos')
        self.mark_sel_btn = QPushButton('Marcar seleção')
        self.unmark_sel_btn = QPushButton('Desmarcar seleção')
        self.summary_label = QLabel('Nenhum item analisado.')
        for w in (self.select_all_btn, self.select_none_btn, self.mark_sel_btn, self.unmark_sel_btn):
            sel_row.addWidget(w)
        sel_row.addStretch()
        sel_row.addWidget(self.summary_label)
        layout.addLayout(sel_row)

        footer = QHBoxLayout()
        self.execute_btn = QPushButton('Executar selecionados')
        self.close_btn = QPushButton('Fechar')
        footer.addWidget(self.execute_btn)
        footer.addStretch()
        footer.addWidget(self.close_btn)
        layout.addLayout(footer)

        self.analyze_btn.clicked.connect(lambda: self._start_worker('scan'))
        self.cancel_btn.clicked.connect(self._cancel_worker)
        self.restore_btn.clicked.connect(self._confirm_restore)
        self.select_all_btn.clicked.connect(lambda: self._set_all_eligible(True))
        self.select_none_btn.clicked.connect(lambda: self._set_all_eligible(False))
        self.mark_sel_btn.clicked.connect(lambda: self.table.set_selected_checked(True))
        self.unmark_sel_btn.clicked.connect(lambda: self.table.set_selected_checked(False))
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        self.execute_btn.clicked.connect(self._confirm_execute)
        self.close_btn.clicked.connect(self.accept)

        self._refresh_buttons()

    def _main_window(self):
        return self.parent().window() if self.parent() else None

    def _pending_temp_count(self) -> int:
        return len(PendingTempJournal(self.db_name).load())

    def _refresh_buttons(self):
        pending_temp = self._pending_temp_count()
        self.restore_btn.setVisible(pending_temp > 0)
        self.restore_btn.setText(f'Restaurar temporários pendentes ({pending_temp})')
        self.restore_btn.setEnabled((not self.busy) and pending_temp > 0 and bool(self.service))

        can_edit = not ConfigManager().is_read_only()
        has_eligible = any(it.status == 'eligible' for it in self.items)
        self.analyze_btn.setEnabled((not self.busy) and bool(self.service))
        self.cancel_btn.setEnabled(self.busy)
        self.timeout_spin.setEnabled(not self.busy)
        self.select_all_btn.setEnabled((not self.busy) and has_eligible)
        self.select_none_btn.setEnabled((not self.busy) and bool(self.selected))
        self.mark_sel_btn.setEnabled((not self.busy) and has_eligible)
        self.unmark_sel_btn.setEnabled((not self.busy) and has_eligible)
        self.execute_btn.setEnabled((not self.busy) and can_edit and bool(self.selected) and bool(self.service))
        self.close_btn.setEnabled(not self.busy)

        eligible_cnt = sum(1 for it in self.items if it.status == 'eligible')
        disambig_cnt = sum(1 for it in self.items if it.status == 'needs_disambiguation')
        done_cnt = sum(1 for it in self.items if it.status == 'completed')
        blocked_cnt = sum(1 for it in self.items if it.status in ('blocked', 'failed', 'rolled_back'))
        if not self.items:
            self.summary_label.setText('Nenhum item analisado.')
        else:
            self.summary_label.setText(
                f'Total: {len(self.items)} · Elegíveis: {eligible_cnt} · Selecionados: {len(self.selected)} · '
                f'Concluídos: {done_cnt} · Desambiguar: {disambig_cnt} · Bloqueados: {blocked_cnt}'
            )

    def _render_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            key = item.local_path
            chk = QTableWidgetItem()
            chk.setData(Qt.ItemDataRole.UserRole, key)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if item.status == 'eligible':
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            chk.setFlags(flags)
            chk.setCheckState(
                Qt.CheckState.Checked if (item.status == 'eligible' and key in self.selected)
                else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, QTableWidgetItem(item.rel_folder or '(raiz)'))
            self.table.setItem(row, 2, QTableWidgetItem(item.local_name))
            self.table.setItem(row, 3, QTableWidgetItem(item.drive_name or item.base_name))

            badge = {
                'eligible': 'Pronto',
                'needs_disambiguation': 'Desambiguar primeiro',
                'completed': 'Concluído',
                'rolled_back': 'Revertido',
                'failed': 'Falha',
                'blocked': 'Bloqueado',
            }.get(item.status, item.status)
            state_item = QTableWidgetItem(f'{badge}: {item.reason}')
            if item.status == 'completed':
                state_item.setForeground(QColor('#1B5E20'))
            elif item.status == 'needs_disambiguation':
                state_item.setForeground(QColor('#E65100'))
            elif item.status in ('blocked', 'failed', 'rolled_back'):
                state_item.setForeground(QColor('#B71C1C'))
            self.table.setItem(row, 4, state_item)
        self.table.blockSignals(False)
        self._refresh_buttons()

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self._refresh_buttons()

    def _set_all_eligible(self, checked: bool):
        if checked:
            self.selected = {it.local_path for it in self.items if it.status == 'eligible'}
        else:
            self.selected.clear()
        self._render_table()

    def _on_double_click(self, row: int, _col: int):
        if 0 <= row < len(self.items):
            link = self.items[row].drive_web_link
            if link:
                QDesktopServices.openUrl(QUrl(link))

    def _confirm_execute(self):
        chosen = [it for it in self.items if it.status == 'eligible' and it.local_path in self.selected]
        if not chosen:
            return
        answer = QMessageBox.question(
            self,
            'Confirmar correção de sufixos (n)',
            f'Serão ressincronizados {len(chosen)} arquivo(s) em mini-lotes:\n'
            '1. Renomear temporariamente no Google Drive e aguardar o InSync atualizar no HD;\n'
            '2. Devolver o nome original no Drive e confirmar no HD (preservando ID e tags).\n\n'
            'Um backup local do banco será criado antes. Certifique-se de que o InSync está rodando. Continuar?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_worker('execute', items=chosen)

    def _confirm_restore(self):
        cnt = self._pending_temp_count()
        if cnt <= 0:
            return
        answer = QMessageBox.question(
            self,
            'Restaurar nomes temporários',
            f'Existem {cnt} renomeio(s) temporário(s) pendente(s) de uma execução anterior. '
            'Deseja restaurar os nomes originais no Google Drive agora?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_worker('restore')

    def _start_worker(self, mode: str, *, items=None):
        if self.busy:
            return
        writes = mode in ('execute', 'restore')
        win = self._main_window()
        if writes and win and hasattr(win, 'begin_maintenance_operation'):
            if not win.begin_maintenance_operation():
                return
            self.maintenance = True
        self.busy = True
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText('Iniciando...')
        self._refresh_buttons()

        self.thread = QThread(self)
        self.worker = InsyncSuffixRepairWorker(
            self.db_name,
            self.service,
            mode=mode,
            items=items,
            wait_timeout_sec=float(self.timeout_spin.value()),
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.thread.start()

    def _cancel_worker(self):
        if self.worker:
            self.cancel_btn.setEnabled(False)
            self.status_label.setText('Cancelando com segurança (revertendo pendentes da etapa 1, se houver)...')
            self.worker.cancel()

    def _cleanup_thread(self):
        if self.thread:
            self.thread.quit()
            self.thread.wait(5000)
            self.thread.deleteLater()
            self.thread = None
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        self.busy = False
        if self.maintenance:
            win = self._main_window()
            if win and hasattr(win, 'end_maintenance_operation'):
                win.end_maintenance_operation()
            if win and hasattr(win, '_force_refresh_after_sync'):
                win._force_refresh_after_sync()
            self.maintenance = False
        self._refresh_buttons()

    def _on_progress(self, pct: int, message: str):
        if pct >= 0:
            self.progress.setValue(min(100, pct))
        if message:
            self.status_label.setText(message)

    def _on_finished(self, payload: dict):
        mode = payload.get('mode')
        if mode == 'scan':
            self.items = payload.get('items') or []
            self.selected.clear()
            if payload.get('cancelled'):
                self.status_label.setText('Análise cancelada pelo usuário.')
            else:
                eligible = sum(1 for it in self.items if it.status == 'eligible')
                self.status_label.setText(
                    f'Análise concluída: {len(self.items)} arquivo(s) com sufixo (n), '
                    f'sendo {eligible} elegível(is) para correção segura.'
                )
            self._render_table()
        elif mode == 'restore':
            restored = payload.get('restored', 0)
            remaining = payload.get('remaining', 0)
            errs = payload.get('errors') or []
            self.status_label.setText(
                f'Restauração concluída: {restored} restaurado(s), {remaining} pendente(s).'
            )
            if errs:
                QMessageBox.warning(self, 'Avisos na restauração', '\n'.join(errs[:15]))
        else:
            completed = payload.get('completed', 0)
            rolled = payload.get('rolled_back', 0)
            failed = payload.get('failed', 0)
            skipped = payload.get('skipped', 0)
            cancelled = payload.get('cancelled', False)
            self.selected = {it.local_path for it in self.items if it.status == 'eligible' and it.local_path in self.selected}
            self.status_label.setText(
                f'{"Cancelado" if cancelled else "Concluído"}: {completed} corrigido(s), '
                f'{rolled} revertido(s), {skipped} pulado(s), {failed} falha(s).'
            )
            self._render_table()
            errs = payload.get('errors') or []
            if errs or failed or rolled:
                lines = [
                    f'Corrigidos: {completed}',
                    f'Revertidos no Drive: {rolled}',
                    f'Pulados: {skipped}',
                    f'Falhas: {failed}',
                ]
                if errs:
                    lines.append('\nDetalhes:\n' + '\n'.join(errs[:15]))
                QMessageBox.warning(self, 'Resultado com avisos', '\n'.join(lines))
        self._cleanup_thread()

    def _on_failed(self, error_text: str):
        self.status_label.setText(f'Erro: {error_text}')
        QMessageBox.critical(self, 'Erro na operação', error_text)
        self._cleanup_thread()

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(
                self,
                'Operação em andamento',
                'Aguarde a conclusão ou cancele antes de fechar esta janela.',
            )
            event.ignore()
            return
        super().closeEvent(event)
