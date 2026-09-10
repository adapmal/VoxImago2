"""Interface para diagnostico e reparo manual do banco."""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


class DatabaseHealthDialog(QDialog):
    def __init__(self, parent=None, indexer=None):
        super().__init__(parent)
        self.indexer = indexer
        self.process = None
        self.current_writes = False
        self.setWindowTitle('Saude e reparo do banco')
        self.resize(860, 600)

        layout = QVBoxLayout(self)
        intro = QLabel(
            'As verificacoes so rodam quando solicitadas. O diagnostico nao '
            'altera o banco; reparos criam um backup antes de escrever.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        buttons = QHBoxLayout()
        self.diagnose_btn = QPushButton('Diagnosticar banco')
        self.index_btn = QPushButton('Reparar indice de busca')
        self.audit_btn = QPushButton('Auditar vinculos com Drive')
        self.repair_links_btn = QPushButton('Reparar vinculos seguros')
        for button in (
            self.diagnose_btn, self.index_btn,
            self.audit_btn, self.repair_links_btn,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        self.close_btn = QPushButton('Fechar')
        close_row.addWidget(self.close_btn)
        layout.addLayout(close_row)

        self.diagnose_btn.clicked.connect(
            lambda: self._run_health(repair=False)
        )
        self.index_btn.clicked.connect(
            lambda: self._run_health(repair=True)
        )
        self.audit_btn.clicked.connect(
            lambda: self._run_link_revalidation(apply=False)
        )
        self.repair_links_btn.clicked.connect(
            lambda: self._run_link_revalidation(apply=True)
        )
        self.close_btn.clicked.connect(self.accept)

    def _main_window(self):
        return self.parent().window() if self.parent() else None

    def _run_health(self, repair):
        if repair:
            answer = QMessageBox.question(
                self,
                'Reparar indice',
                'O indice sera reconstruido somente se estiver inconsistente. '
                'Um backup sera criado antes. Continuar?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        script = os.path.join('scripts', 'database_health.py')
        args = [script, '--db', self.indexer.db_name, '--full']
        if repair:
            args.append('--repair-indexes')
        self._start_process(args, writes=repair)

    def _run_link_revalidation(self, apply):
        if apply:
            answer = QMessageBox.question(
                self,
                'Revalidar e reparar',
                'A operacao baixara o inventario do Drive, calculara hashes '
                'somente para suspeitos e aplicara apenas correspondencias '
                'unicas. Ambiguidades ficarao em quarentena. Continuar?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        script = os.path.join('scripts', 'revalidate_drive_links.py')
        args = [script, '--db', self.indexer.db_name]
        if apply:
            args.extend([
                '--hash-mode', 'suspects', '--apply',
                '--prune-stale-drive-cache',
            ])
        self._start_process(args, writes=apply)

    def _start_process(self, arguments, *, writes):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            return
        win = self._main_window()
        if win and hasattr(win, 'begin_maintenance_operation'):
            if not win.begin_maintenance_operation():
                return

        self.current_writes = writes
        self.output.clear()
        self.output.appendPlainText('Iniciando operacao...\n')
        self.progress.show()
        self._set_buttons_enabled(False)

        self.process = QProcess(self)
        self.process.setWorkingDirectory(os.path.abspath('.'))
        self.process.setProgram(sys.executable)
        self.process.setArguments(arguments)
        self.process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)
        self.process.start()

    def _read_output(self):
        if not self.process:
            return
        raw = bytes(self.process.readAllStandardOutput())
        if raw:
            self.output.appendPlainText(raw.decode('utf-8', errors='replace').rstrip())
            scrollbar = self.output.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _process_finished(self, exit_code, _exit_status):
        self._read_output()
        self.progress.hide()
        self._set_buttons_enabled(True)
        if exit_code == 0:
            self.output.appendPlainText('\nOperacao concluida sem alertas.')
        elif exit_code == 1:
            self.output.appendPlainText('\nOperacao concluida com alertas para revisao.')
        else:
            self.output.appendPlainText(f'\nOperacao terminou com codigo {exit_code}.')

        win = self._main_window()
        if win and hasattr(win, 'end_maintenance_operation'):
            win.end_maintenance_operation()
        if self.current_writes and win and hasattr(win, '_force_refresh_after_sync'):
            win._force_refresh_after_sync()
        self.current_writes = False

    def _process_error(self, error):
        self.output.appendPlainText(f'\nFalha ao iniciar/executar: {error}')
        if error == QProcess.ProcessError.FailedToStart:
            self.progress.hide()
            self._set_buttons_enabled(True)
            win = self._main_window()
            if win and hasattr(win, 'end_maintenance_operation'):
                win.end_maintenance_operation()
            self.current_writes = False

    def _set_buttons_enabled(self, enabled):
        for button in (
            self.diagnose_btn, self.index_btn,
            self.audit_btn, self.repair_links_btn, self.close_btn,
        ):
            button.setEnabled(enabled)

    def closeEvent(self, event):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(
                self, 'Operacao em andamento',
                'Aguarde a operacao terminar antes de fechar esta janela.',
            )
            event.ignore()
            return
        super().closeEvent(event)
