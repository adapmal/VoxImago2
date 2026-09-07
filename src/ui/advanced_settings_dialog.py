import os
import csv
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton, QLineEdit, QMessageBox, QHBoxLayout, QFileDialog, QWidget
)
from PyQt6.QtCore import Qt
from src.utils.config_manager import ConfigManager

class AdvancedSettingsDialog(QDialog):
    def __init__(self, parent=None, indexer=None, service=None):
        super().__init__(parent)
        self.indexer = indexer
        self.service = service
        self.config_mgr = ConfigManager()
        self.setWindowTitle("Sistema / Avançado")
        self.setFixedSize(400, 200)
        
        self.layout = QVBoxLayout(self)
        
        self.auth_layout = QVBoxLayout()
        self.auth_label = QLabel("Área restrita. Digite a senha do administrador:")
        self.auth_input = QLineEdit()
        self.auth_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.auth_btn = QPushButton("Entrar")
        self.auth_btn.clicked.connect(self._check_password)
        
        self.auth_hint = QLabel("<i>Dica de senha: voximago2026</i>")
        self.auth_hint.setStyleSheet("color: gray; font-size: 10px;")
        
        self.auth_layout.addWidget(self.auth_label)
        self.auth_layout.addWidget(self.auth_input)
        self.auth_layout.addWidget(self.auth_btn)
        self.auth_layout.addWidget(self.auth_hint)
        
        self.settings_layout = QVBoxLayout()
        
        self.info_label = QLabel("🛠️ <b>Ferramentas de Recuperação de Desastre</b>")
        
        self.restore_btn = QPushButton("Restauração Cega de Tags (via CSV)")
        self.restore_btn.setStyleSheet("background-color: #DC3545; color: white; font-weight: bold; padding: 10px;")
        self.restore_btn.clicked.connect(self._run_blind_restore)
        
        self.settings_layout.addWidget(self.info_label)
        self.settings_layout.addWidget(self.restore_btn)
        
        self.main_widget = QWidget()
        self.main_widget.setLayout(self.settings_layout)
        self.main_widget.setVisible(False)
        
        self.auth_widget = QWidget()
        self.auth_widget.setLayout(self.auth_layout)
        
        self.layout.addWidget(self.auth_widget)
        self.layout.addWidget(self.main_widget)
        
    def _check_password(self):
        if self.auth_input.text() == "voximago2026":
            self.auth_widget.setVisible(False)
            self.main_widget.setVisible(True)
        else:
            QMessageBox.warning(self, "Acesso Negado", "Senha incorreta.")
            
    def _run_blind_restore(self):
        if not self.indexer:
            QMessageBox.critical(self, "Erro", "Banco de dados não conectado.")
            return
            
        reply = QMessageBox.question(
            self, "Atenção Crítica",
            "Isto vai sobrescrever as tags atuais no banco de dados local com base em um arquivo CSV de backup.\n\nTem certeza que deseja continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
            
        default_dir = os.path.join(os.path.abspath('.'), 'config', 'backups')
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Selecionar CSV de Backup", default_dir, "CSV Files (*.csv)"
        )
        if not file_path:
            return
            
        try:
            updates = 0
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                from src.database.database import to_canonical_id
                from src.database.search import SearchEngine
                norm_engine = SearchEngine(None)
                for row in reader:
                    file_id = row.get('file_id') or row.get('id') or row.get('path')
                    description = (row.get('description') or '').strip()
                    if file_id and description:
                        canon_id = to_canonical_id(file_id)
                        norm_desc = norm_engine.normalize_text(description)
                        # Restaura a descrição no banco local e no índice FTS5
                        self.indexer.cursor.execute(
                            "UPDATE files SET description = ? WHERE file_id = ? OR path = ?",
                            (description, canon_id, canon_id)
                        )
                        updates += self.indexer.cursor.rowcount
                        self.indexer.cursor.execute(
                            "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ?",
                            (description, norm_desc, canon_id)
                        )
                self.indexer.conn.commit()
                
            if self.config_mgr.is_sandbox():
                self.indexer.export_to_shared_cache()
                
            QMessageBox.information(self, "Sucesso", f"Restauração concluída!\n{updates} arquivos tiveram suas tags atualizadas localmente.")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Falha na restauração: {e}")
