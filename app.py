# app.py - Vox Imago
# Ponto de entrada principal do aplicativo.
# Inicializa a interface gráfica e exibe a janela principal.

import sys
import logging
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
    from PyQt6.QtWidgets import QApplication
    from src.ui.ui import DriveFileGalleryApp

    app = QApplication(sys.argv)
    logging.debug('QApplication criado')
    window = DriveFileGalleryApp()
    logging.debug('DriveFileGalleryApp instanciado')
    window.show()
    logging.debug('Janela principal exibida')
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
