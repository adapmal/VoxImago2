'''
Painel de Vocabulário Controlado (Controlled Vocabulary Panel) para o VoxImago v2.1
Carrega e categoriza as tags das 13 colunas da planilha do Google Sheets.
Exibe chips/pills clicáveis por categoria para busca e etiquetagem didática.
'''

import os
import csv
import urllib.request
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QGroupBox
)
from PyQt6.QtCore import pyqtSignal, Qt
from src.utils.config_manager import ConfigManager

VOCAB_CACHE_FILE = os.path.join('config', 'vocab_cache.csv')


class VocabManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VocabManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self.config_mgr = ConfigManager()
            self.categories = {}  # { 'Categoria': ['tag1', 'tag2', ...] }
            self.all_tags = set()
            self.load_vocabulary()

    def load_vocabulary(self):
        # 1. Tentar baixar a versão mais recente da planilha online
        url = self.config_mgr.get('sheets_vocab_url')
        downloaded = False
        if url:
            try:
                os.makedirs(os.path.dirname(VOCAB_CACHE_FILE), exist_ok=True)
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response, open(VOCAB_CACHE_FILE, 'wb') as out_file:
                    out_file.write(response.read())
                downloaded = True
            except Exception as e:
                logging.warning(f"Não foi possível baixar vocabulário online, usando cache local: {e}")

        # 2. Ler do cache CSV local
        if os.path.exists(VOCAB_CACHE_FILE):
            try:
                with open(VOCAB_CACHE_FILE, 'r', encoding='utf-8-sig', errors='ignore') as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
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

    def get_all_tags(self):
        return sorted(list(self.all_tags))

    def suggest_similar_tags(self, query):
        if not query:
            return []
        q_lower = query.lower()
        return [t for t in self.get_all_tags() if q_lower in t.lower()]


class VocabPanel(QWidget):
    tagSelected = pyqtSignal(str)  # Emitido quando um chip/pill de tag é clicado

    def __init__(self, parent=None):
        super().__init__(parent)
        self.vocab_mgr = VocabManager()
        self.setMaximumWidth(320)

        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)

        title_label = QLabel("🏷️ Vocabulário Controlado")
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        main_layout.addWidget(title_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filtrar tags por nome...")
        self.search_input.textChanged.connect(self._filter_tags)
        main_layout.addWidget(self.search_input)

        # Scroll Area para conter as categorias de chips
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.container_widget = QWidget()
        self.container_layout = QVBoxLayout(self.container_widget)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.container_layout.setSpacing(8)

        self.scroll_area.setWidget(self.container_widget)
        main_layout.addWidget(self.scroll_area)

        self._build_category_chips()

    def _build_category_chips(self, filter_text=""):
        # Limpar widgets anteriores
        while self.container_layout.count():
            item = self.container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        filter_lower = filter_text.lower().strip()

        for cat_name, tags in self.vocab_mgr.categories.items():
            if not tags:
                continue

            matching_tags = [t for t in tags if not filter_lower or filter_lower in t.lower()]
            if not matching_tags:
                continue

            group_box = QGroupBox(cat_name)
            group_box.setStyleSheet("QGroupBox { font-weight: bold; font-size: 12px; margin-top: 6px; } QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 3px; }")
            group_layout = QVBoxLayout(group_box)
            group_layout.setContentsMargins(6, 12, 6, 6)

            # Container flexível para os chips
            chips_widget = QWidget()
            chips_layout = QHBoxLayout(chips_widget)
            chips_layout.setContentsMargins(0, 0, 0, 0)
            chips_layout.setSpacing(4)

            # Usar layout em embrulho se disponível, senão organizar em fluxo
            chip_container_layout = QVBoxLayout()
            row_layout = QHBoxLayout()
            row_layout.setSpacing(4)
            row_length = 0

            for tag in matching_tags:
                btn = QPushButton(tag)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton { background-color: #E9ECEF; color: #212529; border: 1px solid #CED4DA; border-radius: 12px; padding: 3px 8px; font-size: 11px; }"
                    "QPushButton:hover { background-color: #007BFF; color: white; border-color: #0056B3; }"
                )
                btn.clicked.connect(lambda checked, t=tag: self.tagSelected.emit(t))

                row_layout.addWidget(btn)
                row_length += len(tag) + 4
                if row_length > 30:
                    row_layout.addStretch()
                    chip_container_layout.addLayout(row_layout)
                    row_layout = QHBoxLayout()
                    row_layout.setSpacing(4)
                    row_length = 0

            if row_layout.count() > 0:
                row_layout.addStretch()
                chip_container_layout.addLayout(row_layout)

            group_layout.addLayout(chip_container_layout)
            self.container_layout.addWidget(group_box)

        self.container_layout.addStretch()

    def _filter_tags(self, text):
        self._build_category_chips(text)
