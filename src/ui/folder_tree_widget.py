'''
Navegador de Árvore de Diretórios (Folder Tree Widget) para o VoxImago v2.1
Exibe a estrutura vertical de 4 Níveis (Ano > Categoria > País > Evento/Casa)
e permite filtrar o grid com um clique ou arrastar e soltar imagens para mover.
'''

import os
import json
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeView,
    QHeaderView, QAbstractItemView
)
from PyQt6.QtGui import QFileSystemModel, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtCore import pyqtSignal, Qt, QDir, QUrl
from src.utils.config_manager import ConfigManager


class DropEnabledTreeView(QTreeView):
    """QTreeView customizado que aceita drops de arquivos arrastados da grade."""
    
    filesDropped = pyqtSignal(list, str)  # (list_of_items_or_paths, target_folder_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-voximago-file-items"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            model = self.model()
            if isinstance(model, QFileSystemModel):
                path = model.filePath(index)
                if os.path.isdir(path):
                    self.setCurrentIndex(index)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            event.ignore()
            return

        model = self.model()
        if not isinstance(model, QFileSystemModel):
            event.ignore()
            return

        target_path = model.filePath(index)
        if not os.path.isdir(target_path):
            event.ignore()
            return

        dropped_items = []
        # 1. Tentar ler payload estruturado de arquivos do VoxImago
        if event.mimeData().hasFormat("application/x-voximago-file-items"):
            try:
                raw_bytes = event.mimeData().data("application/x-voximago-file-items")
                dropped_items = json.loads(bytes(raw_bytes).decode('utf-8'))
            except Exception:
                dropped_items = []

        # 2. Fallback para URLs de arquivos locais
        if not dropped_items and event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    local_p = os.path.normpath(url.toLocalFile())
                    dropped_items.append({
                        'file_id': local_p,
                        'name': os.path.basename(local_p),
                        'path': local_p,
                        'source': 'local'
                    })

        if dropped_items:
            event.acceptProposedAction()
            self.filesDropped.emit(dropped_items, os.path.normpath(target_path))
        else:
            event.ignore()


class FolderTreeWidget(QWidget):
    folderSelected = pyqtSignal(str)  # Emitido com o caminho completo da pasta selecionada
    filesDroppedOnFolder = pyqtSignal(list, str)  # (items, target_folder_path)

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

        # Componente TreeView com suporte a Drop
        self.tree_view = DropEnabledTreeView(self)
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
        self.tree_view.filesDropped.connect(self.filesDroppedOnFolder.emit)
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
