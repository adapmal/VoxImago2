"""Revisão paginada e explícita de nomes repetidos no Drive."""
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                            QComboBox, QProgressBar, QTableWidgetItem, QMessageBox, QFileDialog)

from src.ui.review_table import ReviewTable
from src.services.name_disambiguation import NameJournal, NameDisambiguationWorker, uncertain_operation_ids
from src.services.snapshot_management import check_path
from src.utils.config_manager import ConfigManager


class NameDisambiguationDialog(QDialog):
    PAGE_SIZE = 100

    def __init__(self, parent=None, indexer=None, drive_service=None):
        super().__init__(parent)
        self.db_name = str(Path(indexer.db_name if indexer else 'data/file_index.db').resolve())
        self.service, self.indexer = drive_service, indexer
        self.plan, self.selected, self.page = None, set(), 0
        self.busy = self.maintenance = False
        self.history_busy = False
        self.thread = self.worker = None
        self._active_job = None
        self.setWindowTitle('Desambiguar nomes no Drive — prévia e histórico')
        self.resize(1180, 720)
        layout = QVBoxLayout(self)
        note = QLabel('Analisa todo o Banco de Imagens por pasta + nome (inclusive maiúsculas/minúsculas), sem baixar mídias. '
                      'Arquivos com conteúdos distintos são renomeados pelo ID (_0, _1… antes da extensão). '
                      'Cópias idênticas na mesma pasta (mesmo MD5/tamanho, sem divergência de SHA-256) fundem tags no preservado '
                      'e movem os excedentes para _TestesBanco\\Banco_Apagado. '
                      'Pastas, atalhos, hashes ausentes, SHA-256 divergentes e nomes incompatíveis ficam bloqueados.')
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QHBoxLayout()
        self.analyze = QPushButton('Analisar Banco de Imagens')
        self.history = QComboBox()
        self.resume = QPushButton('Abrir / retomar revisão')
        self.cancel = QPushButton('Cancelar')
        for widget in (self.analyze, self.history, self.resume, self.cancel):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel('Análise não altera nada na nuvem. Duplo clique abre o arquivo pelo ID.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = ReviewTable(0, 6)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setHorizontalHeaderLabels(['Aplicar', 'Pasta', 'Nome atual / ID', 'Ação / Nome aprovado', 'MD5 / tamanho', 'Estado / aviso'])
        for col, width in enumerate((60, 220, 210, 200, 160, 240)):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)
        pages = QHBoxLayout()
        self.previous = QPushButton('← Anteriores')
        self.next = QPushButton('Próximos →')
        self.page_label = QLabel()
        self.all = QPushButton('Marcar elegíveis')
        self.none = QPushButton('Desmarcar todos')
        self.mark = QPushButton('Marcar seleção')
        self.unmark = QPushButton('Desmarcar seleção')
        for widget in (self.previous, self.page_label, self.next, self.all, self.none, self.mark, self.unmark):
            pages.addWidget(widget)
        layout.addLayout(pages)
        layout.addWidget(QLabel('Shift/Ctrl: selecionar linhas · Espaço: alternar Aplicar · '
                               'É necessário aprovar todos os membros pendentes de cada grupo.'))
        footer = QHBoxLayout()
        self.execute = QPushButton('Executar / conferir selecionados')
        self.reconcile = QPushButton('Conferir / liberar incertas')
        self.export_history = QPushButton('Exportar histórico de nomes')
        self.import_history = QPushButton('Importar histórico de nomes')
        self.close_button = QPushButton('Fechar')
        for widget in (self.execute, self.reconcile, self.export_history, self.import_history, self.close_button):
            footer.addWidget(widget)
        layout.addLayout(footer)
        self.analyze.clicked.connect(lambda: self._start())
        self.resume.clicked.connect(self._resume)
        self.cancel.clicked.connect(lambda: self.worker.cancel() if self.worker else None)
        self.previous.clicked.connect(lambda: self._page(-1))
        self.next.clicked.connect(lambda: self._page(1))
        self.all.clicked.connect(lambda: self._select(True))
        self.none.clicked.connect(lambda: self._select(False))
        self.mark.clicked.connect(lambda: self.table.set_selected_checked(True))
        self.unmark.clicked.connect(lambda: self.table.set_selected_checked(False))
        self.table.itemChanged.connect(self._checked)
        self.table.cellDoubleClicked.connect(self._open)
        self.execute.clicked.connect(self._execute)
        self.reconcile.clicked.connect(self._reconcile)
        self.export_history.clicked.connect(lambda: self._history_file(False))
        self.import_history.clicked.connect(lambda: self._history_file(True))
        self.close_button.clicked.connect(self.accept)
        self._jobs()
        self._buttons()

    def _jobs(self, current=None):
        journal = NameJournal(self.db_name)
        try:
            self.history.clear()
            for job, created in journal.jobs():
                self.history.addItem(datetime.fromtimestamp(created).strftime('%d/%m %H:%M:%S') + ' — ' + job[:8], job)
            if current:
                self.history.setCurrentIndex(self.history.findData(current))
        finally:
            journal.close()

    def _resume(self):
        if self.busy or not self.history.currentData():
            return
        journal = NameJournal(self.db_name)
        try:
            self._set_plan(journal.load(self.history.currentData()))
        finally:
            journal.close()

    def _set_plan(self, plan):
        self.plan, self.page = plan, 0
        self.selected = set()  # Nunca aprovar alterações só por abrir a revisão.
        self._render()

    @staticmethod
    def _action_and_state_text(row):
        action = row.get('action') or 'rename'
        state = row.get('state') or 'pending'
        tags_preview = row.get('merged_description') or '—'
        if action == 'dedup_keep':
            action_text = f"Manter nome ({row.get('new_name') or row['source']['name']})\nTags finais: {tags_preview}"
        elif action == 'dedup_move':
            action_text = f"Mover → {row.get('archive_path') or '_TestesBanco/Banco_Apagado'}"
        elif action == 'dedup_keep_and_rename':
            action_text = f"Fundir tags → {row['new_name']}\nTags finais: {tags_preview}"
        else:
            action_text = row.get('new_name', '')

        if row.get('error'):
            return action_text, row['error']

        reasons = row.get('discrepancy_reasons') or []
        suffix = (' | ' + '; '.join(reasons)) if reasons else ''
        if state == 'tags_done':
            state_text = 'Tags gravadas; falta renomear'
        elif state in ('writing', 'uncertain'):
            state_text = 'Conferir resposta anterior'
        elif state == 'blocked':
            state_text = 'Bloqueado'
        elif state == 'completed':
            state_text = {
                'dedup_move': 'Movido p/ Banco_Apagado',
                'dedup_keep': 'Confirmado (nome mantido + tags)',
                'dedup_keep_and_rename': 'Confirmado (tags + renomeado)',
            }.get(action, 'Confirmado')
        elif state == 'pending':
            state_text = {
                'dedup_move': 'Mover cópia duplicada p/ Banco_Apagado',
                'dedup_keep': 'Preservar nome e fundir tags' + suffix,
                'dedup_keep_and_rename': 'Grupo misto: fundir tags e renomear' + suffix,
            }.get(action, 'Pendente')
        else:
            state_text = state
        return action_text, state_text

    def _render(self):
        rows = self.plan['items'] if self.plan else []
        uncertain_ids = uncertain_operation_ids(self.db_name, self.plan)
        start = self.page * self.PAGE_SIZE
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row in rows[start:start + self.PAGE_SIZE]:
            pos = self.table.rowCount()
            self.table.insertRow(pos)
            check = QTableWidgetItem()
            fid = row['source']['id']
            check.setData(Qt.ItemDataRole.UserRole, fid)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if row['state'] not in ('blocked', 'completed'):
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            check.setFlags(flags)
            check.setCheckState(Qt.CheckState.Checked if fid in self.selected else Qt.CheckState.Unchecked)
            self.table.setItem(pos, 0, check)
            action_text, state_text = self._action_and_state_text(row)
            if not row.get('error') and row.get('state') != 'completed' and fid in uncertain_ids:
                state_text = 'Conferir resposta anterior'
            values = [
                row['folder_path'],
                row['source']['name'] + '\n' + fid,
                action_text,
                str(row['source'].get('md5Checksum', '')) + '\n' + str(row['source'].get('size', '')),
                state_text,
            ]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(pos, col, item)
        self.table.blockSignals(False)
        self.page_label.setText(f'{min(start + 1, len(rows))}–{min(start + self.PAGE_SIZE, len(rows))} / {len(rows)}')
        self._buttons(uncertain_ids)

    def _checked(self, item):
        if item.column() == 0 and self.table.checkable(item):
            fid = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                self.selected.add(fid)
            else:
                self.selected.discard(fid)
            self._buttons()

    def _select(self, enabled):
        self.selected = {r['source']['id'] for r in self.plan['items'] if r['state'] not in ('blocked', 'completed')} if enabled and self.plan else set()
        self._render()

    def _page(self, step):
        self.page += step
        self._render()

    def _open(self, row, col):
        if self.plan and col != 0:
            fid = self.plan['items'][self.page * self.PAGE_SIZE + row]['source']['id']
            QDesktopServices.openUrl(QUrl('https://drive.google.com/file/d/' + fid + '/view'))

    def _buttons(self, uncertain_ids=None):
        for widget in (self.analyze, self.history, self.resume, self.table, self.all, self.none, self.mark,
                       self.unmark, self.export_history, self.import_history, self.close_button):
            widget.setEnabled(not self.busy)
        cfg = ConfigManager()
        if uncertain_ids is None:
            uncertain_ids = uncertain_operation_ids(self.db_name, self.plan)
        self.execute.setEnabled(not self.busy and bool(self.selected) and not cfg.is_read_only() and not cfg.is_sandbox())
        self.reconcile.setEnabled(not self.busy and bool(self.selected & uncertain_ids) and not cfg.is_sandbox())
        self.cancel.setEnabled(self.busy and not self.history_busy)
        self.previous.setEnabled(not self.busy and self.page > 0)
        self.next.setEnabled(not self.busy and bool(self.plan) and (self.page + 1) * self.PAGE_SIZE < len(self.plan['items']))

    def _reconcile(self):
        if not self.reconcile.isEnabled() or not self.plan:
            return
        uncertain_ids = uncertain_operation_ids(self.db_name, self.plan)
        targets = list(self.selected & uncertain_ids)
        if not targets:
            return
        if QMessageBox.question(self, 'Conferir operações incertas',
                                f'Conferir {len(targets)} item(ns) no Drive pelo ID?\n'
                                'Nenhuma renomeação será executada agora.\n'
                                '• Já renomeados conforme o plano serão confirmados no histórico.\n'
                                '• Comprovadamente não executados (mesmo ID, pasta, conteúdo, versão e sem colisão) serão liberados no diário para nova aprovação.\n'
                                '• Divergências ou erros mantêm o bloqueio.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._start(job=self.plan['id'], selected=targets, reconcile=True)

    def _execute(self):
        if not self.execute.isEnabled():
            return
        selected_rows = [
            r for r in (self.plan or {}).get('items', [])
            if r['source']['id'] in self.selected and r['state'] not in ('blocked', 'completed')
        ]
        has_dedup = any((r.get('action') in ('dedup_keep', 'dedup_move', 'dedup_keep_and_rename')) for r in selected_rows)
        has_rename = any(((r.get('action') or 'rename') in ('rename', 'dedup_keep_and_rename')) for r in selected_rows)
        details = []
        if has_dedup:
            details.append('• Cópias idênticas na mesma pasta terão as tags fundidas no preservado e os excedentes movidos para _TestesBanco\\Banco_Apagado (sem exclusão definitiva).')
        if has_rename:
            details.append('• Itens com conteúdos distintos serão renomeados pelo ID (_0, _1…); projetos de edição podem precisar de religação.')
        if not details:
            details.append('• Os itens selecionados serão executados/conferidos pelo ID no Drive.')
        if QMessageBox.question(self, 'Confirmar execução na nuvem',
                                f'Executar/conferir {len(self.selected)} itens?\n'
                                + '\n'.join(details) + '\n'
                                'Evite alterações simultâneas em outros PCs. Cancelar preserva as etapas já confirmadas.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._start(job=self.plan['id'], selected=list(self.selected))

    def _start(self, **args):
        if self.busy:
            return
        if not self.service and not args.get('history_path'):
            QMessageBox.warning(self, 'Login necessário', 'Conecte ao Drive antes de analisar ou executar.')
            return
        parent = self.parent()
        if parent and hasattr(parent, 'begin_maintenance_operation'):
            if not parent.begin_maintenance_operation():
                return
            self.maintenance = True
        self.busy = True
        self._active_job = args.get('job')
        self.history_busy = bool(args.get('history_path'))
        self._buttons()
        self.progress.setRange(0, 0)
        self.thread = QThread(self)
        self.worker = NameDisambiguationWorker(self.service, self.db_name, **args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.thread.finished.connect(self.worker.deleteLater)
        self.worker.progress.connect(self._progress)
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
            if not self.thread.wait(1000):
                return False
            self.thread.deleteLater()
        self.thread = self.worker = None
        self.busy = False
        self.history_busy = False
        if self.maintenance:
            self.parent().end_maintenance_operation()
            self.maintenance = False
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._buttons()
        return True

    def _finished(self, result):
        if not self._cleanup():
            QTimer.singleShot(100, lambda: self._finished(result))
            return
        if 'history_message' in result:
            self.status.setText(result['history_message'])
            self._clear_search_cache()
        elif 'items' in result:
            self._jobs(result['id'])
            self._set_plan(result)
            self.status.setText(f'{len(result["items"])} itens em conflito. Revise e marque os grupos desejados; nada foi alterado no Drive.')
        elif 'confirmed' in result or 'released' in result:
            if self._active_job:
                self._jobs(self._active_job)
                self._resume()
            summary = (
                f'Conferência: {result.get("confirmed", 0)} confirmada(s), '
                f'{result.get("released", 0)} liberada(s), '
                f'{result.get("blocked", 0)} mantida(s) bloqueada(s). '
            )
            self.status.setText(summary + ('Cancelado. ' if result.get('cancelled') else '') + '\n'.join(result.get('errors', [])))
            if result.get('errors'):
                QMessageBox.warning(self, 'Conferência com avisos', self.status.text())
            self._clear_search_cache()
        else:
            if self._active_job:
                self._jobs(self._active_job)
                self._resume()
            parts = [f'Confirmados: {result.get("completed", 0)}.']
            if result.get('dedup_moved'):
                parts.append(f'Movidos p/ Banco_Apagado: {result["dedup_moved"]}.')
            if result.get('tags_updated'):
                parts.append(f'Tags atualizadas: {result["tags_updated"]}.')
            if result.get('cancelled'):
                parts.append('Cancelado.')
            if result.get('dedup_log_path'):
                parts.append(f'Relatório dedup: {result["dedup_log_path"]}')
            if result.get('errors'):
                parts.append('\n'.join(result['errors']))
            self.status.setText(' '.join(parts))
            if result.get('errors'):
                QMessageBox.warning(self, 'Execução interrompida', '\n'.join(parts))
            self._clear_search_cache()

    def _failed(self, message):
        if not self._cleanup():
            QTimer.singleShot(100, lambda: self._failed(message))
            return
        self.status.setText(message)
        QMessageBox.warning(self, 'Operação interrompida', message)

    def _clear_search_cache(self):
        if self.parent():
            engine = getattr(self.parent(), 'search_engine', None)
            if engine:
                engine.clear_cache()

    def _history_file(self, importing):
        chooser = QFileDialog.getOpenFileName if importing else QFileDialog.getSaveFileName
        path, _ = chooser(self, 'Importar histórico' if importing else 'Exportar histórico', '', 'Histórico JSON (*.json)')
        if not path:
            return
        try:
            path = check_path(path)
            if path.suffix.lower() != '.json':
                raise ValueError('Escolha um arquivo .json para o histórico; não substitua bancos SQLite.')
            self._start(history_path=str(path), import_history=importing)
        except Exception as exc:
            QMessageBox.warning(self, 'Histórico', str(exc))

    def done(self, result):
        if not self.busy:
            super().done(result)

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)
