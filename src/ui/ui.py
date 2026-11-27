'''
 ui.py - Interface principal do Vox Imago
 Contém DriveFileGalleryApp e FileDetailsPanel.
 Gerencia a interface gráfica, eventos, filtros, autenticação e integração com os módulos do projeto.
'''

import os
import webbrowser
import logging
from src.authentication import AuthWorker
from src.services.local_scan import LocalScan
from src.drive.processing import start_drive_folder_processing

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QMessageBox, QDialog, QDialogButtonBox, QProgressBar, QPushButton, QSplitter, QVBoxLayout, QProgressDialog, QListView, QSystemTrayIcon, QMenu,
    QAbstractItemView, QApplication
)

from PyQt6.QtGui import QIcon, QAction
from src.ui.details_panel import FileDetailsPanel
from PyQt6.QtCore import Qt, QTimer, QStringListModel, QDate, QThread, pyqtSignal, QThreadPool, QRunnable, QMetaObject, QSize, QItemSelectionModel, QPoint
from src.ui.list_view import FileListView
from src.database.database import FileIndexer
from src.database.search import SearchEngine
from src.ui.local_dialog import OptionsDialog
from src.drive.drive_dialog import DriveFolderDialog
from src.utils.utils import load_settings, save_settings, filter_existing_files
from src.ui.thumbnails import ThumbnailCache, ThumbnailManager, FileListDelegate
from src.ui.main_bar import MainBar
from src.ui.list_model import FileListModel
from src.ui.list_update import list_update
from src.ui.folder_tree_widget import FolderTreeWidget
from src.ui.vocab_panel import VocabPanel


SCOPES = ['https://www.googleapis.com/auth/drive']


class ServiceStatusDialog(QDialog):
    def __init__(self, status_text, show_progress=False, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self.setWindowTitle("Status dos Serviços")
        self.setModal(True)
        self.resize(400, 200)

        layout = QVBoxLayout(self)

        self.text_label = QLabel(status_text)
        self.text_label.setWordWrap(True)
        layout.addWidget(self.text_label)

        self.progress_bar = QProgressBar(self)
        if show_progress:
            self.progress_bar.setRange(0, 0)
            layout.addWidget(self.progress_bar)
        else:
            self.progress_bar.hide()

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        button_box.accepted.connect(self.accept)
        layout.addWidget(button_box)

    def update_status(self, new_text):
        self.text_label.setText(new_text)


class DriveFileGalleryApp(QMainWindow):
    thumbnail_generated = pyqtSignal(str)

    def _reindex_local_files(self):
        scan_paths = self.config_mgr.get_resolved_scan_paths(persist=True)
        if scan_paths and len(scan_paths) > 0:
            self._start_local_scan(scan_paths)
            self.status_bar.showMessage("Reindexando arquivos locais...", 5000)
        else:
            self.status_bar.showMessage(
                "Nenhuma pasta local configurada para reindexação.", 5000)

    def check_drive_updates_available(self):
        if not self.is_authenticated or not self.service:
            return
        if self.drive_sync_running or (self.inc_sync_thread and self.inc_sync_thread.isRunning()):
            return
        try:
            if self.drive_check_thread and self.drive_check_thread.isRunning():
                return
        except RuntimeError:
            pass

        from src.drive.incremental_sync import DriveUpdateCheckWorker
        self.drive_check_thread = QThread(self)
        self.drive_check_worker = DriveUpdateCheckWorker(self.service, self.config_mgr)
        self.drive_check_worker.moveToThread(self.drive_check_thread)
        self.drive_check_thread.started.connect(self.drive_check_worker.run)

        def on_check_done(has_updates, info_text):
            self._set_bottom_drive_status_badge(has_updates, info_text)
            if hasattr(self, 'main_bar') and self.main_bar:
                self.main_bar.set_drive_updates_available(has_updates, info_text)
            if self.drive_check_thread:
                self.drive_check_thread.quit()
                self.drive_check_thread.wait(1000)
                self.drive_check_thread.deleteLater()
                self.drive_check_thread = None
            if self.drive_check_worker:
                self.drive_check_worker.deleteLater()
                self.drive_check_worker = None

        self.drive_check_worker.check_finished.connect(on_check_done)
        self.drive_check_thread.start()

    def _open_hash_deduplicator(self, initial_folders=None):
        from src.ui.hash_dedup_dialog import HashDedupDialog
        dlg = HashDedupDialog(
            self,
            indexer=self.indexer,
            drive_service=self.service,
            initial_folders=initial_folders,
        )
        dlg.exec()

    def _open_shortcut_migration(self):
        from src.ui.shortcut_migration_dialog import ShortcutMigrationDialog
        dlg = ShortcutMigrationDialog(self, indexer=self.indexer, drive_service=self.service)
        dlg.exec()

    def _open_name_disambiguation(self):
        from src.ui.name_disambiguation_dialog import NameDisambiguationDialog
        dlg = NameDisambiguationDialog(self, indexer=self.indexer, drive_service=self.service)
        dlg.exec()

    def _open_shortcut_audit(self):
        from src.ui.shortcut_audit_dialog import ShortcutAuditDialog
        dlg = ShortcutAuditDialog(self, indexer=self.indexer, drive_service=self.service)
        dlg.exec()

    def _open_insync_suffix_repair(self):
        from src.ui.insync_suffix_repair_dialog import InsyncSuffixRepairDialog
        dlg = InsyncSuffixRepairDialog(self, indexer=self.indexer, drive_service=self.service)
        dlg.exec()

    def _open_raw_tag_sync(self):
        from src.ui.raw_tag_sync_dialog import RawTagSyncDialog
        dlg = RawTagSyncDialog(self, indexer=self.indexer, drive_service=self.service)
        dlg.exec()

    def _start_path_reconciliation(self):
        if self.maintenance_running or self.queue_execution_running or self.drive_sync_running:
            QMessageBox.information(
                self, "Operação em andamento",
                "Aguarde a operação atual terminar antes de reconciliar os caminhos."
            )
            return
        try:
            if self.reconcile_thread and self.reconcile_thread.isRunning():
                return
        except RuntimeError:
            pass

        scan_paths = self.config_mgr.get_resolved_scan_paths(persist=True)
        if not scan_paths:
            QMessageBox.warning(self, "Aviso", "Nenhuma pasta local configurada.")
            return

        from src.services.path_reconciler import PathReconcileWorker
        from PyQt6.QtWidgets import QProgressDialog

        self.maintenance_running = True
        progress = QProgressDialog(
            "Reconciliando endereços de arquivos movidos...", "Cancelar", 0, 100, self
        )
        progress.setWindowTitle("Reconciliação de Caminhos")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setFixedSize(420, 140)
        progress.setValue(0)

        self.reconcile_thread = QThread(self)
        self.reconcile_worker = PathReconcileWorker(self.indexer.db_name, scan_paths)
        self.reconcile_worker.moveToThread(self.reconcile_thread)

        def on_prog(pct, msg):
            progress.setValue(max(0, min(100, pct)))
            progress.setLabelText(msg)

        def cleanup():
            self.maintenance_running = False
            if self.reconcile_thread:
                self.reconcile_thread.quit()
                self.reconcile_thread.wait(2000)
                self.reconcile_thread.deleteLater()
                self.reconcile_thread = None
            if self.reconcile_worker:
                self.reconcile_worker.deleteLater()
                self.reconcile_worker = None

        def on_done(result):
            progress.close()
            cleanup()
            self._force_refresh_after_sync()
            msg = (
                f"Reconciliação concluída em {result.get('elapsed_sec', 0):.1f}s!\n\n"
                f"• Arquivos movidos atualizados: {result.get('relocated', 0):,}\n"
                f"• Duplicatas fundidas: {result.get('merged', 0):,}\n"
                f"• Novos arquivos indexados: {result.get('new_files', 0):,}\n"
                f"• Novas pastas adicionadas: {result.get('new_folders', 0):,}\n"
                f"• Pastas obsoletas removidas: {result.get('removed_folders', 0):,}"
            )
            QMessageBox.information(self, "Reconciliação de Caminhos", msg)
            self.status_bar.showMessage("✅ Caminhos de arquivos movidos reconciliados com sucesso.", 5000)

        def on_err(err_msg):
            progress.close()
            cleanup()
            if "cancelada" not in str(err_msg).lower():
                QMessageBox.warning(self, "Aviso", f"Reconciliação interrompida: {err_msg}")

        progress.canceled.connect(self.reconcile_worker.cancel)
        self.reconcile_worker.progress_update.connect(on_prog)
        self.reconcile_worker.finished.connect(on_done)
        self.reconcile_worker.failed.connect(on_err)
        self.reconcile_thread.started.connect(self.reconcile_worker.run)
        progress.show()
        self.reconcile_thread.start()

    def __init__(self):
        print('DEBUG: DriveFileGalleryApp.__init__ INICIO')
        super().__init__()
        from src.ui.popup_text_selection import install_popup_text_selection
        install_popup_text_selection()
        self.setWindowTitle("VoxImago - Galeria de Arquivos")
        self.setMinimumSize(1260, 660)

        self.scroll_loading = False

        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(3)


        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon("assets/VMico.png"))
        self.tray_icon.setToolTip("Vox Imago - Galeria de Arquivos")

        self.tray_menu = QMenu()
        self.show_action = QAction("Mostrar Janela", self)
        self.show_action.triggered.connect(self.show)
        self.tray_menu.addAction(self.show_action)

        self.tray_menu.addSeparator()

        self.status_action = QAction("Status dos Serviços", self)
        self.status_action.triggered.connect(self.show_service_status)
        self.tray_menu.addAction(self.status_action)

        self.tray_menu.addSeparator()

        self.quit_action = QAction("Sair", self)
        self.quit_action.triggered.connect(self.close)
        self.tray_menu.addAction(self.quit_action)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)

        self.tray_icon.show()

        from src.utils.config_manager import ConfigManager
        self.config_mgr = ConfigManager()
        self.service = None
        self.indexer = FileIndexer()
        self.search_engine = SearchEngine(self.indexer)
        self.current_view = 'local'
        self.current_page = 0
        self.page_size = 50
        self.search_term = ""
        self.current_filter = "media"
        self.current_sort = "created_desc"
        self.current_folder_id = None
        self.advanced_filters = {'category': 'media'}
        self.explorer_special_active = True
        self.is_loading = False
        self.all_files_loaded = False
        self.is_authenticated = False
        self.drive_sync_after_local_scan = False
        self.drive_sync_running = False
        self.queue_execution_running = False
        self.maintenance_running = False
        self.full_sync_deferred = False
        self.incremental_sync_deferred = False
        self.incremental_sync_deferred_manual = False

        self.auth_thread = None
        self.auth_worker = None
        self.local_scan_thread = None
        self.inc_sync_thread = None
        self.local_scan_worker = None
        self.reconcile_thread = None
        self.reconcile_worker = None
        self.drive_check_thread = None
        self.drive_check_worker = None
        self.drive_badge_timer = QTimer(self)
        self.drive_badge_timer.timeout.connect(self.check_drive_updates_available)
        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.handle_search_request)

        self.token_check_timer = QTimer()
        self.token_check_timer.timeout.connect(self._check_token_refresh)
        self.token_check_timer.start(30 * 60 * 1000)

        self.suggestion_timer = QTimer()
        self.suggestion_timer.setSingleShot(True)
        self.suggestion_timer.timeout.connect(self.update_search_suggestions)
        
        self.incremental_sync_timer = QTimer()
        self.incremental_sync_timer.timeout.connect(lambda: self._run_incremental_sync(force_manual=False))
        self.incremental_sync_timer.start(15 * 60 * 1000)  # 15 minutos

        # Backup local independente da sincronização e da distribuição manual.
        self.catalog_backup_worker = None
        self.catalog_backup_timer = QTimer(self)
        self.catalog_backup_timer.timeout.connect(self._backup_local_catalog)
        self.catalog_backup_timer.start(60 * 60 * 1000)
        QTimer.singleShot(60000, self._backup_local_catalog)


        self.completer_model = QStringListModel()

        self._setup_ui()
        self.show()

        self.thumbnail_generated.connect(self.refresh_details_panel_thumbnail)

        settings = load_settings()
        self.show_drive_metadata = settings.get('show_drive_metadata', True)
        scan_paths = settings.get('scan_paths')

        has_saved_config = bool(scan_paths and len(scan_paths) > 0)
        self.auth_worker = AuthWorker()
        has_saved_auth = self.auth_worker.is_authenticated() if self.auth_worker else False
        refreshed_service = None
        if self.auth_worker:
            refreshed_service = self.auth_worker.refresh_token()
            if refreshed_service:
                self.service = refreshed_service
                has_saved_auth = True
        if has_saved_auth:
            self._start_auth(auto=True)
        else:
            self.update_ui_for_auth_state(False)
            list_update.load_next_batch(self)

        if not has_saved_config:
            self.current_view = 'local'
            self.current_folder_id = None
            try:
                existing_count = self.indexer.conn.execute(
                    "SELECT COUNT(*) FROM files WHERE source='local'").fetchone()[0]
                if existing_count == 0:
                    list_update.clear_display(self)
                    self.all_files_loaded = True
                    self.loading_label.setText("Nenhum arquivo encontrado.")
                else:
                    print(
                        f"📁 {existing_count} arquivos locais encontrados no banco")
                    list_update.load_next_batch(self)
            except Exception as e:
                print(f"⚠️ Erro ao verificar arquivos existentes: {e}")
                list_update.clear_display(self)
                self.all_files_loaded = True
                self.loading_label.setText("Nenhum arquivo encontrado.")
            self.loading_label.show()

    def close(self):
        try:
            super().close()
        except Exception as e:
            QMessageBox.critical(
                self, "Erro", f"Ocorreu um erro ao fechar o aplicativo:\n{e}")

    def set_view_mode(self, mode=None):
        if mode is None:
            if self.action_grid_view.isChecked():
                mode = "grid"
            elif self.action_list_view.isChecked():
                mode = "list"
            else:
                mode = "grid"

        self.file_list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.file_list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.file_list_view.setMovement(QListView.Movement.Static)

        if mode == "grid":
            self.file_list_view.setViewMode(QListView.ViewMode.IconMode)
            self.file_list_view.setWrapping(True)
            self.file_list_view.setIconSize(QSize(200, 200))
            self.file_list_view.setGridSize(QSize(240, 280))
            self.file_list_view.setSpacing(8)
            self.action_grid_view.setChecked(True)
            self.action_list_view.setChecked(False)
            self.file_list_view.verticalScrollBar().setSingleStep(60)
            self.file_list_view.verticalScrollBar().setPageStep(240)
        else:
            self.file_list_view.setViewMode(QListView.ViewMode.ListMode)
            self.file_list_view.setWrapping(False)
            self.file_list_view.setIconSize(QSize(48, 48))
            self.file_list_view.setGridSize(QSize())
            self.file_list_view.setSpacing(4)
            self.action_grid_view.setChecked(False)
            self.action_list_view.setChecked(True)
            self.file_list_view.verticalScrollBar().setSingleStep(20)
            self.file_list_view.verticalScrollBar().setPageStep(60)

    def on_tray_icon_activated(self, reason):
        try:
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                if self.isMinimized() or not self.isVisible():
                    self.showNormal()
                    self.raise_()
                    self.activateWindow()
                else:
                    self.showMinimized()
        except Exception as e:
            QMessageBox.critical(
                self, "Erro", f"Ocorreu um erro ao ativar o ícone da bandeja:\n{e}")

    def show_service_status(self):
        try:
            status_lines = []

            if self.is_authenticated:
                status_lines.append("✓ Autenticado no Google Drive")
            else:
                status_lines.append("✗ Não autenticado no Google Drive")

            if self.local_scan_thread and self.local_scan_thread.isRunning():
                status_lines.append("⟳ Escaneamento local em andamento...")
            else:
                try:
                    local_count = self.indexer.get_file_count(source='local')
                    status_lines.append(
                        f"✓ Arquivos locais indexados: {local_count}")
                except:
                    status_lines.append("✗ Erro ao acessar arquivos locais")

            try:
                drive_count = self.indexer.get_file_count(source='drive')
                status_lines.append(
                    f"✓ Arquivos do Drive indexados: {drive_count}")
            except:
                status_lines.append("✗ Erro ao acessar arquivos do Drive")

            if self.is_loading:
                status_lines.append("⟳ Carregando arquivos...")

            if self.current_view == 'local':
                status_lines.append("👁️ Visualizando: Arquivos Locais")
            elif self.current_view == 'drive':
                status_lines.append("👁️ Visualizando: Google Drive")
            else:
                status_lines.append("👁️ Visualizando: Unificado")

            if self.search_term:
                status_lines.append(f"🔍 Pesquisa ativa: '{self.search_term}'")

            if self.explorer_special_active:
                status_lines.append("🏠 Modo Explorer Local ativo")

            status_text = "\n".join(status_lines)
            show_progress = (
                self.local_scan_thread and self.local_scan_thread.isRunning())
            dialog = ServiceStatusDialog(status_text, show_progress, self)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(
                self, "Erro", f"Ocorreu um erro ao exibir o status dos serviços:\n{e}")

    def _check_token_refresh(self):
        if not self.is_authenticated or not self.auth_worker:
            return
        try:
            service = self.auth_worker.refresh_token()
            if service:
                self.service = service
        except Exception as e:
            print(f"Erro ao atualizar token: {e}")

    def _setup_ui(self):
        print('DEBUG: _setup_ui chamado')
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.main_bar = MainBar(self)
        main_layout.addWidget(self.main_bar)

        self.vocab_panel = VocabPanel(parent=self)
        self.vocab_panel.tagSelected.connect(self.on_vocab_tag_selected)
        self.vocab_panel.vocabUpdated.connect(self._on_vocab_updated)
        main_layout.addWidget(self.vocab_panel)

        self.main_bar.vocab_toggle_btn.clicked.connect(self.vocab_panel.toggle_visibility)

        self.action_grid_view = self.main_bar.action_grid_view
        self.action_list_view = self.main_bar.action_list_view

        self.main_bar.sync_now_requested.connect(self._run_incremental_sync)

        self.main_bar.action_scan_options.triggered.connect(
            self._show_scan_options)
        self.main_bar.action_sync_drive.triggered.connect(
            self._start_drive_sync)
        self.main_bar.action_reindex.triggered.connect(
            self._reindex_local_files)
        self.main_bar.action_reconcile_paths.triggered.connect(
            self._start_path_reconciliation)
        self.main_bar.action_insync_suffix_repair.triggered.connect(
            self._open_insync_suffix_repair)
        self.main_bar.action_raw_tag_sync.triggered.connect(
            self._open_raw_tag_sync)
        self.main_bar.action_deduplicate_hash.triggered.connect(
            self._open_hash_deduplicator)
        self.main_bar.action_shortcut_migration.triggered.connect(self._open_shortcut_migration)
        self.main_bar.action_name_disambiguation.triggered.connect(self._open_name_disambiguation)
        self.main_bar.action_shortcut_audit.triggered.connect(self._open_shortcut_audit)
        self.main_bar.action_clear_cache.triggered.connect(
            self.clear_thumbnail_cache)
        self.main_bar.action_advanced_settings.triggered.connect(
            self._open_advanced_settings)
        self.main_bar.action_explorer.triggered.connect(
            self.toggle_explorer_special)
        self.main_bar.action_grid_view.triggered.connect(
            lambda: self.set_view_mode(None))
        self.main_bar.action_list_view.triggered.connect(
            lambda: self.set_view_mode(None))
        self.main_bar.login_button.clicked.connect(self._start_auth)
        self.main_bar.logout_button.clicked.connect(self.handle_logout)
        self.main_bar.search_entry.textChanged.connect(
            self.handle_search_input)
        self.main_bar.sort_combo.activated.connect(self.change_sort_order)
        self.main_bar.category_combo.activated.connect(
            self.change_filter_type_combo)
        self.main_bar.tag_filter_combo.activated.connect(
            self.change_tag_filter_combo)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.file_list_view = FileListView()
        self.file_list_model = FileListModel([])
        self.file_list_view.setModel(self.file_list_model)
        self.file_list_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list_delegate = FileListDelegate(
            self.file_list_view, indexer=self.indexer)
        self.file_list_delegate.requestThumbnail.connect(
            self.handle_request_thumbnail)
        self.file_list_view.setItemDelegate(self.file_list_delegate)
        self.file_list_view.setUniformItemSizes(True)
        self.file_list_view.setMinimumHeight(400)
        self.file_list_view.setMinimumWidth(500)
        self.file_list_view.filesSelected.connect(self.on_files_selected)
        self.file_list_view.fileDoubleClicked.connect(self.on_double_click)
        self.file_list_view.verticalScrollBar().valueChanged.connect(self.on_scroll)

        self.set_view_mode("grid")
        self.file_list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.file_list_view.setDragEnabled(True)
        self.file_list_view.setAcceptDrops(False)
        self.file_list_view.setDropIndicatorShown(False)
        self.file_list_view.setDragDropMode(
            QAbstractItemView.DragDropMode.DragOnly)

        self.folder_tree = FolderTreeWidget(parent=self)
        self.folder_tree.folderSelected.connect(self.on_folder_tree_selected)
        self.folder_tree.filesDroppedOnFolder.connect(self.on_files_dropped_on_folder)
        self.folder_tree.compareFolderHashRequested.connect(
            lambda folder_path: self._open_hash_deduplicator(initial_folders=[folder_path])
        )

        self._prev_move_items, self._prev_delete_items = self._staging_action_snapshots()
        self._suppress_staging_reload = False
        self.main_bar.staging_queue.queueChanged.connect(self._on_staging_queue_changed)
        self.queue_confirmation_timer = QTimer(self)
        self.queue_confirmation_timer.setInterval(60000)
        self.queue_confirmation_timer.timeout.connect(self._check_queue_confirmations)
        self.queue_confirmation_timer.start()
        self._queue_confirmation_worker = None

        self.details_panel = FileDetailsPanel(self)
        
        self.file_list_view.deleteRequested.connect(self.details_panel._delete_file_action)
        self.file_list_view.selectFolderRequested.connect(self.select_folder_for_file)

        self.splitter.addWidget(self.folder_tree)
        self.splitter.addWidget(self.file_list_view)
        self.splitter.addWidget(self.details_panel)

        self.details_panel.setMinimumWidth(320)
        self.details_panel.setMaximumWidth(550)
        self.splitter.setSizes([240, 700, 360])

        main_layout.addWidget(self.splitter)

        self.loading_label = QLabel("Carregando...")
        self.loading_label.setFixedHeight(30)
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.hide()
        main_layout.addWidget(self.loading_label)

        self.status_bar = self.statusBar()
        self.drive_status_badge = QLabel("", self)
        self.drive_status_badge.setVisible(False)
        self.status_bar.addPermanentWidget(self.drive_status_badge)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setMinimumWidth(280)
        self.progress_bar.setMaximumWidth(420)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        # Widget permanente: mensagens temporarias da QStatusBar nao o ocultam.
        self.status_bar.addPermanentWidget(self.progress_bar)

        self.sync_cancel_btn = QPushButton("⏹️ Parar", self)
        self.sync_cancel_btn.setToolTip("Interromper a sincronização atual mantendo o log da última sincronia completa.")
        self.sync_cancel_btn.setStyleSheet(
            "background-color: #DC3545; color: white; font-weight: bold; padding: 2px 8px; border-radius: 4px;"
        )
        self.sync_cancel_btn.setVisible(False)
        self.sync_cancel_btn.clicked.connect(self._cancel_incremental_sync)
        self.status_bar.addPermanentWidget(self.sync_cancel_btn)
        self._set_bottom_drive_status_badge(False)

        self.all_loaded_label = QLabel("Todos os arquivos foram carregados.")
        self.all_loaded_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.all_loaded_label.setFixedHeight(30)
        self.all_loaded_label.hide()
        main_layout.addWidget(self.all_loaded_label)

        self.update_ui_for_auth_state(False)
        self.update_filter_buttons()

        self.setStyleSheet("""
            /* Fundo principal escuro */
            QMainWindow {
                background-color: #121212;
                font-family: 'Segoe UI', sans-serif;
                font-size: 12px;
                color: #ffffff;
            }

    /* Barra unificada: fundo escuro com sombra sutil */
    QFrame {
        background-color: #1e1e1e;
        border: 1px solid #333333;
        border-radius: 10px;
    }            /* Botões: menores e consistentes */
            QPushButton {
                background-color: #1976d2;
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;  /* Reduzido de 10px 16px para 6px 12px */
                font-weight: 500;
                font-size: 12px;  /* Tamanho de fonte menor */
            }
            QPushButton:hover {
                background-color: #1565c0;
            }
            QPushButton:pressed {
                background-color: #0d47a1;
            }
            QPushButton:disabled {
                background-color: #424242;
                color: #9e9e9e;
            }

            QPushButton{
                font-weight: bold;  /* Destaque o botão */
            }

            QPushButton#more_filters_button:checked {
                background-color: #0d47a1;  /* Azul mais escuro quando ativo */
            }

            /* QToolButton (botão "Ferramentas"): mesmo estilo dos QPushButton */
            QToolButton {
                background-color: #1976d2;
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;  /* Mesmo padding reduzido */
                font-weight: 500;
                font-size: 12px;
            }
            QToolButton:hover {
                background-color: #1565c0;
            }
            QToolButton:pressed {
                background-color: #0d47a1;
            }
            QToolButton:disabled {
                background-color: #424242;
                color: #9e9e9e;
            }
            QToolButton::menu-indicator {
                image: none;  /* Remove ícone padrão do menu */
            }

            /* Combos: estilo escuro */
            QComboBox {
                border: 1px solid #555555;
                border-radius: 6px;
                padding: 4px 8px;  /* Padding menor */
                background-color: #2c2c2c;
                color: #ffffff;
                font-size: 12px;
            }
            QComboBox:hover {
                border-color: #1976d2;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox::down-arrow {
                image: url(down_arrow_dark.png);
                width: 12px;
                height: 12px;
            }
            QComboBox QAbstractItemView {
                background-color: #2c2c2c;
                color: #ffffff;
                selection-background-color: #1976d2;
            }

            /* Labels: texto branco */
            QLabel {
                color: #ffffff;
                font-size: 12px;
            }
            QLabel#title {
                font-weight: bold;
                font-size: 14px;
                color: #1976d2;
            }

            #advanced_filters_widget QLabel {
                margin: 0px;
                padding: 0px;
                font-size: 12px;
            }
            #advanced_filters_widget QComboBox,
            #advanced_filters_widget QDateEdit,
            #advanced_filters_widget QCheckBox,
            #advanced_filters_widget QPushButton {
                margin: 0px;
                padding: 2px 4px;
            }
            #advanced_filters_widget {
                max-height: 40px;
            }
            /* Lista de arquivos: fundo alternado escuro */
            QListView {
                background-color: #1e1e1e;
                border: 1px solid #333333;
                border-radius: 8px;
                alternate-background-color: #2c2c2c;
            }
            QListView::item {
                padding: 6px;  /* Padding menor */
                border-bottom: 1px solid #333333;
                color: #ffffff;
                border: 1px solid transparent;
            }
            QListView::item:selected {
                background-color: #1976d2;
                color: #ffffff;
                border-color: #1976d2;
            }

            /* Painel de detalhes: card escuro */
            FileDetailsPanel {
                background-color: #1e1e1e;
                border: 1px solid #333333;
                border-radius: 10px;
            }
            FileDetailsPanel QLabel {
                font-size: 12px;
                margin: 3px 0;  /* Margem menor */
                color: #ffffff;
            }

            /* Barra de progresso: azul para sucesso */
            QProgressBar {
                border: 1px solid #555555;
                border-radius: 4px;
                text-align: center;
                background-color: #2c2c2c;
            }
            QProgressBar::chunk {
                background-color: #1976d2;
                border-radius: 4px;
            }

            /* Status bar: fundo escuro */
            QStatusBar {
                background-color: #1e1e1e;
                border-top: 1px solid #333333;
                color: #ffffff;
            }

            /* Entradas de texto: fundo escuro */
            QLineEdit {
                background-color: #2c2c2c;
                border: 1px solid #555555;
                border-radius: 6px;
                padding: 4px;  /* Padding menor */
                color: #ffffff;
            }
            QLineEdit:focus {
                border-color: #1976d2;
            }
        """)

        self._pending_thumbs = set()

    def handle_request_thumbnail(self, file_item):
        try:
            if not file_item:
                return
            if ThumbnailCache.is_thumbnail_cached(file_item):
                return
            key = file_item.get('id') or file_item.get('path')
            if not key or key in self._pending_thumbs:
                return
            self._pending_thumbs.add(key)

            class _ThumbRunnable(QRunnable):
                def __init__(self, app_ref, item, key):
                    super().__init__()
                    self.app_ref = app_ref
                    self.item = item
                    self.key = key

                def run(self):
                    try:
                        src = (self.item.get('source') or '').lower()
                        if src == 'local':
                            ThumbnailManager.generate_local_thumbnail(
                                self.item, size=(256, 256))
                    except Exception:
                        pass
                    finally:
                        try:
                            QMetaObject.invokeMethod(
                                self.app_ref.file_list_view.viewport(),
                                "update",
                                Qt.ConnectionType.QueuedConnection
                            )
                        except Exception:
                            pass
                        try:
                            self.app_ref.thumbnail_generated.emit(self.key)
                        except Exception:
                            pass
                        try:
                            self.app_ref._pending_thumbs.discard(self.key)
                        except Exception:
                            pass

            self.thread_pool.start(_ThumbRunnable(self, file_item, key))
        except Exception:
            pass

    def refresh_details_panel_thumbnail(self, file_key: str):
        try:
            curr = getattr(self.details_panel, 'current_file_item', None)
            if not curr:
                return
            curr_key = curr.get('id') or curr.get('path')
            if curr_key != file_key:
                return
            if ThumbnailCache.is_thumbnail_cached(curr):
                self.details_panel.update_details(curr)
        except Exception:
            pass

    def _populate_extension_combo(self):
        self.indexer.ensure_conn()
        self.extension_combo.clear()
        self.extension_combo.addItem("Todas", "")
        self.indexer.cursor.execute("SELECT DISTINCT name FROM files")
        extensions = set()
        for row in self.indexer.cursor.fetchall():
            name = row[0]
            ext = os.path.splitext(name)[1].lower()
            if ext:
                extensions.add(ext)
        for ext in sorted(extensions):
            self.extension_combo.addItem(ext, ext)

    def _get_file_category(self, filename):
        if not filename:
            return "others"

        ext = os.path.splitext(filename)[1].lower()

        image_extencions = {'.jpg', '.jpeg', '.png',
                            '.gif', '.bmp', '.webp', '.svg', '.ico', '.tif', '.tiff'}
        video_extensions = {'.mp4', '.avi', '.mov', '.wmv',
                            '.flv', '.mkv', '.webm', '.m4v', '.3gp',
                            '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob'}
        document_extensions = {'.pdf', '.doc', '.docx', '.txt',
                               '.rtf', '.odt', '.xls', '.xlsx', '.ppt', '.pptx'}
        audio_extensions = {'.mp3', '.wav',
                            '.flac', '.aac', '.ogg', '.wma', '.m4a'}

        if ext in image_extencions:
            return "images"
        elif ext in video_extensions:
            return "videos"
        elif ext in document_extensions:
            return "documents"
        elif ext in audio_extensions:
            return "audios"
        else:
            return "others"

    def apply_advanced_filters(self):
        filters = {}

        extension_value = self.extension_combo.currentData()
        if extension_value not in [None, '']:
            filters['extension'] = extension_value

        category_value = self.main_bar.category_combo.currentData()
        if category_value not in [None, '']:
            filters['category'] = category_value

        default_modified = QDate.currentDate().addDays(-7)
        if self.modified_after_date.date() != default_modified:
            filters['modified_after'] = self.modified_after_date.date().toPyDate()

        default_created_after = QDate.currentDate().addDays(-7)
        if self.created_after_date.date() != default_created_after:
            filters['created_after'] = self.created_after_date.date().toPyDate()

        default_created_before = QDate.currentDate()
        if self.created_before_date.date() != default_created_before:
            filters['created_before'] = self.created_before_date.date().toPyDate()

        if self.starred_checkbox.isChecked():
            filters['is_starred'] = True

        self.advanced_filters = filters
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        print(f"🔍 Filtros aplicados: {filters}")
        list_update.load_next_batch(self)

    def clear_advanced_filters(self):
        self.extension_combo.setCurrentIndex(0)
        self.modified_after_date.setDate(QDate.currentDate().addDays(-7))
        self.created_after_date.setDate(QDate.currentDate().addDays(-7))
        self.created_before_date.setDate(QDate.currentDate())
        self.advanced_filters = {}
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)
        self.starred_checkbox.setChecked(False)

    def clear_thumbnail_cache(self):
        self.indexer.clear_thumbnail_cache()
        self.status_bar.showMessage("Cache de miniaturas limpo.", 5000)
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def navigate_to_root(self, source):
        self.current_view = None
        self.current_folder_id = None
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def navigate_to_folder(self, folder_id):
        self.search_term = ""
        self.search_entry.clear()
        self.current_folder_id = folder_id
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def update_filter_buttons(self):
        self.main_bar.category_combo.setEnabled(True)
        self.main_bar.tag_filter_combo.setEnabled(True)
        self.main_bar.sort_combo.setEnabled(True)

    def change_filter_type_combo(self, index):
        selected_category = self.main_bar.category_combo.itemData(index)
        self.current_filter = selected_category
        print(f"DEBUG: Filtro alterado para: {self.current_filter}")
        if selected_category:
            self.advanced_filters['category'] = selected_category
        else:
            self.advanced_filters.pop('category', None)
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def change_tag_filter_combo(self, index):
        selected_tag_filter = self.main_bar.tag_filter_combo.itemData(index)
        print(f"DEBUG: Filtro de tags alterado para: {selected_tag_filter}")
        if selected_tag_filter and selected_tag_filter != 'all':
            self.advanced_filters['tag_filter'] = selected_tag_filter
        else:
            self.advanced_filters.pop('tag_filter', None)
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def _run_incremental_sync(self, force_manual=True):
        if not self.is_authenticated or not self.service:
            if hasattr(self, 'folder_tree') and self.folder_tree:
                self.folder_tree._refresh_tree(show_status=False)
            if hasattr(self, 'status_bar') and self.status_bar:
                self.status_bar.showMessage("🔄 Modo Offline: Pastas locais e árvore atualizadas.", 3000)
            return

        try:
            local_scan_running = bool(
                self.local_scan_thread and self.local_scan_thread.isRunning()
            )
        except (AttributeError, RuntimeError):
            local_scan_running = False

        if (self.queue_execution_running or self.maintenance_running
                or local_scan_running):
            self.incremental_sync_deferred = True
            self.incremental_sync_deferred_manual = (
                self.incremental_sync_deferred_manual or bool(force_manual)
            )
            if self.queue_execution_running:
                reason = 'fila'
            elif self.maintenance_running:
                reason = 'manutencao'
            else:
                reason = 'varredura local'
            logging.info(
                "Sincronizacao incremental adiada: %s em andamento.", reason
            )
            self.status_bar.showMessage(
                "Sincronização aguardará a operação atual terminar.", 4000
            )
            return

        if self.drive_sync_running:
            self.incremental_sync_deferred = True
            self.incremental_sync_deferred_manual = (
                self.incremental_sync_deferred_manual or bool(force_manual)
            )
            self.status_bar.showMessage(
                "⏳ A sincronização incremental aguardará a sincronização completa.",
                3000,
            )
            return
            
        try:
            if hasattr(self, 'inc_sync_thread') and self.inc_sync_thread is not None and self.inc_sync_thread.isRunning():
                logging.info("⏳ Sincronização incremental já está em andamento, ignorando ciclo.")
                self.status_bar.showMessage("⏳ Sincronização já em andamento...", 2000)
                return
        except RuntimeError:
            self.inc_sync_thread = None

        logging.info("⏳ Disparando Sincronização Incremental (Background)...")
        self.update_drive_sync_progress(
            -1, "🔄 Verificando atualizações no Google Drive..."
        )
        if hasattr(self, 'sync_cancel_btn') and self.sync_cancel_btn:
            self.sync_cancel_btn.setEnabled(True)
            self.sync_cancel_btn.setVisible(True)

        from src.drive.incremental_sync import IncrementalSyncWorker
        from src.utils.config_manager import ConfigManager

        # Tanto a sincronia manual quanto a automática usam o último log local (com margem de tolerância).
        self.inc_sync_thread = QThread()
        self.inc_sync_worker = IncrementalSyncWorker(
            self.service, ConfigManager(), self.search_engine.indexer, force_window_days=None
        )
        self.inc_sync_worker.moveToThread(self.inc_sync_thread)

        self.inc_sync_thread.started.connect(self.inc_sync_worker.run)

        def cleanup_inc_thread():
            if self.inc_sync_thread:
                self.inc_sync_thread.quit()
                self.inc_sync_thread.wait(2000)
                self.inc_sync_thread.deleteLater()
                self.inc_sync_thread = None
            self.progress_bar.setVisible(False)
            if hasattr(self, 'sync_cancel_btn') and self.sync_cancel_btn:
                self.sync_cancel_btn.setVisible(False)
                self.sync_cancel_btn.setEnabled(True)

        def on_inc_finished(count):
            cleanup_inc_thread()
            self._set_bottom_drive_status_badge(False)
            if hasattr(self, 'main_bar') and self.main_bar:
                self.main_bar.set_drive_updates_available(False)
            # Se for sync automático de segundo plano e não houve arquivos alterados, não perturba a tela
            if count > 0 or force_manual:
                self._force_refresh_after_sync()
            else:
                if hasattr(self, 'folder_tree') and self.folder_tree:
                    self.folder_tree._refresh_tree(show_status=False)
            if count > 0:
                self.status_bar.showMessage(f"✅ Sincronização: {count} arquivo(s) do Drive e pastas locais verificadas.", 5000)
            else:
                self.status_bar.showMessage("🔄 Sincronização: Tudo atualizado (Drive e pastas locais).", 4000)
            self._run_deferred_incremental_sync()

        def on_inc_cancelled(processed_count):
            cleanup_inc_thread()
            if processed_count > 0:
                self._force_refresh_after_sync()
            self.status_bar.showMessage(
                f"⏹️ Sincronização interrompida ({processed_count:,} processado(s)); próximo sync continuará do último log salvo.",
                6000,
            )
            self._run_deferred_incremental_sync()

        def on_inc_failed(err):
            cleanup_inc_thread()
            logging.error(f"Sincronização incremental falhou: {err}")
            self.status_bar.showMessage(f"⚠️ Erro no sync em segundo plano: {err}", 4000)
            self._run_deferred_incremental_sync()

        self.inc_sync_worker.sync_finished.connect(on_inc_finished)
        self.inc_sync_worker.sync_cancelled.connect(on_inc_cancelled)
        self.inc_sync_worker.sync_failed.connect(on_inc_failed)
        self.inc_sync_worker.sync_warning.connect(
            lambda message: self.status_bar.showMessage(
                f"⚠️ {message}", 10000
            )
        )
        self.inc_sync_worker.progress_update.connect(
            self.update_drive_sync_progress
        )
        
        self.inc_sync_thread.start()

    def begin_queue_execution(self):
        """Reserva a escrita do banco para a fila antes do primeiro item."""
        incremental_running = False
        try:
            incremental_running = bool(
                self.inc_sync_thread and self.inc_sync_thread.isRunning()
            )
        except (AttributeError, RuntimeError):
            incremental_running = False
        local_scan_running = False
        try:
            local_scan_running = bool(
                self.local_scan_thread and self.local_scan_thread.isRunning()
            )
        except (AttributeError, RuntimeError):
            local_scan_running = False

        if (self.drive_sync_running or incremental_running
                or local_scan_running or self.maintenance_running):
            QMessageBox.warning(
                self,
                "Banco ocupado",
                "Há uma sincronização, varredura ou manutenção em andamento. "
                "Aguarde sua conclusão antes de executar a fila.",
            )
            return False
        self.queue_execution_running = True
        return True

    def _check_queue_confirmations(self):
        """Somente reconcilia operações já aprovadas; nunca executa pendentes."""
        queue = self.main_bar.staging_queue
        if (self._queue_confirmation_worker or queue.busy or queue.load_error
                or self.queue_execution_running or self.drive_sync_running or self.maintenance_running
                or not self.service or self.config_mgr.is_read_only()):
            return
        if any(t and t.isRunning() for t in (self.inc_sync_thread, self.local_scan_thread, queue._autosave_worker)):
            return
        from src.services.contextual_queue import ACTIVE, ContextQueueWorker
        pending = [i.operation_id for i in queue.items if i.execution_state in ACTIVE]
        if not pending:
            return
        if not self.begin_maintenance_operation():
            return
        self._queue_confirmation_result = {}
        offset = getattr(self, '_queue_confirmation_offset', 0) % len(pending)
        selected = (pending[offset:] + pending[:offset])[:50]
        self._queue_confirmation_offset = (offset + len(selected)) % len(pending)
        self._queue_confirmation_worker = ContextQueueWorker(queue.store.path, queue.store.legacy_path,
            self.indexer.db_name, self.service, selected=selected, check_only=True, parent=self)
        self._queue_confirmation_worker.result.connect(lambda r: setattr(self, '_queue_confirmation_result', r))
        self._queue_confirmation_worker.failed.connect(lambda message: logging.warning('Confirmação da fila: %s', message))
        self._queue_confirmation_worker.finished.connect(self._queue_confirmations_finished)
        self._queue_confirmation_worker.start()

    def _queue_confirmations_finished(self):
        from src.utils.staging_schema import StagingItem
        queue = self.main_bar.staging_queue
        try:
            queue.items = [StagingItem.from_dict(r) for r in queue.store.load()]
            queue._tag_index = None
        except Exception as exc:
            queue.load_error = str(exc)
        self._queue_confirmation_worker.deleteLater()
        self._queue_confirmation_worker = None
        self.end_maintenance_operation()
        queue.queueChanged.emit(queue.count())
        result = self._queue_confirmation_result
        if result.get('applied'):
            self._force_refresh_after_sync()
        if result.get('completed'):
            self.status_bar.showMessage(f'Fila: {result["completed"]} operação(ões) confirmada(s) no Drive.', 8000)

    def end_queue_execution(self):
        self.queue_execution_running = False
        self._run_deferred_incremental_sync()

    def begin_maintenance_operation(self):
        if self.queue_execution_running or self.drive_sync_running or self.maintenance_running:
            QMessageBox.warning(
                self, 'Operacao em andamento',
                'Aguarde a fila ou a sincronizacao terminar.',
            )
            return False
        try:
            if ((self.inc_sync_thread and self.inc_sync_thread.isRunning())
                    or (self.local_scan_thread and self.local_scan_thread.isRunning())):
                QMessageBox.warning(
                    self, 'Operacao em andamento',
                    'Aguarde a sincronizacao ou varredura terminar.',
                )
                return False
        except (AttributeError, RuntimeError):
            pass
        self.maintenance_running = True
        if hasattr(self, 'main_bar'):
            self.main_bar.staging_queue.busy = True
        return True

    def closeEvent(self, event):
        worker = getattr(getattr(self, 'details_panel', None), '_batch_worker', None)
        queue = getattr(getattr(self, 'main_bar', None), 'staging_queue', None)
        autosave = getattr(queue, '_autosave_worker', None)
        if ((worker is not None and worker.isRunning())
                or (autosave is not None and autosave.isRunning())
                or self.queue_execution_running or self.maintenance_running):
            QMessageBox.information(self, 'Operação em andamento',
                'Aguarde a gravação/execução terminar. Para interromper a preparação de tags, use Cancelar preparação na barra inferior.')
            event.ignore()
            return
        super().closeEvent(event)

    def _backup_local_catalog(self):
        if (self.catalog_backup_worker is not None or self.maintenance_running
                or self.queue_execution_running or self.drive_sync_running):
            return
        if any(worker and worker.isRunning() for worker in
               (self.inc_sync_thread, self.local_scan_thread)):
            return
        if not self.begin_maintenance_operation():
            return
        from src.ui.snapshot_dialog import SnapshotWorker
        from src.services.snapshot_management import local_backup
        self.catalog_backup_worker = SnapshotWorker(
            lambda: local_backup(self.indexer.db_name, automatic=True), self)
        self.catalog_backup_worker.succeeded.connect(
            lambda path: logging.info('Backup local do catálogo: %s', path) if path else None)
        def failed(message):
            logging.warning('Backup local não concluído: %s', message)
            self.status_bar.showMessage('Backup local não concluído. Consulte o log: ' + message, 15000)
        self.catalog_backup_worker.failed.connect(failed)
        def finished():
            self.catalog_backup_worker.deleteLater()
            self.catalog_backup_worker = None
            self.end_maintenance_operation()
        self.catalog_backup_worker.finished.connect(finished)
        self.catalog_backup_worker.start()

    def end_maintenance_operation(self):
        self.maintenance_running = False
        if hasattr(self, 'main_bar'):
            self.main_bar.staging_queue.release()
        self._run_deferred_incremental_sync()

    def _run_deferred_incremental_sync(self):
        if self.full_sync_deferred:
            self.full_sync_deferred = False
            QTimer.singleShot(300, self._start_drive_sync)
            return
        if self.incremental_sync_deferred:
            manual = self.incremental_sync_deferred_manual
            self.incremental_sync_deferred = False
            self.incremental_sync_deferred_manual = False
            QTimer.singleShot(
                300, lambda: self._run_incremental_sync(force_manual=manual)
            )


    def update_ui_for_auth_state(self, is_auth):
        self.main_bar.category_combo.setEnabled(True)
        self.main_bar.sort_combo.setEnabled(True)

    def change_filter_type_combo(self, index):
        selected_category = self.main_bar.category_combo.itemData(index)
        self.current_filter = selected_category
        print(f"DEBUG: Filtro alterado para: {self.current_filter}")
        if selected_category:
            self.advanced_filters['category'] = selected_category
        else:
            self.advanced_filters.pop('category', None)
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def change_sort_order(self, index):
        self.current_sort = self.main_bar.sort_combo.itemData(index)
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def toggle_explorer_special(self):
        self.explorer_special_active = self.main_bar.action_explorer.isChecked()
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

    def toggle_advanced_filters(self):
        if self.advanced_filters_widget.isVisible():
            self.advanced_filters_widget.hide()
            self.more_filters_button.setText("Mais Filtros ▼")
            self.more_filters_button.setChecked(False)
        else:
            self.advanced_filters_widget.show()
            self.more_filters_button.setText("Menos Filtros ▲")
            self.more_filters_button.setChecked(True)
            self.apply_advanced_filters()

    def handle_search_input(self, text):
        if text:
            self.search_timer.stop()
            self.search_timer.start(500)
            self.update_search_suggestions()
        else:
            self.search_timer.stop()
            self.search_term = ""
            self.completer_model.setStringList([])
            self.current_page = 0
            self.all_files_loaded = False
            self.current_folder_id = None
            list_update.clear_display(self)
            list_update.load_next_batch(self)

        self.update_filter_buttons()

    def update_search_suggestions(self):
        try:
            text = self.main_bar.search_entry.text().strip()
            if not text:
                self.completer_model.setStringList([])
                return

            suggestions = self.search_engine.get_search_suggestions(
                text, self.is_authenticated)

            normalized_text = self.search_engine.normalize_text(text)
            if normalized_text != text.lower():
                normalized_suggestions = self.search_engine.get_search_suggestions(
                    normalized_text, self.is_authenticated)
                suggestions = list(set(suggestions + normalized_suggestions))

            self.completer_model.setStringList(suggestions[:10])
        except Exception as e:
            QMessageBox.critical(
                self, "Erro", f"Ocorreu um erro ao atualizar sugestões de busca:\n{e}")

    def handle_search_request(self):
        try:
            original_term = self.main_bar.search_entry.text().strip()
            self.search_term = original_term.lower()

            if self.search_term:
                normalized_term = self.search_engine.normalize_text(
                    self.search_term)
                print(
                    f"🔍 Busca: '{original_term}' → Normalizado: '{normalized_term}'")

            self.current_page = 0
            self.all_files_loaded = False
            self.current_folder_id = None
            list_update.clear_display(self)
            list_update.load_next_batch(self)
        except Exception as e:
            QMessageBox.critical(
                self, "Erro", f"Ocorreu um erro ao buscar arquivos:\n{e}")

    def update_ui_for_auth_state(self, is_authenticated):
        self.is_authenticated = is_authenticated
        self.main_bar.login_button.setVisible(not is_authenticated)
        self.main_bar.logout_button.setVisible(is_authenticated)
        self._set_bottom_drive_status_badge(False)

    def on_scroll(self, value):
        scroll_bar = self.file_list_view.verticalScrollBar()
        threshold = scroll_bar.maximum() * 0.2
        if value >= scroll_bar.maximum() - threshold and not self.is_loading and not self.all_files_loaded:
            self.scroll_loading = True
            list_update.load_next_batch(self)

    def _open_advanced_settings(self):
        from src.ui.advanced_settings_dialog import AdvancedSettingsDialog
        dialog = AdvancedSettingsDialog(self, indexer=self.search_engine.indexer, service=self.service)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._force_refresh_after_sync()

    def _show_scan_options(self):
        if self.local_scan_thread and self.local_scan_thread.isRunning():
            QMessageBox.warning(
                self, "Aviso", "Aguarde a conclusão do escaneamento local.")
            return

        dialog = OptionsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:

            selected_paths = dialog.get_selected_folders()
            settings = load_settings()
            settings['scan_paths'] = selected_paths
            save_settings(settings)

            if selected_paths:
                self.config_mgr.set('scan_folders', selected_paths)
                self.folder_tree.set_root_directory(selected_paths[0])

            self.current_view = 'local'
            self.current_folder_id = None
            self.current_folder_path_filter = None
            list_update.clear_display(self)
            self.all_files_loaded = False
            self.current_page = 0

            if selected_paths:
                self._start_local_scan(selected_paths)
            else:
                self.indexer.clear_source('local')
                self.loading_label.setText("Nenhum arquivo encontrado.")
                self.loading_label.show()
                self.all_files_loaded = True

        list_update.load_next_batch(self)

    def force_rescan_local(self):
        if self.local_scan_thread and self.local_scan_thread.isRunning():
            QMessageBox.warning(
                self, "Aviso", "Aguarde a conclusão do escaneamento local.")
            return

        dialog = OptionsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_paths = dialog.get_selected_folders() if hasattr(
                dialog, 'get_selected_folders') else None
            settings = load_settings()
            settings['scan_paths'] = selected_paths
            save_settings(settings)

            self.current_view = 'local'
            self.current_folder_id = None
            list_update.clear_display(self)
            self.all_files_loaded = False
            self.current_page = 0

            if selected_paths:
                self._start_local_scan(selected_paths)
            else:
                self.indexer.clear_source('local')
                self.loading_label.setText("Nenhum arquivo encontrado.")
                self.loading_label.show()
                self.all_files_loaded = True

            list_update.load_next_batch(self)

    def _start_local_scan(self, paths_to_scan):
        if self.maintenance_running or self.queue_execution_running:
            QMessageBox.information(self, 'Operação em andamento',
                'Aguarde a preparação ou execução da fila terminar antes de iniciar a varredura.')
            return
        from src.services.local_scan import LocalScan
        self.all_loaded_label.hide()

        self.local_scan_thread = QThread()
        self.local_scan_worker = LocalScan(self.indexer.db_name, paths_to_scan)
        self.local_scan_worker.moveToThread(self.local_scan_thread)

        self.local_scan_progress = QProgressDialog(
            "Escaneando arquivos locais...", "Cancelar", 0, 100, self)
        self.local_scan_progress.setWindowModality(self.windowModality())
        self.local_scan_progress.setAutoClose(True)
        self.local_scan_progress.setAutoReset(True)
        self.local_scan_progress.setFixedSize(400, 150)
        self.local_scan_progress.setStyleSheet("""
            QProgressBar { min-height: 25px; border: 1px solid #ccc; border-radius: 5px; text-align: center; }
            QProgressBar::chunk { background-color: #1976d2; border-radius: 5px; }
        """)

        self.total_files_to_process = 0

        def on_total_found(total):
            self.total_files_to_process = total
            print(f"DEBUG: Total de arquivos a processar: {total:,}")

        def update_progress(value):
            if self.total_files_to_process > 0:
                percent = min(
                    98, int((value / self.total_files_to_process) * 100))
            else:
                percent = min(98, int(value / 1000))

            self.local_scan_progress.setValue(percent)
            print(
                f"DEBUG: Progresso - {value:,}/{self.total_files_to_process:,} arquivos ({percent}%)")

        def update_status(msg):
            self.local_scan_progress.setLabelText(msg)
            self.status_bar.showMessage(msg, 2000)

        def on_finished():
            failure = getattr(self.local_scan_worker, 'failure_message', None)
            cancelled = bool(getattr(self.local_scan_worker, 'cancelled', False))
            if failure or cancelled:
                self.local_scan_progress.setLabelText("Escaneamento interrompido.")
            else:
                self.local_scan_progress.setValue(100)
                self.local_scan_progress.setLabelText("Escaneamento concluído.")
            self.local_scan_progress.close()
            self.on_local_scan_finished(
                error_message=failure, cancelled=cancelled
            )

        self.local_scan_worker.total_files_found.connect(on_total_found)
        self.local_scan_worker.progress_update.connect(update_progress)
        self.local_scan_worker.update_status_signal.connect(update_status)
        self.local_scan_worker.finished.connect(on_finished)

        def cancel_scan():
            print("DEBUG: Botão cancelar pressionado")
            self.local_scan_worker.terminate()
            self.local_scan_progress.close()

        self.local_scan_progress.canceled.connect(cancel_scan)

        self.local_scan_thread.started.connect(self.local_scan_worker.run)

        self.local_scan_progress.show()
        self.local_scan_thread.start()

    def on_local_scan_finished(self, error_message=None, cancelled=False):
        if self.local_scan_worker:
            try:
                self.local_scan_worker.finished.disconnect(
                    self.on_local_scan_finished)
            except TypeError:
                pass

        # Conservar o FileIndexer da interface evita conexoes abandonadas e
        # referencias obsoletas no delegate de thumbnails.
        self.indexer.ensure_conn()
        self.indexer.clear_query_cache()
        self.search_engine.clear_cache()
        if cancelled:
            self.status_bar.showMessage("Varredura local cancelada.", 5000)
        elif error_message:
            self.tray_icon.showMessage(
                "Erro na varredura local",
                f"A varredura foi interrompida: {error_message}",
                QSystemTrayIcon.MessageIcon.Critical,
                5000,
            )
            self.status_bar.showMessage(
                f"Erro na varredura local: {error_message}", 8000
            )
        else:
            try:
                file_count = self.indexer.get_file_count(source='local')
                self.tray_icon.showMessage(
                    "Sincronização Local Concluída",
                    f"Escaneamento concluído. {file_count} arquivos locais indexados",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            except Exception as e:
                logging.warning(
                    "Erro ao acessar banco apos varredura local: %s", e
                )
                self.tray_icon.showMessage(
                    "Sincronização Local Concluída",
                    "Escaneamento concluído.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            self.status_bar.showMessage("Escaneamento local concluído.", 5000)

        if self.local_scan_thread:
            self.local_scan_thread.quit()
            self.local_scan_thread.wait()

        self.local_scan_worker = None
        self.local_scan_thread = None
        self.local_scan_progress = None
        self._run_deferred_incremental_sync()

        self.current_view = 'local'
        self.current_filter = "all"
        self.advanced_filters = {}
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        self.main_bar.category_combo.setCurrentIndex(0)
        list_update.load_next_batch(self)
        if hasattr(self.search_engine, '_paged_cache'):
            self.search_engine._paged_cache.clear()

    def update_local_scan_progress(self, files_processed):
        self.status_bar.showMessage(
            f"Escaneando arquivos locais... {files_processed} arquivos processados.")

    def _start_auth(self, auto=False):
        if self.auth_thread and self.auth_thread.isRunning():
            self.status_bar.showMessage(
                "Processo de login já em andamento...", 5000)
            return

        if not auto:
            self.status_bar.showMessage(
                "Abrindo navegador para autenticação...", 5000)
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, 0)
            self.main_bar.login_button.setEnabled(False)
        else:
            self.status_bar.showMessage(
                "Verificando autenticação automática...", 5000)
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, 0)
            self.main_bar.login_button.setEnabled(False)

        self.auth_thread = QThread()
        self.auth_worker = AuthWorker()
        self.auth_worker.moveToThread(self.auth_thread)

        self.auth_worker.authenticated.connect(self.on_auth_success)
        self.auth_worker.auth_failed.connect(self.on_auth_fail)

        self.auth_thread.started.connect(self.auth_worker.run)
        self.auth_thread.start()

    def on_auth_success(self, service):
        self.progress_bar.setVisible(False)
        self.main_bar.login_button.setEnabled(True)
        self.status_bar.showMessage(
            "Login bem-sucedido.", 5000)
        self.service = service
        self.update_ui_for_auth_state(True)

        self.main_bar.update_profile(service)
        QTimer.singleShot(1500, self.check_drive_updates_available)
        if hasattr(self, 'drive_badge_timer'):
            self.drive_badge_timer.start(180000)

        settings = load_settings()
        has_saved_config = bool(settings.get('scan_paths') and len(
            settings.get('scan_paths', [])) > 0)
        list_update.load_next_batch(self)

        self.auth_thread.quit()
        self.auth_thread.wait()

    def on_auth_fail(self, error_message):
        self.progress_bar.setVisible(False)
        self.main_bar.login_button.setEnabled(True)
        QMessageBox.critical(self, "Erro de Autenticação", error_message)
        self.status_bar.showMessage("Login falhou.", 5000)

        self.auth_thread.quit()
        self.auth_thread.wait()
        self.auth_worker.deleteLater()
        self.auth_thread.deleteLater()
        self.auth_worker = None
        self.auth_thread = None

    def handle_logout(self):
        try:
            if self.auth_worker:
                self.auth_worker.remove_token()
            self.status_bar.showMessage("Logout bem-sucedido.", 5000)
            self.service = None
            self.indexer.clear_source('drive')
            self.update_ui_for_auth_state(False)

            self.main_bar.reset_profile()
            self.main_bar.set_drive_updates_available(False)
            self._set_bottom_drive_status_badge(False)
            if hasattr(self, 'drive_badge_timer'):
                self.drive_badge_timer.stop()

            self.current_view = 'local'
            self.current_folder_id = None
            list_update.load_next_batch(self)
        except Exception as e:
            print(f"Erro ao fazer logout: {e}")
            QMessageBox.warning(self, "Erro", f"Falha ao fazer logout: {e}")

    def _start_drive_sync(self):
        print("🔄 _start_drive_sync chamado pelo usuário")

        try:
            local_scan_running = bool(
                self.local_scan_thread and self.local_scan_thread.isRunning()
            )
        except (AttributeError, RuntimeError):
            local_scan_running = False

        if (self.queue_execution_running or self.maintenance_running
                or local_scan_running):
            QMessageBox.information(
                self, "Aviso",
                "A fila ou a manutencao do banco esta em andamento. "
                "A sincronizacao foi adiada.",
            )
            self.full_sync_deferred = True
            return

        if self.drive_sync_running:
            print("⚠️ Drive sync já está em execução - ignorando nova solicitação")
            QMessageBox.information(
                self, "Aviso", "Sincronização do Drive já está em andamento.")
            return


        try:
            incremental_running = (
                self.inc_sync_thread is not None
                and self.inc_sync_thread.isRunning()
            )
        except (AttributeError, RuntimeError):
            incremental_running = False
        if incremental_running:
            QMessageBox.information(
                self, "Aviso",
                "A sincronização completa começará após a incremental."
            )
            self.full_sync_deferred = True
            return

        if not self.service:
            print("❌ Não autenticado no Google Drive")
            QMessageBox.warning(
                self, "Erro", "Não autenticado no Google Drive.")
            return

        self.drive_sync_running = True
        print("🚀 Iniciando nova sincronização do Drive...")

        started = start_drive_folder_processing(
            self, self.service, self.indexer, force_dialog=True)
        if not started:
            self.drive_sync_running = False


    def _show_drive_folder_selection(self):
        if not self.service:
            QMessageBox.warning(
                self, "Erro", "Não autenticado no Google Drive.")
            return

        folder_dialog = DriveFolderDialog(self.service, self)

        if folder_dialog.exec() == QDialog.DialogCode.Accepted:
            folder_dialog.save_settings()
            QMessageBox.information(
                self, "Configuração Salva",
                "As configurações de pastas do Google Drive foram salvas.\n\n"
                "Use 'Selecionar Pastas do Drive' para sincronizar com as pastas selecionadas.")

    def on_drive_sync_finished(self):
        self.drive_sync_running = False
        print("✅ Drive sync finalizado - flag liberada")
        if hasattr(self, 'main_bar') and self.main_bar:
            self.main_bar.set_drive_updates_available(False)
        self._set_bottom_drive_status_badge(False)

        if self.drive_sync_running:
            print("⚠️ AVISO: Flag ainda está True após liberação!")

        prev_selected_id = None
        try:
            selected_indexes = self.file_list_view.selectedIndexes()
            if selected_indexes:
                current_item = self.file_list_model.data(
                    selected_indexes[0], Qt.ItemDataRole.UserRole)
                if current_item:
                    prev_selected_id = current_item.get(
                        'id') or current_item.get('path')
        except Exception:
            prev_selected_id = None

        self.status_bar.showMessage("Sincronização do Drive concluída.", 5000)
        self.progress_bar.setVisible(False)

        # O worker ja fechou a conexao dele. Preservar esta instancia evita
        # que o delegate de thumbnails continue preso a um indexador antigo.
        self.indexer.ensure_conn()
        self.indexer.clear_query_cache()
        self.search_engine.clear_cache()

        if hasattr(self.indexer, '_paged_cache'):
            self.indexer._paged_cache.clear()
        if hasattr(self.indexer, '_count_cache'):
            self.indexer._count_cache.clear()
        if hasattr(self.search_engine, '_paged_cache'):
            self.search_engine._paged_cache.clear()

        self.current_page = 0
        self.all_files_loaded = False
        self.current_view = 'local'
        self.current_folder_id = None
        self.advanced_filters = {}

        list_update.clear_display(self)

        QTimer.singleShot(
            100, lambda: self._force_refresh_after_sync(prev_selected_id))
        self._run_deferred_incremental_sync()

    def _reload_keeping_scroll(self, prev_selected_id=None, restore_delay_ms=150):
        # 1. Capturar âncora de scroll e seleção antes de limpar
        anchor_id = prev_selected_id
        selected_indexes = self.file_list_view.selectedIndexes()
        if not anchor_id and selected_indexes:
            curr_item = self.file_list_model.data(selected_indexes[0], Qt.ItemDataRole.UserRole)
            if curr_item:
                anchor_id = curr_item.get('id') or curr_item.get('path')

        if not anchor_id:
            top_idx = self.file_list_view.indexAt(QPoint(24, 24))
            if top_idx.isValid():
                top_item = self.file_list_model.data(top_idx, Qt.ItemDataRole.UserRole)
                if top_item:
                    anchor_id = top_item.get('id') or top_item.get('path')

        scroll_bar = self.file_list_view.verticalScrollBar()
        saved_scroll_value = scroll_bar.value()

        # Determinar quantos lotes já estavam carregados na tela
        loaded_count = self.file_list_model.rowCount()
        batches_to_load = max(1, (loaded_count + self.page_size - 1) // self.page_size) if loaded_count > 0 else 1

        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)

        # Recarregar proporcionalmente para manter a posição de trabalho
        for _ in range(batches_to_load):
            if self.all_files_loaded:
                break
            list_update.load_next_batch(self)

        if saved_scroll_value > 0:
            max_scroll = scroll_bar.maximum()
            scroll_bar.setValue(min(saved_scroll_value, max_scroll))

        # 2. Restaurar posição de rolagem e seleção
        had_selection = bool(selected_indexes or prev_selected_id)

        def _restore_position():
            restored = False
            if anchor_id:
                target_idx = self._find_model_index_by_id(anchor_id)
                if target_idx and target_idx.isValid():
                    if had_selection:
                        sel_model = self.file_list_view.selectionModel()
                        if sel_model:
                            sel_model.select(
                                target_idx,
                                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
                            )
                        self.file_list_view.setCurrentIndex(target_idx)
                        self._refresh_details_by_id(anchor_id)
                    self.file_list_view.scrollTo(target_idx, QAbstractItemView.ScrollHint.PositionAtCenter)
                    restored = True

            if not restored and saved_scroll_value > 0:
                max_scroll = self.file_list_view.verticalScrollBar().maximum()
                self.file_list_view.verticalScrollBar().setValue(min(saved_scroll_value, max_scroll))

        QTimer.singleShot(restore_delay_ms, _restore_position)

    def _force_refresh_after_sync(self, prev_selected_id=None):
        try:
            if hasattr(self, 'search_engine'):
                self.search_engine.clear_cache()
            if hasattr(self, 'indexer'):
                self.indexer.clear_query_cache()

            self._reload_keeping_scroll(prev_selected_id=prev_selected_id, restore_delay_ms=150)

            # 3. Atualizar também a árvore de diretórios e varredura de pastas no disco
            if hasattr(self, 'folder_tree') and self.folder_tree:
                self.folder_tree._refresh_tree(show_status=False)

            self.status_bar.showMessage(
                "Interface atualizada com novos metadados.", 3000)

        except Exception as e:
            print(f"Erro ao forçar atualização após sincronização: {e}")
            self.status_bar.showMessage("Erro ao atualizar interface.", 3000)

    def on_metadata_fusion_completed(self, fusion_count):
        try:
            print(
                f"[UI] Fusão de metadados concluída: {fusion_count} arquivos fusionados")

            if hasattr(self.search_engine, '_paged_cache'):
                self.search_engine._paged_cache.clear()

            if fusion_count > 0:
                self.status_bar.showMessage(
                    f"Metadados fusionados: {fusion_count} arquivos atualizados", 3000)

                selected_indexes = self.file_list_view.selectedIndexes()
                if selected_indexes:
                    current_item = self.file_list_model.data(
                        selected_indexes[0], Qt.ItemDataRole.UserRole)
                    if current_item:
                        file_id = current_item.get(
                            'id') or current_item.get('path')
                        if file_id:
                            self._update_file_in_model_and_details(file_id)

        except Exception as e:
            print(f"Erro no callback de fusão de metadados: {e}")

    def _update_file_in_model_and_details(self, file_id):
        try:
            self.indexer.ensure_conn()
            self.indexer.cursor.execute(
                """
                SELECT file_id, name, path, mimeType, source, description, thumbnailLink, thumbnailPath, size, modifiedTime, createdTime, parentId, starred, thumbnailRotation
                FROM files WHERE file_id = ?
                """,
                (file_id,)
            )
            row = self.indexer.cursor.fetchone()
            if row:
                updated_item = {
                    'id': row[0],
                    'name': row[1],
                    'path': row[2],
                    'mimeType': row[3],
                    'source': row[4],
                    'description': row[5],
                    'thumbnailLink': row[6],
                    'thumbnailPath': row[7],
                    'size': row[8],
                    'modifiedTime': row[9],
                    'createdTime': row[10],
                    'parentId': row[11],
                    'starred': bool(row[12]) if len(row) > 12 else False,
                    'thumbnailRotation': int(row[13] or 0) if len(row) > 13 else 0,
                }

                self.file_list_model.updateFileById(file_id, updated_item)

                selected_indexes = self.file_list_view.selectedIndexes()
                if selected_indexes:
                    current_item = self.file_list_model.data(
                        selected_indexes[0], Qt.ItemDataRole.UserRole)
                    if current_item:
                        current_id = current_item.get(
                            'id') or current_item.get('path')
                        if current_id == file_id:
                            self.details_panel.update_details(updated_item)

        except Exception as e:
            print(f"Erro ao atualizar arquivo no modelo: {e}")

    def _refresh_details_by_id(self, file_id: str):
        try:
            if not file_id:
                return
            self.indexer.ensure_conn()
            self.indexer.cursor.execute(
                """
                SELECT file_id, name, path, mimeType, source, description, thumbnailLink, thumbnailPath, size, modifiedTime, createdTime, parentId, starred, thumbnailRotation
                FROM files WHERE file_id = ?
                """,
                (file_id,)
            )
            row = self.indexer.cursor.fetchone()
            if not row:
                return
            updated_item = {
                'id': row[0],
                'name': row[1],
                'path': row[2],
                'mimeType': row[3],
                'source': row[4],
                'description': row[5],
                'thumbnailLink': row[6],
                'thumbnailPath': row[7],
                'size': row[8],
                'modifiedTime': row[9],
                'createdTime': row[10],
                'parentId': row[11],
                'starred': bool(row[12]) if len(row) > 12 else False,
                'thumbnailRotation': int(row[13] or 0) if len(row) > 13 else 0,
            }
            self.details_panel.update_details(updated_item)
        except Exception as e:
            print(f"[UI] Falha ao atualizar detalhes pós-sync: {e}")

    def _find_model_index_by_id(self, file_id: str):
        try:
            if not file_id:
                return None
            model = self.file_list_model
            rows = model.rowCount()
            for row in range(rows):
                idx = model.index(row, 0)
                item = model.data(idx, Qt.ItemDataRole.UserRole)
                if not item:
                    continue
                item_id = item.get('id') or item.get('path')
                if item_id == file_id:
                    return idx
            return None
        except Exception:
            return None

    def _reselect_and_refresh(self, file_id: str):
        try:
            idx = self._find_model_index_by_id(file_id)
            if idx:
                sel_model = self.file_list_view.selectionModel()
                if sel_model:
                    sel_model.select(
                        idx, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
                    self.file_list_view.setCurrentIndex(idx)
                    self.file_list_view.scrollTo(idx)
                self._refresh_details_by_id(file_id)
                return
            self._refresh_details_by_id(file_id)
        except Exception as e:
            print(f"[UI] Falha ao re-selecionar/atualizar: {e}")

    def on_drive_sync_failed(self, error_message):
        self.drive_sync_running = False
        print(f"❌ Drive sync falhou - flag liberada: {error_message}")

        if self.drive_sync_running:
            print("⚠️ AVISO: Flag ainda está True após erro!")

        self.status_bar.showMessage(error_message, 5000)
        self.tray_icon.showMessage(
            "Erro de Sincronização", f"Falha na sincronização do Google Drive: {error_message}", QSystemTrayIcon.MessageIcon.Critical, 5000)
        self.progress_bar.setVisible(False)
        auth_error = any(
            marker in str(error_message).lower()
            for marker in ('401', 'invalid_grant', 'unauthorized', 'token expired')
        )
        if auth_error:
            self.update_ui_for_auth_state(False)
        self._run_deferred_incremental_sync()

    def _cancel_incremental_sync(self):
        if hasattr(self, 'inc_sync_worker') and self.inc_sync_worker:
            self.inc_sync_worker.cancel()
        if hasattr(self, 'sync_cancel_btn') and self.sync_cancel_btn:
            self.sync_cancel_btn.setEnabled(False)
        if hasattr(self, 'status_bar') and self.status_bar:
            self.status_bar.showMessage("⏹️ Interrompendo sincronização...", 3000)

    def _set_bottom_drive_status_badge(self, has_updates: bool, detail_text: str = ""):
        if not hasattr(self, 'drive_status_badge') or not self.drive_status_badge:
            return
        if not self.is_authenticated:
            self.drive_status_badge.setVisible(False)
            return
        from datetime import datetime
        last_ts = self.config_mgr.get('last_sync_timestamp')
        ts_info = (
            datetime.fromtimestamp(int(last_ts)).strftime('%d/%m %H:%M:%S')
            if last_ts else 'nunca'
        )
        if has_updates:
            self.drive_status_badge.setText("🟡 Atualizações no Drive")
            self.drive_status_badge.setStyleSheet(
                "background-color: #FFF8E1; color: #B7791F; border: 1px solid #F6E05E; "
                "border-radius: 10px; padding: 2px 8px; font-weight: bold; font-size: 11px;"
            )
            self.drive_status_badge.setToolTip(
                (detail_text + "\n" if detail_text else "")
                + f"Última sincronia completa: {ts_info}"
            )
        else:
            self.drive_status_badge.setText("🟢 Drive atualizado")
            self.drive_status_badge.setStyleSheet(
                "background-color: #E8F5E9; color: #2E7D32; border: 1px solid #A5D6A7; "
                "border-radius: 10px; padding: 2px 8px; font-weight: bold; font-size: 11px;"
            )
            self.drive_status_badge.setToolTip(f"Última sincronia completa: {ts_info}")
        self.drive_status_badge.setVisible(True)

    def update_drive_sync_progress(self, value, msg):
        self.progress_bar.setVisible(True)
        if value < 0:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("Consultando...")
        else:
            safe_value = max(0, min(100, int(value)))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(safe_value)
            self.progress_bar.setFormat(f"{safe_value}%")
        self.status_bar.showMessage(msg, 0)

    def update_drive_status_message(self, msg):
        self.status_bar.showMessage(msg)

    def debug_system_status(self):
        print(f"\n" + "="*60)
        print(f"🖥️ DEBUG STATUS DO SISTEMA")
        print(f"="*60)

        auth_status = "✅ Autenticado" if self.is_authenticated else "❌ Não autenticado"
        print(f"🔐 Google Drive: {auth_status}")

        local_scan_running = self.local_scan_thread and self.local_scan_thread.isRunning()
        print(
            f"🔄 Scan local: {'⏳ Executando' if local_scan_running else '✅ Parado'}")
        print("🔄 Sync Drive: ✅ Gerenciado pelo processing.py")

        try:
            local_count = self.indexer.get_file_count(source='local')
            drive_count = self.indexer.get_file_count(
                source='drive') if self.is_authenticated else 0
            total_count = self.indexer.get_file_count()

            print(f"📁 Arquivos locais: {local_count:,}")
            print(f"☁️ Arquivos Drive: {drive_count:,}")
            print(f"📊 Total geral: {total_count:,}")
        except Exception as e:
            print(f"❌ Erro ao contar arquivos: {e}")

        current_search = self.search_entry.text().strip()
        current_results = len(self.file_list_model._files) if hasattr(
            self.file_list_model, '_files') else 0
        print(
            f"🔍 Busca atual: '{current_search}' ({current_results} resultados)")

        print(f"\n💡 ATALHOS DE DEBUG:")
        print(f"   F10 - Testar amostras de acentos")
        print(f"   F11 - Status detalhado do banco")
        print(f"   F12 - Debug da busca atual (ou este status se vazio)")
        print(f"="*60)

    def debug_database_status(self):
        print(f"\n" + "="*60)
        print(f"💾 DEBUG STATUS DO BANCO DE DADOS")
        print(f"="*60)

        try:
            self.indexer.cursor.execute('SELECT COUNT(*) FROM files')
            files_count = self.indexer.cursor.fetchone()[0]

            self.indexer.cursor.execute('SELECT COUNT(*) FROM search_index')
            search_count = self.indexer.cursor.fetchone()[0]

            print(f"📋 Tabela 'files': {files_count:,} registros")
            print(f"🔍 Tabela 'search_index': {search_count:,} registros")

            consistent = files_count == search_count
            status = "✅ CONSISTENTE" if consistent else "⚠️ INCONSISTENTE"
            print(f"🎯 Consistência files ↔ search_index: {status}")

            self.indexer.cursor.execute(
                'SELECT source, COUNT(*) FROM files GROUP BY source')
            sources = self.indexer.cursor.fetchall()
            print(f"\n📂 Distribuição por fonte:")
            for source, count in sources:
                print(f"   {source}: {count:,} arquivos")

            self.indexer.cursor.execute('''
                SELECT name, normalized_name FROM search_index 
                WHERE name != normalized_name 
                LIMIT 5
            ''')
            accent_samples = self.indexer.cursor.fetchall()

            if accent_samples:
                print(f"\n📝 Amostras de normalização no índice:")
                for original, normalized in accent_samples:
                    print(f"   '{original}' → '{normalized}'")
            else:
                print(f"\n📝 Nenhum arquivo com acentos encontrado no índice")

        except Exception as e:
            print(f"❌ Erro ao acessar banco: {e}")

        print(f"="*60)

    def debug_test_accent_samples(self):
        print(f"\n" + "="*60)
        print(f"🧪 DEBUG TESTE DE AMOSTRAS COM ACENTOS")
        print(f"="*60)

        test_samples = [
            ("formação", "formacao"),
            ("ação", "acao"),
            ("coração", "coracao"),
            ("documentação", "documentacao"),
            ("expiação", "expiacao"),
            ("damião", "damiao"),
            ("são", "sao"),
            ("joão", "joao"),
            ("educação", "educacao"),
            ("informação", "informacao")
        ]

        print(f"🔍 Testando {len(test_samples)} termos com acentos...\n")

        total_tests = len(test_samples)
        passed_tests = 0

        for i, (original, expected_norm) in enumerate(test_samples, 1):
            actual_norm = self.search_engine.normalize_text(original)
            norm_ok = actual_norm == expected_norm

            try:
                results_with_accent = self.search_engine.load_files_paged(
                    source=None, page=0, page_size=3, search_term=original,
                    sort_by='name_asc', filter_type='all'
                )
                results_without_accent = self.search_engine.load_files_paged(
                    source=None, page=0, page_size=3, search_term=expected_norm,
                    sort_by='name_asc', filter_type='all'
                )

                search_ok = len(results_with_accent) == len(
                    results_without_accent)
                has_results = len(results_with_accent) > 0

                if norm_ok and search_ok:
                    passed_tests += 1
                    status = "✅" if has_results else "✓"
                else:
                    status = "❌"

                result_info = f"({len(results_with_accent)} resultados)" if has_results else "(sem resultados)"

                print(
                    f"{i:2d}. {status} '{original}' → '{actual_norm}' {result_info}")

                if has_results and i <= 3:
                    example = results_with_accent[0].get('name', 'N/A')[:50]
                    print(f"     Exemplo: {example}...")

            except Exception as e:
                print(f"{i:2d}. ❌ '{original}' → ERRO: {e}")

        success_rate = (passed_tests / total_tests) * 100
        print(f"\n📊 RESULTADO DO TESTE:")
        print(
            f"   ✅ Testes passaram: {passed_tests}/{total_tests} ({success_rate:.1f}%)")
        print(
            f"   🎯 Status geral: {'APROVADO' if success_rate >= 80 else 'PRECISA MELHORIAS'}")

        if success_rate < 100:
            print(
                f"\n💡 Dica: Se alguns testes falharam, execute a reconstrução do índice")
            print(f"   Comando: python -c \"from database import FileIndexer; FileIndexer().rebuild_search_index_with_normalization()\"")

        print(f"="*60)

    def _add_thumbnail_widgets(self, files_to_add):
        if self.current_page == 0:
            self.file_list_model.setFiles(files_to_add)
        else:
            self.file_list_model.addFiles(files_to_add)

    def on_folder_tree_selected(self, folder_path):
        root_dir = getattr(getattr(self, 'folder_tree', None), 'root_dir', None)
        is_root = bool(
            folder_path and root_dir
            and os.path.normcase(os.path.normpath(folder_path)) == os.path.normcase(os.path.normpath(root_dir))
        )
        target_filter = os.path.normpath(folder_path) if (folder_path and not is_root) else None
        curr_filter = getattr(self, 'current_folder_path_filter', None)
        same_filter = (
            (curr_filter is None and target_filter is None)
            or (
                curr_filter is not None and target_filter is not None
                and os.path.normcase(os.path.normpath(curr_filter)) == os.path.normcase(os.path.normpath(target_filter))
            )
        )
        if same_filter and self.file_list_model.rowCount() > 0 and not self.is_loading:
            return

        self.current_folder_path_filter = target_filter
        self.is_loading = False
        self.current_page = 0
        self.all_files_loaded = False
        list_update.clear_display(self)
        list_update.load_next_batch(self)

        if hasattr(self, 'status_bar') and self.status_bar:
            if target_filter:
                self.status_bar.showMessage(f"📁 Modo Pasta: {os.path.basename(target_filter)} (Busca recursiva em subpastas ativada)", 3500)
            else:
                self.status_bar.showMessage("🌐 Modo Global: Busca em Todo o Acervo ativada.", 3500)

    def select_folder_for_file(self, file_item):
        """Seleciona na árvore e filtra a busca pela pasta que contém o arquivo clicado."""
        if not file_item:
            return

        fid = file_item.get('file_id') or file_item.get('id')
        fname = file_item.get('name') or ''
        fpath = file_item.get('path') or ''
        is_dir = file_item.get('mimeType') in ('application/vnd.google-apps.folder', 'folder', 'directory')
        virtual_path = bool(file_item.get('is_staged_move'))

        if not file_item.get('is_staged_move') and hasattr(self, 'main_bar') and hasattr(self.main_bar, 'staging_queue'):
            norm_f = os.path.normcase(os.path.normpath(fpath)) if fpath else ''
            for it in self.main_bar.staging_queue.items:
                if it.action_type == 'move' and it.new_value:
                    it_p = os.path.normcase(os.path.normpath(it.path)) if it.path else ''
                    if (fid and it.file_id == fid) or (norm_f and it_p == norm_f):
                        fpath = it.new_value
                        virtual_path = True
                        break

        if not virtual_path and fpath and not os.path.exists(fpath) and (file_item.get('source') or 'local') == 'local':
            try:
                from src.services.path_reconciler import resolve_and_heal_local_path
                healed = resolve_and_heal_local_path(file_item)
                if healed:
                    fpath = healed
            except Exception:
                pass

        if not virtual_path and (not fpath or not os.path.exists(fpath)) and hasattr(self, 'indexer') and self.indexer:
            try:
                self.indexer.ensure_conn()
                rows = []
                if fid:
                    rows = self.indexer.cursor.execute(
                        "SELECT path FROM files WHERE source='local' AND file_id=? AND path IS NOT NULL LIMIT 2",
                        (fid,),
                    ).fetchall()
                if not rows and fname:
                    rows = self.indexer.cursor.execute(
                        "SELECT path FROM files WHERE source='local' AND name=? AND path IS NOT NULL LIMIT 2",
                        (fname,),
                    ).fetchall()
                if len(rows) == 1 and rows[0][0]:
                    fpath = rows[0][0]
            except Exception:
                pass

        if not fpath:
            if hasattr(self, 'status_bar') and self.status_bar:
                self.status_bar.showMessage("Pasta local não encontrada para este arquivo.", 4000)
            return

        norm_fpath = os.path.normpath(fpath)
        folder_path = norm_fpath if (is_dir or os.path.isdir(norm_fpath)) else os.path.normpath(os.path.dirname(norm_fpath))
        if not folder_path or not os.path.isdir(folder_path):
            if hasattr(self, 'status_bar') and self.status_bar:
                self.status_bar.showMessage(f"Pasta local não encontrada: {folder_path}", 4000)
            return

        selected_in_tree = False
        if hasattr(self, 'folder_tree') and self.folder_tree:
            selected_in_tree = bool(self.folder_tree.select_and_expand_folder(folder_path))
            if not selected_in_tree:
                self.folder_tree._update_header_for_folder(folder_path)
                self.on_folder_tree_selected(folder_path)
        else:
            self.on_folder_tree_selected(folder_path)

    def on_files_dropped_on_folder(self, dropped_items, target_folder_path):
        if not self.main_bar.staging_queue.can_edit():
            return
        if not dropped_items or not target_folder_path:
            return

        from src.ui.staging_queue import StagingQueue, StagingItem
        from src.ui.move_conflict_dialog import MoveConflictDialog
        queue = StagingQueue()
        staged_count = 0
        reverted_count = 0

        # Separar itens entre não-conflitantes e conflitantes (mesmo nome já existe no destino)
        valid_items_to_process = []
        conflicts = []
        source_folder_names = []

        for item in dropped_items:
            src_path = item.get('path') or item.get('file_id') or ''
            if not src_path:
                continue
            src_path = os.path.normpath(src_path)
            fname = item.get('name') or os.path.basename(src_path)
            dst_path = os.path.normpath(os.path.join(target_folder_path, fname))

            if os.path.normpath(os.path.dirname(src_path)).lower() == os.path.normpath(target_folder_path).lower():
                continue  # Arquivo já está nesta mesma pasta

            # Proteção contra mover pasta para dentro de si mesma
            if os.path.isdir(src_path):
                norm_src = src_path.lower()
                norm_tgt = os.path.normpath(target_folder_path).lower()
                if norm_tgt == norm_src or norm_tgt.startswith(norm_src + os.sep):
                    if hasattr(self, 'status_bar') and self.status_bar:
                        self.status_bar.showMessage("❌ Não é permitido mover uma pasta para dentro de si mesma.", 4000)
                    continue

            fid = item.get('file_id') or item.get('id') or src_path

            # Obter o caminho físico original registrado no banco SQLite
            db_orig_path = None
            if hasattr(self, 'indexer') and self.indexer:
                try:
                    self.indexer.ensure_conn()
                    self.indexer.cursor.execute("SELECT path FROM files WHERE file_id = ? OR path = ? LIMIT 1", (fid, src_path))
                    r = self.indexer.cursor.fetchone()
                    if r and r[0]:
                        db_orig_path = os.path.normpath(r[0])
                except Exception:
                    pass
            if not db_orig_path:
                db_orig_path = item.get('physical_path') or item.get('orig_path') or src_path

            # Se o arquivo já está marcado para exclusão definitiva (delete), ignora a tentativa de mover
            is_deleted = any(
                (it.file_id == fid or (it.path and os.path.normpath(it.path).lower() == src_path.lower())) and it.action_type == 'delete'
                for it in queue.items
            )
            if is_deleted:
                continue

            # Se o destino é a pasta original do banco, é uma reversão (não gera conflito)
            is_revert = (os.path.normpath(dst_path).lower() == os.path.normpath(db_orig_path).lower())

            # Checar se já existe um arquivo com esse nome no destino
            has_clash = False
            if not is_revert:
                if os.path.exists(dst_path):
                    has_clash = True
                elif hasattr(self, 'indexer') and self.indexer:
                    try:
                        self.indexer.cursor.execute("SELECT 1 FROM files WHERE LOWER(path) = ? LIMIT 1", (dst_path.lower(),))
                        if self.indexer.cursor.fetchone():
                            has_clash = True
                    except Exception:
                        pass

            item_data = {
                'fid': fid,
                'fname': fname,
                'src_path': src_path,
                'db_orig_path': db_orig_path,
                'dst_path': dst_path,
                'is_revert': is_revert
            }

            if has_clash:
                conflicts.append(item_data)
                src_parent_name = os.path.basename(os.path.dirname(src_path))
                if src_parent_name and src_parent_name not in source_folder_names:
                    source_folder_names.append(src_parent_name)
            else:
                valid_items_to_process.append(item_data)

        # Se houver conflitos, abrir diálogo para o usuário decidir
        conflict_action = "create_subfolder"
        conflict_subfolder = "mesmonome"
        if conflicts:
            sug_name = source_folder_names[0] if source_folder_names else "mesmonome"
            dialog = MoveConflictDialog(conflicts, target_folder_path, source_folder_name=sug_name, parent=self)
            if dialog.exec() == MoveConflictDialog.DialogCode.Accepted:
                conflict_action, conflict_subfolder = dialog.get_result()
            else:
                # Usuário cancelou a operação
                if hasattr(self, 'status_bar') and self.status_bar:
                    self.status_bar.showMessage("Movimentação cancelada pelo usuário.", 3000)
                return

        self._suppress_staging_reload = True
        try:
            # Processar itens não-conflitantes
            for it in valid_items_to_process:
                fid = it['fid']
                src_path = it['src_path']
                fname = it['fname']
                db_orig_path = it['db_orig_path']
                dst_path = it['dst_path']

                for q_it in list(queue.items):
                    if (q_it.file_id == fid or (q_it.path and os.path.normpath(q_it.path).lower() == src_path.lower())) and q_it.action_type == 'move':
                        queue.remove_item(q_it)

                if it['is_revert']:
                    reverted_count += 1
                else:
                    st_item = StagingItem(fid, fname, db_orig_path, 'move', old_value=db_orig_path, new_value=dst_path)
                    queue.add_item(st_item)
                    staged_count += 1

            # Processar itens conflitantes de acordo com a escolha do usuário
            if conflicts:
                if conflict_action == "create_subfolder":
                    sub_dir = os.path.normpath(os.path.join(target_folder_path, conflict_subfolder))
                    for it in conflicts:
                        fid = it['fid']
                        src_path = it['src_path']
                        fname = it['fname']
                        db_orig_path = it['db_orig_path']
                        sub_dst_path = os.path.normpath(os.path.join(sub_dir, fname))

                        for q_it in list(queue.items):
                            if (q_it.file_id == fid or (q_it.path and os.path.normpath(q_it.path).lower() == src_path.lower())) and q_it.action_type == 'move':
                                queue.remove_item(q_it)

                        st_item = StagingItem(fid, fname, db_orig_path, 'move', old_value=db_orig_path, new_value=sub_dst_path)
                        queue.add_item(st_item)
                        staged_count += 1

                elif conflict_action == "rename":
                    for it in conflicts:
                        fid = it['fid']
                        src_path = it['src_path']
                        fname = it['fname']
                        db_orig_path = it['db_orig_path']
                        dst_path = it['dst_path']

                        base, ext = os.path.splitext(dst_path)
                        counter = 1
                        while True:
                            candidate = f"{base}_{counter}{ext}"
                            if not os.path.exists(candidate) and not any(q_it.new_value and os.path.normpath(q_it.new_value).lower() == os.path.normpath(candidate).lower() for q_it in queue.items):
                                dst_path = candidate
                                break
                            counter += 1

                        for q_it in list(queue.items):
                            if (q_it.file_id == fid or (q_it.path and os.path.normpath(q_it.path).lower() == src_path.lower())) and q_it.action_type == 'move':
                                queue.remove_item(q_it)

                        st_item = StagingItem(fid, os.path.basename(dst_path), db_orig_path, 'move', old_value=db_orig_path, new_value=dst_path)
                        queue.add_item(st_item)
                        staged_count += 1
        finally:
            self._suppress_staging_reload = False

        self._prev_move_items, self._prev_delete_items = self._staging_action_snapshots()

        folder_name = os.path.basename(target_folder_path)
        if hasattr(self, 'status_bar') and self.status_bar:
            if staged_count > 0:
                self.status_bar.showMessage(f"📦 {staged_count} arquivo(s) enfileirado(s) para mover para '{folder_name}'.", 4000)
            elif reverted_count > 0:
                self.status_bar.showMessage(f"↩️ {reverted_count} arquivo(s) retornado(s) à pasta original.", 4000)

        # Atualizar grid mantendo lotes carregados e posição de rolagem
        self._reload_keeping_scroll(restore_delay_ms=50)

    def _staging_action_snapshots(self):
        items = getattr(getattr(getattr(self, 'main_bar', None), 'staging_queue', None), 'items', [])
        moves = [f"{it.file_id}_{it.action_type}_{it.new_value}" for it in items if it.action_type == 'move']
        deletes = [f"{it.file_id}_{it.path}" for it in items if it.action_type == 'delete']
        return moves, deletes

    def _on_staging_queue_changed(self, count):
        try:
            # Exclusões e alterações de tags atualizam apenas o visual/opacidade no lugar,
            # preservando página, seleção e posição de rolagem do usuário.
            # Apenas movimentações virtuais ('move') precisam recarregar a lista da pasta.
            current_move_items, current_delete_items = self._staging_action_snapshots()
            if not hasattr(self, '_prev_move_items'):
                self._prev_move_items = []
            if not hasattr(self, '_prev_delete_items'):
                self._prev_delete_items = []

            if current_move_items != self._prev_move_items:
                self._prev_move_items = list(current_move_items)
                self._prev_delete_items = list(current_delete_items)
                if (
                    not getattr(self, '_suppress_staging_reload', False)
                    and getattr(self, 'current_folder_path_filter', None)
                ):
                    self._reload_keeping_scroll(restore_delay_ms=50)
            elif current_delete_items != self._prev_delete_items:
                self._prev_delete_items = list(current_delete_items)
                if hasattr(self, 'file_list_view') and self.file_list_view:
                    self.file_list_view.viewport().update()
        except Exception as e:
            print(f"[UI] Erro ao tratar mudança na fila de staging: {e}")

    def focus_staging_item(self, staging_item):
        """Ao clicar em um item na janela da Fila, navega na árvore para a pasta atual do arquivo (incluindo destino de 'move') e o seleciona na grade."""
        if not staging_item:
            return

        fid = staging_item.file_id
        fname = staging_item.file_name
        original_path = staging_item.path or staging_item.old_value or ''
        fpath = original_path
        target_file_obj = None

        # Resolver sempre o registro local exato. Nome e usado somente como
        # ultimo recurso quando existe um unico homonimo no banco.
        if hasattr(self, 'indexer') and self.indexer:
            try:
                from src.database.database import to_canonical_id
                self.indexer.ensure_conn()
                columns = (
                    "file_id,name,path,mimeType,source,description,thumbnailLink,"
                    "thumbnailPath,size,modifiedTime,createdTime,parentId,starred,"
                    "webContentLink,thumbnailRotation"
                )
                canon_id = to_canonical_id(fid)
                canon_path = to_canonical_id(original_path)
                rows = self.indexer.cursor.execute(
                    f"SELECT {columns} FROM files WHERE source='local' "
                    "AND (file_id=? OR path=?) LIMIT 2",
                    (canon_id, canon_path),
                ).fetchall()
                if not rows and fname:
                    rows = self.indexer.cursor.execute(
                        f"SELECT {columns} FROM files WHERE source='local' "
                        "AND name=? LIMIT 2",
                        (fname,),
                    ).fetchall()
                if len(rows) == 1:
                    row = rows[0]
                    target_file_obj = {
                        'id': row[0], 'name': row[1], 'path': row[2],
                        'mimeType': row[3], 'source': row[4],
                        'description': row[5], 'thumbnailLink': row[6],
                        'thumbnailPath': row[7], 'size': row[8],
                        'modifiedTime': row[9], 'createdTime': row[10],
                        'parentId': row[11], 'starred': bool(row[12]),
                        'webContentLink': row[13],
                        'thumbnailRotation': int(row[14] or 0),
                    }
                    fpath = row[2] or fpath
                    fid = row[0]
            except Exception as exc:
                logging.warning("Falha ao resolver item da fila para foco: %s", exc)

        if not fpath:
            self.status_bar.showMessage(
                f"Não foi possível localizar com segurança: {fname}", 5000
            )
            return

        fpath = os.path.normpath(fpath)

        # 1. Verificar se o arquivo possui um movimento de 'move' registrado na fila
        has_moved_staged = False
        dst_folder_name = ""
        if hasattr(self, 'main_bar') and hasattr(self.main_bar, 'staging_queue'):
            for it in self.main_bar.staging_queue.items:
                if it.action_type == 'move':
                    p_match = (it.path and os.path.normpath(it.path).lower() == fpath.lower())
                    id_match = (it.file_id == fid)
                    if p_match or id_match:
                        fpath = os.path.normpath(it.new_value)
                        has_moved_staged = True
                        dst_folder_name = os.path.basename(os.path.dirname(fpath))
                        if target_file_obj:
                            target_file_obj = dict(target_file_obj)
                            target_file_obj['physical_path'] = target_file_obj.get('path')
                            target_file_obj['orig_path'] = target_file_obj.get('path')
                            target_file_obj['path'] = fpath
                            target_file_obj['parentId'] = os.path.dirname(fpath)
                            target_file_obj['is_staged_move'] = True
                        break

        folder_path = os.path.normpath(os.path.dirname(fpath))

        # 2. Expandir e selecionar a pasta na Árvore de Diretórios
        if hasattr(self, 'folder_tree') and self.folder_tree:
            self.folder_tree.select_and_expand_folder(folder_path)

        # 3. Notificação informativa caso o arquivo tenha sido movido para outra pasta na fila
        if has_moved_staged and hasattr(self, 'status_bar') and self.status_bar:
            self.status_bar.showMessage(f"📦 Arquivo movido na fila para '{dst_folder_name}' (exibindo na pasta de destino atual).", 5000)

        # 4. Localizar e iluminar/selecionar o arquivo na grade do meio
        def _find_and_select(retries=10):
            if not hasattr(self, 'file_list_model') or not self.file_list_model:
                return

            target_idx = -1
            selected_obj = None
            for idx, f in enumerate(self.file_list_model._files):
                p = os.path.normpath(f.get('path') or '')
                if (p.lower() == fpath.lower()
                        or f.get('id') == fid or f.get('file_id') == fid):
                    target_idx = idx
                    selected_obj = f
                    break

            if (target_idx < 0 and target_file_obj
                    and not getattr(self, 'is_loading', False)):
                # A grade e paginada e pode estar filtrada. Para um pedido de
                # foco explicito, inserir o registro exato e mais confiavel que
                # desistir depois de 400 itens ou selecionar um homonimo.
                self.file_list_model.addFiles([target_file_obj])
                target_idx = len(self.file_list_model._files) - 1
                selected_obj = target_file_obj

            if target_idx >= 0:
                q_idx = self.file_list_model.index(target_idx, 0)
                if q_idx.isValid() and self.file_list_view.selectionModel():
                    self.file_list_view.setCurrentIndex(q_idx)
                    self.file_list_view.selectionModel().setCurrentIndex(
                        q_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
                    )
                    self.file_list_view.scrollTo(q_idx, QAbstractItemView.ScrollHint.PositionAtCenter)
                if selected_obj and hasattr(self, 'details_panel') and self.details_panel:
                    self.details_panel.update_details(selected_obj)
                if hasattr(self, 'status_bar') and self.status_bar and not has_moved_staged:
                    self.status_bar.showMessage(f"🔍 Item em foco: {fname}", 4000)
            else:
                if getattr(self, 'is_loading', False) and retries > 0:
                    QTimer.singleShot(60, lambda: _find_and_select(retries - 1))
                elif retries <= 0:
                    self.status_bar.showMessage(
                        f"Não foi possível exibir com segurança: {fname}", 5000
                    )

        QTimer.singleShot(100, lambda: _find_and_select(10))

    def on_vocab_tag_selected(self, tag_text):
        focus_widget = QApplication.focusWidget()
        is_search_focused = (focus_widget == getattr(self.main_bar, 'search_entry', None))

        # Se há foto(s) selecionada(s) ou o painel de detalhes está visível com fotos ativas:
        has_selected_files = bool(
            self.file_list_view.selectedIndexes() or
            (hasattr(self, 'details_panel') and self.details_panel and self.details_panel.isVisible() and
             (getattr(self.details_panel, 'current_file_item', None) or getattr(self.details_panel, 'current_files_list', None)))
        )

        if not is_search_focused and has_selected_files:
            if hasattr(self, 'details_panel') and self.details_panel:
                self.details_panel._add_tag_directly(tag_text)
                if hasattr(self, 'status_bar') and self.status_bar:
                    self.status_bar.showMessage(f"🏷️ Tag '{tag_text}' adicionada à foto / lote selecionado.", 3000)
                return

        # Caso contrário, insere no campo de pesquisa da barra principal
        current = self.main_bar.search_entry.text().strip()
        if not current:
            self.main_bar.search_entry.setText(tag_text)
        elif tag_text.lower() not in current.lower():
            self.main_bar.search_entry.setText(f"{current} {tag_text}")
        if hasattr(self, 'status_bar') and self.status_bar:
            self.status_bar.showMessage(f"🔍 Tag '{tag_text}' adicionada ao campo de busca.", 3000)

    def _on_vocab_updated(self):
        from src.ui.vocab_panel import VocabManager
        vocab_tags = VocabManager().get_all_tags()
        
        # 1. Atualizar completer da barra de busca principal
        if hasattr(self.main_bar, 'search_entry'):
            from PyQt6.QtWidgets import QCompleter
            completer = QCompleter(vocab_tags, self.main_bar.search_entry)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self.main_bar.search_entry.setCompleter(completer)
        
        # 2. Atualizar completer e sugestões no painel de detalhes
        if hasattr(self, 'details_panel') and self.details_panel:
            if hasattr(self.details_panel, 'tag_chips_widget'):
                self.details_panel.tag_chips_widget.update_vocab_suggestions(vocab_tags)
            if getattr(self.details_panel, '_is_batch_mode', False) and getattr(self.details_panel, 'current_files_list', None):
                self.details_panel._update_suggested_tags_batch(self.details_panel.current_files_list)
            elif getattr(self.details_panel, 'current_file_item', None):
                self.details_panel._update_suggested_tags(self.details_panel.current_file_item)

    def on_file_selected(self, file_item):
        pass

    def on_files_selected(self, files_list):
        if files_list:
            if len(files_list) == 1:
                self.details_panel.update_details(files_list[0])
            else:
                self.details_panel.update_details_batch(files_list)
        # Uma selecao vazia pode ser transitoria durante recarga/paginacao ou
        # resultar de um clique no fundo. O painel conserva o ultimo contexto;
        # ele so e substituido quando outro arquivo e efetivamente selecionado.

    def on_double_click(self, file_item):
        if not file_item:
            return

        is_folder = (file_item.get('mimeType') in ['application/vnd.google-apps.folder', 'folder', 'directory'])
        fpath = file_item.get('path') or file_item.get('id')
        if not is_folder and fpath and os.path.isdir(fpath):
            is_folder = True

        if is_folder:
            self.search_term = ""
            self.main_bar.search_entry.clear()
            if fpath and hasattr(self, 'folder_tree') and self.folder_tree:
                self.folder_tree.select_and_expand_folder(fpath)
            else:
                self.current_folder_path_filter = fpath
                self.current_folder_id = file_item.get('id') or file_item.get('file_id')
                self.current_page = 0
                self.all_files_loaded = False
                list_update.clear_display(self)
                list_update.load_next_batch(self)
            return

        # Para arquivos (fotos, vídeos, documentos):
        fpath = file_item.get('path')
        if not fpath:
            # Tentar buscar no banco SQLite se houver caminho físico correspondente
            fid = file_item.get('file_id') or file_item.get('id')
            fname = file_item.get('name')
            if hasattr(self, 'indexer') and self.indexer:
                try:
                    self.indexer.ensure_conn()
                    self.indexer.cursor.execute("SELECT path FROM files WHERE (file_id = ? OR name = ?) AND path IS NOT NULL LIMIT 1", (fid, fname))
                    row = self.indexer.cursor.fetchone()
                    if row and row[0]:
                        fpath = row[0]
                except Exception:
                    pass

        if fpath and not os.path.exists(fpath):
            from src.services.path_reconciler import resolve_and_heal_local_path
            fpath = resolve_and_heal_local_path(file_item, indexer=getattr(self, 'indexer', None)) or fpath

        if fpath:
            norm_p = os.path.normpath(fpath)
            if os.path.exists(norm_p):
                # Arquivos .MTS / .M2TS usam codec AVCHD/AC-3 que o player nativo do Windows não reconhece sem pacote externo;
                # abrimos no player interno FFmpeg do VoxImago que decodifica vídeo e áudio nativamente.
                if os.path.splitext(norm_p)[1].lower() in ('.mts', '.m2ts', '.ts'):
                    self.file_list_view.show_quick_preview()
                    return
                try:
                    os.startfile(norm_p)
                    return
                except Exception:
                    try:
                        from PyQt6.QtGui import QDesktopServices
                        from PyQt6.QtCore import QUrl
                        QDesktopServices.openUrl(QUrl.fromLocalFile(norm_p))
                        return
                    except Exception:
                        pass

        # Se não houver arquivo local, abre na nuvem (Google Drive)
        if file_item.get('webViewLink'):
            webbrowser.open(file_item['webViewLink'])
        elif file_item.get('webContentLink'):
            webbrowser.open(file_item['webContentLink'])

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Backspace:
            if self.current_folder_id:
                self.go_to_parent_folder()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            selected_indexes = self.file_list_view.selectedIndexes()
            if selected_indexes:
                file_item = self.file_list_model.data(
                    selected_indexes[0], Qt.ItemDataRole.UserRole)
                self.on_double_click(file_item)
        elif event.key() == Qt.Key.Key_Delete:
            selected_indexes = self.file_list_view.selectedIndexes()
            if selected_indexes:
                self.file_list_view.deleteRequested.emit()
        elif event.key() == Qt.Key.Key_F12:
            current_search = self.main_bar.search_entry.text().strip()
            if current_search:
                self.search_engine.debug_search_normalization(current_search)
            else:
                self.debug_system_status()
        elif event.key() == Qt.Key.Key_F11:
            self.debug_database_status()
        elif event.key() == Qt.Key.Key_F10:
            self.debug_test_accent_samples()
        elif event.key() == Qt.Key.Key_F7:
            print("🔧 F7: Debug CPU Profile (não implementado)")
        elif event.key() == Qt.Key.Key_F8:
            print("📊 F8: Debug Memory Snapshot (não implementado)")
        elif event.key() == Qt.Key.Key_F9:
            print("💾 F9: Debug Memory Stats (não implementado)")
        elif event.key() == Qt.Key.Key_S and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            print("🔄 Atalho Ctrl+S - Iniciando sincronização do Google Drive...")
            self._start_drive_sync()
        else:
            super().keyPressEvent(event)

    def go_to_parent_folder(self):
        self.indexer.cursor.execute(
            "SELECT parentId FROM files WHERE file_id = ?", (self.current_folder_id,))
        row = self.indexer.cursor.fetchone()
        parent_id = row[0] if row else None
        self.navigate_to_folder(parent_id)


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
