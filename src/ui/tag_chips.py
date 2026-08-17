"""
Componente de Chips de Tags interativo (estilo Gmail) para o VoxImago.MB
Permite alternar entre visualização em chips coloridos e modo de edição de texto puro (✏️).
Totalmente adaptado ao tema escuro e sem contornos pretos residuais.
"""

from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QHBoxLayout, QVBoxLayout,
    QLineEdit, QPlainTextEdit, QCompleter, QSizePolicy, QLayout,
    QPushButton, QStackedWidget
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QRect, QSize


class FlowLayout(QLayout):
    """Layout fluido que posiciona widgets quebrando linha automaticamente."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self.item_list = []

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item):
        self.item_list.append(item)

    def count(self):
        return len(self.item_list)

    def itemAt(self, index):
        if 0 <= index < len(self.item_list):
            return self.item_list[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self.item_list):
            return self.item_list.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self.item_list:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self.spacing()

        for item in self.item_list:
            space_x = spacing
            space_y = spacing
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y()


class TagChip(QFrame):
    """
    Pílula / Chip individual sem botões extras e sem subcontornos pretos ou quadrados.
    Estados:
      - 'original': Tag já salva no arquivo (fundo Branco, texto Preto)
      - 'added': Tag recém-adicionada (fundo Verde, texto Branco)
      - 'removed': Tag original marcada para exclusão (fundo Vermelho, texto Branco riscado)
    """

    chipClicked = pyqtSignal(object)
    chipEditRequested = pyqtSignal(object)

    def __init__(self, text, state='original', parent=None):
        super().__init__(parent)
        self.setObjectName("TagChip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.text_value = text.strip()
        self.state = state
        self._init_ui()

    def _init_ui(self):
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 3, 9, 3)
        layout.setSpacing(0)

        self.label = QLabel(self.text_value)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.label)

        self._update_style()

    def set_state(self, state):
        self.state = state
        self._update_style()

    def _update_style(self):
        if self.state == 'added':
            self.setStyleSheet("""
                QFrame#TagChip {
                    background-color: #28A745;
                    border: none;
                    border-radius: 11px;
                }
            """)
            self.label.setStyleSheet("background: transparent; border: none; font-size: 12px; font-weight: bold; color: #FFFFFF;")
        elif self.state == 'removed':
            self.setStyleSheet("""
                QFrame#TagChip {
                    background-color: #DC3545;
                    border: none;
                    border-radius: 11px;
                }
            """)
            self.label.setStyleSheet("background: transparent; border: none; font-size: 12px; color: #FFFFFF; text-decoration: line-through;")
        else:
            self.setStyleSheet("""
                QFrame#TagChip {
                    background-color: #FFFFFF;
                    border: none;
                    border-radius: 11px;
                }
                QFrame#TagChip:hover {
                    background-color: #E2E3E5;
                }
            """)
            self.label.setStyleSheet("background: transparent; border: none; font-size: 12px; font-weight: 500; color: #000000;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.chipClicked.emit(self)
        elif event.button() == Qt.MouseButton.RightButton:
            self.chipEditRequested.emit(self)
        else:
            super().mousePressEvent(event)


class ChipInputField(QLineEdit):
    """Campo de texto embutido no final dos chips para digitação rápida."""

    tagCommitted = pyqtSignal(str)
    backspacePressedOnEmpty = pyqtSignal()

    def __init__(self, vocab_suggestions=None, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Adicionar tag...")
        self.setStyleSheet("""
            QLineEdit {
                border: none;
                background: transparent;
                color: #FFFFFF;
                font-size: 12px;
                padding: 3px 6px;
                min-width: 90px;
            }
        """)

        if vocab_suggestions:
            self.update_suggestions(vocab_suggestions)

    def update_suggestions(self, vocab_suggestions):
        if vocab_suggestions:
            completer = QCompleter(vocab_suggestions, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self.setCompleter(completer)
            completer.activated.connect(self._on_completer_activated)

    def _on_completer_activated(self, text):
        if text.strip():
            self.tagCommitted.emit(text.strip())
            self.clear()
            if self.completer() and self.completer().popup():
                self.completer().popup().hide()

    def keyPressEvent(self, event):
        key = event.key()
        text = self.text().strip()

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Comma):
            if self.completer() and self.completer().popup() and self.completer().popup().isVisible():
                popup = self.completer().popup()
                current_idx = popup.currentIndex()
                if current_idx.isValid():
                    chosen = current_idx.data()
                    if chosen:
                        text = chosen.strip()
                popup.hide()

            if text:
                self.tagCommitted.emit(text)
                self.clear()
            if self.completer() and self.completer().popup():
                self.completer().popup().hide()
            return
        elif key == Qt.Key.Key_Backspace:
            if not self.text():
                self.backspacePressedOnEmpty.emit()
                return

        super().keyPressEvent(event)


class TagChipsWidget(QFrame):
    """
    Container completo de tags com suporte a:
    1. Modo Chips: Bolinhas visuais (⚪ Brancas, 🟢 Verdes, 🔴 Vermelhas Riscadas)
    2. Modo Texto: Edição livre de texto legível (Ctrl+C, Ctrl+V, seleção de texto)
    3. Rastreamento explícito de intenção (Tags Adicionadas vs Tags Removidas)
    """

    tagsChanged = pyqtSignal(str) # Emite a string formatada

    def __init__(self, vocab_suggestions=None, parent=None):
        super().__init__(parent)
        self.setObjectName("TagChipsContainer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.original_tags = []
        self.chips = []
        self.vocab_suggestions = vocab_suggestions or []
        self._is_updating = False
        self._is_text_mode = False

        self._init_ui()

    def update_vocab_suggestions(self, vocab_suggestions):
        self.vocab_suggestions = vocab_suggestions or []
        if hasattr(self, 'input_field') and self.input_field:
            self.input_field.update_suggestions(self.vocab_suggestions)

    def _init_ui(self):
        self.setStyleSheet("""
            QFrame#TagChipsContainer {
                background-color: #2D2D2D;
                border: 1px solid #444444;
                border-radius: 6px;
                padding: 4px;
            }
            QFrame#TagChipsContainer:focus-within {
                border: 1px solid #007ACC;
            }
        """)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.root_layout.addWidget(self.stack)

        # Página 0: Container dos Chips com FlowLayout
        self.chips_container = QWidget()
        self.chips_container.setStyleSheet("background: transparent;")
        self.flow_layout = FlowLayout(self.chips_container, margin=2, spacing=6)

        self.input_field = ChipInputField(self.vocab_suggestions, self.chips_container)
        self.input_field.tagCommitted.connect(self._on_input_tag_committed)
        self.input_field.backspacePressedOnEmpty.connect(self._on_backspace_empty)
        self.flow_layout.addWidget(self.input_field)

        self.stack.addWidget(self.chips_container)

        # Página 1: Modo de Edição de Texto Puro (para copiar, colar e editar caracteres)
        self.text_editor = QPlainTextEdit()
        self.text_editor.setStyleSheet("""
            QPlainTextEdit {
                background-color: #2D2D2D;
                color: #FFFFFF;
                border: none;
                font-size: 12px;
                padding: 4px;
            }
        """)
        self.text_editor.setMaximumHeight(80)
        self.text_editor.setPlaceholderText("Digite as tags separadas por vírgula...")
        self.stack.addWidget(self.text_editor)

    def mousePressEvent(self, event):
        if not self._is_text_mode:
            self.input_field.setFocus()
        super().mousePressEvent(event)

    def toggle_mode(self):
        """Alterna entre Modo Chips e Modo Texto."""
        if self._is_text_mode:
            self.set_mode('chips')
        else:
            self.set_mode('text')

    def set_mode(self, mode):
        """Define explicitamente o modo ('chips' ou 'text')."""
        if mode == 'text':
            self._is_text_mode = True
            active_tags = self.get_active_tags()
            self.text_editor.setPlainText(", ".join(active_tags))
            self.stack.setCurrentIndex(1)
            self.text_editor.setFocus()
        else:
            self._is_text_mode = False
            raw_text = self.text_editor.toPlainText().strip()
            new_tags = self._parse_tags(raw_text)
            self._sync_chips_from_text_edit(new_tags)
            self.stack.setCurrentIndex(0)
            self.input_field.setFocus()

    def _sync_chips_from_text_edit(self, new_tags):
        """Reconstrói os chips após uma edição livre no modo texto mantendo os estados corretos."""
        self._is_updating = True
        
        for chip in list(self.chips):
            self.flow_layout.removeWidget(chip)
            chip.deleteLater()
        self.chips.clear()

        new_tags_lower = [t.lower() for t in new_tags]
        orig_tags_lower = [t.lower() for t in self.original_tags]

        # 1. Tags originais mantidas -> Branco | Tags originais apagadas no texto -> Vermelho riscado
        for ot in self.original_tags:
            if ot.lower() in new_tags_lower:
                self._create_chip(ot, state='original')
            else:
                self._create_chip(ot, state='removed')

        # 2. Tags novas adicionadas no texto -> Verde
        for nt in new_tags:
            if nt.lower() not in orig_tags_lower:
                self._create_chip(nt, state='added')

        self.flow_layout.removeWidget(self.input_field)
        self.flow_layout.addWidget(self.input_field)
        self.input_field.clear()

        self._is_updating = False
        self._notify_changed()

    def _parse_tags(self, text):
        """Converte texto em lista de tags únicas (suporta vírgula ou palavras soltas)."""
        if not text:
            return []
        if ',' in text:
            raw = [t.strip() for t in text.split(',') if t.strip()]
        else:
            raw = [t.strip() for t in text.split() if t.strip()]
        
        seen = set()
        result = []
        for t in raw:
            norm = t.lower()
            if norm not in seen:
                seen.add(norm)
                result.append(t)
        return result

    def set_tags(self, original_desc, added_tags=None, removed_tags=None):
        """
        Define o estado dos chips usando rastreamento explícito de intenção:
        - original_desc: Tags oficiais salvas no arquivo
        - added_tags: Tags que o usuário mandou adicionar
        - removed_tags: Tags que o usuário mandou remover
        """
        self._is_updating = True
        
        for chip in list(self.chips):
            self.flow_layout.removeWidget(chip)
            chip.deleteLater()
        self.chips.clear()

        self.original_tags = self._parse_tags(original_desc)
        added_set = set(t.lower() for t in (added_tags or []))
        removed_set = set(t.lower() for t in (removed_tags or []))

        # 1. Tags originais
        for ot in self.original_tags:
            ot_lower = ot.lower()
            if ot_lower in removed_set:
                self._create_chip(ot, state='removed')
            else:
                self._create_chip(ot, state='original')

        # 2. Tags adicionadas que não estavam originalmente no arquivo
        orig_lower = [t.lower() for t in self.original_tags]
        for at in (added_tags or []):
            at_lower = at.lower()
            if at_lower not in orig_lower and at_lower not in removed_set:
                self._create_chip(at, state='added')

        self.flow_layout.removeWidget(self.input_field)
        self.flow_layout.addWidget(self.input_field)
        self.input_field.clear()

        if self._is_text_mode:
            self.text_editor.setPlainText(", ".join(self.get_active_tags()))

        self._is_updating = False

    def _create_chip(self, text, state='original'):
        chip = TagChip(text, state=state, parent=self.chips_container)
        chip.chipClicked.connect(self._on_chip_clicked)
        chip.chipEditRequested.connect(self._on_chip_edit_requested)
        self.chips.append(chip)
        
        self.flow_layout.removeWidget(self.input_field)
        self.flow_layout.addWidget(chip)
        self.flow_layout.addWidget(self.input_field)
        return chip

    def _on_input_tag_committed(self, text):
        new_tags = self._parse_tags(text)
        for t in new_tags:
            self.add_tag(t)

    def add_tag(self, tag_text):
        tag_text = tag_text.strip()
        if not tag_text:
            return

        self.input_field.clear()
        if self.input_field.completer() and self.input_field.completer().popup():
            self.input_field.completer().popup().hide()

        norm_input = tag_text.lower()
        
        for chip in self.chips:
            if chip.text_value.lower() == norm_input:
                if chip.state == 'removed':
                    chip.set_state('original')
                    self._notify_changed()
                self.input_field.setFocus()
                return

        orig_lower = [t.lower() for t in self.original_tags]
        state = 'original' if norm_input in orig_lower else 'added'
        self._create_chip(tag_text, state=state)
        self._notify_changed()
        self.input_field.setFocus()

    def _on_chip_clicked(self, chip):
        """Clique esquerdo: apaga ou restaura."""
        if chip.state == 'added':
            self.chips.remove(chip)
            self.flow_layout.removeWidget(chip)
            chip.deleteLater()
        elif chip.state == 'original':
            chip.set_state('removed')
        elif chip.state == 'removed':
            chip.set_state('original')

        self._notify_changed()

    def _on_chip_edit_requested(self, chip):
        """Clique direito: retira o chip e coloca no campo para editar."""
        text = chip.text_value
        self._on_chip_clicked(chip)
        self.input_field.setText(text)
        self.input_field.setFocus()

    def _on_backspace_empty(self):
        if not self.chips:
            return
        last_chip = self.chips[-1]
        self._on_chip_clicked(last_chip)

    def get_active_tags(self):
        """Retorna apenas as tags ativas (não riscadas)."""
        return [c.text_value for c in self.chips if c.state != 'removed']

    def get_explicit_intent(self):
        """Retorna a tupla (tags_adicionadas, tags_removidas) para staging seguro."""
        added = [c.text_value for c in self.chips if c.state == 'added']
        removed = [c.text_value for c in self.chips if c.state == 'removed']
        return added, removed

    def get_description_string(self):
        """Retorna a string completa separada por vírgula para persistência."""
        return ", ".join(self.get_active_tags())

    def _notify_changed(self):
        if not self._is_updating:
            self.tagsChanged.emit(self.get_description_string())
