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
    """QFileSystemModel otimizado com cache instantâneo de subpastas do banco de dados (0ms)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._known_parent_folders = set()
        self._load_parents_from_db()

    def _load_parents_from_db(self):
        try:
            import sqlite3
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            db_path = os.path.join(base_dir, 'data', 'file_index.db')
            if not os.path.exists(db_path):
                db_path = os.path.abspath('data/file_index.db')
            if os.path.exists(db_path):
                con = sqlite3.connect(db_path)
                q = "SELECT DISTINCT parentId FROM files WHERE mimeType IN ('folder', 'application/vnd.google-apps.folder', 'directory')"
                self._known_parent_folders = {
                    os.path.normcase(os.path.normpath(r[0]))
                    for r in con.execute(q) if r[0]
                }
                con.close()
        except Exception:
            pass

    def hasChildren(self, parent=QModelIndex()):
        if not parent.isValid():
            return super().hasChildren(parent)
        path = self.filePath(parent)
        if not path:
            return False
        norm_p = os.path.normcase(os.path.normpath(path))
        if norm_p in self._known_parent_folders:
            return True
        return self.rowCount(parent) > 0

    def add_known_parent(self, path):
        if path:
            self._known_parent_folders.add(os.path.normcase(os.path.normpath(path)))

    def clear_subdirs_cache(self):
        self._load_parents_from_db()


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
        header_layout.setSpacing(4)

        # Botão de alternância intuitivo: Modo Pasta x Todo o Acervo
        self.btn_filter_toggle = QPushButton("🌐 Todo o Acervo")
        self.btn_filter_toggle.setCheckable(True)
        self.btn_filter_toggle.setChecked(False)
        self.btn_filter_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_filter_toggle.setToolTip("Modo de busca global em Todo o Acervo ativo.\nClique em uma pasta da árvore para filtrar, ou clique aqui para desvincular.")
        self.btn_filter_toggle.setStyleSheet(
            "QPushButton { background-color: #F8F9FA; color: #495057; border: 1px solid #CED4DA; border-radius: 4px; padding: 4px 8px; font-size: 11px; font-weight: bold; text-align: left; }"
            "QPushButton:hover { background-color: #E9ECEF; color: #212529; }"
            "QPushButton:checked { background-color: #007BFF; color: white; border-color: #0056B3; }"
        )
        self.btn_filter_toggle.clicked.connect(self._on_filter_toggle_clicked)
        header_layout.addWidget(self.btn_filter_toggle, 1)

        self.btn_new_folder = QPushButton("➕ Nova")
        self.btn_new_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_folder.setToolTip("Criar nova subpasta dentro da pasta selecionada")
        self.btn_new_folder.setStyleSheet(
            "QPushButton { background-color: #E9ECEF; color: #212529; border: 1px solid #CED4DA; border-radius: 4px; padding: 4px 7px; font-size: 11px; font-weight: bold; }"
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

    def _copy_path_to_clipboard(self, target_path):
        from PyQt6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        if clipboard and target_path:
            clipboard.setText(target_path)
            win = self.window()
            if hasattr(win, 'status_bar') and win.status_bar:
                win.status_bar.showMessage(f"📋 Caminho copiado: {target_path}", 3500)

    def _show_tree_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        target_path = self.model.filePath(index) if index.isValid() else self.root_dir

        menu = QMenu(self)
        action_copy_path = menu.addAction("📋 Copiar Caminho")
        action_open_explorer = menu.addAction("📂 Abrir no Explorer")
        menu.addSeparator()
        action_new = menu.addAction("📁 Criar Nova Pasta Aqui...")
        menu.addSeparator()
        action_refresh = menu.addAction("🔄 Recarregar Árvore")

        action_copy_path.triggered.connect(lambda: self._copy_path_to_clipboard(target_path))
        action_open_explorer.triggered.connect(lambda: os.startfile(target_path) if os.path.exists(target_path) else None)
        action_new.triggered.connect(lambda: self.create_new_folder(target_path))
        action_refresh.triggered.connect(self._refresh_tree)

        menu.addSeparator()
        action_sync_drive = menu.addAction("☁️ Sincronizar Tags desta Pasta com o Google Drive")
        action_sync_drive.triggered.connect(lambda: self.sync_folder_tags_with_drive(target_path))

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def sync_folder_tags_with_drive(self, folder_path):
        if not folder_path or not os.path.exists(folder_path):
            return

        win = self.window()
        service = getattr(win, 'service', None)
        if not service:
            QMessageBox.warning(self, "Google Drive Desconectado", "Conecte-se ao Google Drive para sincronizar as tags desta pasta.")
            return

        if hasattr(win, 'status_bar') and win.status_bar:
            win.status_bar.showMessage(f"⏳ Sincronizando tags de '{os.path.basename(folder_path)}' com o Google Drive...", 0)

        from src.utils.utils import resolve_drive_folder_id_by_path
        target_folder_id = resolve_drive_folder_id_by_path(service, folder_path)

        if not target_folder_id:
            QMessageBox.warning(self, "Pasta Não Encontrada no Drive", f"Não foi possível localizar a pasta '{os.path.basename(folder_path)}' correspondente no Google Drive.")
            if hasattr(win, 'status_bar') and win.status_bar:
                win.status_bar.clearMessage()
            return

        try:
            drive_id = self.config_mgr.get_current_drive_id()
            kwargs = {'supportsAllDrives': True, 'includeItemsFromAllDrives': True}
            if drive_id:
                kwargs['corpora'] = 'drive'
                kwargs['driveId'] = drive_id

            page_token = None
            drive_files = []
            while True:
                res = service.files().list(
                    q=f"'{target_folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                    fields='nextPageToken, files(id, name, description, webViewLink)',
                    pageSize=1000, pageToken=page_token, **kwargs
                ).execute()
                drive_files.extend(res.get('files', []))
                page_token = res.get('nextPageToken')
                if not page_token:
                    break

            indexer = getattr(win, 'indexer', None)
            if indexer:
                indexer.ensure_conn()
                from src.database.search import SearchEngine
                norm_engine = SearchEngine(None)

                for df in drive_files:
                    fname = df['name']
                    desc = (df.get('description') or '').strip()
                    norm_desc = norm_engine.normalize_text(desc)
                    link = df.get('webViewLink') or f"https://drive.google.com/file/d/{df['id']}/view?usp=drivesdk"
                    local_p = os.path.normpath(os.path.join(folder_path, fname)).lower()

                    indexer.cursor.execute(
                        "UPDATE files SET description = ?, webContentLink = ? WHERE LOWER(path) = ? OR LOWER(file_id) = ?",
                        (desc, link, local_p, local_p)
                    )
                    indexer.cursor.execute(
                        "UPDATE search_index SET description = ?, normalized_description = ? WHERE LOWER(file_id) = ?",
                        (desc, norm_desc, local_p)
                    )
                indexer.conn.commit()
                indexer.export_to_shared_cache()

            if hasattr(win, '_force_refresh_after_sync'):
                win._force_refresh_after_sync()
            else:
                if hasattr(win, 'search_engine') and win.search_engine:
                    win.search_engine.clear_cache()
                self.folderSelected.emit(folder_path)
                if hasattr(win, 'current_page'):
                    win.current_page = 0
                    win.all_files_loaded = False
                    from src.ui.list_update import list_update
                    list_update.clear_display(win)
                    list_update.load_next_batch(win)

            if hasattr(win, 'status_bar') and win.status_bar:
                win.status_bar.showMessage(f"✅ {len(drive_files)} arquivos da pasta '{os.path.basename(folder_path)}' sincronizados com o Drive!", 4000)

        except Exception as e:
            QMessageBox.critical(self, "Erro na Sincronização", f"Ocorreu um erro ao sincronizar com o Drive:\n{e}")

    def _refresh_tree(self):
        self.model.clear_subdirs_cache()
        self.model.setRootPath(self.root_dir)
        self.tree_view.setRootIndex(self.model.index(self.root_dir))

    def create_new_folder(self, target_parent_path=None):
        if self.config_mgr.is_read_only():
            QMessageBox.warning(self, "Modo Somente Leitura", "O aplicativo está em Modo Somente Leitura. Desative o modo somente leitura para criar pastas.")
            return

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

        from src.ui.staging_queue import StagingQueue, StagingItem
        queue = StagingQueue()
        for it in queue.items:
            if it.action_type == 'create_folder' and os.path.normcase(os.path.normpath(it.new_value)) == os.path.normcase(new_path):
                QMessageBox.information(self, "Pasta em Fila", f"A pasta '{folder_name}' já está agendada na Fila de Revisão.")
                return

        # 1. Cria a pasta localmente para que o QFileSystemModel a exiba na árvore e receba drag-and-drop
        try:
            os.makedirs(new_path, exist_ok=True)
        except Exception as e_mk:
            QMessageBox.critical(self, "Erro ao Criar Pasta", f"Não foi possível preparar a pasta local:\n{e_mk}")
            return

        # 2. Agenda a criação oficial / sincronização na nuvem na Fila de Revisão
        st_item = StagingItem(new_path, folder_name, new_path, 'create_folder', old_value="", new_value=new_path)
        queue.add_item(st_item)
        
        # 3. Registrar como pasta conhecida e atualizar visualização
        self.model.add_known_parent(target_parent_path)
        self.model.clear_subdirs_cache()
        parent_idx = self.model.index(target_parent_path)
        if parent_idx.isValid():
            self.tree_view.expand(parent_idx)

        new_idx = self.model.index(new_path)
        if new_idx.isValid():
            self.tree_view.setCurrentIndex(new_idx)
            self.folderSelected.emit(new_path)
        
        win = self.window()
        if hasattr(win, 'status_bar') and win.status_bar:
            win.status_bar.showMessage(f"🟢 Nova pasta '{folder_name}' agendada na Fila (com badge verde)!", 4000)
        self.tree_view.viewport().update()

    def _on_filter_toggle_clicked(self):
        if not self.btn_filter_toggle.isChecked():
            # Desvincular pasta -> Modo 1: Todo o Acervo
            self.clear_folder_selection()
        else:
            # Se foi marcado, verifica se há uma pasta selecionada
            indexes = self.tree_view.selectedIndexes()
            if indexes and self.model.isDir(indexes[0]):
                folder_p = self.model.filePath(indexes[0])
                self._update_header_for_folder(folder_p)
                self.folderSelected.emit(folder_p)
            else:
                self.btn_filter_toggle.setChecked(False)
                self.btn_filter_toggle.setText("🌐 Todo o Acervo")

    def _update_header_for_folder(self, folder_path):
        if folder_path and os.path.exists(folder_path):
            folder_name = os.path.basename(folder_path) or folder_path
            self.btn_filter_toggle.blockSignals(True)
            self.btn_filter_toggle.setChecked(True)
            self.btn_filter_toggle.setText(f"📁 {folder_name}  ✕")
            self.btn_filter_toggle.setToolTip(f"Filtrando por: {folder_path}\nClique para desvincular e buscar em Todo o Acervo.")
            self.btn_filter_toggle.blockSignals(False)
        else:
            self.btn_filter_toggle.blockSignals(True)
            self.btn_filter_toggle.setChecked(False)
            self.btn_filter_toggle.setText("🌐 Todo o Acervo")
            self.btn_filter_toggle.setToolTip("Modo de busca global em Todo o Acervo ativo.\nClique em uma pasta da árvore para filtrar, ou clique aqui para desvincular.")
            self.btn_filter_toggle.blockSignals(False)

    def clear_folder_selection(self):
        self.tree_view.selectionModel().clearSelection()
        self._update_header_for_folder(None)
        self.folderSelected.emit("")

    def _on_selection_changed(self, selected, deselected):
        indexes = self.tree_view.selectedIndexes()
        if indexes and self.model.isDir(indexes[0]):
            folder_path = self.model.filePath(indexes[0])
            if folder_path:
                self._update_header_for_folder(folder_path)
                self.folderSelected.emit(folder_path)
        else:
            self._update_header_for_folder(None)
            self.folderSelected.emit("")

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
            self.clear_folder_selection()

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
            self._update_header_for_folder(norm_p)
            self.folderSelected.emit(norm_p)
