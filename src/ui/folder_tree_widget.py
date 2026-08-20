'''
Navegador de Árvore de Diretórios (Folder Tree Widget) para o VoxImago v2.1
Exibe a estrutura vertical de 4 Níveis (Ano > Categoria > País > Evento/Casa)
e permite filtrar o grid com um clique ou arrastar e soltar imagens para mover.
'''

import os
import json
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeView,
    QHeaderView, QAbstractItemView, QPushButton, QInputDialog,
    QMessageBox, QMenu, QStyledItemDelegate
)
from PyQt6.QtGui import QFileSystemModel, QDragEnterEvent, QDragMoveEvent, QDropEvent, QCursor, QColor, QPen, QFont
from PyQt6.QtCore import pyqtSignal, Qt, QDir, QUrl, QModelIndex
from src.utils.config_manager import ConfigManager


class FolderFileSystemModel(QFileSystemModel):
    """QFileSystemModel otimizado e nativo para navegação ultra-rápida de diretórios."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._subdirs_cache = {}

    def clear_subdirs_cache(self):
        self._subdirs_cache.clear()


class FolderTreeDelegate(QStyledItemDelegate):
    def __init__(self, tree_view, parent=None):
        super().__init__(parent)
        self.tree_view = tree_view

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        model = self.tree_view.model()
        if model and hasattr(model, 'filePath'):
            folder_path = os.path.normcase(os.path.normpath(model.filePath(index)))
            from src.ui.staging_queue import StagingQueue
            queue = StagingQueue()
            is_staged_folder = False
            for it in queue.items:
                if it.action_type == 'create_folder' and os.path.normcase(os.path.normpath(it.new_value)) == folder_path:
                    is_staged_folder = True
                    break
                elif it.action_type == 'move' and os.path.normcase(os.path.normpath(it.new_value)) == folder_path:
                    is_staged_folder = True
                    break

            if is_staged_folder:
                painter.save()
                pen = QPen(QColor("#28A745"), 2)
                painter.setPen(pen)
                rect = option.rect.adjusted(1, 1, -1, -1)
                painter.drawRect(rect)
                painter.setPen(QColor("#28A745"))
                font = QFont("Arial", 8, QFont.Weight.Bold)
                painter.setFont(font)
                painter.drawText(rect.right() - 42, rect.top() + 14, "🟢 Fila")
                painter.restore()

        if index == getattr(self.tree_view, '_current_drag_target_index', None):
            painter.save()
            pen = QPen(QColor("#007BFF"), 2)
            painter.setPen(pen)
            rect = option.rect.adjusted(1, 1, -1, -1)
            painter.drawRect(rect)
            painter.restore()



class DropEnabledTreeView(QTreeView):
    """QTreeView customizado que aceita drops de arquivos arrastados da grade sem trocar a pasta ativa."""
    
    filesDropped = pyqtSignal(list, str)  # (list_of_items_or_paths, target_folder_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self._current_drag_target_index = None
        self.setItemDelegate(FolderTreeDelegate(self, self))

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-voximago-file-items"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        index = self.indexAt(event.position().toPoint())
        prev_target = self._current_drag_target_index
        
        target_valid = False
        if index.isValid():
            model = self.model()
            if isinstance(model, QFileSystemModel):
                path = model.filePath(index)
                if os.path.isdir(path):
                    target_valid = True
                    self._current_drag_target_index = index
                    event.acceptProposedAction()
                    
        if not target_valid:
            self._current_drag_target_index = None
            event.ignore()

        if prev_target != self._current_drag_target_index:
            self.viewport().update()

    def dragLeaveEvent(self, event):
        self._current_drag_target_index = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self._current_drag_target_index = None
        self.viewport().update()
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
        self.setMaximumWidth(340)

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
        self.title_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        header_layout.addWidget(self.title_label)

        header_layout.addStretch()

        self.btn_new_folder = QPushButton("➕ Nova")
        self.btn_new_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_folder.setToolTip("Criar nova subpasta dentro da pasta selecionada")
        self.btn_new_folder.setStyleSheet(
            "QPushButton { background-color: #E9ECEF; color: #212529; border: 1px solid #CED4DA; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #007BFF; color: white; border-color: #0056B3; }"
        )
        self.btn_new_folder.clicked.connect(lambda: self.create_new_folder())
        header_layout.addWidget(self.btn_new_folder)

        layout.addLayout(header_layout)

        # Modelo do Sistema de Arquivos (com detecção real de subpastas para triângulos)
        self.model = FolderFileSystemModel(self)
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

        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._show_tree_context_menu)

        self.tree_view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.tree_view.filesDropped.connect(self.filesDroppedOnFolder.emit)
        layout.addWidget(self.tree_view)

        # Atualizar visualização quando o modo Sandbox alternar
        self.config_mgr.modeChanged.connect(self._on_mode_changed)

    def _show_tree_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        target_path = self.model.filePath(index) if index.isValid() else self.root_dir

        menu = QMenu(self)
        action_new = menu.addAction("📁 Criar Nova Pasta Aqui...")
        action_open_explorer = menu.addAction("📂 Abrir no Explorer")
        menu.addSeparator()
        action_refresh = menu.addAction("🔄 Recarregar Árvore")

        action_new.triggered.connect(lambda: self.create_new_folder(target_path))
        action_open_explorer.triggered.connect(lambda: os.startfile(target_path) if os.path.exists(target_path) else None)
        action_refresh.triggered.connect(self._refresh_tree)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _refresh_tree(self):
        self.model.clear_subdirs_cache()
        self.model.setRootPath(self.root_dir)
        self.tree_view.setRootIndex(self.model.index(self.root_dir))

    def create_new_folder(self, target_parent_path=None):
        if not target_parent_path:
            indexes = self.tree_view.selectedIndexes()
            if indexes:
                target_parent_path = self.model.filePath(indexes[0])
            else:
                target_parent_path = self.root_dir

        if not target_parent_path or not os.path.exists(target_parent_path):
            target_parent_path = self.root_dir

        parent_name = os.path.basename(target_parent_path) or target_parent_path

        folder_name, ok = QInputDialog.getText(
            self,
            "Criar Nova Pasta",
            f"Criar nova pasta dentro de:\n📂 {parent_name}\n\nNome da pasta:",
        )
        if not ok or not folder_name.strip():
            return

        folder_name = folder_name.strip()
        invalid_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
        if any(c in folder_name for c in invalid_chars):
            QMessageBox.warning(
                self, "Nome Inválido",
                "O nome da pasta não pode conter os seguintes caracteres:\n\\ / : * ? \" < > |\n"
            )
            return

        new_path = os.path.normpath(os.path.join(target_parent_path, folder_name))
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Pasta Já Existe", f"A pasta '{folder_name}' já existe neste local.")
            return

        if self.config_mgr.is_read_only():
            from src.ui.staging_queue import StagingQueue, StagingItem
            queue = StagingQueue()
            st_item = StagingItem(new_path, folder_name, new_path, 'create_folder', old_value="", new_value=new_path)
            queue.add_item(st_item)
            win = self.window()
            if hasattr(win, 'status_bar') and win.status_bar:
                win.status_bar.showMessage(f"🟢 Nova pasta '{folder_name}' agendada na Fila (Modo Somente Leitura)!", 4000)
            self.tree_view.viewport().update()
            return


        try:
            os.makedirs(new_path, exist_ok=True)

            # Registrar a pasta no banco de dados SQLite local
            win = self.window()
            if hasattr(win, 'indexer') and win.indexer:
                try:
                    import time
                    now = int(time.time())
                    win.indexer.ensure_conn()
                    win.indexer.cursor.execute(
                        "INSERT OR REPLACE INTO files (file_id, name, path, mimeType, source, description, parentId, createdTime, modifiedTime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (new_path, folder_name, new_path, 'folder', 'local', '', target_parent_path, now, now)
                    )
                    win.indexer.conn.commit()
                except Exception as e_db:
                    pass

            # Limpar cache e expandir pasta pai
            self.model.clear_subdirs_cache()
            parent_idx = self.model.index(target_parent_path)
            if parent_idx.isValid():
                self.tree_view.expand(parent_idx)

            # Selecionar a nova pasta criada
            new_idx = self.model.index(new_path)
            if new_idx.isValid():
                self.tree_view.setCurrentIndex(new_idx)
                self.folderSelected.emit(new_path)

            if hasattr(win, 'status_bar') and win.status_bar:
                win.status_bar.showMessage(f"✅ Pasta '{folder_name}' criada com sucesso!", 4000)

        except Exception as err:
            QMessageBox.critical(self, "Erro ao Criar Pasta", f"Não foi possível criar a pasta:\n{err}")

    def _on_selection_changed(self, selected, deselected):
        indexes = self.tree_view.selectedIndexes()
        if indexes and self.model.isDir(indexes[0]):
            folder_path = self.model.filePath(indexes[0])
            if folder_path:
                self.folderSelected.emit(folder_path)

    def _on_mode_changed(self, key, value):
        if key in ('sandbox_mode', 'sandbox_path'):
            new_root = self._determine_root_dir()
            self.set_root_directory(new_root)

    def set_root_directory(self, path):
        if path and os.path.exists(path):
            self.root_dir = path
            self.model.clear_subdirs_cache()
            self.model.setRootPath(self.root_dir)
            self.tree_view.setRootIndex(self.model.index(self.root_dir))

    def select_and_expand_folder(self, folder_path):
        """Expande os níveis intermediários e seleciona a pasta alvo na árvore."""
        if not folder_path or not os.path.exists(folder_path):
            return

        norm_p = os.path.normpath(folder_path)
        root_norm = os.path.normpath(self.root_dir)

        # Montar a cadeia de pastas pai a partir da raiz até o destino
        parents = []
        curr = norm_p
        while curr and curr.lower() != root_norm.lower() and os.path.dirname(curr) != curr:
            parents.append(curr)
            curr = os.path.dirname(curr)

        # Expandir de cima para baixo
        for p in reversed(parents):
            idx = self.model.index(p)
            if idx.isValid():
                self.tree_view.expand(idx)

        target_idx = self.model.index(norm_p)
        if target_idx.isValid():
            self.tree_view.setCurrentIndex(target_idx)
            self.tree_view.scrollTo(target_idx, QAbstractItemView.ScrollHint.PositionAtCenter)
            self.folderSelected.emit(norm_p)
