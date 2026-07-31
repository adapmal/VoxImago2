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
        self.unified_layout.addSpacing(15)

        self.unified_layout.addStretch()

        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("Pesquisar...")
        self.search_entry.setFixedWidth(300)
        self.unified_layout.addWidget(self.search_entry)
        self.unified_layout.addStretch()

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

        for menu in [self.menu_utils, self.menu_diag, self.menu_view]:
            menu.hovered.connect(show_action_tooltip)

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
