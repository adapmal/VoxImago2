"""
Diálogo de Tratamento de Conflito de Nomes ao Mover Arquivos no VoxImago.
Permite ao usuário decidir o que fazer quando arquivos com o mesmo nome já existem na pasta destino:
1. Criar uma subpasta dedicada (padrão: "mesmonome" ou nome da pasta de origem) e mover para lá.
2. Renomear com sufixo (_1, _2...).
3. Ignorar/Pular os arquivos conflitantes.
"""

import os
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QRadioButton, 
    QLineEdit, QPushButton, QListWidget, QButtonGroup, QFrame, QMessageBox
)
from PyQt6.QtCore import Qt
from src.utils.path_validation import validate_path_component


class MoveConflictDialog(QDialog):
    def __init__(self, conflict_items, target_folder_path, source_folder_name="", parent=None):
        super().__init__(parent)
        self.conflict_items = conflict_items
        self.target_folder_path = target_folder_path
        self.source_folder_name = source_folder_name or "mesmonome"
        
        self.chosen_action = "create_subfolder"  # 'create_subfolder', 'rename', 'skip'
        self.subfolder_name = self._suggest_subfolder_name()
        
        self.setWindowTitle("⚠️ Conflito de Arquivos Homônimos Detectado")
        self.setMinimumWidth(540)
        self.setMinimumHeight(440)
        
        self._init_ui()

    def _suggest_subfolder_name(self):
        # Sugere o nome da pasta de origem ou 'mesmonome'
        if self.source_folder_name and self.source_folder_name.lower() not in ("origem", "raiz", ""):
            return self.source_folder_name
        return "mesmonome"

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # 1. Cabeçalho explicativo
        dest_name = os.path.basename(self.target_folder_path)
        count = len(self.conflict_items)
        header_text = (
            f"<b>Aviso de Duplicidade:</b> Foram detectados <b>{count} arquivo(s)</b> com o mesmo nome "
            f"já existentes na pasta destino: <br>"
            f"<code style='color: #00bcd4;'>{dest_name}</code>"
        )
        lbl_header = QLabel(header_text)
        lbl_header.setWordWrap(True)
        layout.addWidget(lbl_header)

        # 2. Lista dos arquivos conflitantes
        lbl_list = QLabel("Arquivos que já existem no destino:")
        lbl_list.setStyleSheet("font-size: 11px; color: #aaa;")
        layout.addWidget(lbl_list)

        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(110)
        for it in self.conflict_items:
            fname = it.get('name') or os.path.basename(it.get('path') or '')
            self.list_widget.addItem(f"📄 {fname}")
        layout.addWidget(self.list_widget)

        # Separador
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # 3. Grupo de opções
        lbl_options = QLabel("Como você deseja resolver esses arquivos duplicados?")
        lbl_options.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_options)

        self.btn_group = QButtonGroup(self)

        # Opção 1: Criar Subpasta (Recomendada)
        opt1_layout = QVBoxLayout()
        self.radio_subfolder = QRadioButton("📁 Criar uma subpasta dentro do destino e mover os arquivos para lá (Recomendado)")
        self.radio_subfolder.setChecked(True)
        self.btn_group.addButton(self.radio_subfolder, 1)
        opt1_layout.addWidget(self.radio_subfolder)

        subfolder_input_layout = QHBoxLayout()
        subfolder_input_layout.setContentsMargins(24, 0, 0, 0)
        lbl_sub = QLabel("Nome da Subpasta:")
        self.txt_subfolder = QLineEdit(self.subfolder_name)
        self.txt_subfolder.setPlaceholderText("Ex: mesmonome, Camera 01, Homônimos...")
        subfolder_input_layout.addWidget(lbl_sub)
        subfolder_input_layout.addWidget(self.txt_subfolder, 1)
        opt1_layout.addLayout(subfolder_input_layout)
        layout.addLayout(opt1_layout)

        # Opção 2: Renomear com sufixo
        self.radio_rename = QRadioButton("✏️ Manter na mesma pasta e renomear com sufixo (ex: foto_1.jpg, foto_2.jpg)")
        self.btn_group.addButton(self.radio_rename, 2)
        layout.addWidget(self.radio_rename)

        # Opção 3: Ignorar/Pular
        self.radio_skip = QRadioButton("❌ Ignorar (não mover estes arquivos conflitantes)")
        self.btn_group.addButton(self.radio_skip, 3)
        layout.addWidget(self.radio_skip)

        # Conectar mudanças de rádio para habilitar/desabilitar o campo de texto
        self.radio_subfolder.toggled.connect(lambda chk: self.txt_subfolder.setEnabled(chk))

        layout.addStretch()

        # 4. Botões de Ação
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancelar Tudo")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_confirm = QPushButton("Confirmar e Enfileirar")
        self.btn_confirm.setDefault(True)
        self.btn_confirm.setStyleSheet("background-color: #1976d2; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_confirm.clicked.connect(self._on_confirm)
        btn_layout.addWidget(self.btn_confirm)

        layout.addLayout(btn_layout)

    def _on_confirm(self):
        if self.radio_subfolder.isChecked():
            self.chosen_action = "create_subfolder"
            self.subfolder_name = self.txt_subfolder.text().strip() or "mesmonome"
            valid, error = validate_path_component(self.subfolder_name, 'nome da subpasta')
            if not valid:
                QMessageBox.warning(self, "Nome Inválido", error)
                self.txt_subfolder.setFocus()
                return
        elif self.radio_rename.isChecked():
            self.chosen_action = "rename"
        elif self.radio_skip.isChecked():
            self.chosen_action = "skip"
        self.accept()

    def get_result(self):
        """Retorna (chosen_action, subfolder_name)"""
        return self.chosen_action, self.subfolder_name
