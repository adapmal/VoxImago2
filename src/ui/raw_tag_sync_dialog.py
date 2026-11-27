"""Tela de revisão e execução direta para sincronizar tags entre arquivos RAW e suas cópias (JPG/PNG/HEIC/etc.)."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.services.insync_suffix_repair import _resolve_active_roots_and_drive
from src.services.raw_tag_sync import RawTagSyncGroup, RawTagSyncWorker, compute_bidirectional_tags
from src.ui.review_table import ReviewTable
from src.utils.config_manager import ConfigManager


class RawTagSyncDialog(QDialog):
    def __init__(self, parent=None, indexer=None, drive_service=None):
        super().__init__(parent)
        self.db_name = str(Path(indexer.db_name if indexer else 'data/file_index.db').resolve())
        self.indexer = indexer
        self.service = drive_service
        self.groups: list[RawTagSyncGroup] = []
        self.selected: set[str] = set()
        self.thread: QThread | None = None
        self.worker: RawTagSyncWorker | None = None
        self.busy = False
        self.maintenance = False

        self.setWindowTitle('Sincronizar Tags RAW ↔ Cópias (JPG/PNG/HEIC)')
        self.resize(1280, 760)

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Localiza pares e grupos de arquivos RAW (.ARW, .CR2, .DNG, .RAF, etc.) e suas respectivas cópias '
            '(.JPG, .JPEG, .PNG, .HEIC, etc.) na mesma pasta ou em subpastas de formato do mesmo evento '
            '(como "RAW", "Arquivo Bruto Original", "JPG", "Seleção Tratadas") e soma as tags nos dois sentidos. '
            'Também permite incluir a tag "RAW" exclusivamente em todos os arquivos brutos (mesmo sem cópia JPG/PNG), '
            'garantindo que a tag "RAW" nunca seja copiada para arquivos JPG/PNG. '
            'A gravação é feita diretamente no Google Drive com proteção ETag, Diário próprio e atualização imediata do banco local.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel('Escopo (vazio = Banco inteiro):'))
        self.scope_edit = QLineEdit()
        self.scope_edit.setPlaceholderText('Banco de Imagens inteiro (ou escolha uma pasta/ano específico)...')
        self.browse_btn = QPushButton('Escolher pasta...')
        self.clear_scope_btn = QPushButton('Limpar')
        scope_row.addWidget(self.scope_edit, 1)
        scope_row.addWidget(self.browse_btn)
        scope_row.addWidget(self.clear_scope_btn)
        layout.addLayout(scope_row)

        opts_row = QHBoxLayout()
        self.ignore_filenames_chk = QCheckBox('Não copiar tags de nomes de arquivos antigos')
        self.ignore_filenames_chk.setChecked(True)
        self.ignore_filenames_chk.setToolTip(
            'Quando marcado, evita copiar tags como "f466929408.jpeg" ou "IMG_1203.JPG" deixadas pelo deduplicador de um formato para o outro.'
        )
        self.add_raw_tag_chk = QCheckBox('Incluir a tag "RAW" em todos os arquivos brutos (mesmo sem cópia JPG/PNG)')
        self.add_raw_tag_chk.setChecked(True)
        self.add_raw_tag_chk.setToolTip(
            'Adiciona a tag "RAW" exclusivamente em todos os arquivos brutos (.ARW, .CR2, .DNG, .RAF, etc.) do escopo, '
            'sem jamais copiar a tag "RAW" para cópias JPG/PNG/HEIC.'
        )
        opts_row.addWidget(self.ignore_filenames_chk)
        opts_row.addSpacing(18)
        opts_row.addWidget(self.add_raw_tag_chk)
        opts_row.addStretch()
        layout.addLayout(opts_row)

        action_row = QHBoxLayout()
        self.analyze_btn = QPushButton('Analisar arquivos e duplas RAW ↔ Cópias')
        self.cancel_btn = QPushButton('Cancelar')
        self.cancel_btn.setEnabled(False)
        action_row.addWidget(self.analyze_btn)
        action_row.addWidget(self.cancel_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status_label = QLabel('A análise é somente leitura e consulta as descrições atuais direto do Google Drive.')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = ReviewTable(0, 7)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setHorizontalHeaderLabels([
            'Aplicar',
            'Pasta / Evento',
            'Arquivo(s) RAW',
            'Cópia(s) (JPG/PNG)',
            'Tags a somar no RAW',
            'Tags a somar na Cópia',
            'Vínculo / Estado',
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((60, 250, 180, 180, 190, 180, 200)):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)

        sel_row = QHBoxLayout()
        self.select_all_btn = QPushButton('Marcar todos')
        self.select_pairs_btn = QPushButton('Marcar só duplas RAW↔Cópia')
        self.select_none_btn = QPushButton('Desmarcar todos')
        self.mark_sel_btn = QPushButton('Marcar seleção')
        self.unmark_sel_btn = QPushButton('Desmarcar seleção')
        self.summary_label = QLabel('Nenhum grupo analisado.')
        for w in (
            self.select_all_btn,
            self.select_pairs_btn,
            self.select_none_btn,
            self.mark_sel_btn,
            self.unmark_sel_btn,
        ):
            sel_row.addWidget(w)
        sel_row.addStretch()
        sel_row.addWidget(self.summary_label)
        layout.addLayout(sel_row)

        footer = QHBoxLayout()
        self.execute_btn = QPushButton('Sincronizar tags dos selecionados')
        self.close_btn = QPushButton('Fechar')
        footer.addWidget(self.execute_btn)
        footer.addStretch()
        footer.addWidget(self.close_btn)
        layout.addLayout(footer)

        self.browse_btn.clicked.connect(self._choose_scope_folder)
        self.clear_scope_btn.clicked.connect(lambda: self.scope_edit.setText(''))
        self.ignore_filenames_chk.toggled.connect(self._recompute_preview_tags)
        self.add_raw_tag_chk.toggled.connect(self._on_add_raw_tag_toggled)
        self.analyze_btn.clicked.connect(lambda: self._start_worker('scan'))
        self.cancel_btn.clicked.connect(self._cancel_worker)
        self.select_all_btn.clicked.connect(lambda: self._set_all_eligible(True))
        self.select_pairs_btn.clicked.connect(self._select_only_pairs)
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

    def _choose_scope_folder(self):
        roots, _ = _resolve_active_roots_and_drive(ConfigManager())
        start_dir = self.scope_edit.text().strip() or (roots[0] if roots else '')
        chosen = QFileDialog.getExistingDirectory(self, 'Escolher pasta do Banco de Imagens', start_dir)
        if chosen:
            self.scope_edit.setText(chosen)

    def _recompute_preview_tags(self):
        if not self.groups or self.busy:
            return
        ignore_fn = self.ignore_filenames_chk.isChecked()
        add_raw = self.add_raw_tag_chk.isChecked()
        for group in self.groups:
            if group.status not in ('eligible', 'blocked'):
                continue
            ordered = group.all_members
            merged_map = compute_bidirectional_tags(
                [m.current_description for m in ordered],
                is_raw_flags=[m.is_raw for m in ordered],
                ignore_filename_tags=ignore_fn,
                add_raw_tag=add_raw,
            )
            for idx, member in enumerate(ordered):
                new_desc, added = merged_map[idx]
                member.proposed_description = new_desc
                member.added_tags = added
        self._render_table()

    def _on_add_raw_tag_toggled(self, checked: bool):
        if not self.groups or self.busy:
            return
        if not checked:
            # Desmarca itens 'raw_only' que não terão mais nenhuma tag a somar
            self.selected = {
                gid for gid in self.selected
                if not any(g.group_id == gid and g.match_kind == 'raw_only' for g in self.groups)
            }
        self._recompute_preview_tags()
        if checked and not any(g.match_kind == 'raw_only' for g in self.groups):
            self.status_label.setText(
                'Opção "Incluir tag RAW" ativada. Clique em "Analisar" novamente para carregar também os arquivos RAW sem cópia JPG/PNG.'
            )

    def _refresh_buttons(self):
        can_edit = not ConfigManager().is_read_only()
        has_eligible = any(g.status == 'eligible' and bool(g.members_needing_update) for g in self.groups)
        has_pairs = any(g.status == 'eligible' and g.match_kind != 'raw_only' and bool(g.members_needing_update) for g in self.groups)
        self.scope_edit.setEnabled(not self.busy)
        self.browse_btn.setEnabled(not self.busy)
        self.clear_scope_btn.setEnabled(not self.busy)
        self.ignore_filenames_chk.setEnabled(not self.busy)
        self.add_raw_tag_chk.setEnabled(not self.busy)
        self.analyze_btn.setEnabled((not self.busy) and bool(self.service))
        self.cancel_btn.setEnabled(self.busy)
        self.select_all_btn.setEnabled((not self.busy) and has_eligible)
        self.select_pairs_btn.setEnabled((not self.busy) and has_pairs)
        self.select_none_btn.setEnabled((not self.busy) and bool(self.selected))
        self.mark_sel_btn.setEnabled((not self.busy) and has_eligible)
        self.unmark_sel_btn.setEnabled((not self.busy) and has_eligible)
        self.execute_btn.setEnabled((not self.busy) and can_edit and bool(self.selected) and bool(self.service))
        self.close_btn.setEnabled(not self.busy)

        pairs_elig = sum(1 for g in self.groups if g.status == 'eligible' and g.match_kind != 'raw_only')
        solo_elig = sum(1 for g in self.groups if g.status == 'eligible' and g.match_kind == 'raw_only')
        done_cnt = sum(1 for g in self.groups if g.status == 'completed')
        blocked_cnt = sum(1 for g in self.groups if g.status in ('blocked', 'failed'))
        files_to_write = sum(
            len(g.members_needing_update)
            for g in self.groups
            if g.status == 'eligible' and g.group_id in self.selected
        )
        if not self.groups:
            self.summary_label.setText('Nenhum item analisado.')
        else:
            ro_hint = ' · ⚠️ Ative o Modo Edição para gravar' if not can_edit else ''
            self.summary_label.setText(
                f'Duplas: {pairs_elig} · RAWs s/ cópia: {solo_elig} · '
                f'Selecionados: {len(self.selected)} ({files_to_write} arquivo(s)) · '
                f'Concluídos: {done_cnt} · Bloqueados: {blocked_cnt}{ro_hint}'
            )

    def _render_table(self):
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(self.groups))
            for row, group in enumerate(self.groups):
                key = group.group_id
                can_check = (group.status == 'eligible' and bool(group.members_needing_update))
                chk = QTableWidgetItem()
                chk.setData(Qt.ItemDataRole.UserRole, key)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if can_check:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                chk.setFlags(flags)
                chk.setCheckState(
                    Qt.CheckState.Checked if (can_check and key in self.selected)
                    else Qt.CheckState.Unchecked
                )
                self.table.setItem(row, 0, chk)
                self.table.setItem(row, 1, QTableWidgetItem(group.event_folder or '(raiz)'))

                raw_names = ', '.join(m.name for m in group.raw_members)
                copy_names = ', '.join(m.name for m in group.copy_members) if group.copy_members else '— (sem cópia)'
                raw_item = QTableWidgetItem(raw_names)
                raw_item.setToolTip('\n'.join(f'{m.name}: {m.current_description or "(sem tags)"}' for m in group.raw_members))
                copy_item = QTableWidgetItem(copy_names)
                if group.copy_members:
                    copy_item.setToolTip('\n'.join(f'{m.name}: {m.current_description or "(sem tags)"}' for m in group.copy_members))
                self.table.setItem(row, 2, raw_item)
                self.table.setItem(row, 3, copy_item)

                raw_added = sorted({t for m in group.raw_members for t in m.added_tags}, key=str.casefold)
                copy_added = sorted({t for m in group.copy_members for t in m.added_tags}, key=str.casefold)
                raw_add_item = QTableWidgetItem(', '.join(raw_added) if raw_added else '—')
                copy_add_item = QTableWidgetItem(', '.join(copy_added) if copy_added else '—')
                if raw_added:
                    raw_add_item.setForeground(QColor('#0D47A1'))
                if copy_added:
                    copy_add_item.setForeground(QColor('#0D47A1'))
                self.table.setItem(row, 4, raw_add_item)
                self.table.setItem(row, 5, copy_add_item)

                badge = {
                    'eligible': 'Pronto',
                    'completed': 'Concluído',
                    'blocked': 'Bloqueado',
                    'failed': 'Falha',
                }.get(group.status, group.status)
                state_item = QTableWidgetItem(f'{badge}: {group.reason}')
                state_item.setToolTip(f'{badge}: {group.reason}')
                if group.status == 'completed':
                    state_item.setForeground(QColor('#1B5E20'))
                elif group.status in ('blocked', 'failed'):
                    state_item.setForeground(QColor('#B71C1C'))
                self.table.setItem(row, 6, state_item)
        finally:
            self.table.setUpdatesEnabled(True)
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
            self.selected = {
                g.group_id for g in self.groups
                if g.status == 'eligible' and bool(g.members_needing_update)
            }
        else:
            self.selected.clear()
        self._render_table()

    def _select_only_pairs(self):
        self.selected = {
            g.group_id for g in self.groups
            if g.status == 'eligible' and g.match_kind != 'raw_only' and bool(g.members_needing_update)
        }
        self._render_table()

    def _on_double_click(self, row: int, col: int):
        if not (0 <= row < len(self.groups)):
            return
        group = self.groups[row]
        target = None
        if col in (3, 5) and group.copy_members:
            target = group.copy_members[0]
        elif group.raw_members:
            target = group.raw_members[0]
        if target and target.web_link:
            QDesktopServices.openUrl(QUrl(target.web_link))

    def _confirm_execute(self):
        chosen = [
            g for g in self.groups
            if g.status == 'eligible' and g.group_id in self.selected and bool(g.members_needing_update)
        ]
        if not chosen:
            return
        files_cnt = sum(len(g.members_needing_update) for g in chosen)
        pairs_cnt = sum(1 for g in chosen if g.match_kind != 'raw_only')
        solo_cnt = sum(1 for g in chosen if g.match_kind == 'raw_only')
        answer = QMessageBox.question(
            self,
            'Confirmar sincronização de tags RAW ↔ Cópias',
            f'Serão processados {len(chosen)} item(ns) ({pairs_cnt} dupla(s) RAW↔Cópia e {solo_cnt} RAW(s) sem cópia), '
            f'atualizando {files_cnt} arquivo(s) no Google Drive:\n\n'
            '• As descrições são relidas ao vivo no Google Drive com proteção ETag;\n'
            '• As tags são sempre somadas sem apagar nenhuma tag existente;\n'
            '• A tag "RAW" é aplicada exclusivamente nos arquivos brutos e nunca nas cópias JPG/PNG;\n'
            '• Um backup local do banco e registro no Diário próprio serão criados automaticamente.\n\n'
            'Deseja iniciar agora?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_worker('execute', groups=chosen)

    def _start_worker(self, mode: str, *, groups=None):
        if self.busy:
            return
        writes = (mode == 'execute')
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

        add_raw = self.add_raw_tag_chk.isChecked()
        self.thread = QThread(self)
        self.worker = RawTagSyncWorker(
            self.db_name,
            self.service,
            mode=mode,
            scope_folder=self.scope_edit.text().strip(),
            ignore_filename_tags=self.ignore_filenames_chk.isChecked(),
            add_raw_tag=add_raw,
            include_unpaired_raws=add_raw,
            groups=groups,
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
            self.status_label.setText('Cancelando após concluir o item atual...')
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
            self.groups = payload.get('groups') or []
            self.selected = {
                g.group_id for g in self.groups
                if g.status == 'eligible' and bool(g.members_needing_update)
            }
            if payload.get('cancelled'):
                self.status_label.setText('Análise cancelada pelo usuário.')
            else:
                pairs = sum(1 for g in self.groups if g.status == 'eligible' and g.match_kind != 'raw_only')
                solos = sum(1 for g in self.groups if g.status == 'eligible' and g.match_kind == 'raw_only')
                self.status_label.setText(
                    f'Análise concluída: {pairs} dupla(s) RAW↔Cópia e {solos} arquivo(s) RAW sem cópia '
                    f'pronto(s) para atualizar.'
                )
            self._render_table()
        else:
            completed = payload.get('groups_completed', 0)
            files_upd = payload.get('files_updated', 0)
            failed = payload.get('failed', 0)
            cancelled = payload.get('cancelled', False)
            self.selected = {
                g.group_id for g in self.groups
                if g.status == 'eligible' and g.group_id in self.selected and bool(g.members_needing_update)
            }
            self.status_label.setText(
                f'{"Cancelado" if cancelled else "Concluído"}: {completed} item(ns) sincronizado(s), '
                f'{files_upd} arquivo(s) atualizado(s) no Drive e no banco local, {failed} falha(s).'
            )
            self._render_table()
            errs = payload.get('errors') or []
            if errs or failed:
                QMessageBox.warning(
                    self,
                    'Concluído com avisos',
                    f'Itens concluídos: {completed}\nArquivos atualizados: {files_upd}\nFalhas: {failed}\n\n'
                    + '\n'.join(errs[:15]),
                )
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
