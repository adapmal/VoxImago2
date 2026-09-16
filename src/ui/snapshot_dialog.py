"""Operações manuais de distribuição do catálogo, executadas fora da UI."""
import json
import os
import time

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QPlainTextEdit, QProgressBar, QMessageBox, QFileDialog,
)
from src.utils.config_manager import ConfigManager
from src.database.snapshot import read_manifest, SCHEMA_VERSION
from src.services.snapshot_management import (
    catalog_info, inspect_snapshot, prepare_adoption, check_path,
    cancel_adoption,
)


class SnapshotWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.succeeded.emit(self.operation())
        except Exception as exc:
            self.failed.emit(str(exc))


class SnapshotDialog(QDialog):
    def __init__(self, parent=None, indexer=None):
        super().__init__(parent)
        self.indexer = indexer
        self.config = ConfigManager()
        self.db_path = indexer.db_name if indexer else self.config.get_db_path()
        self.worker = None
        self.review = None
        self._maintenance = False
        self.setWindowTitle('Snapshots — distribuição manual do catálogo')
        self.resize(900, 660)
        layout = QVBoxLayout(self)
        intro = QLabel('Sincronizar consulta o Drive; não publica nem adota snapshots. '
                       'Use esta ferramenta para preparar uma instalação ou recuperar o catálogo. '
                       'Não inclui mídias, thumbnails em cache, credenciais ou a fila. '
                       'A comparação é de inventário/contagens, não uma mesclagem de bancos.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        configured = self.config.get('shared_cache_path')
        root, ext = os.path.splitext(configured)
        mode_suffix = '_sandbox' if self.config.is_sandbox() else ''
        default = f'{root}{mode_suffix}_v{SCHEMA_VERSION}{ext or ".db"}'
        layout.addWidget(QLabel('Snapshot de origem (.manifest.json ou .db):'))
        row = QHBoxLayout()
        self.source = QLineEdit(default)
        row.addWidget(self.source)
        self.browse = QPushButton('Escolher…')
        row.addWidget(self.browse)
        layout.addLayout(row)
        layout.addWidget(QLabel('Destino da publicação (caminho-base, não uma geração já publicada):'))
        self.destination = QLineEdit(default)
        layout.addWidget(self.destination)
        row = QHBoxLayout()
        self.compare = QPushButton('Comparar / verificar snapshot')
        self.publish = QPushButton('Publicar snapshot desta máquina')
        self.adopt = QPushButton('Preparar adoção ao reiniciar')
        self.cancel_pending = QPushButton('Cancelar adoção preparada')
        for btn in (self.compare, self.publish, self.adopt, self.cancel_pending):
            row.addWidget(btn)
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output)
        self.close_button = QPushButton('Fechar')
        layout.addWidget(self.close_button)
        self.browse.clicked.connect(self._browse)
        self.compare.clicked.connect(self._compare)
        self.publish.clicked.connect(self._publish_preview)
        self.adopt.clicked.connect(self._adopt)
        self.cancel_pending.clicked.connect(self._cancel_pending)
        self.close_button.clicked.connect(self.accept)
        self.source.textChanged.connect(self._invalidate_review)
        self._enable(True)

    def _invalidate_review(self):
        self.review = None
        self.adopt.setEnabled(False)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Selecionar snapshot', '', 'Snapshot (*.manifest.json *.db)')
        if path:
            self.source.setText(path)

    def _enable(self, enabled):
        for widget in (self.source, self.destination, self.browse, self.compare,
                       self.close_button, self.cancel_pending):
            widget.setEnabled(enabled)
        self.publish.setEnabled(enabled and self.indexer is not None and not self.config.is_read_only())
        self.adopt.setEnabled(enabled and self.review is not None and not self.config.is_read_only())

    def _run(self, operation, callback):
        if self.worker is not None:
            return
        win = self.parent().window() if self.parent() else None
        if win and hasattr(win, 'begin_maintenance_operation'):
            if not win.begin_maintenance_operation():
                return
            self._maintenance = True
        self._enable(False)
        self.progress.show()
        result = {}
        self.worker = SnapshotWorker(operation, self)
        self.worker.succeeded.connect(lambda value: result.update(value=value))
        self.worker.failed.connect(lambda error: result.update(error=error))
        def finished():
            self.worker.deleteLater()
            self.worker = None
            self.progress.hide()
            if self._maintenance:
                win.end_maintenance_operation()
                self._maintenance = False
            self._enable(True)
            if 'error' in result:
                self.output.appendPlainText('Falha: ' + result['error'])
                QMessageBox.warning(self, 'Operação não concluída', result['error'])
            else:
                callback(result.get('value'))
        self.worker.finished.connect(finished)
        self.worker.start()

    def _compare(self):
        source = self.source.text().strip()
        self._invalidate_review()
        def operation():
            info = inspect_snapshot(source)
            try:
                local = catalog_info(self.db_path)
            except Exception as exc:
                local = {'aviso': str(exc)}
            return info, local
        def done(result):
            self.review, local = result
            manifest = self.review['manifest']
            self.output.setPlainText(
                'Snapshot verificado: ' + self.review['path'] + '\n'
                'Origem: ' + str(manifest.get('source_machine', 'não registrada')) + '\n'
                'Data: ' + (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(manifest['created_at']))
                            if manifest.get('created_at') else 'não registrada') + '\n'
                'Geração: ' + str(manifest.get('generation', 'arquivo sem manifesto')) + '\n'
                'Identidade do acervo: ' + str(manifest.get('catalog_mode', 'não registrada; confira a origem')) + '\n'
                'Snapshot: ' + json.dumps({k: self.review[k] for k in ('counts', 'validations', 'version', 'roots')}, ensure_ascii=False) + '\n'
                'Catálogo local: ' + json.dumps(local, ensure_ascii=False) + '\n\n'
                'Contagens diferentes não significam corrupção. Nenhum dado foi importado.')
            self._enable(True)
        self._run(operation, done)

    def _publish_preview(self):
        target = self.destination.text().strip()
        def preview():
            check_path(target)
            if not target.lower().endswith('.db'):
                raise ValueError('O destino precisa ser um caminho-base terminado em .db.')
            if os.path.abspath(target) == os.path.abspath(self.db_path):
                raise ValueError('O destino não pode ser o banco ativo.')
            return catalog_info(self.db_path), read_manifest(target) or {}
        def confirm(result):
            info, current = result
            if QMessageBox.question(self, 'Publicar catálogo',
                    f'Publicar {info["counts"]} em:\n{target}\n\n'
                    f'Geração atualmente publicada: {current.get("generation", "nenhuma")}\n'
                    'Isso disponibiliza uma nova cópia; não atualiza as outras máquinas. '
                    'A retenção compartilhada mantém as três gerações mais recentes. Continuar?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            def publish():
                from src.database.database import FileIndexer
                indexer = FileIndexer(self.db_path)
                try:
                    if not indexer.export_to_shared_cache(target, expected_previous_generation=current.get('generation')):
                        raise RuntimeError('Snapshot não publicado. Consulte o log; se outra máquina publicou, repita a comparação.')
                finally:
                    indexer.close()
                return target
            self._run(publish, lambda value: self.output.appendPlainText('Snapshot publicado manualmente: ' + value))
        self._run(preview, confirm)

    def _adopt(self):
        if self.review is None or self.config.is_read_only():
            return
        info = self.review
        mapping = []
        for root in info['roots']:
            new = QFileDialog.getExistingDirectory(self, f'Onde está nesta máquina a pasta: {root}?', '')
            if not new:
                return
            mapping.append((root, new))
        if QMessageBox.question(self, 'Preparar substituição do catálogo',
                'Na próxima abertura, após nova confirmação, o catálogo atual será substituído. '
                'Tags locais, favoritos, rotações e vínculos serão os da cópia escolhida; não haverá mesclagem. '
                'Um backup do banco atual será preservado. A fila precisa estar vazia. '
                'Nenhuma mídia será movida ou baixada.\n\nMapeamento: ' + str(mapping) + '\nContinuar?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: prepare_adoption(self.db_path, info, mapping),
                  lambda _: self.output.appendPlainText('Adoção preparada. Feche e reabra o aplicativo para confirmar a aplicação. '
                                                        'O banco atual ainda não foi substituído. Não crie novas operações na fila até reiniciar.'))

    def _cancel_pending(self):
        if self.worker is None:
            cancel_adoption(self.db_path)
            self.output.appendPlainText('Adoção pendente cancelada. O catálogo e a cópia preparada foram preservados.')

    def reject(self):
        if self.worker is None:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
