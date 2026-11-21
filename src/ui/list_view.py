'''
Visualizador de lista de arquivos personalizado com menu de contexto e funcionalidade de arrastar e soltar.
'''

import os
from PyQt6.QtWidgets import QListView, QMenu, QApplication, QAbstractItemView
from PyQt6.QtCore import Qt, QMimeData, pyqtSignal
from PyQt6.QtGui import QDrag, QCursor
from src.ui.quick_preview import open_quick_preview_dialog


class FileListView(QListView):
    fileSelected = pyqtSignal(object)
    filesSelected = pyqtSignal(list)
    fileDoubleClicked = pyqtSignal(object)
    deleteRequested = pyqtSignal()
    selectFolderRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionRectVisible(False)  # Desativa o quadradinho/rubberband retangular
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.drag_start_position = None
        self._pressed_selected_index = None
        self._anchor_index = None
        self.doubleClicked.connect(self._emit_double_click)

    def setModel(self, model):
        super().setModel(model)
        self._anchor_index = None
        if self.selectionModel():
            self.selectionModel().selectionChanged.connect(self._emit_selection)

    def _is_index_deleted(self, index):
        if not index or not index.isValid():
            return False
        item_data = index.data(Qt.ItemDataRole.UserRole)
        if not item_data:
            return False
        fid = item_data.get('file_id') or item_data.get('id')
        fpath = os.path.normpath(item_data.get('path', '')).lower() if item_data.get('path') else ''
        try:
            from src.ui.staging_queue import StagingQueue
            queue = StagingQueue()
            for it in queue.items:
                if it.action_type == 'delete':
                    if it.file_id == fid or (it.path and os.path.normpath(it.path).lower() == fpath):
                        return True
        except Exception:
            pass
        return False

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_A:
            # Selecionar todos os itens exceto os deletados
            model = self.model()
            if model and self.selectionModel():
                from PyQt6.QtCore import QItemSelection, QItemSelectionModel
                sel = QItemSelection()
                for r in range(model.rowCount()):
                    idx = model.index(r, 0)
                    if not self._is_index_deleted(idx):
                        sel.select(idx, idx)
                self.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            return
        elif event.key() == Qt.Key.Key_Space:
            self.show_quick_preview()
            return
        elif event.key() == Qt.Key.Key_Delete:
            if self.selectedIndexes():
                self.deleteRequested.emit()
            return
        super().keyPressEvent(event)
        # Atualiza a âncora ao navegar com as setas do teclado sem Shift
        if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            cur = self.currentIndex()
            if cur.isValid():
                self._anchor_index = cur

    def _emit_selection(self, selected, deselected):
        indexes = sorted(self.selectedIndexes(), key=lambda idx: idx.row())
        items = []
        for index in indexes:
            item_data = index.data(Qt.ItemDataRole.UserRole)
            if item_data:
                items.append(item_data)
        if items:
            self.fileSelected.emit(items[0])
            self.filesSelected.emit(items)
        else:
            self.filesSelected.emit([])

    def _emit_double_click(self, index):
        file_item = index.data(Qt.ItemDataRole.UserRole)
        if file_item:
            self.fileDoubleClicked.emit(file_item)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.pos())
            if index.isValid():
                file_item = index.data(Qt.ItemDataRole.UserRole)
                if file_item:
                    self.fileDoubleClicked.emit(file_item)
                    return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.pos())
            modifiers = event.modifiers()
            has_shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            has_ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

            # A area cinza funciona como fundo, nao como comando para abandonar
            # o contexto atual. Preservar selecao e painel de detalhes evita
            # perder o fio da meada durante a navegacao.
            if not index.isValid():
                self._pressed_selected_index = None
                self.drag_start_position = None
                event.accept()
                return

            # Se clicou sobre um item já selecionado sem Ctrl/Shift, preserva a seleção para arrastar em lote
            if index in self.selectedIndexes() and not (has_shift or has_ctrl):
                self.drag_start_position = event.pos()
                self._pressed_selected_index = index
                return

            self._pressed_selected_index = None
            self.drag_start_position = event.pos()

            if has_shift:
                # Seleção contínua linear confiável (do primeiro item ao último) pulando itens deletados
                anchor = self._anchor_index if (self._anchor_index is not None and self._anchor_index.isValid()) else self.currentIndex()
                if not anchor or not anchor.isValid():
                    selected = self.selectedIndexes()
                    if selected:
                        anchor = selected[0]
                    else:
                        anchor = index

                start_row = min(anchor.row(), index.row())
                end_row = max(anchor.row(), index.row())

                from PyQt6.QtCore import QItemSelection, QItemSelectionModel
                model = self.model()
                if model and self.selectionModel():
                    sel = QItemSelection()
                    for r in range(start_row, end_row + 1):
                        idx_r = model.index(r, 0)
                        if not self._is_index_deleted(idx_r):
                            sel.select(idx_r, idx_r)
                    if has_ctrl:
                        self.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.Select)
                    else:
                        self.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                    self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                return

            elif has_ctrl:
                # Seleção toggle individual com Ctrl
                if self.selectionModel():
                    from PyQt6.QtCore import QItemSelectionModel
                    self.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Toggle)
                    self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                    self._anchor_index = index
                return

            else:
                # Clique simples sem modificadores: seleciona apenas o item e define nova âncora
                self._anchor_index = index
                if self.selectionModel():
                    from PyQt6.QtCore import QItemSelectionModel
                    self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                return

        # Para outros botões (ex: botão direito), mantém o padrão
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Iniciar Drag & Drop apenas se houver movimento suficiente a partir de um clique em item
        if self.drag_start_position is not None:
            if (event.pos() - self.drag_start_position).manhattanLength() > QApplication.startDragDistance():
                self._pressed_selected_index = None
                self.startDrag(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)
                self.drag_start_position = None
                return
        # NÃO chama super().mouseMoveEvent() quando o botão esquerdo está pressionado para evitar a seleção por polígono/retângulo (rubberband)
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
        else:
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if getattr(self, '_pressed_selected_index', None) is not None:
                idx = self._pressed_selected_index
                self._pressed_selected_index = None
                self.drag_start_position = None
                if idx.isValid() and self.selectionModel():
                    from PyQt6.QtCore import QItemSelectionModel
                    self.selectionModel().setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                    self._anchor_index = idx
            self.drag_start_position = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        if not indexes:
            index = self.indexAt(self.mapFromGlobal(QCursor.pos()))
            if index.isValid():
                self.setCurrentIndex(index)
        # Filtrar itens que estão marcados para exclusão (itens deletados nunca são movidos)
        indexes = [idx for idx in self.selectedIndexes() if not self._is_index_deleted(idx)]
        if not indexes:
            return

        mime_data = QMimeData()
        urls = []
        file_items = []
        for index in indexes:
            file_item = index.data(Qt.ItemDataRole.UserRole)
            if file_item:
                file_items.append(file_item)
                if file_item.get('source') == 'local' and file_item.get('path'):
                    urls.append(QUrl.fromLocalFile(file_item['path']))
                elif file_item.get('source') == 'drive' and file_item.get('webViewLink'):
                    urls.append(QUrl(file_item['webViewLink']))

        mime_data.setUrls(urls)
        try:
            import json
            mime_data.setData("application/x-voximago-file-items", json.dumps(file_items).encode('utf-8'))
        except Exception:
            pass

        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)

    def show_context_menu(self, position):
        clicked_index = self.indexAt(position)
        if clicked_index.isValid() and clicked_index not in self.selectedIndexes():
            if self.selectionModel():
                from PyQt6.QtCore import QItemSelectionModel
                self.selectionModel().setCurrentIndex(clicked_index, QItemSelectionModel.SelectionFlag.ClearAndSelect)

        selected_indexes = self.selectedIndexes()
        selected_items = [idx.data(Qt.ItemDataRole.UserRole) for idx in selected_indexes if idx.isValid() and idx.data(Qt.ItemDataRole.UserRole)]

        if not selected_items:
            return

        clicked_item = clicked_index.data(Qt.ItemDataRole.UserRole) if clicked_index.isValid() else None
        file_item = clicked_item or selected_items[0]
        menu = QMenu()

        batch_edit_action = None
        if len(selected_items) >= 2:
            batch_edit_action = menu.addAction(f"✏️ Editar Tags em Lote ({len(selected_items)} selecionados)...")
            menu.addSeparator()

        select_folder_action = menu.addAction("📂 Selecionar pasta")
        open_action = menu.addAction("Abrir no Explorer")
        copy_pt_action = menu.addAction("Copiar Caminho em Português")
        copy_en_action = menu.addAction("Copiar Caminho em Inglês")

        action = menu.exec(self.viewport().mapToGlobal(position))
        if action is None:
            return
        if batch_edit_action and action == batch_edit_action:
            from src.ui.batch_editor import BatchEditorDialog
            dialog = BatchEditorDialog(selected_items, parent=self)
            dialog.exec()
        elif action == select_folder_action:
            self.selectFolderRequested.emit(file_item)
        elif action == open_action:
            if file_item.get('source') == 'local':
                from src.services.path_reconciler import resolve_and_heal_local_path
                resolved_p = resolve_and_heal_local_path(file_item) or file_item.get('path')
                if resolved_p:
                    folder = os.path.dirname(resolved_p)
                    if os.path.exists(folder):
                        os.startfile(folder)
        elif action == copy_pt_action or action == copy_en_action:
            caminho = file_item.get('path') or file_item.get('webViewLink')
            if caminho:
                if file_item.get('source') == 'local' and file_item.get('path'):
                    caminho = os.path.normpath(caminho)
                if action == copy_pt_action:
                    caminho = caminho.replace(
                        'Shared drives', 'Drives compartilhados')
                elif action == copy_en_action:
                    caminho = caminho.replace(
                        'Drives compartilhados', 'Shared drives')
                QApplication.clipboard().setText(caminho)


    def show_quick_preview(self):
        indexes = self.selectedIndexes()
        if not indexes:
            return

        file_item = indexes[0].data(Qt.ItemDataRole.UserRole)
        if not file_item:
            return

        from src.services.path_reconciler import resolve_and_heal_local_path

        def resolve_item_path(item_dict):
            if not item_dict:
                return None
            p = resolve_and_heal_local_path(item_dict) or item_dict.get('path')
            if p and os.path.exists(p):
                cur_idx = self.selectedIndexes()
                if cur_idx:
                    try:
                        self.model().setData(cur_idx[0], item_dict, Qt.ItemDataRole.UserRole)
                    except Exception:
                        pass
                return p
            return None

        if not resolve_item_path(file_item):
            return

        active_item = [file_item]

        def step_in_list(event, _current_idx):
            QListView.keyPressEvent(self, event)
            new_indexes = self.selectedIndexes()
            if not new_indexes:
                return None
            new_item = new_indexes[0].data(Qt.ItemDataRole.UserRole)
            if new_item and resolve_item_path(new_item):
                active_item[0] = new_item
                # Mantém items sincronizado no índice 0 para o diálogo compartilhado
                items[0] = new_item
                return 0
            return None

        items = [file_item]
        open_quick_preview_dialog(
            self,
            items,
            start_index=0,
            resolve_path_fn=resolve_item_path,
            on_step_fn=step_in_list,
        )
