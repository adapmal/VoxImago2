# Painel de detalhes do arquivo exibe informações, miniatura e edição visual de tags com chips interativos.

import os
import sys
import webbrowser
import logging
from datetime import datetime
from PIL import Image

from PyQt6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFormLayout, QScrollArea, QMessageBox, QGroupBox, QLineEdit
)
from PyQt6.QtGui import QFont, QImage, QPainter, QPixmap
from PyQt6.QtCore import Qt, QTimer

from src.utils.utils import format_size
from src.ui.thumbnails import ThumbnailCache, ThumbnailManager
from src.drive.auto_tagger import AutoTagger
from src.ui.staging_queue import StagingQueue, StagingItem
from src.drive.match import select_unique_drive_candidate_by_size
from src.utils.config_manager import ConfigManager
from src.utils.path_validation import validate_path_component
from src.ui.vocab_panel import VocabManager
from src.ui.tag_chips import TagChipsWidget


class FileDetailsPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(360)
        self.auto_tagger = AutoTagger()
        self.staging_queue = StagingQueue()
        self.config_mgr = ConfigManager()

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(10)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)

        self.title_label = QLabel("Detalhes do Arquivo")
        self.title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content_layout.addWidget(self.title_label)

        self.thumbnail_label = QLabel()
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setFixedSize(300, 300)
        self.thumbnail_label.setStyleSheet("background-color: #2D2D2D; border-radius: 6px;")
        self.content_layout.addWidget(self.thumbnail_label, alignment=Qt.AlignmentFlag.AlignCenter)

        # Ações de mídia (Girar e Excluir)
        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(8)

        self.btn_rotate = QPushButton("🔄 Girar 90°")
        self.btn_rotate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_rotate.setStyleSheet("background-color: #17A2B8; color: white; font-weight: bold; padding: 5px;")
        self.btn_rotate.clicked.connect(self._rotate_image_action)
        actions_layout.addWidget(self.btn_rotate)

        self.btn_delete = QPushButton("🗑️ Excluir Arquivo")
        self.btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete.setStyleSheet("background-color: #DC3545; color: white; font-weight: bold; padding: 5px;")
        self.btn_delete.clicked.connect(self._delete_file_action)
        actions_layout.addWidget(self.btn_delete)

        self.content_layout.addLayout(actions_layout)

        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.Shape.HLine)
        self.separator.setFrameShadow(QFrame.Shadow.Sunken)
        self.content_layout.addWidget(self.separator)

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
        
        # Componente de Chips/Pills visuais estilo Gmail + Barra de Ações de Tags
        tags_container_widget = QWidget()
        tags_vbox = QVBoxLayout(tags_container_widget)
        tags_vbox.setContentsMargins(0, 0, 0, 0)
        tags_vbox.setSpacing(3)

        tags_top_bar = QHBoxLayout()
        tags_top_bar.setContentsMargins(0, 0, 0, 0)
        tags_top_bar.addStretch()

        self.btn_toggle_edit = QPushButton("✏️ Editar")
        self.btn_toggle_edit.setToolTip("Alternar entre modo visual (bolinhas) e modo texto puro")
        self.btn_toggle_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_edit.setStyleSheet("""
            QPushButton {
                background-color: #3A3A3A;
                color: #E0E0E0;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 2px 5px;
                font-size: 11px;
                font-family: 'Segoe UI Emoji', Arial, sans-serif;
            }
            QPushButton:hover {
                background-color: #007ACC;
                border-color: #007ACC;
                color: #FFFFFF;
            }
        """)
        self.btn_toggle_edit.clicked.connect(self._toggle_tag_edit_mode)
        tags_top_bar.addWidget(self.btn_toggle_edit)
        tags_vbox.addLayout(tags_top_bar)

        self.tag_chips_widget = TagChipsWidget(all_tags, self)
        self.tag_chips_widget.tagsChanged.connect(self._on_tags_changed)
        tags_vbox.addWidget(self.tag_chips_widget)

        self.form_layout.addRow(QLabel("<b>Tags:</b>"), tags_container_widget)

        self.open_drive_button = QPushButton("☁️ Abrir no Drive")
        self.open_drive_button.setVisible(False)
        self.open_drive_button.clicked.connect(self.open_drive_link)
        self.form_layout.addRow(QLabel(""), self.open_drive_button)

        self.open_folder_button = QPushButton("📂 Abrir Pasta Local")
        self.open_folder_button.setVisible(False)
        self.open_folder_button.clicked.connect(self.open_folder)
        self.form_layout.addRow(QLabel(""), self.open_folder_button)

        self.content_layout.addLayout(self.form_layout)

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

        self.content_layout.addWidget(self.suggestions_group)
        self.content_layout.addStretch()

        self.scroll_area.setWidget(self.content_widget)
        self.main_layout.addWidget(self.scroll_area)

        self.current_file_item = None
        self.current_files_list = []
        self._is_batch_mode = False
        self.parent_app = parent
        self.current_suggestions = []
        self._is_updating = False

        self.config_mgr.modeChanged.connect(lambda k, v: self._apply_permission_mode())
        self.hide()

    def _toggle_tag_edit_mode(self):
        self.tag_chips_widget.toggle_mode()
        if self.tag_chips_widget._is_text_mode:
            self.btn_toggle_edit.setText("🏷️ Bolinhas")
            self.btn_toggle_edit.setToolTip("Voltar para o modo de bolinhas/chips")
        else:
            self.btn_toggle_edit.setText("✏️ Editar")
            self.btn_toggle_edit.setToolTip("Alternar para modo texto puro")

    def _apply_permission_mode(self):
        # No modo Somente Leitura, todas as edições são permitidas na interface
        # para acumular na Fila de Revisão (Staging Queue). Apenas a execução no Drive é bloqueada.
        self.name_edit.setReadOnly(False)
        self.name_edit.setStyleSheet("background-color: #FFFFFF; color: #000000;")
        # A rotacao grava uma preferencia persistente no banco, portanto nao
        # deve aparentar estar disponivel quando nenhuma escrita e permitida.
        self.btn_rotate.setEnabled(not self.config_mgr.is_read_only())
        self.btn_delete.setEnabled(True)
        self.btn_add_all_suggestions.setEnabled(True)
        self.tag_chips_widget.setEnabled(True)
        self.btn_toggle_edit.setEnabled(True)

    def update_details(self, file_item):
        if hasattr(self, 'tag_chips_widget') and self.tag_chips_widget and getattr(self.tag_chips_widget, '_is_text_mode', False):
            self.tag_chips_widget.set_mode('chips')
        self._is_updating = True
        self.current_file_item = file_item
        self.current_files_list = [file_item]
        self._is_batch_mode = False
        self.btn_rotate.setText("🔄 Girar 90°")

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

        # Inspecionar explicitamente a Fila de Staging (com caminhos normalizados)
        raw_fid = file_item.get('file_id') or file_item.get('id') or ''
        raw_path = file_item.get('path', '')
        norm_fid = os.path.normcase(os.path.normpath(raw_fid)) if raw_fid else ''
        norm_path = os.path.normcase(os.path.normpath(raw_path)) if raw_path else ''

        staged_added = []
        staged_removed = []
        orig_db_desc = file_item.get('description', '')

        for it in list(self.staging_queue.items):
            it_fid = os.path.normcase(os.path.normpath(it.file_id)) if it.file_id else ''
            it_path = os.path.normcase(os.path.normpath(it.path)) if it.path else ''
            if (it_fid and (it_fid == norm_fid or it_fid == norm_path)) or (it_path and (it_path == norm_path or it_path == norm_fid)):
                if it.action_type in ('set_description', 'add_tags', 'remove_tags'):
                    old_tags = set(self.tag_chips_widget._parse_tags(it.old_value))
                    new_tags = set(self.tag_chips_widget._parse_tags(it.new_value))
                    staged_added = list(new_tags - old_tags)
                    staged_removed = list(old_tags - new_tags)
                    orig_db_desc = it.old_value
                    break

        # Carregar tags usando intenção explícita (Branco = salvas no banco, Verde = adicionadas na fila, Vermelho = removidas na fila)
        self.tag_chips_widget.set_tags(orig_db_desc, staged_added, staged_removed)
        self._apply_permission_mode()


        self.open_drive_button.setVisible(True)

        if file_item.get('source') == 'local' and file_item.get('path'):
            self.open_folder_button.setVisible(True)
        else:
            self.open_folder_button.setVisible(False)

        self._update_suggested_tags(file_item)
        self._load_thumbnail(file_item)

        # Checar se já está na fila de deleção
        is_deleted = False
        for it in list(self.staging_queue.items):
            it_fid = os.path.normcase(os.path.normpath(it.file_id)) if it.file_id else ''
            it_path = os.path.normcase(os.path.normpath(it.path)) if it.path else ''
            if ((it_fid and (it_fid == norm_fid or it_fid == norm_path)) or (it_path and (it_path == norm_path or it_path == norm_fid))) and it.action_type == 'delete':
                is_deleted = True
                break
        
        self._apply_delete_visual_state(is_deleted)
        if is_deleted:
            self._fade_current_thumbnail()

        self._is_updating = False
        self.show()

    def _get_effective_description(self, file_item):
        """Retorna a descrição atual considerando itens pendentes na fila de staging."""
        if not file_item:
            return ""
        fid = file_item.get('file_id') or file_item.get('id')
        for it in self.staging_queue.items:
            if it.file_id == fid and it.action_type in ('set_description', 'add_tags', 'remove_tags'):
                return it.new_value
        return file_item.get('description', '')

    def update_details_batch(self, files_list):
        if hasattr(self, 'tag_chips_widget') and self.tag_chips_widget and getattr(self.tag_chips_widget, '_is_text_mode', False):
            self.tag_chips_widget.set_mode('chips')
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

        # Calcular interseção de tags originais do banco e tags efetivas de cada arquivo
        common_db = None
        common_eff = None

        for file_item in files_list:
            db_tags = set(self.tag_chips_widget._parse_tags(file_item.get('description', '')))
            eff_tags = set(self.tag_chips_widget._parse_tags(self._get_effective_description(file_item)))

            if common_db is None:
                common_db = db_tags
            else:
                common_db = common_db.intersection(db_tags)

            if common_eff is None:
                common_eff = eff_tags
            else:
                common_eff = common_eff.intersection(eff_tags)

        common_db = common_db or set()
        common_eff = common_eff or set()

        self._batch_initial_common_tags = set(t.lower() for t in common_eff)

        common_db_lower = {t.lower(): t for t in common_db}
        common_eff_lower = {t.lower(): t for t in common_eff}

        staged_added = [t for t in common_eff if t.lower() not in common_db_lower]
        staged_removed = [t for t in common_db if t.lower() not in common_eff_lower]
        orig_desc = ", ".join(sorted(list(common_db)))

        # Renderiza pílulas coloridas em lote: Branco (originais), Verde (adicionadas), Vermelho (removidas)
        self.tag_chips_widget.set_tags(orig_desc, staged_added, staged_removed)
        
        rotatable_count = sum(
            1 for item in files_list
            if item and item.get('source') == 'local'
            and item.get('path') and os.path.isfile(item.get('path'))
        )
        self.btn_rotate.setText(
            f"🔄 Girar seleção 90° ({rotatable_count})"
        )
        self.btn_rotate.setEnabled(
            bool(rotatable_count) and not self.config_mgr.is_read_only()
        )
        self.btn_delete.setEnabled(not self.config_mgr.is_read_only())
        
        self.open_drive_button.setVisible(False)
        self.open_folder_button.setVisible(False)
        
        self._update_suggested_tags_batch(files_list)
        
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

    def _fade_current_thumbnail(self):
        current_pixmap = self.thumbnail_label.pixmap()
        if current_pixmap and not current_pixmap.isNull() and current_pixmap.width() > 0:
            faded = QImage(current_pixmap.size(), QImage.Format.Format_ARGB32_Premultiplied)
            faded.fill(Qt.GlobalColor.transparent)
            painter = QPainter(faded)
            painter.setOpacity(0.4)
            painter.drawPixmap(0, 0, current_pixmap)
            painter.end()
            self.thumbnail_label.setPixmap(QPixmap.fromImage(faded))

    def _load_thumbnail(self, file_item):
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

    def _refresh_suggestions_view(self):
        if getattr(self, '_is_batch_mode', False) and getattr(self, 'current_files_list', None):
            self._update_suggested_tags_batch(self.current_files_list)
        elif self.current_file_item:
            self._update_suggested_tags(self.current_file_item)

    def _update_suggested_tags(self, file_item):
        while self.suggestions_chips_layout.count():
            item = self.suggestions_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not file_item:
            self.suggestions_group.setVisible(False)
            return

        eff_item = dict(file_item)
        eff_item['description'] = self._get_effective_description(file_item)
        all_suggestions = self.auto_tagger.get_all_suggestions(eff_item)
        active_tags_lower = set(t.lower() for t in self.tag_chips_widget.get_active_tags())
        self.current_suggestions = [t for t in all_suggestions if t.lower() not in active_tags_lower]

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

    def _update_suggested_tags_batch(self, files_list):
        while self.suggestions_chips_layout.count():
            item = self.suggestions_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not files_list:
            self.suggestions_group.setVisible(False)
            return

        common_suggestions = None
        for file_item in files_list:
            eff_item = dict(file_item)
            eff_item['description'] = self._get_effective_description(file_item)
            sugs = set(self.auto_tagger.get_all_suggestions(eff_item))
            if common_suggestions is None:
                common_suggestions = sugs
            else:
                common_suggestions = common_suggestions.intersection(sugs)
                if not common_suggestions:
                    break
        
        active_tags_lower = set(t.lower() for t in self.tag_chips_widget.get_active_tags())
        filtered = [t for t in (common_suggestions or set()) if t.lower() not in active_tags_lower]
        self.current_suggestions = sorted(filtered)
        
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
            btn.setToolTip(f"Adicionar a tag '{tag}' em lote")
            btn.clicked.connect(lambda checked, t=tag: self._add_tag_directly(t))
            self.suggestions_chips_layout.addWidget(btn)

        self.suggestions_chips_layout.addStretch()

    def _add_tag_directly(self, tag):
        self.tag_chips_widget.add_tag(tag)
        self._refresh_suggestions_view()

    def _add_all_suggestions_action(self):
        if not self.current_suggestions:
            return
        for tag in list(self.current_suggestions):
            self.tag_chips_widget.add_tag(tag)
        self._refresh_suggestions_view()

    def _on_tags_changed(self, new_desc):
        if self._is_updating:
            return
            
        if getattr(self, '_is_batch_mode', False) and getattr(self, 'current_files_list', None):
            added_tags, removed_tags = self.tag_chips_widget.get_explicit_intent()
            added_set = set(t.lower() for t in added_tags)
            removed_set = set(t.lower() for t in removed_tags)
            current_active_lower = set(t.lower() for t in self.tag_chips_widget.get_active_tags())

            # Tags que estavam presentes em comum no lote e foram removidas (sejam verdes ou brancas)
            initial_common = getattr(self, '_batch_initial_common_tags', set())
            explicitly_removed = (initial_common - current_active_lower).union(removed_set)

            for item in self.current_files_list:
                fid = item.get('file_id') or item.get('id')
                fpath = item.get('path', '')
                fname = item.get('name', 'N/A')
                
                # Buscar a descrição original salva no banco de dados SQLite para comparação
                db_orig_desc = ""
                if hasattr(self.parent_app, 'indexer') and self.parent_app.indexer:
                    self.parent_app.indexer.ensure_conn()
                    self.parent_app.indexer.cursor.execute(
                        "SELECT description FROM files WHERE file_id = ? OR path = ? LIMIT 1",
                        (fid, fpath)
                    )
                    r = self.parent_app.indexer.cursor.fetchone()
                    if r:
                        db_orig_desc = r[0] or ""

                eff_desc = self._get_effective_description(item)
                
                # Partir da descrição EFETIVA do arquivo (preservando edições individuais prévias)
                current_file_tags = self.tag_chips_widget._parse_tags(eff_desc)
                final_tags = []
                for t in current_file_tags:
                    if t.lower() not in explicitly_removed:
                        final_tags.append(t)
                for at in added_tags:
                    if at.lower() not in [ft.lower() for ft in final_tags]:
                        final_tags.append(at)
                
                file_new_desc = ", ".join(final_tags)
                
                for it in list(self.staging_queue.items):
                    if it.file_id == fid and it.action_type in ('set_description', 'add_tags', 'remove_tags'):
                        self.staging_queue.remove_item(it)
                
                if file_new_desc != db_orig_desc:
                    st_item = StagingItem(fid, fname, fpath, 'set_description', old_value=db_orig_desc, new_value=file_new_desc)
                    self.staging_queue.add_item(st_item)
            self._refresh_suggestions_view()
            return
            
        raw_fid = self.current_file_item.get('file_id') or self.current_file_item.get('id') or ''
        raw_path = self.current_file_item.get('path', '')
        norm_fid = os.path.normcase(os.path.normpath(raw_fid)) if raw_fid else ''
        norm_path = os.path.normcase(os.path.normpath(raw_path)) if raw_path else ''
        fname = self.current_file_item.get('name', '')
        
        # Buscar a descrição original salva no banco de dados SQLite para comparação
        old_desc = ""
        if hasattr(self.parent_app, 'indexer') and self.parent_app.indexer:
            self.parent_app.indexer.ensure_conn()
            self.parent_app.indexer.cursor.execute(
                "SELECT description FROM files WHERE file_id = ? OR path = ? LIMIT 1",
                (raw_fid, raw_path)
            )
            r = self.parent_app.indexer.cursor.fetchone()
            if r:
                old_desc = r[0] or ""

        for it in list(self.staging_queue.items):
            it_fid = os.path.normcase(os.path.normpath(it.file_id)) if it.file_id else ''
            it_path = os.path.normcase(os.path.normpath(it.path)) if it.path else ''
            if (it_fid and (it_fid == norm_fid or it_fid == norm_path)) or (it_path and (it_path == norm_path or it_path == norm_fid)):
                if it.action_type in ('set_description', 'add_tags', 'remove_tags'):
                    self.staging_queue.remove_item(it)

        if new_desc != old_desc:
            item = StagingItem(raw_fid or raw_path, fname, raw_path, 'set_description', old_value=old_desc, new_value=new_desc)
            self.staging_queue.add_item(item)
            
        self._refresh_suggestions_view()


    def _on_name_edited(self, new_name):
        if self._is_updating or not self.current_file_item:
            return
        fid = self.current_file_item.get('file_id') or self.current_file_item.get('id')
        fpath = self.current_file_item.get('path', '')
        
        # Buscar o nome original salvo no banco de dados SQLite para comparação
        old_name = ""
        if hasattr(self.parent_app, 'indexer') and self.parent_app.indexer:
            self.parent_app.indexer.ensure_conn()
            self.parent_app.indexer.cursor.execute(
                "SELECT name FROM files WHERE file_id = ? OR path = ? LIMIT 1",
                (fid, fpath)
            )
            r = self.parent_app.indexer.cursor.fetchone()
            if r:
                old_name = r[0] or ""

        for it in list(self.staging_queue.items):
            if it.file_id == fid and it.action_type == 'rename':
                self.staging_queue.remove_item(it)

        candidate = new_name.strip()
        valid, error = validate_path_component(candidate, 'novo nome')
        if not valid:
            self.name_edit.setStyleSheet('border: 1px solid #DC3545;')
            self.name_edit.setToolTip(error)
            return

        self.name_edit.setStyleSheet('')
        self.name_edit.setToolTip('')
        if candidate != old_name:
            item = StagingItem(fid, old_name, fpath, 'rename', old_value=old_name, new_value=candidate)
            self.staging_queue.add_item(item)

    def _rotate_image_action(self):
        """Persiste um override de 90 graus sem alterar o arquivo original."""
        if self.config_mgr.is_read_only():
            return

        files_to_process = (
            list(getattr(self, 'current_files_list', []))
            if getattr(self, '_is_batch_mode', False)
            else [self.current_file_item]
        )
        eligible = [
            item for item in files_to_process
            if item and item.get('source') == 'local'
            and item.get('path') and os.path.isfile(item.get('path'))
        ]
        if not eligible:
            return

        indexer = getattr(self.parent_app, 'indexer', None)
        if not indexer:
            logging.error('Banco de dados indisponivel para rotacionar miniaturas.')
            return

        try:
            rotations = indexer.rotate_thumbnail_rotations([
                (
                    item.get('file_id') or item.get('id') or item.get('path'),
                    item.get('path'),
                )
                for item in eligible
            ])
        except Exception as exc:
            logging.exception('Falha ao persistir rotacao de miniaturas.')
            QMessageBox.warning(
                self,
                'Rotacao nao aplicada',
                f'Nenhuma rotacao do lote foi salva.\n\nDetalhe: {exc}',
            )
            return

        rotations_by_key = {}
        for result in rotations:
            for raw_key in (result.get('file_id'), result.get('path')):
                if raw_key:
                    rotations_by_key[os.path.normcase(
                        os.path.normpath(raw_key)
                    )] = result['thumbnailRotation']

        model = getattr(self.parent_app, 'file_list_model', None)
        rotated_count = 0
        for item_data in eligible:
            keys = [
                item_data.get('file_id') or item_data.get('id'),
                item_data.get('path'),
            ]
            new_rotation = None
            for raw_key in keys:
                if raw_key:
                    key = os.path.normcase(os.path.normpath(raw_key))
                    if key in rotations_by_key:
                        new_rotation = rotations_by_key[key]
                        break
            if new_rotation is None:
                continue

            item_data['thumbnailRotation'] = new_rotation
            rotated_count += 1
            if model and hasattr(model, 'updateFileById'):
                model_id = item_data.get('id') or item_data.get('path')
                if not model.updateFileById(
                    model_id, {'thumbnailRotation': new_rotation}
                ):
                    model.updateFileById(
                        item_data.get('path'),
                        {'thumbnailRotation': new_rotation},
                    )

        if rotated_count > 0:
            # Recarregar a miniatura no painel de detalhes
            if self.current_file_item:
                self._load_thumbnail(self.current_file_item)

            # Notificar a grade principal para redesenhar as miniaturas
            if self.parent_app and hasattr(self.parent_app, 'file_list_view'):
                self.parent_app.file_list_view.viewport().update()

            try:
                self.window().status_bar.showMessage(f"✅ {rotated_count} miniatura(s) de visualização rotacionada(s)!", 3000)
            except Exception:
                pass

    def _apply_delete_visual_state(self, is_deleted):
        if is_deleted:
            self.btn_delete.setText("Desfazer Exclusão")
        else:
            self.btn_delete.setText("🗑️ Excluir Arquivo")

    def _delete_file_action(self):
        files_to_process = getattr(self, 'current_files_list', []) if getattr(self, '_is_batch_mode', False) else [self.current_file_item]
        is_any_added_to_queue = False
        
        for item_data in files_to_process:
            if not item_data: continue
            fid = item_data.get('file_id') or item_data.get('id')
            fname = item_data.get('name', 'N/A')
            fpath = item_data.get('path', '')

            already_marked = False
            for it in list(self.staging_queue.items):
                if it.file_id == fid and it.action_type == 'delete':
                    already_marked = True
                    self.staging_queue.remove_item(it)
            
            if not already_marked:
                for it in list(self.staging_queue.items):
                    if it.file_id == fid:
                        self.staging_queue.remove_item(it)

                item = StagingItem(fid, fname, fpath, 'delete', old_value='', new_value='')
                self.staging_queue.add_item(item)
                is_any_added_to_queue = True
            
        if not getattr(self, '_is_batch_mode', False):
            self._apply_delete_visual_state(is_any_added_to_queue)
            if is_any_added_to_queue:
                self._fade_current_thumbnail()
            else:
                self._load_thumbnail(self.current_file_item)

    def open_drive_link(self):
        if not self.current_file_item:
            return
        src = (self.current_file_item.get('source') or '').lower()
        fpath = self.current_file_item.get('path', '')
        fname = self.current_file_item.get('name', '')
        
        # Para arquivos de origem exclusivamente 'drive' (sem caminho local), abre o link direto
        if src == 'drive':
            link = self.current_file_item.get('webContentLink') or self.current_file_item.get('webViewLink')
            if link:
                webbrowser.open(link)
                return
            fid = self.current_file_item.get('file_id') or self.current_file_item.get('id')
            if fid and not ('/' in fid or '\\' in fid or fid[1:3] == ':\\'):
                webbrowser.open(f"https://drive.google.com/file/d/{fid}/view")
                return

        # 1. Tentar resolver o link exato Top-Down se o serviço do Drive estiver ativo
        win = self.window()
        drive_service = getattr(win, 'service', None) or getattr(win, 'drive_service', None)
        if not drive_service and hasattr(win, 'main_window'):
            drive_service = getattr(win.main_window, 'service', None) or getattr(win.main_window, 'drive_service', None)

        if drive_service and fname:
            try:
                import urllib.parse
                clean_name = fname.replace("'", "\\'")
                file_size = self.current_file_item.get('size')
                
                kwargs = {
                    'includeItemsFromAllDrives': True,
                    'supportsAllDrives': True,
                }
                current_drive = self.config_mgr.get_current_drive_id()
                if current_drive:
                    kwargs['corpora'] = 'drive'
                    kwargs['driveId'] = current_drive
                else:
                    kwargs['corpora'] = 'allDrives'

                matched_file = None

                # 1. Resolução Top-Down exata desde o Ano / Raiz até a pasta do arquivo
                if fpath and current_drive:
                    try:
                        norm = os.path.normpath(fpath).replace('\\', '/')
                        parts = norm.split('/')
                        root_markers = ['banco de imagens', '_testesbanco']
                        start_idx = -1
                        for marker in root_markers:
                            for idx, part in enumerate(parts):
                                if part.lower() == marker:
                                    start_idx = idx + 1
                                    break
                            if start_idx != -1:
                                break
                                
                        if start_idx == -1:
                            for idx, part in enumerate(parts):
                                if part.isdigit() and len(part) == 4:
                                    start_idx = idx
                                    break

                        if start_idx != -1 and start_idx < len(parts):
                            rel_segments = parts[start_idx:]
                            folder_segments = rel_segments[:-1]
                            curr_pid = current_drive
                            path_ok = True
                            for seg in folder_segments:
                                clean_seg = seg.replace("'", "\\'")
                                q_seg = f"name = '{clean_seg}' and '{curr_pid}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                                res_seg = drive_service.files().list(q=q_seg, fields='files(id, name)', **kwargs).execute()
                                seg_folders = res_seg.get('files', [])
                                if len(seg_folders) == 1:
                                    curr_pid = seg_folders[0]['id']
                                else:
                                    path_ok = False
                                    break
                                    
                            if path_ok and curr_pid:
                                q_exact = f"name = '{clean_name}' and '{curr_pid}' in parents and trashed = false"
                                res_exact = drive_service.files().list(q=q_exact, fields='files(id, name, webViewLink, webContentLink, size)', **kwargs).execute()
                                exact_matches = res_exact.get('files', [])
                                matched_file = select_unique_drive_candidate_by_size(
                                    exact_matches, file_size
                                )
                    except Exception as e_top:
                        logging.debug(f"Falha na resolução top-down: {e_top}")

                # 2. Fallback: nome e tamanho exatos em todo o Drive. Todas as
                # paginas sao consideradas para que um homonimo tardio nao seja ignorado.
                if not matched_file:
                    q = f"name='{clean_name}' and trashed=false"
                    candidates = []
                    page_token = None
                    while True:
                        res = drive_service.files().list(
                            q=q,
                            fields='nextPageToken, files(id, name, webViewLink, size, parents)',
                            pageSize=1000,
                            pageToken=page_token,
                            **kwargs,
                        ).execute()
                        candidates.extend(res.get('files', []))
                        page_token = res.get('nextPageToken')
                        if not page_token:
                            break
                    matched_file = select_unique_drive_candidate_by_size(
                        candidates, file_size
                    )

                if matched_file and matched_file.get('webViewLink'):
                    wlink = matched_file['webViewLink']
                    self.current_file_item['webContentLink'] = wlink
                    if hasattr(win, 'indexer') and win.indexer:
                        win.indexer.ensure_conn()
                        win.indexer.cursor.execute(
                            "UPDATE files SET webContentLink = ? "
                            "WHERE source = 'local' AND size = ? "
                            "AND (file_id = ? OR path = ?)",
                            (
                                wlink,
                                int(file_size or 0),
                                self.current_file_item.get('file_id'),
                                fpath,
                            ),
                        )
                        win.indexer.conn.commit()
                    webbrowser.open(wlink)
                    return
            except Exception as e:
                logging.error(f"Erro ao buscar link do Drive: {e}")

        # Links locais legados nao sao mais abertos sem revalidacao: podem ter
        # sido produzidos pelo algoritmo antigo que escolhia o primeiro homonimo.
        fid = self.current_file_item.get('file_id') or self.current_file_item.get('id')
        if fid and not ('/' in fid or '\\' in fid or (len(fid) > 1 and fid[1] == ':')):
            webbrowser.open(f"https://drive.google.com/file/d/{fid}/view")
            return

        # 3. Fallback final: Busca no Google Drive Web pelo nome do arquivo
        import urllib.parse
        search_term = fname.strip()
        search_url = f"https://drive.google.com/drive/search?q={urllib.parse.quote(search_term)}"
        webbrowser.open(search_url)

    def open_folder(self):
        if self.current_file_item and self.current_file_item.get('path'):
            folder = os.path.dirname(self.current_file_item['path'])
            if os.path.exists(folder):
                os.startfile(folder)
