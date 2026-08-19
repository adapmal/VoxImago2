# app.py - Vox Imago
# Ponto de entrada principal do aplicativo.
# Inicializa a interface gráfica e exibe a janela principal.

import sys
import os
import logging
from PyQt6.QtWidgets import QApplication
from src.ui.ui import DriveFileGalleryApp


def setup_logging():
    os.makedirs('logs', exist_ok=True)
    log_format = '%(asctime)s [%(levelname)s] %(message)s'
    handlers = [
        logging.FileHandler(os.path.join('logs', 'voximago.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
    logging.basicConfig(level=logging.INFO, format=log_format, handlers=handlers)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))


def main():
    setup_logging()
    sys.excepthook = handle_exception
    logging.info("=== Iniciando VoxImago.MB ===")
    app = QApplication(sys.argv)
    print('DEBUG: QApplication criado')
    window = DriveFileGalleryApp()
    print('DEBUG: DriveFileGalleryApp instanciado')
    window.show()
    print('DEBUG: window.show() chamado')
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
