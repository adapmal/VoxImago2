'''
Painel de Vocabulário Controlado Horizontal (Top Collapsible Controlled Vocabulary) para o VoxImago v2.1
Exibe chips/pills organizados em 13 colunas verticais lado a lado, com cabeçalhos fixos, scroll vertical independente por coluna e expansão responsiva.
'''

import os
import csv
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QSizePolicy
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
    tagSelected = pyqtSignal(str)  # Emitido quando uma tag é clicada

    def __init__(self, parent=None):
        super().__init__(parent)
        self.vocab_mgr = VocabManager()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(180)
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
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.columns_container = QWidget()
        self.columns_layout = QHBoxLayout(self.columns_container)
        self.columns_layout.setContentsMargins(4, 4, 4, 4)
        self.columns_layout.setSpacing(10)
        self.columns_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.scroll_area.setWidget(self.columns_container)
        main_layout.addWidget(self.scroll_area)

        self._build_columns()
        self.hide()  # Inicialmente recolhido

    def toggle_visibility(self):
        self.setVisible(not self.isVisible())

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
