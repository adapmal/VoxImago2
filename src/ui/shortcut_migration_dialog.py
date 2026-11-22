"""Importação de Meu Drive preservando sua organização por atalhos."""
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QProgressBar, QMessageBox,
    QCheckBox, QComboBox, QPlainTextEdit, QAbstractItemView,
)

from src.services.shortcut_migration import MigrationJournal, ShortcutMigrationWorker
from src.utils.config_manager import ConfigManager


class ShortcutMigrationDialog(QDialog):
    PAGE_SIZE = 100

    def __init__(self, parent=None, indexer=None, drive_service=None):
        super().__init__(parent)
        self.db_name = indexer.db_name if indexer else 'data/file_index.db'
        self.service = drive_service
        self.plan = None
        self.selected = set()
        self.page = 0
        self.worker = self.thread = None
        self.busy = self.maintenance = False
        self.setWindowTitle('Incorporar Meu Drive ao Banco de Imagens — manter atalhos')
        self.resize(1140, 760)
        layout = QVBoxLayout(self)
        note = QLabel(
            'Informe links ou IDs de pastas. A origem deve estar no Meu Drive de um usuário '
            'e acessível à conta conectada. As pastas originais permanecem; atalhos existentes '
            'são ignorados, inclusive atalhos para pastas. Nenhuma mídia será baixada. '
            'A análise lê os metadados de todo o Banco de Imagens para comparar MD5 + tamanho '
            '(e SHA-256 quando disponível).')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.source = QLineEdit()
        self.destination = QLineEdit()
        self.source.setPlaceholderText('https://drive.google.com/drive/folders/...')
        self.destination.setPlaceholderText('Pasta dentro do Drive compartilhado Banco de Imagens')
        form.addRow('Pasta de origem — Meu Drive:', self.source)
        form.addRow('Pasta de destino — Banco de Imagens:', self.destination)
        layout.addLayout(form)
        layout.addWidget(QLabel('Duplicatas → _TestesBanco / Banco_Apagado / APAGAR (sem exclusão definitiva).'))
        rules = QLabel(
            'O destino receberá uma pasta com o nome da origem e suas subpastas. Colisões '
            'de nomes recebem sufixo por ID. Havendo várias cópias no banco, o alvo será a '
            'mais antiga por data de criação (ID como desempate); confira o ID na tabela. '
            'As descrições dos arquivos são preservadas; a descrição da cópia já existente '
            'no banco não é modificada. Arquivos sem hash ficam bloqueados.')
        rules.setWordWrap(True)
        layout.addWidget(rules)
        controls = QHBoxLayout()
        self.analyze = QPushButton('Analisar pastas')
        self.cancel = QPushButton('Cancelar operação')
        self.cancel.setEnabled(False)
        self.history = QComboBox()
        self.resume = QPushButton('Abrir / retomar revisão')
        for widget in (self.analyze, self.cancel, self.history, self.resume):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel('Pronto para analisar. A análise não move arquivos nem cria atalhos.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Aplicar', 'Arquivo / caminho original', 'Ação', 'Alvo do atalho (ID)', 'Estado / aviso'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 350)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 230)
        layout.addWidget(self.table, 1)
        pages = QHBoxLayout()
        self.previous = QPushButton('← Anteriores')
        self.next = QPushButton('Próximos →')
        self.page_label = QLabel()
        self.select_all = QPushButton('Marcar elegíveis')
        self.select_none = QPushButton('Desmarcar todos')
        for widget in (self.previous, self.page_label, self.next, self.select_all, self.select_none):
            pages.addWidget(widget)
        layout.addLayout(pages)
        self.access = QCheckBox('Confirmei que os usuários da origem têm acesso aos arquivos no Banco de Imagens.')
        layout.addWidget(self.access)
        warning = QLabel('Atalhos não concedem acesso ao alvo. Mover ao Drive compartilhado muda a propriedade para a organização '
                         'e pode alterar o acesso herdado. Use uma conta com as permissões necessárias; a ferramenta não altera compartilhamentos.')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(85)
        layout.addWidget(self.details)
        footer = QHBoxLayout()
        self.execute = QPushButton('Executar selecionados / retomar')
        self.close_button = QPushButton('Fechar')
        footer.addWidget(self.execute)
        footer.addStretch()
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.analyze.clicked.connect(self._analyze)
        self.resume.clicked.connect(self._resume)
        self.execute.clicked.connect(self._execute)
        self.cancel.clicked.connect(lambda: self.worker.cancel() if self.worker else None)
        self.close_button.clicked.connect(self.accept)
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        self.select_all.clicked.connect(lambda: self._select(True))
        self.select_none.clicked.connect(lambda: self._select(False))
        self.access.toggled.connect(self._buttons)
        self.table.itemChanged.connect(self._checked)
        self.table.cellDoubleClicked.connect(self._open_target)
        self.source.textChanged.connect(self._buttons)
        self.destination.textChanged.connect(self._buttons)
        self._histories()
        self._buttons()

    def _histories(self, select_job=None):
        current = select_job if select_job is not None else self.history.currentData()
        self.history.clear()
        if not (Path(self.db_name).resolve().parent / 'shortcut_migration' / 'journal.db').exists():
            return
        journal = MigrationJournal(self.db_name)
        try:
            for job, name, created in journal.jobs():
                self.history.addItem(f'{datetime.fromtimestamp(created):%d/%m %H:%M} — {name}', job)
        finally:
            journal.close()
        if current:
            idx = self.history.findData(current)
            if idx >= 0:
                self.history.setCurrentIndex(idx)

    def _resume(self):
        job = self.history.currentData()
        if not job or self.busy:
            return
        journal = MigrationJournal(self.db_name)
        try:
            self._set_plan(journal.load(job))
        except Exception as exc:
            QMessageBox.warning(self, 'Revisão não carregada', str(exc))
        finally:
            journal.close()

    def _set_plan(self, plan):
        self.plan = plan
        self.source.setText(plan['source']['id'])
        self.destination.setText(plan['destination']['id'])
        self.page = 0
        self.selected = {r['source']['id'] for r in plan['items'] if r['state'] not in ('completed', 'blocked') and not r.get('error')}
        self._render()
        counts = {a: sum(r['action'] == a and not r.get('error') for r in plan['items']) for a in ('import', 'duplicate')}
        errors = sum(r.get('state') == 'error' for r in plan['items'])
        extra = (
            f' | {errors:,} com falha/revisão (desmarcados — use "Marcar elegíveis" ou marque na tabela para retomar).'
            if errors else ' Revisão e progresso salvos localmente.'
        )
        self.status.setText(f'{len(plan["items"]):,} arquivos; {counts["import"]:,} para incorporar; '
                            f'{counts["duplicate"]:,} duplicatas; {plan["skipped_shortcuts"]:,} atalhos ignorados.'
                            + extra)

    def _render(self):
        rows = self.plan['items'] if self.plan else []
        start = self.page * self.PAGE_SIZE
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for local, row in enumerate(rows[start:start + self.PAGE_SIZE]):
            self.table.insertRow(local)
            item = QTableWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, row['source']['id'])
            item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            if row['state'] not in ('completed', 'blocked') and not (row['state'] == 'pending' and row.get('error')):
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if row['source']['id'] in self.selected else Qt.CheckState.Unchecked)
            self.table.setItem(local, 0, item)
            states = dict(pending='Pendente', completed='Concluído', blocked='Bloqueado',
                          error='Revisar / retomar', moving='Movimento a conferir',
                          preparing='Em preparação', shortcut_confirmed='Atalho confirmado')
            target_label = (row.get('target_name') or row['source']['name']) + '\n' + row['target_id']
            values = [' / '.join(row['parts']), 'Duplicata → APAGAR' if row['action'] == 'duplicate' else 'Incorporar ao banco',
                      target_label, row.get('error') or states.get(row.get('state'), 'Pendente')]
            for col, value in enumerate(values, 1):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(local, col, cell)
        self.table.blockSignals(False)
        self.page_label.setText(f'{min(start + 1, len(rows))}–{min(start + self.PAGE_SIZE, len(rows))} de {len(rows):,}')
        self._buttons()

    def _open_target(self, row, column):
        index = self.page * self.PAGE_SIZE + row
        if self.plan and not self.busy and column == 3 and 0 <= index < len(self.plan['items']):
            fid = self.plan['items'][index]['target_id']
            QDesktopServices.openUrl(QUrl('https://drive.google.com/file/d/' + fid + '/view'))

    def _checked(self, item):
        if item.column() != 0 or not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        fid = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(fid)
        else:
            self.selected.discard(fid)
        self._buttons()

    def _select(self, enabled):
        self.selected = ({r['source']['id'] for r in self.plan['items']
                          if r['state'] not in ('completed', 'blocked') and not (r['state'] == 'pending' and r.get('error'))}
                         if enabled and self.plan else set())
        self._render()

    def _change_page(self, delta):
        self.page = max(0, self.page + delta)
        self._render()

    def _buttons(self):
        for widget in (self.source, self.destination, self.analyze, self.history, self.resume,
                       self.table, self.select_all, self.select_none, self.access, self.close_button):
            widget.setEnabled(not self.busy)
        self.cancel.setEnabled(self.busy)
        count = len(self.plan['items']) if self.plan else 0
        self.previous.setEnabled(not self.busy and self.page > 0)
        self.next.setEnabled(not self.busy and (self.page + 1) * self.PAGE_SIZE < count)
        # Alterar o texto exige análise: nunca executar um plano de outro escopo.
        scope_matches = bool(self.plan and self.source.text() == self.plan['source']['id']
                             and self.destination.text() == self.plan['destination']['id'])
        cfg = ConfigManager()
        self.execute.setEnabled(not self.busy and scope_matches and bool(self.selected) and self.access.isChecked()
                                and not cfg.is_read_only() and not cfg.is_sandbox())

    def _analyze(self):
        if not self.source.text().strip() or not self.destination.text().strip():
            QMessageBox.warning(self, 'Pastas necessárias', 'Informe os links/IDs da origem e do destino.')
            return
        self._start(source=self.source.text(), destination=self.destination.text())

    def _execute(self):
        if not self.execute.isEnabled():
            return
        if QMessageBox.question(self, 'Confirmar incorporação',
                                f'Processar {len(self.selected):,} arquivos selecionados?\n\n'
                                'Serão criados atalhos na origem e os originais serão movidos. '
                                'Duplicatas irão para Banco_Apagado/APAGAR. As etapas concluídas não são desfeitas '
                                'ao cancelar. Atalhos existentes permanecem intactos.\n\n'
                                'Evite editar estes arquivos por outras máquinas durante a execução.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._start(job=self.plan['id'], selected=list(self.selected))

    def _start(self, **args):
        if self.busy:
            return
        if not self.service:
            QMessageBox.warning(self, 'Login necessário', 'Conecte o aplicativo ao Google Drive antes de analisar ou executar.')
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
        self.worker = ShortcutMigrationWorker(self.service, self.db_name, **args)
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

    def _finished(self, result):
        if not self._cleanup():
            QTimer.singleShot(100, lambda: self._finished(result))
            return
        if 'items' in result:
            self._set_plan(result)
            self._histories(select_job=result['id'])
            self.details.setPlainText('Análise salva. Confira a seleção e o acesso dos usuários antes de executar.')
        elif result.get('cancelled') and 'completed' not in result:
            self._histories()
            self.status.setText('Análise cancelada.')
            self.details.setPlainText('Análise cancelada pelo usuário. Nenhuma revisão anterior foi alterada.')
        else:
            job = self.plan['id']
            self._histories(select_job=job)
            journal = MigrationJournal(self.db_name)
            try:
                self._set_plan(journal.load(job))
            finally:
                journal.close()
            message = (f'Confirmados nesta execução: {result["completed"]}; incorporados: {result["imported"]}; '
                       f'duplicatas arquivadas: {result["duplicates"]}.\n'
                       f'Diário: {result["journal"]}\n' + '\n'.join(result['errors']))
            if result.get('cancelled'):
                message += '\nCancelado. Use Abrir / retomar revisão para continuar.'
                self.status.setText('Execução cancelada. Etapas concluídas preservadas no diário.')
            if any(r.get('state') == 'error' for r in self.plan['items']):
                message += '\n\nItens interrompidos ficaram desmarcados para revisão; marque-os ou clique em "Marcar elegíveis" para revalidar/retomar.'
            self.details.setPlainText(message)
            uncertain = result.get('uncertain_creates') or []
            if uncertain:
                if QMessageBox.question(
                    self,
                    'Criação anterior não localizada',
                    message + '\n\nSe você já conferiu no Drive que a pasta/atalho não foi criado, deseja liberar o bloqueio no diário para tentar novamente?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                ) == QMessageBox.StandardButton.Yes:
                    journal = MigrationJournal(self.db_name)
                    try:
                        for key in uncertain:
                            journal.delete_creating_object(key)
                    finally:
                        journal.close()
                    self.status.setText('Bloqueio de criação liberado. Marque os itens desejados e clique em Executar selecionados / retomar.')
            elif result['errors']:
                QMessageBox.warning(self, 'Execução interrompida — confira o diário', message)
        self.progress.setValue(100)

    def _failed(self, message):
        if not self._cleanup():
            QTimer.singleShot(100, lambda: self._failed(message))
            return
        self.details.setPlainText(message + '\nEtapas confirmadas permanecem no diário local. Use Abrir / retomar revisão.')
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
