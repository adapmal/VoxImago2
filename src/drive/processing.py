"""
Módulo processing

Este módulo trata do processamento de arquivos e dados relacionados ao Google Drive
no VoxImago.MB. Inclui funções para análise, transformação, preparação e manipulação
de arquivos durante operações de sincronização, upload, download e fusão.
"""

import logging
from .drive_sync import DriveSync
from .drive_dialog import DriveFolderDialog
from PyQt6.QtCore import QThread, QTimer
from PyQt6.QtWidgets import QProgressDialog, QMessageBox


def start_drive_folder_processing(parent, service, indexer, force_dialog=False):
    from src.utils.utils import load_settings

    if force_dialog:
        folder_dialog = DriveFolderDialog(service, parent)
        if folder_dialog.exec() != folder_dialog.DialogCode.Accepted:
            return False  # Usuário cancelou
        folder_dialog.save_settings()
        selected_folders = folder_dialog.get_selected_folders()
        if not selected_folders:
            QMessageBox.warning(
                parent, "Aviso", "Nenhuma pasta do Drive selecionada.")
            return False
    else:
        settings = load_settings()
        selected_folders = settings.get('drive_folders', [])

        if not selected_folders:
            folder_dialog = DriveFolderDialog(service, parent)
            if folder_dialog.exec() != folder_dialog.DialogCode.Accepted:
                return False
            folder_dialog.save_settings()
            selected_folders = folder_dialog.get_selected_folders()
            if not selected_folders:
                QMessageBox.warning(
                    parent, "Aviso", "Nenhuma pasta do Drive selecionada.")
                return False
        else:
            folder_names = []
            for folder_id in selected_folders:
                if folder_id.startswith('0') and len(folder_id) > 10:
                    folder_names.append("Shared Drive (Banco de Imagens)")
                elif folder_id == 'root':
                    folder_names.append("Meu Drive")
                else:
                    folder_names.append(f"Pasta: {folder_id[:15]}...")

            logging.info("Usando configuracoes salvas: %s", ', '.join(folder_names))
            logging.info(f"📁 Usando configurações salvas: {selected_folders}")
    thread = QThread()
    worker = DriveSync(service, db_name=indexer.db_name,
                       selected_folders=selected_folders)
    worker.moveToThread(thread)
    progress = QProgressDialog(
        "Sincronizando arquivos...", "Cancelar", 0, 100, parent)
    progress.setWindowModality(parent.windowModality())
    progress.setAutoClose(True)
    progress.setAutoReset(True)
    progress.setFixedSize(400, 150)
    progress.setStyleSheet("""
        QProgressBar { min-height: 25px; border: 1px solid #ccc; border-radius: 5px; text-align: center; }
        QProgressBar::chunk { background-color: #4CAF50; border-radius: 5px; }
    """)

    total_files_to_sync = [0]

    def on_total_found(total):
        total_files_to_sync[0] = total
        logging.debug("Total de arquivos a sincronizar: %s", f'{total:,}')
        if hasattr(parent, 'update_drive_sync_progress'):
            parent.update_drive_sync_progress(
                0, f"Preparando {total:,} arquivos do Drive..."
            )

    def update_progress(value, msg):
        if value < 0:
            progress.setRange(0, 0)
        else:
            if progress.maximum() == 0:
                progress.setRange(0, 100)
            progress.setValue(value)
        progress.setLabelText(msg)
        if value >= 100:
            progress.setValue(100)
            progress.setLabelText("Sincronização concluída.")

    def update_status(msg):
        progress.setLabelText(msg)
        logging.debug("Status da sincronizacao: %s", msg)

    cleanup_executed = [False]

    def cleanup_thread():
        if cleanup_executed[0]:
            logging.debug("Cleanup da sincronizacao ja executado.")
            return

        cleanup_executed[0] = True
        logging.debug("Iniciando cleanup do thread de sincronizacao.")

        try:
            if 'worker' in locals() and worker:
                logging.debug("Parando worker de sincronizacao.")
                worker.terminate()
                worker.is_running = False

            if 'progress' in locals() and progress:
                try:
                    progress.close()
                    logging.debug("Dialogo de progresso fechado.")
                except:
                    pass

            if 'worker' in locals() and worker:
                worker.is_running = False

            if 'thread' in locals() and thread and thread.isRunning():
                logging.debug("Aguardando thread finalizar de forma cooperativa.")
                thread.quit()
                if not thread.wait(5000):
                    logging.warning("Thread de sincronizacao demorou para finalizar.")
                    thread.wait(2000)
                else:
                    logging.debug("Thread de sincronizacao finalizada.")

            thread_still_running = ('thread' in locals() and thread and thread.isRunning())
            if not thread_still_running:
                if 'worker' in locals() and worker:
                    try:
                        worker.deleteLater()
                        logging.debug("Worker de sincronizacao liberado.")
                    except:
                        pass
                if 'thread' in locals() and thread:
                    try:
                        thread.deleteLater()
                        logging.debug("Thread de sincronizacao liberada.")
                    except:
                        pass
            else:
                logging.warning(
                    "Thread de sincronizacao ainda ativa; deleteLater adiado."
                )

            logging.debug("Cleanup da sincronizacao concluido.")
        except Exception as e:
            logging.exception("Erro no cleanup da sincronizacao: %s", e)
            try:
                if 'progress' in locals() and progress:
                    progress.close()
            except Exception:
                pass

    def on_sync_finished():
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        logging.info("[%s] Sincronizacao Drive finalizada.", timestamp)

        try:
            if hasattr(parent, 'on_drive_sync_finished'):
                parent.on_drive_sync_finished()
        except Exception as e:
            logging.warning("Erro ao notificar termino da sincronizacao: %s", e)

        cleanup_thread()

    def on_sync_failed(error):
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        logging.error("[%s] Sincronizacao Drive falhou: %s", timestamp, error)

        try:
            if hasattr(parent, 'on_drive_sync_failed'):
                parent.on_drive_sync_failed(str(error))
        except Exception as e:
            logging.warning("Erro ao notificar falha da sincronizacao: %s", e)

        cleanup_thread()

    def on_canceled():
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        logging.info("[%s] Cancelamento da sincronizacao solicitado.", timestamp)

        try:
            if hasattr(parent, 'drive_sync_running'):
                parent.drive_sync_running = False
                logging.debug("Flag drive_sync_running liberada apos cancelamento.")
            if hasattr(parent, 'progress_bar'):
                parent.progress_bar.setVisible(False)
            if hasattr(parent, 'status_bar'):
                parent.status_bar.showMessage(
                    "Sincronização do Drive cancelada.", 4000
                )
            if hasattr(parent, '_run_deferred_incremental_sync'):
                parent._run_deferred_incremental_sync()
        except Exception as e:
            logging.warning("Erro ao liberar estado apos cancelamento: %s", e)

        cleanup_thread()

    def emergency_cleanup():
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        logging.info("[%s] Cleanup de emergencia da sincronizacao.", timestamp)
        try:
            if 'worker' in locals() and worker:
                worker.is_running = False
            if 'thread' in locals() and thread and thread.isRunning():
                thread.quit()
                thread.wait(2000)
        except:
            pass

    import atexit
    atexit.register(emergency_cleanup)

    worker.total_files_found.connect(on_total_found)
    worker.progress_update.connect(update_progress)
    if hasattr(parent, 'update_drive_sync_progress'):
        worker.progress_update.connect(parent.update_drive_sync_progress)
    worker.update_status.connect(update_status)
    if hasattr(parent, 'update_drive_status_message'):
        worker.update_status.connect(parent.update_drive_status_message)
    worker.sync_finished.connect(on_sync_finished)
    worker.sync_failed.connect(on_sync_failed)

    progress.canceled.connect(on_canceled)
    thread.started.connect(worker.run)

    progress.show()

    import datetime
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    folder_info = f"{len(selected_folders)} pasta(s) selecionada(s)"
    logging.info(
        "[%s] Thread de sincronizacao iniciada para %s.",
        timestamp, folder_info,
    )
    if hasattr(parent, 'update_drive_sync_progress'):
        parent.update_drive_sync_progress(
            -1, "Preparando sincronização completa do Google Drive..."
        )
    thread.start()
    return True
