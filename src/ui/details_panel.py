# Painel de detalhes do arquivo exibe informações, miniatura e edição ao vivo de tags.

import os
import sys
import webbrowser
from datetime import datetime
from PyQt6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFormLayout, QScrollArea, QMessageBox, QGroupBox, QLineEdit, QCompleter
)
from PyQt6.QtGui import QFont, QImage, QPainter, QPixmap
from PyQt6.QtCore import Qt
from src.utils.utils import format_size
from src.ui.thumbnails import ThumbnailCache, ThumbnailManager
from src.drive.auto_tagger import AutoTagger
from src.ui.staging_queue import StagingQueue, StagingItem
from src.utils.config_manager import ConfigManager
from src.ui.vocab_panel import VocabManager

class TagsLineEdit(QLineEdit):
    def __init__(self, vocab_list, parent=None):
        super().__init__(parent)
        self.vocab_list = vocab_list
        self.completer = QCompleter(self.vocab_list, self)
        self.completer.setWidget(self)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.activated.connect(self.insertCompletion)

    def insertCompletion(self, completion):
        text = self.text()
        parts = text.split(',')
        if len(parts) > 1:
            parts[-1] = " " + completion
            self.setText(", ".join(parts) + ", ")
        else:
            self.setText(completion + ", ")
        self.textEdited.emit(self.text())

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        text = self.text()
        prefix = text.split(',')[-1].strip()
        self.completer.setCompletionPrefix(prefix)
        if prefix:
            self.completer.complete()
        else:
            self.completer.popup().hide()

class FileDetailsPanel(QFrame):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)

        self.config_mgr = ConfigManager()
        self.auto_tagger = AutoTagger()
        self.staging_queue = StagingQueue()

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_loading = False

        self.content_widget = QWidget()
        self.main_layout = QVBoxLayout(self.content_widget)
        self.main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(8)

        self.title_label = QLabel("Detalhes do Arquivo")
        self.title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_layout.addWidget(self.title_label)

        self.thumbnail_label = QLabel()
        self.thumbnail_label.setFixedSize(300, 300)
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_layout.addWidget(self.thumbnail_label)

        btn_actions_layout = QVBoxLayout()

        self.btn_rotate = QPushButton("🔄 Girar Foto 90°")
        self.btn_rotate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_rotate.setStyleSheet("background-color: #6C757D; color: white; padding: 4px 10px; font-weight: bold;")
        self.btn_rotate.clicked.connect(self._rotate_image_action)
        btn_actions_layout.addWidget(self.btn_rotate)

        self.btn_delete = QPushButton("🗑️ Excluir Arquivo")
        self.btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete.setStyleSheet("background-color: #DC3545; color: white; padding: 4px 10px; font-weight: bold;")
        self.btn_delete.clicked.connect(self._delete_file_action)
        btn_actions_layout.addWidget(self.btn_delete)

        self.main_layout.addLayout(btn_actions_layout)

        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.Shape.HLine)
        self.separator.setFrameShadow(QFrame.Shadow.Sunken)
        self.main_layout.addWidget(self.separator)

        self.form_layout = QFormLayout()
        self.form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Nome do arquivo...")
        self.name_edit.textEdited.connect(self._on_name_edited)
        self.form_layout.addRow(QLabel("<b>Nome:</b>"), self.name_edit)

        self.source_label = QLabel("N/A")
        self.size_label = QLabel("N/A")
        self.path_label = QLabel("N/A")
        self.path_label.setWordWrap(True)
        self.created_label = QLabel("N/A")

        self.form_layout.addRow(QLabel("<b>Fonte:</b>"), self.source_label)
        self.form_layout.addRow(QLabel("<b>Tamanho:</b>"), self.size_label)
        self.form_layout.addRow(QLabel("<b>Caminho:</b>"), self.path_label)
        self.form_layout.addRow(QLabel("<b>Criação:</b>"), self.created_label)

        vocab_mgr = VocabManager()
        all_tags = []
        for tags in vocab_mgr.categories.values():
            all_tags.extend(tags)
        
        self.description_edit = TagsLineEdit(all_tags)
        self.description_edit.setPlaceholderText("Digite as tags separadas por vírgula...")
        self.description_edit.textEdited.connect(self._on_description_changed)
        self.form_layout.addRow(QLabel("<b>Tags/Desc:</b>"), self.description_edit)

        self.diff_label = QLabel()
        self.diff_label.setWordWrap(True)
        self.diff_label.setStyleSheet("padding: 4px; background: #1E1E1E; border-radius: 4px; font-size: 11px;")
        self.diff_label.linkActivated.connect(self._handle_diff_link)
        self.form_layout.addRow(QLabel("<b>Status Fila:</b>"), self.diff_label)

        self.open_drive_button = QPushButton("☁️ Abrir no Drive")
        self.open_drive_button.setVisible(False)
        self.open_drive_button.clicked.connect(self.open_drive_link)
        self.form_layout.addRow(QLabel(""), self.open_drive_button)

        self.open_folder_button = QPushButton("📂 Abrir Pasta Local")
        self.open_folder_button.setVisible(False)
        self.open_folder_button.clicked.connect(self.open_folder)
        self.form_layout.addRow(QLabel(""), self.open_folder_button)

        self.main_layout.addLayout(self.form_layout)

        self.suggestions_group = QGroupBox("💡 Tags Sugeridas (Auto-Tagger)")
        self.suggestions_group.setStyleSheet("QGroupBox { font-weight: bold; margin-top: 10px; }")
        self.suggestions_container_layout = QVBoxLayout(self.suggestions_group)

        self.btn_add_all_suggestions = QPushButton("✨ Acrescentar Todas as Sugeridas")
        self.btn_add_all_suggestions.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_all_suggestions.setStyleSheet(
            "background-color: #28A745; color: white; font-weight: bold; padding: 4px 8px; font-size: 11px; margin-bottom: 4px;"
        )
        self.btn_add_all_suggestions.clicked.connect(self._add_all_suggestions_action)
        self.suggestions_container_layout.addWidget(self.btn_add_all_suggestions)

        self.suggestions_chips_widget = QWidget()
        self.suggestions_chips_layout = QHBoxLayout(self.suggestions_chips_widget)
        self.suggestions_chips_layout.setContentsMargins(0, 0, 0, 0)
        self.suggestions_chips_layout.setSpacing(4)
        self.suggestions_container_layout.addWidget(self.suggestions_chips_widget)

        self.main_layout.addWidget(self.suggestions_group)
        self.main_layout.addStretch()

        self.scroll_area.setWidget(self.content_widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)

        self.current_file_item = None
        self.parent_app = parent
        self.current_suggestions = []
        self._is_updating = False

        self.config_mgr.modeChanged.connect(lambda k, v: self._apply_permission_mode())
        self.staging_queue.queueChanged.connect(lambda c: self._update_diff_preview())

        self.hide()

    def _apply_permission_mode(self):
        is_ro = self.config_mgr.is_read_only()
        self.name_edit.setReadOnly(is_ro)
        self.description_edit.setReadOnly(is_ro)
        self.btn_rotate.setEnabled(not is_ro)
        self.btn_delete.setEnabled(not is_ro)
        self.btn_add_all_suggestions.setEnabled(not is_ro)
        if is_ro:
            self.name_edit.setStyleSheet("background-color: #E9ECEF; color: #6C757D;")
            self.description_edit.setStyleSheet("background-color: #E9ECEF; color: #6C757D;")
        else:
            self.name_edit.setStyleSheet("background-color: #FFFFFF; color: #000000;")
            self.description_edit.setStyleSheet("background-color: #FFFFFF; color: #000000;")

    def update_details(self, file_item):
        self._is_updating = True
        self.current_file_item = file_item
        self.current_files_list = [file_item]
        self._is_batch_mode = False

        self.title_label.setText("Detalhes do Arquivo")
        self.name_edit.setText(file_item.get('name', 'N/A'))
        self.source_label.setText("Google Drive" if file_item.get('source') == 'drive' else "Local")
        self.size_label.setText(format_size(file_item.get('size', 0)) if file_item.get(
            'mimeType') not in ['application/vnd.google-apps.folder', 'folder'] else "N/A")

        raw_path = file_item.get('path', file_item.get('webViewLink', 'N/A'))
        display_path = raw_path.replace('\\', '/') if raw_path and isinstance(raw_path, str) else raw_path
        self.path_label.setText(display_path)

        created_timestamp = file_item.get('createdTime')
        if created_timestamp:
            try:
                if isinstance(created_timestamp, (int, float)):
                    created_date = datetime.fromtimestamp(created_timestamp)
                else:
                    created_date = datetime.fromisoformat(str(created_timestamp).replace('Z', '+00:00'))
                self.created_label.setText(created_date.strftime('%d/%m/%Y'))
            except Exception:
                self.created_label.setText(str(created_timestamp))
        else:
            self.created_label.setText("N/A")

        self.description_edit.setText(file_item.get('description', ''))
        self._apply_permission_mode()

        if file_item.get('source') == 'local' and file_item.get('webContentLink'):
            self.open_drive_button.setVisible(True)
        else:
            self.open_drive_button.setVisible(False)

        if file_item.get('source') == 'local' and file_item.get('path'):
            self.open_folder_button.setVisible(True)
        else:
            self.open_folder_button.setVisible(False)

        self._update_suggested_tags(file_item)
        self._update_diff_preview()

        self.thumbnail_label.setPixmap(ThumbnailManager.get_generic_thumbnail(
            file_item.get('mimeType'), size=(300, 300)))

        local_path = file_item.get('path')
        if local_path and os.path.exists(local_path):
            try:
                cached = None
                if ThumbnailCache.is_thumbnail_cached(file_item):
                    cached = ThumbnailCache.get_existing_thumbnail_cache_path(file_item)
                else:
                    cached = ThumbnailManager.generate_local_thumbnail(file_item, size=(300, 300))
                    
                if cached:
                    pixmap = QPixmap(cached)
                    if not pixmap.isNull():
                        self.thumbnail_label.setPixmap(pixmap.scaled(
                            self.thumbnail_label.size(),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation
                        ))
            except Exception:
                pass

        self._is_updating = False
        self.show()

    def update_details_batch(self, files_list):
        self._is_updating = True
        self.current_file_item = None
        self.current_files_list = files_list
        self._is_batch_mode = True

        self.title_label.setText(f"{len(files_list)} Arquivos Selecionados")
        self.name_edit.setText("Múltiplos Arquivos")
        self.name_edit.setReadOnly(True)
        self.source_label.setText("Variados")
        self.size_label.setText("Vários")
        self.path_label.setText("Múltiplos Caminhos")
        self.created_label.setText("N/A")

        self.description_edit.setText("")
        self.description_edit.setPlaceholderText("Adicione tags em lote...")
        
        self.btn_rotate.setEnabled(not self.config_mgr.is_read_only())
        self.btn_delete.setEnabled(not self.config_mgr.is_read_only())
        
        self.open_drive_button.setVisible(False)
        self.open_folder_button.setVisible(False)
        self.suggestions_group.setVisible(False)
        
        self.diff_label.setText(f"<span style='color: #6C757D;'>Modo Batch Ativo ({len(files_list)} arquivos). Alterações serão aplicadas a todos.</span>")
        
        # Ocultar miniatura no modo lote
        faded = QImage(300, 300, QImage.Format.Format_ARGB32_Premultiplied)
        faded.fill(Qt.GlobalColor.transparent)
        self.thumbnail_label.setPixmap(QPixmap.fromImage(faded))

        self._is_updating = False
        self.show()

    def clear_details(self):
        self._is_updating = True
        self.current_file_item = None
        self.current_files_list = []
        self._is_batch_mode = False
        self.hide()
        self._is_updating = False

    def _update_suggested_tags(self, file_item):
        while self.suggestions_chips_layout.count():
            item = self.suggestions_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.current_suggestions = self.auto_tagger.get_all_suggestions(file_item)
        if not self.current_suggestions:
            self.suggestions_group.setVisible(False)
            return

        self.suggestions_group.setVisible(True)
        for tag in self.current_suggestions[:8]:
            btn = QPushButton(f"➕ {tag}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background-color: #E2E3E5; color: #383D41; border: 1px solid #D6D8DB; border-radius: 9px; padding: 2px 6px; font-size: 11px; }"
                "QPushButton:hover { background-color: #28A745; color: white; border-color: #1E7E34; }"
            )
            btn.setToolTip(f"Adicionar a tag '{tag}'")
            btn.clicked.connect(lambda checked, t=tag: self._add_tag_directly(t))
            self.suggestions_chips_layout.addWidget(btn)

        self.suggestions_chips_layout.addStretch()

    def _add_tag_directly(self, tag):
        if not self.current_file_item or self.config_mgr.is_read_only():
            return
        current_tags = [t.strip() for t in self.description_edit.text().split(',')]
        if tag not in current_tags:
            current_tags.append(tag)
            current_tags = [t for t in current_tags if t]
            new_text = ", ".join(current_tags)
            self.description_edit.setText(new_text)
            self._on_description_changed()

    def _add_all_suggestions_action(self):
        if not self.current_file_item or self.config_mgr.is_read_only() or not self.current_suggestions:
            return
        current_tags = [t.strip() for t in self.description_edit.text().split(',') if t.strip()]
        for tag in self.current_suggestions:
            if tag not in current_tags:
                current_tags.append(tag)
        new_text = ", ".join(current_tags)
        self.description_edit.setText(new_text)
        self._on_description_changed()

    def _on_description_changed(self, text=None):
        if self._is_updating or self.config_mgr.is_read_only():
            return
            
        if getattr(self, '_is_batch_mode', False) and getattr(self, 'current_files_list', None):
            new_desc = self.description_edit.text().strip()
            # Em batch, a edição adiciona as tags para todos
            for item in self.current_files_list:
                fid = item.get('file_id') or item.get('id')
                fpath = item.get('path', '')
                fname = item.get('name', 'N/A')
                old_desc = item.get('description', '')
                
                # Remover itens anteriores de edição deste mesmo arquivo
                for it in list(self.staging_queue.items):
                    if it.file_id == fid and it.action_type in ('set_description', 'add_tags', 'remove_tags'):
                        self.staging_queue.remove_item(it)
                
                # Adicionar na fila
                st_item = StagingItem(fid, fname, fpath, 'add_tags', old_value=old_desc, new_value=new_desc)
                self.staging_queue.add_item(st_item)
            return
            
        if not self.current_file_item:
            return

        new_desc = self.description_edit.text().strip()
        self._stage_description_change(new_desc)

    def _stage_description_change(self, new_desc):
        fid = self.current_file_item.get('file_id') or self.current_file_item.get('id')
        fname = self.current_file_item.get('name', '')
        fpath = self.current_file_item.get('path', '')
        old_desc = self.current_file_item.get('description', '')

        for it in list(self.staging_queue.items):
            if it.file_id == fid and it.action_type in ('set_description', 'add_tags', 'remove_tags'):
                self.staging_queue.remove_item(it)

        if new_desc != old_desc:
            item = StagingItem(fid, fname, fpath, 'set_description', old_value=old_desc, new_value=new_desc)
            self.staging_queue.add_item(item)

        self._update_diff_preview()

    def _on_name_edited(self, new_name):
        if self._is_updating or not self.current_file_item or self.config_mgr.is_read_only():
            return
        fid = self.current_file_item.get('file_id') or self.current_file_item.get('id')
        old_name = self.current_file_item.get('name', '')
        fpath = self.current_file_item.get('path', '')

        for it in list(self.staging_queue.items):
            if it.file_id == fid and it.action_type == 'rename':
                self.staging_queue.remove_item(it)

        if new_name.strip() and new_name.strip() != old_name:
            item = StagingItem(fid, old_name, fpath, 'rename', old_value=old_name, new_value=new_name.strip())
            self.staging_queue.add_item(item)

        self._update_diff_preview()

    def _rotate_image_action(self):
        if self.config_mgr.is_read_only():
            return
            
        files_to_process = getattr(self, 'current_files_list', []) if getattr(self, '_is_batch_mode', False) else [self.current_file_item]
        
        for item_data in files_to_process:
            if not item_data: continue
            fid = item_data.get('file_id') or item_data.get('id')
            fname = item_data.get('name', '')
            fpath = item_data.get('path', '')

            item = StagingItem(fid, fname, fpath, 'rotate_90', old_value='0', new_value='90')
            self.staging_queue.add_item(item)
            
        if not getattr(self, '_is_batch_mode', False):
            self._update_diff_preview()

    def _delete_file_action(self):
        if self._is_updating or self.config_mgr.is_read_only():
            return
            
        files_to_process = getattr(self, 'current_files_list', []) if getattr(self, '_is_batch_mode', False) else [self.current_file_item]

        for item_data in files_to_process:
            if not item_data: continue
            fid = item_data.get('file_id') or item_data.get('id')
            fname = item_data.get('name', 'N/A')
            fpath = item_data.get('path', '')

            # Remover outras ações pendentes
            for it in list(self.staging_queue.items):
                if it.file_id == fid:
                    self.staging_queue.remove_item(it)

            item = StagingItem(fid, fname, fpath, 'delete', old_value='', new_value='')
            self.staging_queue.add_item(item)
            
        if not getattr(self, '_is_batch_mode', False):
            # Esmaecer a miniatura para indicar exclusão visualmente
            current_pixmap = self.thumbnail_label.pixmap()
            if current_pixmap and not current_pixmap.isNull() and current_pixmap.width() > 0:
                faded = QImage(current_pixmap.size(), QImage.Format.Format_ARGB32_Premultiplied)
                faded.fill(Qt.GlobalColor.transparent)
                painter = QPainter(faded)
                painter.setOpacity(0.4)
                painter.drawPixmap(0, 0, current_pixmap)
                painter.end()
                self.thumbnail_label.setPixmap(QPixmap.fromImage(faded))
            self._update_diff_preview()

    def _update_diff_preview(self):
        if getattr(self, '_is_batch_mode', False) and getattr(self, 'current_files_list', None):
            new_desc = self.description_edit.text().strip()
            if new_desc:
                tags = [t.strip() for t in new_desc.split(',') if t.strip()]
                parts = [f"<a href='remove_tag:{t}' style='color: #28A745; font-weight: bold; text-decoration: none;'>+ {t}</a>" for t in tags]
                self.diff_label.setText(f"<b>Tags em Lote:</b> " + ", ".join(parts))
            else:
                self.diff_label.setText(f"<span style='color: #6C757D;'>Modo Batch Ativo ({len(self.current_files_list)} arquivos). Alterações serão aplicadas a todos.</span>")
            return

        if not self.current_file_item:
            self.diff_label.setText("Nenhuma alteração pendente.")
            return

        fid = self.current_file_item.get('file_id') or self.current_file_item.get('id')
        pending = [it for it in self.staging_queue.items if it.file_id == fid]

        if not pending:
            self.diff_label.setText("<span style='color: #6C757D;'>Sem alterações pendentes (Original)</span>")
            return

        lines = []
        for it in pending:
            if it.action_type == 'set_description':
                old_tags = set(t.strip() for t in it.old_value.split(',') if t.strip())
                new_tags = set(t.strip() for t in it.new_value.split(',') if t.strip())
                added = new_tags - old_tags
                removed = old_tags - new_tags

                diff_parts = []
                for t in new_tags:
                    if t in added:
                        diff_parts.append(f"<a href='remove_tag:{t}' style='color: #28A745; font-weight: bold; text-decoration: none;' title='Clique para remover'>+ {t}</a>")
                    else:
                        diff_parts.append(f"<span style='color: #E2E3E5;'>{t}</span>")
                for t in removed:
                    diff_parts.append(f"<span style='color: #DC3545; text-decoration: line-through;'>- {t}</span>")

                lines.append("<b>Tags:</b> " + ", ".join(diff_parts))

            elif it.action_type == 'rename':
                lines.append(f"<b>Renomear:</b> <span style='color: #FFC107;'>{it.new_value}</span>")

            elif it.action_type == 'rotate_90':
                lines.append(f"<b>Girar:</b> <span style='color: #17A2B8;'>90° Horário</span>")

        self.diff_label.setText("<br>".join(lines))

    def open_drive_link(self):
        if self.current_file_item and self.current_file_item.get('webViewLink'):
            webbrowser.open(self.current_file_item['webViewLink'])

    def open_folder(self):
        if self.current_file_item and self.current_file_item.get('path'):
            folder = os.path.dirname(self.current_file_item['path'])
            if os.path.exists(folder):
                os.startfile(folder)

    def _handle_diff_link(self, url):
        if url.startswith("remove_tag:"):
            tag_to_remove = url.split(":", 1)[1]
            current_text = self.description_edit.text()
            tags = [t.strip() for t in current_text.split(',') if t.strip()]
            if tag_to_remove in tags:
                tags.remove(tag_to_remove)
                self.description_edit.setText(", ".join(tags))
                self._on_description_changed()
