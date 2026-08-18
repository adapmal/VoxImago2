'''
Painel de Vocabulário Controlado Horizontal (Top Collapsible Controlled Vocabulary) para o VoxImago v2.1
Exibe chips/pills organizados em 13 colunas verticais lado a lado, com cabeçalhos fixos, scroll vertical independente por coluna e expansão responsiva.
'''

import os
import csv
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QSizePolicy, QTableWidget, QTableWidgetItem, QMessageBox, QDialog
)
from PyQt6.QtCore import pyqtSignal, Qt
from src.utils.config_manager import ConfigManager

VOCAB_CACHE_FILE = os.path.join('config', 'vocab_cache.csv')


class VocabManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.config_mgr = ConfigManager()
            cls._instance.categories = {}  # { 'Categoria': ['tag1', 'tag2', ...] }
            cls._instance.all_tags = set()
            cls._instance.load_vocabulary()
        return cls._instance

    def __init__(self, *args, **kwargs):
        pass

    def load_vocabulary(self):
        if os.path.exists(VOCAB_CACHE_FILE):
            try:
                with open(VOCAB_CACHE_FILE, 'r', encoding='utf-8-sig', errors='ignore') as f:
                    reader = csv.reader(f)
                    header = None
                    for row in reader:
                        # Pula linhas completamente vazias
                        if any(cell.strip() for cell in row):
                            header = row
                            break
                    
                    if header:
                        clean_headers = [h.strip() for h in header if h.strip()]
                        self.categories = {h: [] for h in clean_headers}

                        for row in reader:
                            for idx, val in enumerate(row):
                                if idx < len(header):
                                    col_name = header[idx].strip()
                                    clean_val = val.strip()
                                    if col_name and clean_val and not clean_val.startswith('-'):
                                        if clean_val not in self.categories[col_name]:
                                            self.categories[col_name].append(clean_val)
                                            self.all_tags.add(clean_val)
            except Exception as e:
                logging.error(f"Erro ao ler CSV de vocabulário controlado: {e}")


    def sync_from_sheets(self):
        url = self.config_mgr.get('sheets_vocab_url')
        if not url: return False
        
        import re
        match = re.search(r'/d/([a-zA-Z0-9-_]+)', url)
        if match:
            sheet_id = match.group(1)
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        else:
            csv_url = url
            
        try:
            import urllib.request
            req = urllib.request.Request(csv_url)
            with urllib.request.urlopen(req) as response:
                data = response.read()
                with open(VOCAB_CACHE_FILE, 'wb') as f:
                    f.write(data)
            self.categories = {}
            self.all_tags = set()
            self.load_vocabulary()
            return True
        except Exception as e:
            logging.error(f"Erro ao sync from sheets: {e}")
            return False

    def get_all_tags(self):
        return sorted(list(self.all_tags))

    def suggest_similar_tags(self, query):
        if not query:
            return []
        q_lower = query.lower()
        return [t for t in self.get_all_tags() if q_lower in t.lower()]


class DetachedVocabDialog(QDialog):
    """Janela flutuante independente para o Vocabulário Controlado."""
    def __init__(self, vocab_panel, parent=None):
        super().__init__(parent)
        self.vocab_panel = vocab_panel
        self.setWindowTitle("🏷️ Vocabulário Controlado Oficial - VoxImago")
        self.setWindowFlags(Qt.WindowType.Window)
        self.resize(1100, 520)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class VocabPanel(QWidget):
    tagSelected = pyqtSignal(str)  # Emitido quando uma tag é clicada
    vocabUpdated = pyqtSignal()   # Emitido quando a planilha é salva ou sincronizada

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self.vocab_mgr = VocabManager()
        self._is_detached = False
        self.detached_dialog = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(200)
        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 4, 10, 6)
        main_layout.setSpacing(4)

        # 1. Barra superior fixa (Título + Filtro + Botão Recolher)
        top_bar = QHBoxLayout()
        title_label = QLabel("🏷️ Vocabulário Controlado Oficial (13 Categorias)")
        title_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #1976D2;")
        top_bar.addWidget(title_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Filtrar tags...")
        self.search_input.setFixedWidth(220)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_tags)
        top_bar.addWidget(self.search_input)

        top_bar.addStretch()

        self.btn_sync = QPushButton("🔄 Atualizar Planilha")
        self.btn_sync.setFixedHeight(26)
        self.btn_sync.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync.setStyleSheet("background-color: #28A745; color: white; border-radius: 4px; padding: 2px 10px; font-weight: bold; font-size: 11px;")
        self.btn_sync.clicked.connect(self._sync_vocab)
        top_bar.addWidget(self.btn_sync)

        self.btn_edit = QPushButton("✏️ Modo de Edição")
        self.btn_edit.setFixedHeight(26)
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_edit.setStyleSheet("background-color: #FFC107; color: black; border-radius: 4px; padding: 2px 10px; font-weight: bold; font-size: 11px;")
        self.btn_edit.clicked.connect(self._toggle_edit_mode)
        top_bar.addWidget(self.btn_edit)

        self.btn_save = QPushButton("💾 Salvar")
        self.btn_save.setFixedHeight(26)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.setStyleSheet("background-color: #007BFF; color: white; border-radius: 4px; padding: 2px 10px; font-weight: bold; font-size: 11px;")
        self.btn_save.clicked.connect(self._save_edit_mode)
        self.btn_save.setVisible(False)
        top_bar.addWidget(self.btn_save)

        self.btn_cancel = QPushButton("❌ Cancelar")
        self.btn_cancel.setFixedHeight(26)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet("background-color: #DC3545; color: white; border-radius: 4px; padding: 2px 10px; font-weight: bold; font-size: 11px;")
        self.btn_cancel.clicked.connect(self._cancel_edit_mode)
        self.btn_cancel.setVisible(False)
        top_bar.addWidget(self.btn_cancel)

        self.btn_detach = QPushButton("🗗 Janela Separada")
        self.btn_detach.setFixedHeight(26)
        self.btn_detach.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_detach.setToolTip("Desacoplar este painel para uma janela flutuante independente que pode ser movida para outra tela")
        self.btn_detach.setStyleSheet(
            "QPushButton { background-color: #6C757D; color: white; border-radius: 4px; padding: 2px 10px; font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { background-color: #5A6268; }"
        )
        self.btn_detach.clicked.connect(self.toggle_detached)
        top_bar.addWidget(self.btn_detach)

        self.btn_close = QPushButton("✕ Recolher")
        self.btn_close.setFixedHeight(26)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setStyleSheet(
            "QPushButton { background-color: #E0E0E0; border: 1px solid #BDBDBD; border-radius: 4px; padding: 2px 10px; font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { background-color: #D32F2F; color: white; border-color: #B71C1C; }"
        )
        self.btn_close.clicked.connect(self.hide)
        top_bar.addWidget(self.btn_close)

        main_layout.addLayout(top_bar)

        # 2. Scroll Horizontal principal contendo as 13 colunas
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.StyledPanel)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.columns_container = QWidget()
        # Removido fixed height interno para permitir que o scroll funcione com as políticas corretas
        self.columns_layout = QHBoxLayout(self.columns_container)
        self.columns_layout.setContentsMargins(4, 4, 4, 4)
        self.columns_layout.setSpacing(10)
        self.columns_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.scroll_area.setWidget(self.columns_container)
        main_layout.addWidget(self.scroll_area)

        self.table_widget = QTableWidget()
        self.table_widget.setVisible(False)
        main_layout.addWidget(self.table_widget)


        self._build_columns()
        self.hide()  # Inicialmente recolhido

    def toggle_visibility(self):
        if getattr(self, '_is_detached', False) and getattr(self, 'detached_dialog', None):
            if self.detached_dialog.isVisible():
                self.detached_dialog.hide()
            else:
                self.detached_dialog.show()
                self.detached_dialog.raise_()
                self.detached_dialog.activateWindow()
        else:
            self.setVisible(not self.isVisible())

    def toggle_detached(self):
        if getattr(self, '_is_detached', False):
            self.attach_to_main_window()
        else:
            self.detach_to_window()

    def detach_to_window(self):
        if getattr(self, '_is_detached', False):
            if hasattr(self, 'detached_dialog') and self.detached_dialog:
                self.detached_dialog.show()
                self.detached_dialog.raise_()
                self.detached_dialog.activateWindow()
            return

        self._is_detached = True
        self.btn_detach.setText("🗖 Acoplar ao App")
        self.btn_detach.setToolTip("Retornar o painel de vocabulário para dentro da janela principal")
        self.btn_close.setVisible(False)

        self.detached_dialog = DetachedVocabDialog(self, parent=self.parent_app)
        dlg_layout = QVBoxLayout(self.detached_dialog)
        dlg_layout.setContentsMargins(4, 4, 4, 4)
        dlg_layout.addWidget(self)

        self.show()
        self.detached_dialog.show()
        self.detached_dialog.raise_()
        self.detached_dialog.activateWindow()

    def attach_to_main_window(self):
        if not getattr(self, '_is_detached', False):
            return

        self._is_detached = False
        self.btn_detach.setText("🗗 Janela Separada")
        self.btn_detach.setToolTip("Desacoplar este painel para uma janela flutuante independente")
        self.btn_close.setVisible(True)

        if hasattr(self, 'detached_dialog') and self.detached_dialog:
            self.detached_dialog.layout().removeWidget(self)
            self.detached_dialog.close()
            self.detached_dialog = None

        if self.parent_app and hasattr(self.parent_app, 'centralWidget') and self.parent_app.centralWidget():
            main_layout = self.parent_app.centralWidget().layout()
            if main_layout:
                main_layout.insertWidget(1, self)
        self.show()

    def _sync_vocab(self):
        self.btn_sync.setText("🔄 Atualizando...")
        self.btn_sync.setEnabled(False)
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        
        success = self.vocab_mgr.sync_from_sheets()
        if success:
            self._build_columns()
            self.vocabUpdated.emit()
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Sucesso", "Vocabulário sincronizado com o Google Sheets!")
        else:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Erro", "Falha ao baixar do Google Sheets.")
        
        self.btn_sync.setText("🔄 Atualizar Planilha")
        self.btn_sync.setEnabled(True)

    def _toggle_edit_mode(self):
        self.scroll_area.setVisible(False)
        self.btn_edit.setVisible(False)
        self.btn_sync.setVisible(False)
        self.btn_save.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.search_input.setVisible(False)
        self.table_widget.setVisible(True)
        self._populate_table()

    def _cancel_edit_mode(self):
        self.table_widget.setVisible(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_edit.setVisible(True)
        self.btn_sync.setVisible(True)
        self.search_input.setVisible(True)
        self.scroll_area.setVisible(True)

    def _populate_table(self):
        categories = list(self.vocab_mgr.categories.keys())
        self.table_widget.setColumnCount(len(categories))
        self.table_widget.setHorizontalHeaderLabels(categories)
        
        max_rows = max([len(v) for v in self.vocab_mgr.categories.values()] + [0])
        # Add 5 empty rows at bottom to allow adding new tags easily
        self.table_widget.setRowCount(max_rows + 5)
        
        for col_idx, cat in enumerate(categories):
            tags = self.vocab_mgr.categories[cat]
            for row_idx in range(self.table_widget.rowCount()):
                val = tags[row_idx] if row_idx < len(tags) else ""
                item = QTableWidgetItem(val)
                self.table_widget.setItem(row_idx, col_idx, item)
        self.table_widget.resizeColumnsToContents()

    def _save_edit_mode(self):
        categories = list(self.vocab_mgr.categories.keys())
        rows = self.table_widget.rowCount()
        
        # Save to local CSV
        import csv
        with open(VOCAB_CACHE_FILE, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(categories)
            for row_idx in range(rows):
                row_data = []
                is_empty = True
                for col_idx in range(len(categories)):
                    item = self.table_widget.item(row_idx, col_idx)
                    val = item.text().strip() if item else ""
                    row_data.append(val)
                    if val: is_empty = False
                if not is_empty:
                    writer.writerow(row_data)
        
        self.vocab_mgr.categories.clear()
        self.vocab_mgr.all_tags.clear()
        self.vocab_mgr.load_vocabulary()
        self._build_columns()
        self._cancel_edit_mode()
        self.vocabUpdated.emit()
        
        # Upload to Google Sheets
        self._sync_up_to_sheets()

    def _sync_up_to_sheets(self):
        from PyQt6.QtWidgets import QMessageBox
        service = None
        if hasattr(self, 'parent_app') and self.parent_app:
            service = getattr(self.parent_app, 'service', getattr(self.parent_app, 'drive_service', None))
        if not service and hasattr(self.window(), 'service'):
            service = getattr(self.window(), 'service', None)
        if not service:
            try:
                from src.authentication import AuthWorker
                service = AuthWorker().refresh_token()
            except Exception:
                pass

        if not service:
            QMessageBox.warning(self, "Aviso", "Não conectado ao Drive. CSV local salvo, mas não enviado ao Sheets.")
            return
            
        try:
            from googleapiclient.http import MediaFileUpload
            media = MediaFileUpload(VOCAB_CACHE_FILE, mimetype='text/csv')
            url = self.vocab_mgr.config_mgr.get('sheets_vocab_url')
            import re
            match = re.search(r'/d/([a-zA-Z0-9-_]+)', url)
            if match:
                sheet_id = match.group(1)
                service.files().update(
                    fileId=sheet_id,
                    media_body=media,
                    supportsAllDrives=True
                ).execute()
                QMessageBox.information(self, "Sucesso", "Planilha na nuvem atualizada com sucesso!")
            else:
                QMessageBox.warning(self, "Aviso", "ID da planilha não encontrado na URL configurada.")
        except Exception as e:
            QMessageBox.warning(self, "Erro", f"Erro ao fazer upload da planilha: {e}")

    def _build_columns(self, filter_text=""):
        # Limpar layout anterior
        while self.columns_layout.count():
            item = self.columns_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        filter_lower = filter_text.lower().strip()

        for cat_name, tags in self.vocab_mgr.categories.items():
            if not tags:
                continue

            matching_tags = [t for t in tags if not filter_lower or filter_lower in t.lower()]
            if not matching_tags:
                continue

            # Coluna de Categoria com cabeçalho fixo no topo e lista rolável
            col_widget = QFrame()
            col_widget.setFrameShape(QFrame.Shape.StyledPanel)
            col_widget.setFixedWidth(170)
            col_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
            col_widget.setStyleSheet(
                "QFrame { background-color: #F8F9FA; border: 1px solid #CED4DA; border-radius: 6px; }"
            )

            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(4, 4, 4, 4)
            col_layout.setSpacing(4)

            # Cabeçalho da Coluna Fixo
            header_label = QLabel(cat_name)
            header_label.setStyleSheet(
                "QLabel { font-weight: bold; font-size: 11px; color: #333333; padding: 4px; background: #E9ECEF; border-radius: 4px; text-align: center; }"
            )
            header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header_label.setWordWrap(True)
            col_layout.addWidget(header_label)

            # Scroll individual para os itens da coluna (com scrollbar vertical funcional)
            tag_scroll = QScrollArea()
            tag_scroll.setWidgetResizable(True)
            tag_scroll.setFrameShape(QFrame.Shape.NoFrame)
            tag_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            tag_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            tag_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

            tag_scroll_content = QWidget()
            tag_layout = QVBoxLayout(tag_scroll_content)
            tag_layout.setContentsMargins(0, 2, 4, 2)
            tag_layout.setSpacing(3)
            tag_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

            for tag in matching_tags:
                btn = QPushButton(tag)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton { background-color: #FFFFFF; color: #212529; border: 1px solid #DEE2E6; border-radius: 4px; padding: 3px 6px; font-size: 11px; text-align: left; }"
                    "QPushButton:hover { background-color: #007BFF; color: white; border-color: #0056B3; font-weight: bold; }"
                )
                btn.clicked.connect(lambda checked, t=tag: self.tagSelected.emit(t))
                tag_layout.addWidget(btn)

            tag_scroll.setWidget(tag_scroll_content)
            col_layout.addWidget(tag_scroll)

            self.columns_layout.addWidget(col_widget)

        self.columns_layout.addStretch()

    def _filter_tags(self, text):
        self._build_columns(text)
