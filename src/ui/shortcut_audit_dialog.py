"""Diálogo para pente-fino e exclusão segura de atalhos no Banco de Imagens."""
from datetime import datetime
import html
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.services.shortcut_audit import (
    DRIVE_FILE_URL,
    ShortcutAuditJournal,
    ShortcutAuditWorker,
)
from src.ui.review_table import ReviewTable
from src.utils.config_manager import ConfigManager

_UNCERTAIN_STATES = frozenset({'uncertain', 'writing_tags', 'writing_trash', 'trashed_waiting_local'})
_NON_CHECKABLE_STATES = frozenset({'completed', 'blocked'}) | _UNCERTAIN_STATES


class _FolderLinkLabel(QLabel):
    """Hiperlink clicável para a pasta no Drive dentro de célula mesclada."""

    def __init__(self, folder_path: str, folder_url: str, parent=None):
        super().__init__(parent)
        self.folder_path = folder_path
        self.folder_url = folder_url
        self.setWordWrap(True)
        self.setContentsMargins(6, 4, 6, 4)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if folder_url:
            href = html.escape(folder_url, quote=True)
            label = html.escape(folder_path)
            self.setText(f'<a href="{href}">{label}</a>')
            self.setTextFormat(Qt.TextFormat.RichText)
            self.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self.setOpenExternalLinks(False)
            self.setToolTip(f'{folder_path}\nAbrir pasta no Drive: {folder_url}')
            self.linkActivated.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        else:
            self.setText(folder_path)
            self.setToolTip(folder_path)


class ShortcutAuditDialog(QDialog):
    PAGE_SIZE = 200

    def __init__(self, parent=None, indexer=None, drive_service=None):
        super().__init__(parent)
        self.db_name = indexer.db_name if indexer else 'data/file_index.db'
        self.service = drive_service
        self.plan = None
        self.selected = set()
        self.uncertain_ids = []
        self.page = 0
        self.worker = self.thread = None
        self.busy = self.maintenance = False
        self.setWindowTitle('Pente-fino de atalhos no Banco de Imagens')
        self.resize(1180, 780)

        layout = QVBoxLayout(self)
        note = QLabel(
            'Localiza atalhos no Banco de Imagens. Atalhos com tags só são enviados à Lixeira após '
            'gravar no arquivo real apenas as tags que ainda não existem nele (sempre somando e preservando '
            'as tags originais). Atalhos com tags cujo destino esteja ausente, na Lixeira ou fora do Banco '
            'permanecem bloqueados para revisão manual.'
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        controls = QHBoxLayout()
        self.analyze = QPushButton('Analisar atalhos')
        self.cancel = QPushButton('Cancelar operação')
        self.cancel.setEnabled(False)
        self.history = QComboBox()
        self.resume = QPushButton('Abrir / retomar revisão')
        self.reconcile_button = QPushButton('Conferir / liberar incertas')
        for widget in (self.analyze, self.cancel, self.history, self.resume, self.reconcile_button):
            controls.addWidget(widget)
        layout.addLayout(controls)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel('Pronto para analisar os atalhos do Banco de Imagens (somente leitura).')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.table = ReviewTable(0, 6)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setHorizontalHeaderLabels([
            'Aplicar',
            'Pasta no Drive',
            'Atalho / ID',
            'Tags no atalho',
            'Arquivo real (destino)',
            'Tags a somar / Estado',
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 58)
        self.table.setColumnWidth(1, 260)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 180)
        self.table.setColumnWidth(4, 240)
        layout.addWidget(self.table, 1)

        pages = QHBoxLayout()
        self.previous = QPushButton('← Anteriores')
        self.next = QPushButton('Próximos →')
        self.page_label = QLabel()
        self.select_all = QPushButton('Marcar elegíveis')
        self.select_none = QPushButton('Desmarcar todos')
        self.mark_selected = QPushButton('Marcar seleção')
        self.unmark_selected = QPushButton('Desmarcar seleção')
        for widget in (self.previous, self.page_label, self.next, self.select_all, self.select_none):
            pages.addWidget(widget)
        layout.addLayout(pages)

        selection_controls = QHBoxLayout()
        selection_controls.addWidget(
            QLabel('Shift: intervalo · Ctrl: seleção múltipla · Espaço: alternar Aplicar (página atual)')
        )
        selection_controls.addWidget(self.mark_selected)
        selection_controls.addWidget(self.unmark_selected)
        layout.addLayout(selection_controls)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(90)
        layout.addWidget(self.details)

        footer = QHBoxLayout()
        self.execute = QPushButton('Transferir tags e excluir selecionados (Lixeira)')
        self.close_button = QPushButton('Fechar')
        footer.addWidget(self.execute)
        footer.addStretch()
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        self.analyze.clicked.connect(self._analyze)
        self.resume.clicked.connect(self._resume)
        self.reconcile_button.clicked.connect(self._reconcile)
        self.execute.clicked.connect(self._execute)
        self.cancel.clicked.connect(lambda: self.worker.cancel() if self.worker else None)
        self.close_button.clicked.connect(self.accept)
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        self.select_all.clicked.connect(lambda: self._select(True))
        self.select_none.clicked.connect(lambda: self._select(False))
        self.mark_selected.clicked.connect(lambda: self.table.set_selected_checked(True))
        self.unmark_selected.clicked.connect(lambda: self.table.set_selected_checked(False))
        self.table.itemChanged.connect(self._checked)
        self.table.cellDoubleClicked.connect(self._open_link)

        self._histories()
        self._buttons()

    @staticmethod
    def _is_checkable_row(row: dict) -> bool:
        return row.get('state') not in _NON_CHECKABLE_STATES

    def _histories(self, select_job=None):
        current = select_job if select_job is not None else self.history.currentData()
        self.history.clear()
        if not (Path(self.db_name).resolve().parent / 'shortcut_audit' / 'journal.db').exists():
            return
        journal = ShortcutAuditJournal(self.db_name)
        try:
            for job, created, _drive in journal.jobs():
                self.history.addItem(f'{datetime.fromtimestamp(created):%d/%m %H:%M} — {job[:8]}', job)
        finally:
            journal.close()
        if current:
            idx = self.history.findData(current)
            if idx >= 0:
                self.history.setCurrentIndex(idx)

    def _refresh_uncertain_ids(self):
        if not self.plan:
            self.uncertain_ids = []
            return
        journal = ShortcutAuditJournal(self.db_name)
        try:
            self.uncertain_ids = journal.uncertain_operation_ids(self.plan['id'])
        finally:
            journal.close()

    def _resume(self):
        job = self.history.currentData()
        if not job or self.busy:
            return
        journal = ShortcutAuditJournal(self.db_name)
        try:
            plan = journal.load(job)
        except Exception as exc:
            QMessageBox.warning(self, 'Revisão não carregada', str(exc))
            return
        finally:
            journal.close()
        self._set_plan(plan)

    def _set_plan(self, plan):
        self.plan = plan
        self.page = 0
        self._refresh_uncertain_ids()
        self.selected = {
            r['source']['id'] for r in plan['items']
            if self._is_checkable_row(r) and r.get('state') != 'error'
        }
        self._render()
        issues = plan.get('folder_issues') or []
        summary = (
            f"{plan.get('total_shortcuts', len(plan['items'])):,} atalho(s): "
            f"{plan.get('tags_to_add', 0):,} com tags a somar; "
            f"{plan.get('already_on_target', 0):,} já com todas as tags no arquivo real; "
            f"{plan.get('without_tags', 0):,} sem tags; "
            f"{plan.get('blocked', 0):,} bloqueado(s)."
        )
        if plan.get('name_conflicts'):
            summary += f" {plan['name_conflicts']:,} destrava(m) colisão de nome na pasta."
        if self.uncertain_ids:
            summary += f" {len(self.uncertain_ids):,} operação(ões) incerta(s) — clique em Conferir / liberar incertas."
        if issues:
            summary += f' ({len(issues):,} pasta(s) com hierarquia incompleta isolada(s).)'
        self.status.setText(summary)

    @staticmethod
    def _format_target_cell(row: dict) -> str:
        target = row.get('target')
        if target:
            lines = [f"{target.get('name') or row['target_id']} ({row['target_id']})"]
            if row.get('target_path'):
                lines.append('Pasta: ' + row['target_path'])
            existing = ', '.join(row.get('existing_target_tags') or [])
            lines.append('Tags atuais: ' + (existing or '(nenhuma)'))
            return '\n'.join(lines)
        status_map = {
            'not_found': 'Não encontrado (404)',
            'trashed': 'Arquivo de destino na Lixeira',
            'no_access': 'Sem permissão de leitura (403)',
            'outside_drive': 'Fora do Banco de Imagens',
            'missing_target_id': 'Sem ID de destino',
            'lookup_error': 'Falha ao consultar destino',
        }
        base = status_map.get(row.get('target_status'), 'Indisponível')
        return f"{base} ({row['target_id']})" if row.get('target_id') else base

    @staticmethod
    def _format_action_cell(row: dict) -> str:
        if row.get('error'):
            return row['error']
        state = row.get('state')
        if state == 'completed':
            return 'Concluído (atalho na Lixeira)'
        if state == 'tags_done':
            return 'Tags já gravadas no arquivo real; pronto para enviar atalho à Lixeira'
        if state in _UNCERTAIN_STATES:
            return 'Conferir resposta anterior no Drive antes de continuar'
        prefix = '[Destrava colisão de nome] ' if row.get('has_name_conflict') else ''
        if row.get('missing_tags'):
            return prefix + 'Somar tags: ' + ', '.join(row['missing_tags']) + ' → Lixeira'
        if row.get('shortcut_tags'):
            return prefix + 'Tags já presentes no arquivo real → Lixeira'
        return prefix + 'Sem tags → enviar apenas o atalho à Lixeira'

    def _render(self):
        rows = self.plan['items'] if self.plan else []
        start = self.page * self.PAGE_SIZE
        visible = rows[start:start + self.PAGE_SIZE]
        self.table.blockSignals(True)
        self.table.clearSpans()
        self.table.setRowCount(0)

        for local, row in enumerate(visible):
            self.table.insertRow(local)
            sc = row['source']
            chk = QTableWidgetItem()
            chk.setData(Qt.ItemDataRole.UserRole, sc['id'])
            chk.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            if self._is_checkable_row(row):
                chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(
                Qt.CheckState.Checked if sc['id'] in self.selected else Qt.CheckState.Unchecked
            )
            self.table.setItem(local, 0, chk)

            folder_item = QTableWidgetItem('')
            folder_item.setData(Qt.ItemDataRole.UserRole, row.get('folder_path') or '')
            folder_item.setData(Qt.ItemDataRole.UserRole + 1, row.get('folder_url') or '')
            folder_item.setToolTip(row.get('folder_path') or '')
            self.table.setItem(local, 1, folder_item)

            sc_label = f"{sc.get('name', '')}\n{sc['id']}"
            if row.get('has_name_conflict'):
                sc_label += '\n[colisão de nome na pasta]'
            tags_label = ', '.join(row.get('shortcut_tags') or []) or '(sem tags)'
            target_label = self._format_target_cell(row)
            action_label = self._format_action_cell(row)
            for col, text in enumerate((sc_label, tags_label, target_label, action_label), 2):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                self.table.setItem(local, col, cell)

        local = 0
        while local < len(visible):
            pid = visible[local].get('parent_id') or ''
            run_end = local + 1
            if pid:
                while run_end < len(visible) and (visible[run_end].get('parent_id') or '') == pid:
                    run_end += 1
            run_len = run_end - local
            if run_len > 1:
                self.table.setSpan(local, 1, run_len, 1)
            link_widget = _FolderLinkLabel(
                visible[local].get('folder_path') or '',
                visible[local].get('folder_url') or '',
            )
            self.table.setCellWidget(local, 1, link_widget)
            local = run_end

        self.table.blockSignals(False)
        self.page_label.setText(
            f'{min(start + 1, len(rows))}–{min(start + self.PAGE_SIZE, len(rows))} de {len(rows):,}'
        )
        self._buttons()

    def _open_link(self, row_idx, column):
        index = self.page * self.PAGE_SIZE + row_idx
        if not self.plan or self.busy or not (0 <= index < len(self.plan['items'])):
            return
        row = self.plan['items'][index]
        if column == 1 and row.get('folder_url'):
            QDesktopServices.openUrl(QUrl(row['folder_url']))
        elif column == 2:
            QDesktopServices.openUrl(QUrl(DRIVE_FILE_URL.format(row['source']['id'])))
        elif column == 4 and row.get('target_id'):
            QDesktopServices.openUrl(QUrl(DRIVE_FILE_URL.format(row['target_id'])))

    def _checked(self, item):
        if item.column() != 0 or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        fid = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(fid)
        else:
            self.selected.discard(fid)
        self._buttons()

    def _select(self, enabled):
        self.selected = (
            {r['source']['id'] for r in self.plan['items'] if self._is_checkable_row(r)}
            if enabled and self.plan else set()
        )
        self._render()

    def _change_page(self, delta):
        self.page = max(0, self.page + delta)
        self._render()

    def _buttons(self):
        for widget in (
            self.analyze,
            self.history,
            self.resume,
            self.table,
            self.select_all,
            self.select_none,
            self.mark_selected,
            self.unmark_selected,
            self.close_button,
        ):
            widget.setEnabled(not self.busy)
        self.cancel.setEnabled(self.busy)
        count = len(self.plan['items']) if self.plan else 0
        self.previous.setEnabled(not self.busy and self.page > 0)
        self.next.setEnabled(not self.busy and (self.page + 1) * self.PAGE_SIZE < count)
        cfg = ConfigManager()
        writable_prod = not cfg.is_read_only() and not cfg.is_sandbox()
        self.reconcile_button.setEnabled(
            not self.busy and bool(self.plan) and bool(self.uncertain_ids) and writable_prod
        )
        self.execute.setEnabled(
            not self.busy and bool(self.plan) and bool(self.selected)
            and not self.uncertain_ids and writable_prod
        )

    def _analyze(self):
        self._start(mode='scan')

    def _execute(self):
        if not self.execute.isEnabled():
            return
        to_tag = sum(
            1 for r in self.plan['items']
            if r['source']['id'] in self.selected and r.get('missing_tags')
        )
        if QMessageBox.question(
            self,
            'Confirmar limpeza de atalhos',
            f'Processar {len(self.selected):,} atalho(s) selecionado(s)?\n\n'
            f'• {to_tag:,} receberão soma de tags ausentes no arquivo real antes de enviar o atalho à Lixeira.\n'
            '• As tags já existentes no arquivo real são preservadas e nada será excluído permanentemente '
            '(atalhos vão para a Lixeira do Drive).\n'
            '• O índice local será atualizado somente por drive_id.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start(mode='execute', job=self.plan['id'], selected=list(self.selected))

    def _reconcile(self):
        if not self.reconcile_button.isEnabled():
            return
        self._start(mode='reconcile', job=self.plan['id'], selected=list(self.uncertain_ids))

    def _start(self, **args):
        if self.busy:
            return
        if not self.service:
            QMessageBox.warning(
                self,
                'Login necessário',
                'Conecte o aplicativo ao Google Drive antes de analisar ou executar.',
            )
            return
        parent = self.parent()
        if parent and hasattr(parent, 'begin_maintenance_operation'):
            if not parent.begin_maintenance_operation():
                return
            self.maintenance = True
        self.busy = True
        self._buttons()
        self.progress.setRange(0, 0)
        self.thread = QThread(self)
        self.worker = ShortcutAuditWorker(self.service, self.db_name, **args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.thread.finished.connect(self.worker.deleteLater)
        self.worker.progress_update.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.thread.start()

    def _progress(self, percent, text):
        self.progress.setRange(0, 0 if percent < 0 else 100)
        if percent >= 0:
            self.progress.setValue(percent)
        self.status.setText(text)

    def _cleanup(self):
        if self.thread:
            self.thread.quit()
            if not self.thread.wait(1500):
                return False
            self.thread.deleteLater()
        self.thread = self.worker = None
        self.busy = False
        if self.maintenance:
            self.parent().end_maintenance_operation()
            self.maintenance = False
        self.progress.setRange(0, 100)
        self._buttons()
        return True

    def _refresh_parent_catalog(self):
        parent = self.parent()
        if not parent:
            return
        if hasattr(parent, '_force_refresh_after_sync'):
            parent._force_refresh_after_sync()
            return
        if hasattr(parent, 'indexer') and hasattr(parent.indexer, 'clear_query_cache'):
            parent.indexer.clear_query_cache()
        if hasattr(parent, 'search_engine') and hasattr(parent.search_engine, 'clear_cache'):
            parent.search_engine.clear_cache()

    def _finished(self, result):
        if not self._cleanup():
            QTimer.singleShot(100, lambda: self._finished(result))
            return
        if 'items' in result:
            self._set_plan(result)
            self._histories(select_job=result['id'])
            notes = result.get('folder_issues') or []
            self.details.setPlainText(
                'Análise salva localmente. Revise as pastas (clique no link da pasta para abrir no Drive), '
                'as tags que serão somadas ao arquivo real e clique em Executar.\n' + '\n'.join(notes)
            )
        elif result.get('cancelled') and 'completed' not in result:
            self._histories()
            self.status.setText('Análise cancelada.')
            self.details.setPlainText('Análise cancelada pelo usuário.')
        else:
            job = self.plan['id']
            self._histories(select_job=job)
            journal = ShortcutAuditJournal(self.db_name)
            try:
                plan = journal.load(job)
            finally:
                journal.close()
            self._set_plan(plan)
            if result.get('local_updated') or result.get('local_cleaned'):
                self._refresh_parent_catalog()
            if 'reconciled' in result:
                msg = (
                    f"Conciliadas: {result['reconciled']}; concluídas: {result['completed']}; "
                    f"destinos atualizados: {result['tags_updated']}; pendentes de revisão: {result['unresolved']}.\n"
                    f"Diário: {result['journal']}\n" + '\n'.join(result['errors'])
                )
            else:
                msg = (
                    f"Concluídos: {result['completed']} atalho(s) na Lixeira; "
                    f"destinos com tags somadas: {result['tags_updated']} ({result['tags_added']} tag(s) novas); "
                    f"índice atualizado/limpo: {result['local_updated']}/{result['local_cleaned']}.\n"
                    f"Diário: {result['journal']}\n" + '\n'.join(result['errors'])
                )
            if result.get('cancelled'):
                msg += '\nExecução cancelada. Etapas confirmadas foram preservadas no diário.'
            self.details.setPlainText(msg)
            if result.get('errors'):
                QMessageBox.warning(self, 'Execução interrompida — confira o diário', msg)
        self.progress.setValue(100)

    def _failed(self, message):
        if not self._cleanup():
            QTimer.singleShot(100, lambda: self._failed(message))
            return
        if self.plan:
            self._refresh_uncertain_ids()
            self._buttons()
        self.details.setPlainText(message + '\nUse Abrir / retomar revisão ou Conferir / liberar incertas.')
        self.status.setText('Operação interrompida.')
        QMessageBox.warning(self, 'Operação interrompida', message)

    def done(self, result):
        if not self.busy:
            super().done(result)

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)
