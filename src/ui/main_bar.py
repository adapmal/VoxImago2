from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QComboBox, QToolButton, QMenu, QPushButton, QToolTip, QWidgetAction, QWidget, QVBoxLayout
)
from PyQt6.QtGui import QAction, QFont, QPixmap, QCursor, QIcon, QActionGroup
from PyQt6.QtCore import pyqtSignal, Qt, QSize
from src.utils.default_avatar import create_default_avatar
from src.google_profile import make_circular_pixmap, PhotoDownloadWorker, GoogleProfileWorker


class MainBar(QFrame):
    profile_requested = pyqtSignal(object)

    def __init__(self, parent=None):
        print('DEBUG: MainBar.__init__ INICIO')
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setFixedHeight(50)

        self.unified_layout = QHBoxLayout(self)
        self.unified_layout.setContentsMargins(15, 8, 15, 8)

        self.app_title_label = QLabel("VI-MB")
        self.app_title_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.unified_layout.addWidget(self.app_title_label)
        self.unified_layout.addSpacing(10)

        # Campo de Pesquisa com botão de limpeza e auto-complete
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("Pesquisar...")
        self.search_entry.setFixedWidth(220)
        self.search_entry.setClearButtonEnabled(True)

        from PyQt6.QtWidgets import QCompleter
        from src.ui.vocab_panel import VocabManager
        vocab_tags = VocabManager().get_all_tags()
        if vocab_tags:
            completer = QCompleter(vocab_tags, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self.search_entry.setCompleter(completer)

        self.unified_layout.addWidget(self.search_entry)
        self.unified_layout.addSpacing(4)

        self.vocab_toggle_btn = QPushButton("🏷️ Tags ▼")
        self.vocab_toggle_btn.setFixedHeight(32)
        self.vocab_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vocab_toggle_btn.setStyleSheet(
            "background-color: #E3F2FD; color: #1565C0; border: 1px solid #BBDEFB; border-radius: 4px; padding: 4px 8px; font-weight: bold; font-size: 12px;")
        self.vocab_toggle_btn.setToolTip("Abrir/Fechar painel horizontal de Vocabulário Controlado (Tags).")
        self.unified_layout.addWidget(self.vocab_toggle_btn)
        self.unified_layout.addSpacing(10)

        self.sort_combo = QComboBox()
        self.sort_combo.addItem("🅰️ Nome (A-Z)", "name_asc")
        self.sort_combo.addItem("🆎 Nome (Z-A)", "name_desc")
        self.sort_combo.addItem("📏 Tamanho (Menor)", "size_asc")
        self.sort_combo.addItem("📏 Tamanho (Maior)", "size_desc")
        self.sort_combo.addItem("📅 Data (Mais recente)", "created_desc")
        self.sort_combo.addItem("📅 Data (Mais antiga)", "created_asc")
        self.sort_combo.setCurrentIndex(4)
        self.sort_combo.setEnabled(False)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(False)
        self.category_combo.addItem("🔵 Todos", "")
        self.category_combo.addItem("📸 Fotos+Vídeos", "media")
        self.category_combo.addItem("🖼️ Imagens", "images")
        self.category_combo.addItem("🎬 Vídeos", "videos")
        self.category_combo.addItem("📄 Documentos", "documents")
        self.category_combo.addItem("🎵 Áudios", "audios")
        filter_label = QLabel("Tipo:")
        filter_label.setFixedWidth(50)
        self.unified_layout.addWidget(filter_label)
        self.unified_layout.addWidget(self.category_combo)
        self.unified_layout.addSpacing(5)
        sort_label = QLabel("Ordenar por:")
        sort_label.setFixedWidth(70)
        self.unified_layout.addWidget(sort_label)
        self.unified_layout.addWidget(self.sort_combo)
        self.unified_layout.addSpacing(5)
        self.unified_layout.addStretch()

        print('DEBUG: MainBar passo 1')
        from src.utils.config_manager import ConfigManager
        from src.utils.sandbox_helper import generate_sample_test_data, ensure_sandbox_directory

        print('DEBUG: MainBar passo 2 - ConfigManager')
        self.config_mgr = ConfigManager()

        print('DEBUG: MainBar passo 3 - Sandbox badge')
        self.sandbox_badge = QLabel("🧪 Sandbox (L:\\Drives Compartilhados\\_TestesBanco)")
        self.sandbox_badge.setStyleSheet(
            "background-color: #FFF3CD; color: #856404; font-weight: bold; padding: 4px 8px; border-radius: 4px; border: 1px solid #FFEEBA;")
        self.sandbox_badge.setToolTip("Ambiente seguro de testes ativo (L:\\Drives Compartilhados\\_TestesBanco). O acervo oficial não é afetado.")
        self.unified_layout.addWidget(self.sandbox_badge)
        self.unified_layout.addSpacing(5)

        print('DEBUG: MainBar passo 4 - Mode toggle btn')
        self.mode_toggle_btn = QPushButton()
        self.mode_toggle_btn.setFixedHeight(34)
        self.mode_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_toggle_btn.clicked.connect(self._toggle_read_only_mode)
        self.unified_layout.addWidget(self.mode_toggle_btn)
        self.unified_layout.addSpacing(5)

        print('DEBUG: MainBar passo 5 - StagingQueue')
        from src.ui.staging_queue import StagingQueue, StagingQueueDialog

        self.staging_queue = StagingQueue()
        self.staging_btn = QPushButton("📋 Fila (0)")
        self.staging_btn.setFixedHeight(34)
        self.staging_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.staging_btn.setStyleSheet(
            "background-color: #E9ECEF; color: #495057; border: 1px solid #CED4DA; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 12px;")
        self.staging_btn.setToolTip("Abre a Fila de Revisão de Alterações Pendentes.")
        self.staging_btn.clicked.connect(self._open_staging_dialog)
        self.unified_layout.addWidget(self.staging_btn)
        self.unified_layout.addSpacing(5)

        self.staging_queue.queueChanged.connect(self._update_staging_button)

        print('DEBUG: MainBar passo 6 - update_mode_ui')
        self._update_mode_ui()
        self.config_mgr.modeChanged.connect(lambda k, v: self._update_mode_ui())
        print('DEBUG: MainBar passo 7 - actions')

        self.action_scan_options = QAction("📂 Selecionar Pastas Locais", self)
        self.action_scan_options.setToolTip(
            "Escolha quais pastas do seu computador serão monitoradas e sincronizadas.")
        self.action_sync_drive = QAction("☁️ Selecionar Pastas do Drive", self)
        self.action_sync_drive.setToolTip(
            "Selecione as pastas do Google Drive que deseja sincronizar com o computador.")
        self.action_reindex = QAction("🔄 Reindexar arquivos locais", self)
        self.action_reindex.setToolTip(
            "Reconstrua o índice de arquivos locais para corrigir inconsistências e acelerar buscas.")
        self.action_clear_cache = QAction("🧹 Limpar Cache", self)
        self.action_clear_cache.setToolTip(
            "Remove arquivos temporários e dados em cache para liberar espaço e corrigir possíveis erros.")

        self.action_toggle_read_only = QAction("🔒 Alternar Modo Somente Leitura / Edição", self)
        self.action_toggle_read_only.setToolTip("Alterna o aplicativo entre modo somente leitura (seguro) e modo de edição.")
        self.action_toggle_read_only.triggered.connect(self._toggle_read_only_mode)

        self.action_toggle_sandbox = QAction("🧪 Alternar Modo Sandbox (L:\\Drives Compartilhados\\_TestesBanco)", self)
        self.action_toggle_sandbox.setToolTip("Ativa ou desativa o ambiente seguro de testes em L:\\Drives Compartilhados\\_TestesBanco.")
        self.action_toggle_sandbox.triggered.connect(self._toggle_sandbox_mode)

        self.action_create_sandbox_sample = QAction("📦 Gerar Amostra de Teste (30-50 Fotos)", self)
        self.action_create_sandbox_sample.setToolTip("Copia uma amostra representativa de arquivos para L:\\Drives Compartilhados\\_TestesBanco.")
        self.action_create_sandbox_sample.triggered.connect(self._generate_sandbox_sample)

        self.view_mode_group = QActionGroup(self)
        self.view_mode_group.setExclusive(True)
        self.action_grid_view = QAction("🖼️ Visualização em Grade", self)
        self.action_grid_view.setToolTip(
            "Exibe os arquivos em formato de grade, facilitando a visualização de imagens e vídeos.")
        self.action_grid_view.setCheckable(True)
        self.action_list_view = QAction("📄 Visualização em Lista", self)
        self.action_list_view.setToolTip(
            "Exibe os arquivos em formato de lista detalhada, mostrando mais informações por linha.")
        self.action_list_view.setCheckable(True)
        self.action_explorer = QAction("🔍 Explorer Local", self)
        self.action_explorer.setToolTip(
            "Abra o modo de navegação local para explorar arquivos e pastas do seu computador.")
        self.action_explorer.setCheckable(True)
        self.view_mode_group.addAction(self.action_grid_view)
        self.view_mode_group.addAction(self.action_list_view)

        self.tools_menu = QMenu("Ferramentas", self)

        # Submenu Modos e Permissões
        self.menu_modes = QMenu("⚙️ Modos & Permissões", self.tools_menu)
        self.menu_modes.addAction(self.action_toggle_read_only)
        self.menu_modes.addAction(self.action_toggle_sandbox)
        self.menu_modes.addAction(self.action_create_sandbox_sample)
        self.tools_menu.addMenu(self.menu_modes)

        # Submenu Utilitários
        self.menu_utils = QMenu("🛠️ Utilitários", self.tools_menu)
        self.menu_utils.addAction(self.action_scan_options)
        self.menu_utils.addAction(self.action_sync_drive)
        self.menu_utils.addAction(self.action_clear_cache)
        self.tools_menu.addMenu(self.menu_utils)

        # Submenu Diagnóstico
        self.menu_diag = QMenu("🩺 Diagnóstico", self.tools_menu)
        self.menu_diag.addAction(self.action_reindex)
        self.tools_menu.addMenu(self.menu_diag)

        # Submenu Visualização
        self.menu_view = QMenu("👁️ Visualização", self.tools_menu)
        self.menu_view.addAction(self.action_explorer)
        self.menu_view.addSeparator()
        self.menu_view.addAction(self.action_grid_view)
        self.menu_view.addAction(self.action_list_view)
        self.tools_menu.addMenu(self.menu_view)

        self.tools_button = QToolButton(self)
        self.tools_button.setText("🛠️ Ferramentas")
        self.tools_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tools_button.setMenu(self.tools_menu)
        self.tools_button.setFixedHeight(36)
        self.tools_button.setStyleSheet("font-size: 14px;")
        self.unified_layout.addWidget(self.tools_button)

        self.avatar_label = QPushButton()
        self.avatar_label.setFixedSize(36, 36)
        self.avatar_label.setIcon(QIcon(QPixmap()))
        self.avatar_label.setIconSize(QSize(36, 36))
        self.avatar_label.setFlat(True)
        self.avatar_label.setStyleSheet(
            "border-radius: 18px; border: 2px solid #555;")
        self.avatar_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar_label.clicked.connect(self._show_profile_dialog)
        default_pixmap = create_default_avatar(36)
        self.avatar_label.setIcon(QIcon(default_pixmap))
        self.login_button = QPushButton("Login")
        self.logout_button = QPushButton("Logout")
        for btn in (self.login_button, self.logout_button):
            btn.setFixedHeight(36)
            btn.setStyleSheet("font-size: 14px; font-weight: normal;")
            btn.setVisible(False)
        self.unified_layout.addWidget(self.login_button)
        self.unified_layout.addWidget(self.logout_button)
        self.unified_layout.addWidget(self.avatar_label)

        self.profile_worker = None
        self.photo_worker = None

        self.user_profile = {}

        def show_action_tooltip(action):
            tip = action.toolTip()
            if tip:
                QToolTip.showText(QCursor.pos(), tip)

        for menu in [self.menu_modes, self.menu_utils, self.menu_diag, self.menu_view]:
            menu.hovered.connect(show_action_tooltip)
        print('DEBUG: MainBar FIM')

    def _show_profile_dialog(self):
        print("DEBUG: _show_profile_dialog chamado")

        if hasattr(self, '_profile_menu') and self._profile_menu is not None and self._profile_menu.isVisible():
            self._profile_menu.close()
            return
        menu = QMenu(self)
        self._profile_menu = menu
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        if self.user_profile:
            display_name = self.user_profile.get('displayName', 'Usuário')
            email = self.user_profile.get('emailAddress', '')
            name_label = QLabel(f"👤 {display_name}")
            name_label.setStyleSheet(
                "font-weight: bold; font-size: 14px; margin: 4px 0;")
            layout.addWidget(name_label)
            if email:
                email_label = QLabel(f"📧 {email}")
                email_label.setStyleSheet("color: #666; margin: 2px 0;")
                layout.addWidget(email_label)
            status_label = QLabel("🔗 Conectado ao Google Drive")
            status_label.setStyleSheet("color: #4CAF50; margin: 6px 0;")
            layout.addWidget(status_label)
        else:
            info_label = QLabel("👤 Usuário não autenticado")
            info_label.setStyleSheet(
                "font-weight: bold; font-size: 14px; margin: 8px 0;")
            layout.addWidget(info_label)
            desc_label = QLabel(
                "Faça login para acessar o Google Drive\ne ver suas informações de perfil.")
            desc_label.setStyleSheet("color: #666; margin: 4px 0;")
            layout.addWidget(desc_label)
        widget_action = QWidgetAction(menu)
        widget_action.setDefaultWidget(widget)
        menu.insertAction(
            menu.actions()[0] if menu.actions() else None, widget_action)

        def clear_menu_ref():
            self._profile_menu = None
        menu.aboutToHide.connect(clear_menu_ref)
        menu.exec(self.avatar_label.mapToGlobal(
            self.avatar_label.rect().bottomLeft()))

    def _handle_login_from_dialog(self):
        self.login_button.click()

    def _handle_logout_from_dialog(self):
        self.logout_button.click()

        # ...existing code...
    def update_profile(self, service):
        if service:
            self.profile_worker = GoogleProfileWorker(service)
            self.profile_worker.profile_loaded.connect(self._on_profile_loaded)
            self.profile_worker.profile_failed.connect(self._on_profile_failed)
            self.profile_worker.start()

    def _on_profile_loaded(self, profile_data):
        self.user_profile = profile_data

        photo_url = profile_data.get('photoLink', '')
        if photo_url:
            self._download_profile_photo(photo_url)

    def _on_profile_failed(self, error_msg):
        print(f"Erro ao carregar perfil: {error_msg}")
        self.user_profile = {
            'displayName': 'Usuário Google', 'emailAddress': ''}

    def _download_profile_photo(self, photo_url):
        self.photo_worker = PhotoDownloadWorker(photo_url, 36)
        self.photo_worker.photo_downloaded.connect(self._on_photo_downloaded)
        self.photo_worker.photo_failed.connect(self._on_photo_failed)
        self.photo_worker.start()

    def _on_photo_downloaded(self, pixmap):
        circular_pixmap = make_circular_pixmap(pixmap)
        self.avatar_label.setIcon(QIcon(circular_pixmap))

    def _on_photo_failed(self, error_msg):
        print(f"Erro ao baixar foto: {error_msg}")

    def reset_profile(self):
        default_pixmap = create_default_avatar(36)
        self.avatar_label.setIcon(QIcon(default_pixmap))
        self.user_profile = {}

    def _update_mode_ui(self):
        is_ro = self.config_mgr.is_read_only()
        is_sb = self.config_mgr.is_sandbox()

        if is_ro:
            self.mode_toggle_btn.setText("🔒 Somente Leitura")
            self.mode_toggle_btn.setStyleSheet(
                "background-color: #E2E3E5; color: #383D41; border: 1px solid #D6D8DB; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 12px;")
            self.mode_toggle_btn.setToolTip("Modo Somente Leitura ativo. Ações de edição estão bloqueadas.")
        else:
            self.mode_toggle_btn.setText("✏️ Modo Edição")
            self.mode_toggle_btn.setStyleSheet(
                "background-color: #D4EDDA; color: #155724; border: 1px solid #C3E6CB; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 12px;")
            self.mode_toggle_btn.setToolTip("Modo Edição ativo. Você pode alterar tags, renomear e mover arquivos.")

        self.sandbox_badge.setVisible(is_sb)

    def _toggle_read_only_mode(self):
        current = self.config_mgr.is_read_only()
        self.config_mgr.set_read_only(not current)

    def _toggle_sandbox_mode(self):
        from PyQt6.QtWidgets import QMessageBox
        current = self.config_mgr.is_sandbox()
        new_val = not current
        self.config_mgr.set_sandbox(new_val)
        status_str = "ATIVADO (L:\\_TestesBanco)" if new_val else "DESATIVADO (Modo Produção Oficial)"
        QMessageBox.information(
            self,
            "Ambiente Sandbox",
            f"O Modo Sandbox foi {status_str}.\n\nAo utilizar o Modo Sandbox, todas as buscas e edições ocorrem em 'L:\\Drives Compartilhados\\_TestesBanco'."
        )

    def _generate_sandbox_sample(self):
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from src.utils.sandbox_helper import generate_sample_test_data

        source_dir = QFileDialog.getExistingDirectory(
            self,
            "Selecione uma pasta com fotos de origem para copiar amostras de teste",
            "L:\\"
        )
        if source_dir:
            ok, msg, count = generate_sample_test_data(source_dir, max_files=50)
            if ok:
                QMessageBox.information(self, "Amostra de Testes Gerada", msg)
            else:
                QMessageBox.warning(self, "Erro ao Gerar Amostra", msg)

    def _update_staging_button(self, count):
        self.staging_btn.setText(f"📋 Fila ({count})")
        if count > 0:
            self.staging_btn.setStyleSheet(
                "background-color: #CCE5FF; color: #004085; border: 1px solid #B8DAFF; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 12px;")
        else:
            self.staging_btn.setStyleSheet(
                "background-color: #E9ECEF; color: #495057; border: 1px solid #CED4DA; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 12px;")

    def _open_staging_dialog(self):
        from src.ui.staging_queue import StagingQueueDialog
        dialog = StagingQueueDialog(self, drive_service=self.parent_app.service, db_indexer=self.parent_app.indexer)
        dialog.exec()


