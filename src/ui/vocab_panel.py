'''
Painel de Vocabulário Controlado Horizontal (Top Collapsible Controlled Vocabulary) para o VoxImago v2.1
Exibe chips/pills organizados horizontalmente por categoria, com suporte a expansão/recolhimento.
'''

import os
import csv
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
        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 4, 10, 8)
        main_layout.setSpacing(4)

        # Barra superior com título, filtro e botão de fechar
        top_bar = QHBoxLayout()
        title_label = QLabel("🏷️ Vocabulário Controlado Oficial (Tags)")
        title_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #1976D2;")
        top_bar.addWidget(title_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filtrar tags...")
        self.search_input.setFixedWidth(200)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_tags)
        top_bar.addWidget(self.search_input)

        top_bar.addStretch()

        self.btn_close = QPushButton("✕ Recolher")
        self.btn_close.setFixedHeight(26)
        self.btn_close.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.btn_close.clicked.connect(self.hide)
        top_bar.addWidget(self.btn_close)

        main_layout.addLayout(top_bar)

        # Scroll horizontal para as categorias
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFixedHeight(120)
        self.scroll_area.setFrameShape(QFrame.Shape.StyledPanel)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.container_widget = QWidget()
        self.container_layout = QHBoxLayout(self.container_widget)
        self.container_layout.setContentsMargins(4, 4, 4, 4)
        self.container_layout.setSpacing(8)

        self.scroll_area.setWidget(self.container_widget)
        main_layout.addWidget(self.scroll_area)

        self._build_category_chips()
        self.hide()  # Inicialmente recolhido

    def toggle_visibility(self):
        self.setVisible(not self.isVisible())

    def _build_category_chips(self, filter_text=""):
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
            group_box.setStyleSheet(
                "QGroupBox { font-weight: bold; font-size: 11px; margin-top: 4px; padding-top: 10px; } "
                "QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 3px; }"
            )
            group_layout = QVBoxLayout(group_box)
            group_layout.setContentsMargins(4, 8, 4, 4)

            scroll_cat = QScrollArea()
            scroll_cat.setWidgetResizable(True)
            scroll_cat.setFrameShape(QFrame.Shape.NoFrame)
            scroll_cat_widget = QWidget()
            scroll_cat_layout = QVBoxLayout(scroll_cat_widget)
            scroll_cat_layout.setContentsMargins(0, 0, 0, 0)
            scroll_cat_layout.setSpacing(2)

            for tag in matching_tags:
                btn = QPushButton(tag)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton { background-color: #E9ECEF; color: #212529; border: 1px solid #CED4DA; border-radius: 9px; padding: 2px 6px; font-size: 11px; text-align: left; }"
                    "QPushButton:hover { background-color: #007BFF; color: white; border-color: #0056B3; }"
                )
                btn.clicked.connect(lambda checked, t=tag: self.tagSelected.emit(t))
                scroll_cat_layout.addWidget(btn)

            scroll_cat_layout.addStretch()
            scroll_cat.setWidget(scroll_cat_widget)
            group_layout.addWidget(scroll_cat)

            group_box.setFixedWidth(160)
            self.container_layout.addWidget(group_box)

        self.container_layout.addStretch()

    def _filter_tags(self, text):
        self._build_category_chips(text)
