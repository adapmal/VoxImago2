# app.py - Vox Imago
# Ponto de entrada principal do aplicativo.
# Inicializa a interface gráfica e exibe a janela principal.

import sys
import logging
import os
from src.utils.logging_setup import configure_logging


def setup_logging():
    return configure_logging(retention_days=45)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))


def main():
    # O console legado do Windows costuma usar cp1252/cp850. Sem esta
    # normalizacao, mensagens antigas com emojis podem gerar mojibake ou ate
    # UnicodeEncodeError antes de chegarem ao log.
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='backslashreplace')
            except (OSError, ValueError):
                pass
    setup_logging()
    sys.excepthook = handle_exception
    logging.info("=== Iniciando VoxImago.MB ===")
    from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog
    from PyQt6.QtCore import QLockFile, QEventLoop, QTimer
    from src.ui.ui import DriveFileGalleryApp

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # Impede que esta versão troque um catálogo aberto por outra instância
    # desta instalação. Ferramentas externas devem estar fechadas na adoção.
    os.makedirs('data', exist_ok=True)
    instance_lock = QLockFile(os.path.abspath('data/voximago-instance.lock'))
    if not instance_lock.tryLock(0):
        QMessageBox.warning(None, 'VoxImago já está aberto',
                            'Feche a outra instância desta instalação antes de continuar.')
        return
    from src.utils.config_manager import ConfigManager
    from src.services.snapshot_management import pending_path, apply_pending_adoption, cancel_adoption
    from src.database.snapshot import sqlite_is_healthy
    from src.ui.snapshot_dialog import SnapshotDialog, SnapshotWorker
    db = ConfigManager().get_db_path()
    was_new = not os.path.exists(db) or os.path.getsize(db) == 0
    if pending_path(db).exists():
        answer = QMessageBox.question(None, 'Aplicar adoção preparada?',
            'Há uma restauração manual preparada. Aplicar agora, preservando antes um backup do banco atual? '
            'Feche outras versões do VoxImago e ferramentas que usem este banco. '
            'Escolher Não cancela a adoção e mantém o banco atual.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            progress = QProgressDialog('Validando e restaurando o catálogo…', '', 0, 0)
            progress.setCancelButton(None)
            progress.show()
            loop = QEventLoop()
            outcome = {}
            worker = SnapshotWorker(lambda: apply_pending_adoption(db))
            worker.succeeded.connect(lambda backup: outcome.update(backup=backup))
            worker.failed.connect(lambda error: outcome.update(error=error))
            worker.finished.connect(loop.quit)
            worker.start()
            loop.exec()
            worker.wait()
            progress.close()
            if 'error' in outcome:
                QMessageBox.critical(None, 'Adoção não concluída', outcome['error'] + '\nA preparação foi mantida para revisão.')
                return
            QMessageBox.information(None, 'Catálogo adotado',
                'Adoção concluída. Backup anterior: ' + str(outcome.get('backup') or 'não havia banco anterior') +
                '\nSincronize com o Drive e execute o diagnóstico para conferir os caminhos desta máquina.')
            was_new = False
        else:
            cancel_adoption(db)
    if os.path.isfile(db) and os.path.getsize(db):
        healthy, reason = sqlite_is_healthy(db)
        if not healthy:
            QMessageBox.warning(None, 'Banco local indisponível',
                'Nenhum snapshot foi adotado automaticamente. O banco foi preservado.\n' + reason +
                '\nUse a ferramenta a seguir para preparar uma restauração manual e depois reabra o aplicativo.')
            SnapshotDialog().exec()
            return
    logging.debug('QApplication criado')
    window = DriveFileGalleryApp()
    logging.debug('DriveFileGalleryApp instanciado')
    window.show()
    app.setQuitOnLastWindowClosed(True)
    if was_new:
        def offer_snapshot():
            if QMessageBox.question(window, 'Nova instalação',
                    'Este catálogo é novo. Deseja escolher um snapshot para inicializá-lo? '
                    'Você também pode fazer isso depois em Sistema / Avançado > Snapshots.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                SnapshotDialog(window, indexer=window.indexer).exec()
        QTimer.singleShot(0, offer_snapshot)
    logging.debug('Janela principal exibida')
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
