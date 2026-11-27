"""Interface para diagnostico e reparo manual do banco."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
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
        self._cancel_file = None
        self._cancel_requested = False
        self._force_kill_ready = False
        self._cancel_timer = None
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
        self.reconcile_paths_btn = QPushButton('Reconciliar arquivos movidos')
        self.audit_btn = QPushButton('Auditar vinculos com Drive')
        self.repair_links_btn = QPushButton('Reparar vinculos seguros')
        for button in (
            self.diagnose_btn, self.index_btn, self.reconcile_paths_btn,
            self.audit_btn, self.repair_links_btn,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.allow_online_cb = QCheckBox(
            'Permitir download de arquivos somente online para validar MD5 (pode acionar sync do Drive)'
        )
        self.allow_online_cb.setChecked(False)
        layout.addWidget(self.allow_online_cb)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        self.cancel_btn = QPushButton('Cancelar')
        self.cancel_btn.setEnabled(False)
        self.close_btn = QPushButton('Fechar')
        close_row.addWidget(self.cancel_btn)
        close_row.addWidget(self.close_btn)
        layout.addLayout(close_row)

        self.diagnose_btn.clicked.connect(
            lambda: self._run_health(repair=False)
        )
        self.index_btn.clicked.connect(
            lambda: self._run_health(repair=True)
        )
        self.reconcile_paths_btn.clicked.connect(
            self._run_reconcile_paths
        )
        self.audit_btn.clicked.connect(
            lambda: self._run_link_revalidation(apply=False)
        )
        self.repair_links_btn.clicked.connect(
            lambda: self._run_link_revalidation(apply=True)
        )
        self.cancel_btn.clicked.connect(self._cancel_process)
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
            online_note = (
                ' Arquivos somente online tambem serao baixados/lidos.'
                if self.allow_online_cb.isChecked()
                else ' Arquivos somente online ficarao pendentes sem download.'
            )
            answer = QMessageBox.question(
                self,
                'Revalidar e reparar',
                'A operacao baixara o inventario do Drive, criara backup, conferira '
                'caminho completo + MD5 real (incluindo trocas cruzadas em transacao) '
                'e deixara conflitos ou ambiguidades bloqueados.' + online_note + ' Continuar?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        script = os.path.join('scripts', 'revalidate_drive_links.py')
        self._cleanup_cancel_file()
        self._cancel_file = os.path.join(
            tempfile.gettempdir(), f'voximago_reval_cancel_{uuid.uuid4().hex}.flag'
        )
        args = [
            script, '--db', self.indexer.db_name,
            '--hash-mode', 'suspects',
            '--cancel-file', self._cancel_file,
        ]
        if self.allow_online_cb.isChecked():
            args.append('--allow-online-hashes')
        if apply:
            args.extend([
                '--apply',
                '--prune-stale-drive-cache',
            ])
        self._start_process(args, writes=apply)

    def _cleanup_cancel_file(self):
        if self._cancel_file and os.path.exists(self._cancel_file):
            try:
                os.remove(self._cancel_file)
            except OSError:
                pass
        self._cancel_file = None

    def _stop_cancel_timer(self):
        if self._cancel_timer is not None:
            self._cancel_timer.stop()
            self._cancel_timer.deleteLater()
            self._cancel_timer = None
        self._cancel_requested = False
        self._force_kill_ready = False
        self.cancel_btn.setText('Cancelar')

    def _enable_force_kill_fallback(self):
        if (
            self.process
            and self.process.state() != QProcess.ProcessState.NotRunning
            and self._cancel_requested
        ):
            self._force_kill_ready = True
            self.cancel_btn.setText('Forcar encerramento')
            self.cancel_btn.setEnabled(True)
            self.output.appendPlainText(
                'A operacao ainda esta aguardando resposta de rede ou finalizando com seguranca. '
                'Se precisar, clique em "Forcar encerramento".'
            )

    def _cancel_process(self):
        if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        if self._force_kill_ready:
            answer = QMessageBox.question(
                self,
                'Forcar encerramento',
                'A operacao ainda nao terminou o encerramento seguro. Deseja interromper o processo imediatamente?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.cancel_btn.setEnabled(False)
            self.output.appendPlainText('\nEncerrando processo imediatamente a pedido do usuario...')
            self.process.kill()
            return

        self._cancel_requested = True
        self.cancel_btn.setEnabled(False)
        if self._cancel_file:
            self.output.appendPlainText(
                '\nCancelamento solicitado. Aguardando a etapa atual encerrar com seguranca...'
            )
            try:
                with open(self._cancel_file, 'w', encoding='utf-8') as f:
                    f.write('cancel')
            except OSError:
                pass
        else:
            self.output.appendPlainText('\nSolicitando cancelamento...')
            self.process.terminate()

        if self._cancel_timer is not None:
            self._cancel_timer.stop()
            self._cancel_timer.deleteLater()
        self._cancel_timer = QTimer(self)
        self._cancel_timer.setSingleShot(True)
        self._cancel_timer.timeout.connect(self._enable_force_kill_fallback)
        self._cancel_timer.start(20000)

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
        self._stop_cancel_timer()
        self._cleanup_cancel_file()
        self.progress.hide()
        self._set_buttons_enabled(True)
        if exit_code == 0:
            self.output.appendPlainText('\nOperacao concluida sem alertas.')
        elif exit_code == 130:
            self.output.appendPlainText('\nOperacao cancelada pelo usuario.')
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
            self._stop_cancel_timer()
            self._cleanup_cancel_file()
            self.progress.hide()
            self._set_buttons_enabled(True)
            win = self._main_window()
            if win and hasattr(win, 'end_maintenance_operation'):
                win.end_maintenance_operation()
            self.current_writes = False

    def _run_reconcile_paths(self):
        win = self._main_window()
        if win and hasattr(win, '_start_path_reconciliation'):
            win._start_path_reconciliation()

    def _set_buttons_enabled(self, enabled):
        for button in (
            self.diagnose_btn, self.index_btn, self.reconcile_paths_btn,
            self.audit_btn, self.repair_links_btn, self.close_btn,
            self.allow_online_cb,
        ):
            button.setEnabled(enabled)
        self.cancel_btn.setEnabled(not enabled)

    def closeEvent(self, event):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(
                self, 'Operacao em andamento',
                'Aguarde a operacao terminar antes de fechar esta janela.',
            )
            event.ignore()
            return
        super().closeEvent(event)
