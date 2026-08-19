'''
Visualizador de lista de arquivos personalizado com menu de contexto e funcionalidade de arrastar e soltar.
'''

import os
from PyQt6.QtWidgets import QListView, QMenu, QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QAbstractItemView
from PyQt6.QtCore import Qt, QMimeData, pyqtSignal, QUrl
from PyQt6.QtGui import QDrag, QCursor, QPixmap, QImage
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PIL import Image
try:
    import rawpy
except ImportError:
    rawpy = None


class FileListView(QListView):
    fileSelected = pyqtSignal(object)
    filesSelected = pyqtSignal(list)
    fileDoubleClicked = pyqtSignal(object)
    deleteRequested = pyqtSignal()

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

            # Clicar em área vazia desmarca todos os itens
            if not index.isValid():
                self._pressed_selected_index = None
                self._anchor_index = None
                self.drag_start_position = None
                self.clearSelection()
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

        file_item = selected_items[0]
        menu = QMenu()

        batch_edit_action = None
        if len(selected_items) >= 2:
            batch_edit_action = menu.addAction(f"✏️ Editar Tags em Lote ({len(selected_items)} selecionados)...")
            menu.addSeparator()

        open_action = menu.addAction("Abrir no Explorer")
        copy_pt_action = menu.addAction("Copiar Caminho em Português")
        copy_en_action = menu.addAction("Copiar Caminho em Inglês")

        action = menu.exec(self.viewport().mapToGlobal(position))
        if batch_edit_action and action == batch_edit_action:
            from src.ui.batch_editor import BatchEditorDialog
            dialog = BatchEditorDialog(selected_items, parent=self)
            dialog.exec()
        elif action == open_action:
            if file_item.get('source') == 'local' and file_item.get('path'):
                folder = os.path.dirname(file_item['path'])
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

        current_player = [None]
        dialog = QDialog(self)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        dialog.setLayout(layout)
        dialog.setModal(True)

        def close_on_key(event):
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
                dialog.close()
            elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                if current_player[0]:
                    player = current_player[0]
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                        delta = 10000  # 10s
                    else:
                        delta = 2000   # 2s
                    
                    if event.key() == Qt.Key.Key_Left:
                        new_pos = max(0, player.position() - delta)
                    else:
                        new_pos = min(player.duration(), player.position() + delta)
                    
                    player.setPosition(new_pos)
                    if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState and new_pos < player.duration():
                        player.play()
            elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                # Propagate key event to list view to change selected item
                QListView.keyPressEvent(self, event)
                
                # Load the newly selected item
                new_indexes = self.selectedIndexes()
                if new_indexes:
                    new_item = new_indexes[0].data(Qt.ItemDataRole.UserRole)
                    new_path = new_item.get('path')
                    if new_path and os.path.exists(new_path):
                        load_file(new_path)
            else:
                QDialog.keyPressEvent(dialog, event)

        dialog.keyPressEvent = close_on_key

        def load_file(file_path):
            if current_player[0]:
                try:
                    current_player[0].stop()
                    current_player[0].setSource(QUrl())
                    current_player[0] = None
                except Exception:
                    pass

            def clear_layout(lay):
                while lay.count():
                    child = lay.takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
                    elif child.layout():
                        clear_layout(child.layout())
                        child.layout().deleteLater()

            clear_layout(layout)

            dialog.setWindowTitle(f"Pré-visualização - {os.path.basename(file_path)}")

            ext = os.path.splitext(file_path)[1].lower()
            video_exts = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v'}
            raw_exts = {'.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2',
                        '.pef', '.srw', '.raf', '.raw'}
            heic_exts = {'.heic', '.heif'}

            def format_time(ms):
                if ms < 0:
                    ms = 0
                s = ms // 1000
                m = s // 60
                s = s % 60
                h = m // 60
                m = m % 60
                if h > 0:
                    return f"{h:02d}:{m:02d}:{s:02d}"
                return f"{m:02d}:{s:02d}"

            if ext in video_exts:
                try:
                    video_widget = QVideoWidget(dialog)
                    video_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    video_widget.keyPressEvent = close_on_key
                    layout.addWidget(video_widget, 1)

                    controls_layout = QHBoxLayout()

                    slider = QSlider(Qt.Orientation.Horizontal, dialog)
                    slider.setRange(0, 0)
                    slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    slider.keyPressEvent = close_on_key
                    controls_layout.addWidget(slider)

                    time_label = QLabel("00:00 / 00:00", dialog)
                    time_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    time_label.setStyleSheet("QLabel { font-family: 'Courier New', monospace; font-size: 11px; min-width: 80px; border: none; background: transparent; }")
                    time_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
                    controls_layout.addWidget(time_label)

                    layout.addLayout(controls_layout, 0)

                    player = QMediaPlayer(dialog)
                    audio = QAudioOutput(dialog)
                    player.setAudioOutput(audio)
                    player.setVideoOutput(video_widget)
                    player.setSource(QUrl.fromLocalFile(file_path))

                    def update_time_label():
                        current = format_time(player.position())
                        total = format_time(player.duration())
                        time_label.setText(f"{current} / {total}")

                    def on_position_changed(position):
                        if not slider.isSliderDown():
                            slider.setValue(position)
                        update_time_label()
                        duration = player.duration()
                        if duration > 0 and position >= duration:
                            player.pause()

                    def on_duration_changed(duration):
                        slider.setRange(0, duration)
                        update_time_label()

                    def on_media_status_changed(status):
                        if status == QMediaPlayer.MediaStatus.EndOfMedia:
                            player.pause()
                            player.setPosition(player.duration())

                    player.positionChanged.connect(on_position_changed)
                    player.durationChanged.connect(on_duration_changed)
                    player.mediaStatusChanged.connect(on_media_status_changed)

                    slider.sliderMoved.connect(player.setPosition)
                    
                    def on_slider_value_changed(val):
                        if slider.isSliderDown():
                            player.setPosition(val)
                    slider.valueChanged.connect(on_slider_value_changed)

                    def on_slider_released():
                        if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState and player.position() < player.duration():
                            player.play()
                    slider.sliderReleased.connect(on_slider_released)

                    player.play()
                    current_player[0] = player
                    dialog.resize(900, 540)
                except Exception as e:
                    label = QLabel(f"Erro ao tentar exibir vídeo: {e}", dialog)
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    layout.addWidget(label)
            elif ext in raw_exts:
                try:
                    image = None
                    if rawpy:
                        try:
                            with rawpy.imread(file_path) as raw:
                                rgb = raw.postprocess()
                                image = Image.fromarray(rgb)
                        except Exception as raw_e:
                            # Fallback para Pillow caso rawpy falhe
                            pass
                    if image is None:
                        image = Image.open(file_path)
                    image = image.convert("RGB")
                    image.thumbnail((800, 600))
                    data = image.tobytes("raw", "RGB")
                    bytes_per_line = image.width * 3
                    qimage = QImage(data, image.width, image.height, bytes_per_line,
                                    QImage.Format.Format_RGB888).copy()
                    pixmap = QPixmap.fromImage(qimage)
                    label = QLabel(dialog)
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    label.setPixmap(pixmap)
                    layout.addWidget(label)
                except Exception as e:
                    label = QLabel(f"Não foi possível exibir RAW: {e}", dialog)
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    layout.addWidget(label)
                dialog.resize(820, 620)
            elif ext in heic_exts:
                try:
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                    image = Image.open(file_path)
                    image = image.convert("RGB")
                    image.thumbnail((800, 600))
                    data = image.tobytes("raw", "RGB")
                    bytes_per_line = image.width * 3
                    qimage = QImage(data, image.width, image.height, bytes_per_line,
                                    QImage.Format.Format_RGB888).copy()
                    pixmap = QPixmap.fromImage(qimage)
                    label = QLabel(dialog)
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    label.setPixmap(pixmap)
                    layout.addWidget(label)
                except Exception as e:
                    label = QLabel(f"Não foi possível exibir HEIC: {e}", dialog)
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    layout.addWidget(label)
                dialog.resize(820, 620)
            else:
                label = QLabel(dialog)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pixmap = QPixmap(file_path)
                if not pixmap.isNull():
                    label.setPixmap(pixmap.scaled(
                        800, 600, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                else:
                    label.setText("Não foi possível carregar a imagem.")
                layout.addWidget(label)
                dialog.resize(820, 620)

        # Load first item
        file_item = indexes[0].data(Qt.ItemDataRole.UserRole)
        file_path = file_item.get('path')
        if file_path and os.path.exists(file_path):
            load_file(file_path)
        else:
            return

        def stop_player():
            if current_player[0]:
                try:
                    current_player[0].stop()
                    current_player[0].setSource(QUrl())
                except Exception:
                    pass
        dialog.finished.connect(stop_player)

        dialog.exec()
