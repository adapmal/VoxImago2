'''
Navegador de Árvore de Diretórios (Folder Tree Widget) para o VoxImago v2.1
Exibe a estrutura vertical de 4 Níveis (Ano > Categoria > País > Evento/Casa)
e permite filtrar o grid de imagens com um clique.
'''

import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeView,
    QHeaderView
)
from PyQt6.QtGui import QFileSystemModel
from PyQt6.QtCore import pyqtSignal, Qt, QDir
from src.utils.config_manager import ConfigManager


class FolderTreeWidget(QWidget):
    folderSelected = pyqtSignal(str)  # Emitido com o caminho completo da pasta selecionada

    def __init__(self, root_dir=None, parent=None):
        super().__init__(parent)
        self.config_mgr = ConfigManager()
        self.root_dir = root_dir or self._determine_root_dir()

        self.setMinimumWidth(220)
        self.setMaximumWidth(320)

        self._init_ui()

    def _determine_root_dir(self):
        if self.config_mgr.is_sandbox():
            sb_path = self.config_mgr.get_sandbox_path()
            if os.path.exists(sb_path):
                return sb_path

        scan_folders = self.config_mgr.get('scan_folders', [])
        if scan_folders:
            for sf in scan_folders:
                if os.path.exists(sf):
                    return sf

        possible_roots = [
            r"L:\Drives Compartilhados\_TestesBanco",
            r"L:\Drives Compartilhados\Banco de Imagens",
            r"L:\Banco de Imagens"
        ]
        for p in possible_roots:
            if os.path.exists(p):
                return p
        return r"L:\Drives Compartilhados"

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        header_layout = QHBoxLayout()
        self.title_label = QLabel("📂 Árvore de Pastas")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        header_layout.addWidget(self.title_label)
        layout.addLayout(header_layout)

        # Modelo do Sistema de Arquivos
        self.model = QFileSystemModel()
        self.model.setFilter(QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot)
        self.model.setRootPath(self.root_dir)

        # Componente TreeView
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.model)
        self.tree_view.setRootIndex(self.model.index(self.root_dir))

        # Ocultar colunas secundárias
        self.tree_view.hideColumn(1)
        self.tree_view.hideColumn(2)
        self.tree_view.hideColumn(3)

        self.tree_view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setAnimated(True)
        self.tree_view.setIndentation(16)
        self.tree_view.setStyleSheet("QTreeView { border: 1px solid #CED4DA; border-radius: 4px; font-size: 12px; }")

        self.tree_view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.tree_view)

        # Atualizar visualização quando o modo Sandbox alternar
        self.config_mgr.modeChanged.connect(self._on_mode_changed)

    def _on_selection_changed(self, selected, deselected):
        indexes = self.tree_view.selectedIndexes()
        if indexes:
            folder_path = self.model.filePath(indexes[0])
            self.folderSelected.emit(folder_path)

    def _on_mode_changed(self, key, value):
        if key in ('sandbox_mode', 'sandbox_path'):
            new_root = self._determine_root_dir()
            self.set_root_directory(new_root)

    def set_root_directory(self, path):
        if path and os.path.exists(path):
            self.root_dir = path
            self.model.setRootPath(self.root_dir)
            self.tree_view.setRootIndex(self.model.index(self.root_dir))
