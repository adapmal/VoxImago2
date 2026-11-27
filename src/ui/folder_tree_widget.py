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
    QMessageBox, QMenu, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem
)
from PyQt6.QtGui import QFileSystemModel, QDragEnterEvent, QDragMoveEvent, QDropEvent, QCursor, QColor, QPen, QFont
from PyQt6.QtCore import pyqtSignal, Qt, QDir, QUrl, QModelIndex, QTimer
from src.utils.config_manager import ConfigManager
from src.utils.path_validation import validate_path_component


class FolderFileSystemModel(QFileSystemModel):
    """QFileSystemModel otimizado com detecção recursiva instantânea de subpastas do banco de dados e do disco local."""

    def __init__(self, parent=None, root_dir=None):
        super().__init__(parent)
        self._known_parent_folders = set()
        self._load_parents_from_db()
        if root_dir:
            self._load_parents_from_disk(root_dir)

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
                for r in con.execute(q):
                    if r[0]:
                        self._known_parent_folders.add(os.path.normcase(os.path.normpath(r[0])))
                con.close()
        except Exception:
            pass

    def _load_parents_from_disk(self, root_dir=None):
        """Varre os diretórios em disco (~0.1s) e registra todas as pastas que possuem subpastas em qualquer nível."""
        if not root_dir or not os.path.exists(root_dir):
            return
        try:
            for root, dirs, _ in os.walk(root_dir):
                if dirs:
                    self._known_parent_folders.add(os.path.normcase(os.path.normpath(root)))
        except Exception:
            pass

    def refresh_subdirs_cache(self, root_dir=None):
        """Recarrega os pais conhecidos a partir do DB e do disco local."""
        self._known_parent_folders.clear()
        self._load_parents_from_db()
        if root_dir:
            self._load_parents_from_disk(root_dir)

    def clear_subdirs_cache(self, root_dir=None):
        self.refresh_subdirs_cache(root_dir)

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
            # O Explorer não deve interpretar o drop como movimento já realizado.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.filesDropped.emit(dropped_items, os.path.normpath(target_path))
        else:
            event.ignore()


class PendingFolderTree(QTreeWidget):
    """Destinos virtuais: drop apenas emite intenção, nunca toca no disco."""
    filesDropped = pyqtSignal(list, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabel('Pastas pendentes — ainda não criadas')
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setMaximumHeight(200)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-voximago-file-items') or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.itemAt(event.position().toPoint()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        if not target:
            event.ignore()
            return
        records = []
        try:
            if event.mimeData().hasFormat('application/x-voximago-file-items'):
                records = json.loads(bytes(event.mimeData().data('application/x-voximago-file-items')).decode('utf-8'))
            else:
                records = [dict(path=u.toLocalFile(), file_id=u.toLocalFile(), name=os.path.basename(u.toLocalFile()), source='local')
                           for u in event.mimeData().urls() if u.isLocalFile()]
            if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
                raise ValueError('Drop inválido')
        except (ValueError, UnicodeError):
            event.ignore()
            return
        self.filesDropped.emit(records, target.data(0, Qt.ItemDataRole.UserRole))
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()


class FolderTreeWidget(QWidget):
    folderSelected = pyqtSignal(str)  # Emitido com o caminho completo da pasta selecionada
    filesDroppedOnFolder = pyqtSignal(list, str)  # (items, target_folder_path)
    compareFolderHashRequested = pyqtSignal(str)  # Abrir comparador de hash com esta pasta incluída

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

        resolved = self.config_mgr.get_resolved_scan_paths(persist=True)
        if resolved:
            return resolved[0]

        return os.path.dirname(self.config_mgr.get_sandbox_path())

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
        self.model = FolderFileSystemModel(self, root_dir=self.root_dir)
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

        self.pending_tree = PendingFolderTree(self)
        self.pending_tree.filesDropped.connect(self.filesDroppedOnFolder.emit)
        self.pending_tree.itemClicked.connect(self._select_pending_folder)
        self.pending_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pending_tree.customContextMenuRequested.connect(self._pending_context_menu)
        layout.addWidget(self.pending_tree)
        from src.ui.staging_queue import StagingQueue
        StagingQueue().queueChanged.connect(self._refresh_pending_folders)
        self._refresh_pending_folders()

        # Atualizar visualização quando o modo Sandbox alternar
        self.config_mgr.modeChanged.connect(self._on_mode_changed)

    def _refresh_pending_folders(self, *_):
        from src.ui.staging_queue import StagingQueue
        self.pending_tree.clear()
        self._pending_paths = set()
        for item in StagingQueue().items:
            if item.action_type != 'create_folder' or item.execution_state not in ('pending', 'blocked'):
                continue
            path = item.new_value or item.path
            if not path:
                continue
            self._pending_paths.add(os.path.normcase(os.path.normpath(path)))
            node = QTreeWidgetItem([path])
            node.setData(0, Qt.ItemDataRole.UserRole, path)
            node.setToolTip(0, 'Pasta virtual. Só será criada após aprovar a execução na Fila.')
            self.pending_tree.addTopLevelItem(node)
        self.pending_tree.setVisible(bool(self._pending_paths))

    def _select_pending_folder(self, node, column=0):
        self.tree_view.selectionModel().blockSignals(True)
        self.tree_view.clearSelection()
        self.tree_view.selectionModel().blockSignals(False)
        self.folderSelected.emit(node.data(0, Qt.ItemDataRole.UserRole))

    def _pending_context_menu(self, position):
        node = self.pending_tree.itemAt(position)
        if node:
            menu = QMenu(self)
            action = menu.addAction('Agendar subpasta aqui…')
            action.triggered.connect(lambda: self.create_new_folder(node.data(0, Qt.ItemDataRole.UserRole)))
            menu.exec(self.pending_tree.viewport().mapToGlobal(position))

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
        action_refresh.triggered.connect(lambda: self._refresh_tree(target_path))

        menu.addSeparator()
        action_sync_drive = menu.addAction("☁️ Sincronizar Tags desta Pasta com o Google Drive")
        action_sync_drive.triggered.connect(lambda: self.sync_folder_tags_with_drive(target_path))

        action_compare_hash = menu.addAction("🧹 Comparar Pasta por Hash com Outras...")
        action_compare_hash.triggered.connect(
            lambda: self.compareFolderHashRequested.emit(os.path.normpath(target_path))
        )

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

        maintenance_acquired = False
        if hasattr(win, 'begin_maintenance_operation'):
            if not win.begin_maintenance_operation():
                if hasattr(win, 'status_bar') and win.status_bar:
                    win.status_bar.clearMessage()
                return
            maintenance_acquired = True

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
                    fields='nextPageToken, files(id, name, description, webViewLink, size)',
                    pageSize=1000, pageToken=page_token, **kwargs
                ).execute()
                drive_files.extend(res.get('files', []))
                page_token = res.get('nextPageToken')
                if not page_token:
                    break

            indexer = getattr(win, 'indexer', None)
            snapshot_exported = True
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
                    drive_size = int(df.get('size') or 0)
                    if drive_size <= 0:
                        continue

                    indexer.cursor.execute(
                        "UPDATE files SET description = ?, webContentLink = ? "
                        "WHERE source = 'local' AND size = ? "
                        "AND (LOWER(path) = ? OR LOWER(file_id) = ?)",
                        (desc, link, drive_size, local_p, local_p)
                    )
                    if indexer.cursor.rowcount == 1:
                        indexer.cursor.execute(
                            "UPDATE search_index SET description = ?, normalized_description = ? WHERE LOWER(file_id) = ?",
                            (desc, norm_desc, local_p)
                        )
                indexer.conn.commit()

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
        finally:
            if maintenance_acquired and hasattr(win, 'end_maintenance_operation'):
                win.end_maintenance_operation()

    def _refresh_tree(self, target_to_expand=None, show_status=True):
        """
        Recarrega a árvore de diretórios descobrindo todas as subpastas em profundidade (recursivo).
        Preserva as pastas que já estavam expandidas e a seleção atual do usuário.
        """
        # 1. Salvar caminhos de pastas atualmente expandidas e a seleção atual
        expanded_paths = []
        for p in list(self.model._known_parent_folders):
            idx = self.model.index(p)
            if idx.isValid() and self.tree_view.isExpanded(idx):
                expanded_paths.append(p)

        selected_path = None
        indexes = self.tree_view.selectedIndexes()
        if indexes and self.model.isDir(indexes[0]):
            selected_path = self.model.filePath(indexes[0])

        # 2. Recarregar o cache com DB + varredura rápida de diretórios no disco
        self.model.refresh_subdirs_cache(self.root_dir)

        # 3. Forçar o QFileSystemModel a invalidar seu cache interno e reler do disco
        self.model.setRootPath("")
        self.model.setRootPath(self.root_dir)
        self.tree_view.setRootIndex(self.model.index(self.root_dir))

        # 4. Restaurar expansões anteriores
        for p in sorted(expanded_paths, key=len):
            idx = self.model.index(p)
            if idx.isValid():
                self.tree_view.expand(idx)

        # 5. Se o usuário clicou em uma pasta específica para recarregar ou tinha uma selecionada, expandir e selecionar
        expand_target = target_to_expand if (target_to_expand and target_to_expand != self.root_dir) else selected_path
        if expand_target and os.path.exists(expand_target):
            self.select_and_expand_folder(expand_target)

        # 6. Sincronizar on-the-fly a pasta alvo no SQLite se aplicável
        win = self.window()
        if hasattr(win, 'indexer') and win.indexer and expand_target and os.path.isdir(expand_target):
            try:
                from src.ui.list_update import list_update
                list_update._sync_single_folder_on_the_fly(win, expand_target)
            except Exception:
                pass

        if show_status and hasattr(win, 'status_bar') and win.status_bar:
            win.status_bar.showMessage("🔄 Árvore de pastas recarregada com sucesso!", 3500)

    def create_new_folder(self, target_parent_path=None):
        from src.ui.staging_queue import StagingQueue, StagingItem
        queue = StagingQueue()
        if not queue.can_edit():
            return
        if self.config_mgr.is_read_only():
            QMessageBox.warning(self, "Modo Somente Leitura", "O aplicativo está em Modo Somente Leitura. Desative o modo somente leitura para criar pastas.")
            return

        if not target_parent_path:
            selected_virtual = self.pending_tree.selectedItems()
            if selected_virtual and not self.tree_view.selectedIndexes():
                target_parent_path = selected_virtual[0].data(0, Qt.ItemDataRole.UserRole)
        if not target_parent_path:
            indexes = self.tree_view.selectedIndexes()
            if indexes:
                target_parent_path = self.model.filePath(indexes[0])
            else:
                target_parent_path = self.root_dir

        if not target_parent_path or (not os.path.exists(target_parent_path)
                and os.path.normcase(os.path.normpath(target_parent_path)) not in self._pending_paths):
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
        valid, error = validate_path_component(folder_name, 'nome da pasta')
        if not valid:
            QMessageBox.warning(
                self, "Nome Inválido",
                error
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

        # Intenção somente. A seção virtual aceita drops sem criar um placeholder.
        st_item = StagingItem(new_path, folder_name, new_path, 'create_folder', old_value="", new_value=new_path)
        if not queue.add_item(st_item):
            return
        self._refresh_pending_folders()
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
        if getattr(self, '_suppress_selection_signal', False):
            return
        indexes = self.tree_view.selectedIndexes()
        if indexes and self.model.isDir(indexes[0]):
            self.pending_tree.clearSelection()
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
            self.model.refresh_subdirs_cache(self.root_dir)
            self.model.setRootPath("")
            self.model.setRootPath(self.root_dir)
            self.tree_view.setRootIndex(self.model.index(self.root_dir))
            self.clear_folder_selection()

    def select_and_expand_folder(self, folder_path):
        """Expande os níveis intermediários e seleciona a pasta alvo na árvore."""
        for index in range(self.pending_tree.topLevelItemCount()):
            node = self.pending_tree.topLevelItem(index)
            if os.path.normcase(node.data(0, Qt.ItemDataRole.UserRole)) == os.path.normcase(folder_path or ''):
                self.pending_tree.setCurrentItem(node)
                self._select_pending_folder(node)
                return True
        if not folder_path or not os.path.exists(folder_path):
            return False

        norm_p = os.path.normpath(folder_path)
        root_norm = os.path.normpath(self.root_dir)
        root_cf = os.path.normcase(root_norm)
        norm_cf = os.path.normcase(norm_p)

        if norm_cf == root_cf:
            self.clear_folder_selection()
            return True

        if not norm_cf.startswith(root_cf + os.sep):
            return False

        # Montar a cadeia de pastas pai estritamente entre a raiz e o destino
        ancestors = []
        curr = os.path.dirname(norm_p)
        while curr and os.path.normcase(curr) != root_cf and os.path.dirname(curr) != curr:
            ancestors.append(curr)
            curr = os.path.dirname(curr)

        # Expandir de cima para baixo
        for p in reversed(ancestors):
            idx = self.model.index(p)
            if idx.isValid():
                self.tree_view.expand(idx)

        target_idx = self.model.index(norm_p)
        if not target_idx.isValid():
            return False

        self._suppress_selection_signal = True
        try:
            self.tree_view.setCurrentIndex(target_idx)
        finally:
            self._suppress_selection_signal = False

        display_path = os.path.normpath(self.model.filePath(target_idx) or norm_p)
        self.tree_view.scrollTo(target_idx, QAbstractItemView.ScrollHint.PositionAtCenter)
        QTimer.singleShot(
            120,
            lambda idx=target_idx: (
                self.tree_view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)
                if idx.isValid() and self.tree_view.currentIndex() == idx
                else None
            ),
        )
        self._update_header_for_folder(display_path)
        self.folderSelected.emit(display_path)
        return True
