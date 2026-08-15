'''
Diálogo de Edição de Tags em Lote (Batch Tag Editor) para o VoxImago v2.1
'''

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QGroupBox, QListWidget
)
from PyQt6.QtCore import Qt
from src.ui.staging_queue import StagingQueue
from src.utils.config_manager import ConfigManager


class BatchEditorDialog(QDialog):
    def __init__(self, selected_files, parent=None):
        super().__init__(parent)
        self.selected_files = selected_files or []
        self.config_mgr = ConfigManager()

        self.setWindowTitle(f"Editar Tags em Lote ({len(self.selected_files)} arquivos selecionados)")
        self.setMinimumSize(550, 420)

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        info_label = QLabel(
            f"<b>Arquivos Selecionados:</b> {len(self.selected_files)} arquivo(s)"
        )
        layout.addWidget(info_label)

        # Previsualização rápida da lista de arquivos
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(90)
        for f in self.selected_files[:20]:
            name = f.get('name', 'Sem nome')
            self.file_list.addItem(f"📄 {name}")
        if len(self.selected_files) > 20:
            self.file_list.addItem(f"... e mais {len(self.selected_files) - 20} arquivo(s).")
        layout.addWidget(self.file_list)

        group_add = QGroupBox("➕ Adicionar Tags")
        layout_add = QVBoxLayout(group_add)
        self.input_add_tags = QLineEdit()
        self.input_add_tags.setPlaceholderText("Digite as tags separadas por vírgula (ex: Dom Cícero, Gavi, Missa)")
        layout_add.addWidget(self.input_add_tags)
        layout.addWidget(group_add)

        group_remove = QGroupBox("➖ Remover Tags")
        layout_remove = QVBoxLayout(group_remove)
        self.input_remove_tags = QLineEdit()
        self.input_remove_tags.setPlaceholderText("Digite as tags a remover separadas por vírgula")
        layout_remove.addWidget(self.input_remove_tags)
        layout.addWidget(group_remove)

        mode_note = QLabel()
        if self.config_mgr.is_read_only():
            mode_note.setText("🔒 Modo Somente Leitura Ativo: Ações bloqueadas.")
            mode_note.setStyleSheet("color: #DC3545; font-weight: bold;")
        elif self.config_mgr.is_sandbox():
            mode_note.setText("🧪 As edições serão agendadas para o ambiente Sandbox (L:\\_TestesBanco).")
            mode_note.setStyleSheet("color: #856404;")
        else:
            mode_note.setText("☁️ As edições serão agendadas para a Fila de Revisão do Google Drive Oficial.")
            mode_note.setStyleSheet("color: #28A745;")
        layout.addWidget(mode_note)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_add_to_queue = QPushButton("📋 Adicionar à Fila de Revisão")
        self.btn_add_to_queue.setStyleSheet(
            "background-color: #007BFF; color: white; font-weight: bold; padding: 6px 14px;")
        self.btn_add_to_queue.clicked.connect(self._add_to_queue)
        if self.config_mgr.is_read_only():
            self.btn_add_to_queue.setEnabled(False)

        btn_layout.addWidget(self.btn_add_to_queue)

        layout.addLayout(btn_layout)

    def _add_to_queue(self):
        tags_add = self.input_add_tags.text().strip()
        tags_remove = self.input_remove_tags.text().strip()

        if not tags_add and not tags_remove:
            QMessageBox.warning(self, "Aviso", "Por favor, digite ao menos uma tag para adicionar ou remover.")
            return

        queue = StagingQueue()
        count = queue.add_batch_tags(self.selected_files, tags_to_add=tags_add, tags_to_remove=tags_remove)

        QMessageBox.information(
            self, "Edição Agendada",
            f"Adicionadas {count} alteração(ões) para {len(self.selected_files)} arquivo(s) na Fila de Revisão.\n\n"
            "Clique no ícone da Fila de Revisão (📋) para visualizar ou confirmar as alterações."
        )
        self.accept()
