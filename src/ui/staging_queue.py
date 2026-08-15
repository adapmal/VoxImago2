'''
Fila de Revisão de Alterações (Staging Queue) para o VoxImago v2.1
Permite acumular alterações em lote, visualizar o preview de modificações (Diff)
e realizar a execução segura no Google Drive e Banco de Dados.
'''

import os
import time
import logging
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QWidget, QFrame, QSplitter
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal
from src.utils.config_manager import ConfigManager


class StagingItem:
    def __init__(self, file_id, file_name, path, action_type, old_value="", new_value=""):
        self.file_id = file_id
        self.file_name = file_name
        self.path = path
        self.action_type = action_type  # 'add_tags', 'remove_tags', 'set_description', 'rename', 'move'
        self.old_value = old_value
        self.new_value = new_value
        self.timestamp = time.time()

    def get_description_summary(self):
        if self.action_type == 'add_tags':
            return f"[+] Adicionar tags: '{self.new_value}'"
        elif self.action_type == 'remove_tags':
            return f"[-] Remover tags: '{self.new_value}'"
        elif self.action_type == 'set_description':
            return f"[=] Nova descrição: '{self.new_value}'"
        elif self.action_type == 'rename':
            return f"[R] Renomear para: '{self.new_value}'"
        elif self.action_type == 'move':
            return f"[M] Mover para: '{self.new_value}'"
        return f"[{self.action_type}] {self.new_value}"


QUEUE_CACHE_FILE = os.path.join('config', 'staging_queue.json')


class _StagingSignals(QObject):
    queueChanged = pyqtSignal(int)  # Emitido com o número total de itens pendentes
    itemAdded = pyqtSignal(object)
    itemRemoved = pyqtSignal(object)
    cleared = pyqtSignal()


class StagingQueue:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.signals = _StagingSignals()
            cls._instance.queueChanged = cls._instance.signals.queueChanged
            cls._instance.itemAdded = cls._instance.signals.itemAdded
            cls._instance.itemRemoved = cls._instance.signals.itemRemoved
            cls._instance.cleared = cls._instance.signals.cleared
            cls._instance.items = []
            cls._instance._load_from_disk()
        return cls._instance

    def __init__(self, *args, **kwargs):
        pass

    def _save_to_disk(self):
        try:
            os.makedirs(os.path.dirname(QUEUE_CACHE_FILE), exist_ok=True)
            data = []
            for it in self.items:
                data.append({
                    'file_id': it.file_id,
                    'file_name': it.file_name,
                    'path': it.path,
                    'action_type': it.action_type,
                    'old_value': it.old_value,
                    'new_value': it.new_value,
                    'timestamp': it.timestamp
                })
            with open(QUEUE_CACHE_FILE, 'w', encoding='utf-8') as f:
                import json
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Erro ao salvar fila no disco: {e}")

    def _load_from_disk(self):
        if os.path.exists(QUEUE_CACHE_FILE):
            try:
                import json
                with open(QUEUE_CACHE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for d in data:
                        try:
                            it = StagingItem(
                                d.get('file_id'), d.get('file_name'), d.get('path'),
                                d.get('action_type'), d.get('old_value', ''), d.get('new_value', '')
                            )
                            it.timestamp = d.get('timestamp', time.time())
                            self.items.append(it)
                        except Exception as item_e:
                            logging.error(f"Erro ao carregar item individual da fila: {item_e}")
            except Exception as e:
                logging.error(f"Erro ao carregar fila do disco: {e}")
                # Clear corrupted queue file
                try:
                    os.remove(QUEUE_CACHE_FILE)
                except:
                    pass

    def add_item(self, item):
        self.items.append(item)
        self.itemAdded.emit(item)
        self.queueChanged.emit(len(self.items))
        self._save_to_disk()

    def add_batch_tags(self, files_list, tags_to_add="", tags_to_remove=""):
        count = 0
        for file_item in files_list:
            fid = file_item.get('file_id') or file_item.get('id')
            fname = file_item.get('name', 'Sem nome')
            fpath = file_item.get('path', '')
            old_desc = file_item.get('description', '')

            if tags_to_add:
                item_add = StagingItem(fid, fname, fpath, 'add_tags', old_value=old_desc, new_value=tags_to_add)
                self.items.append(item_add)
                count += 1

            if tags_to_remove:
                item_rem = StagingItem(fid, fname, fpath, 'remove_tags', old_value=old_desc, new_value=tags_to_remove)
                self.items.append(item_rem)
                count += 1

        self.queueChanged.emit(len(self.items))
        self._save_to_disk()
        return count

    def remove_item(self, item):
        if item in self.items:
            self.items.remove(item)
            self.itemRemoved.emit(item)
            self.queueChanged.emit(len(self.items))
            self._save_to_disk()

    def clear(self):
        self.items.clear()
        self.cleared.emit()
        self.queueChanged.emit(0)
        self._save_to_disk()

    def count(self):
        return len(self.items)


class StagingQueueDialog(QDialog):
    executionCompleted = pyqtSignal(int)  # Emitido com o número de alterações aplicadas

    def __init__(self, parent=None, drive_service=None, db_indexer=None):
        super().__init__(parent)
        self.queue = StagingQueue()
        self.drive_service = drive_service
        self.db_indexer = db_indexer
        self.config_mgr = ConfigManager()

        self.setWindowTitle("Fila de Revisão de Alterações (Staging Queue)")
        self.setMinimumSize(700, 500)

        self._init_ui()
        self._populate_list()
        self.queue.queueChanged.connect(lambda c: self._populate_list())

    def _init_ui(self):
        layout = QVBoxLayout(self)

        header_layout = QHBoxLayout()
        self.header_label = QLabel("📋 Alterações Pendentes para Revisão")
        self.header_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        header_layout.addWidget(self.header_label)

        header_layout.addStretch()

        self.mode_info_label = QLabel()
        self.mode_info_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        header_layout.addWidget(self.mode_info_label)

        layout.addLayout(header_layout)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.list_widget)

        self.details_label = QLabel("Selecione um item para ver o detalhamento da alteração.")
        self.details_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.details_label.setWordWrap(True)
        self.details_label.setStyleSheet("padding: 8px; background: #F8F9FA;")
        layout.addWidget(self.details_label)

        self.list_widget.currentItemChanged.connect(self._on_item_selected)

        btn_layout = QHBoxLayout()

        self.btn_remove_selected = QPushButton("🗑️ Remover Selecionado")
        self.btn_remove_selected.clicked.connect(self._remove_selected_item)
        btn_layout.addWidget(self.btn_remove_selected)

        self.btn_clear_all = QPushButton("🧹 Limpar Fila")
        self.btn_clear_all.clicked.connect(self._clear_queue)
        btn_layout.addWidget(self.btn_clear_all)

        btn_layout.addStretch()

        self.btn_execute = QPushButton("🚀 Executar Alterações")
        self.btn_execute.setStyleSheet(
            "background-color: #28A745; color: white; font-weight: bold; font-size: 13px; padding: 6px 16px;")
        self.btn_execute.clicked.connect(self._execute_queue)
        btn_layout.addWidget(self.btn_execute)

        layout.addLayout(btn_layout)

        self._update_mode_label()

    def _update_mode_label(self):
        if self.config_mgr.is_read_only():
            self.mode_info_label.setText("🔒 Modo Somente Leitura (Execução bloqueada)")
            self.mode_info_label.setStyleSheet("color: #DC3545;")
            self.btn_execute.setEnabled(False)
        else:
            if self.config_mgr.is_sandbox():
                self.mode_info_label.setText("🧪 Alvo: Sandbox (L:\\_TestesBanco)")
                self.mode_info_label.setStyleSheet("color: #856404;")
            else:
                self.mode_info_label.setText("☁️ Alvo: Produção Oficial (Google Drive)")
                self.mode_info_label.setStyleSheet("color: #28A745;")
            self.btn_execute.setEnabled(self.queue.count() > 0)

    def _populate_list(self):
        self.list_widget.clear()
        for idx, item in enumerate(self.queue.items):
            list_item = QListWidgetItem()
            text = f"{idx+1}. {item.file_name} -> {item.get_description_summary()}"
            list_item.setText(text)
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            self.list_widget.addItem(list_item)

        self.header_label.setText(f"📋 Alterações Pendentes para Revisão ({self.queue.count()} itens)")
        self._update_mode_label()

    def _on_item_selected(self, current, previous):
        if current:
            item = current.data(Qt.ItemDataRole.UserRole)
            if item:
                details = (
                    f"<b>Arquivo:</b> {item.file_name}<br>"
                    f"<b>Caminho:</b> {item.path}<br>"
                    f"<b>Ação:</b> {item.get_description_summary()}<br>"
                    f"<b>Descrição Anterior:</b> <i>{item.old_value or '(vazia)'}</i>"
                )
                self.details_label.setText(details)
                return
        self.details_label.setText("Selecione um item para ver o detalhamento da alteração.")

    def _remove_selected_item(self):
        current = self.list_widget.currentItem()
        if current:
            item = current.data(Qt.ItemDataRole.UserRole)
            self.queue.remove_item(item)

    def _clear_queue(self):
        if self.queue.count() > 0:
            reply = QMessageBox.question(
                self, "Limpar Fila",
                "Tem certeza de que deseja cancelar todas as alterações pendentes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.queue.clear()

    def _execute_queue(self):
        if self.config_mgr.is_read_only():
            QMessageBox.warning(self, "Modo Somente Leitura", "As edições estão desabilitadas no modo Somente Leitura.")
            return

        total = self.queue.count()
        if total == 0:
            return

        dest_name = "Sandbox (L:\\_TestesBanco)" if self.config_mgr.is_sandbox() else "Google Drive Oficial"
        reply = QMessageBox.question(
            self, "Confirmar Execução em Lote",
            f"Deseja executar {total} alteração(ões) pendente(s) em {dest_name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        executed_count = 0
        errors = []

        for item in list(self.queue.items):
            try:
                # Processar alteração de tags / descrição
                if item.action_type in ('add_tags', 'remove_tags', 'set_description'):
                    new_desc = item.old_value
                    if item.action_type == 'add_tags':
                        tags = [t.strip() for t in item.new_value.split(',') if t.strip()]
                        existing_tags = [t.strip() for t in item.old_value.split(',') if t.strip()]
                        for t in tags:
                            if t not in existing_tags:
                                existing_tags.append(t)
                        new_desc = ", ".join(existing_tags)
                    elif item.action_type == 'remove_tags':
                        tags_to_rem = [t.strip().lower() for t in item.new_value.split(',') if t.strip()]
                        existing_tags = [t.strip() for t in item.old_value.split(',') if t.strip()]
                        existing_tags = [t for t in existing_tags if t.lower() not in tags_to_rem]
                        new_desc = ", ".join(existing_tags)
                    elif item.action_type == 'set_description':
                        new_desc = item.new_value

                    # Atualizar localmente no banco SQLite se indexer estiver presente
                    if self.db_indexer and item.file_id:
                        self.db_indexer.update_description(item.file_id, new_desc, commit=True)

                    # Atualizar na API do Google Drive se o serviço de drive estiver ativo
                    if self.drive_service and item.file_id and not self.config_mgr.is_sandbox():
                        try:
                            self.drive_service.files().update(
                                fileId=item.file_id, 
                                body={'description': new_desc}, 
                                supportsAllDrives=True
                            ).execute()
                        except Exception as e:
                            logging.error(f"Erro na API Drive ao atualizar desc {item.file_id}: {e}")

                elif item.action_type == 'rename':
                    new_name = item.new_value
                    if self.db_indexer and item.file_id:
                        self.db_indexer.cursor.execute("UPDATE files SET name = ?, name_normalized = ? WHERE file_id = ?", (new_name, new_name.lower(), item.file_id))
                        self.db_indexer.conn.commit()
                    if self.drive_service and item.file_id and not self.config_mgr.is_sandbox():
                        self.drive_service.files().update(
                            fileId=item.file_id, 
                            body={'name': new_name}, 
                            supportsAllDrives=True
                        ).execute()

                elif item.action_type == 'delete':
                    if self.db_indexer and item.file_id:
                        self.db_indexer.cursor.execute("DELETE FROM files WHERE file_id = ?", (item.file_id,))
                        self.db_indexer.conn.commit()
                    if self.drive_service and item.file_id and not self.config_mgr.is_sandbox():
                        self.drive_service.files().update(
                            fileId=item.file_id, 
                            body={'trashed': True}, 
                            supportsAllDrives=True
                        ).execute()

                elif item.action_type == 'rotate_90':
                    if item.path and os.path.exists(item.path):
                        try:
                            from PIL import Image
                            with Image.open(item.path) as img:
                                img = img.rotate(-90, expand=True)
                                img.save(item.path)
                        except Exception as e:
                            logging.error(f"Erro ao rotacionar localmente {item.path}: {e}")
                            errors.append(f"Erro na rotação local {item.file_name}: {e}")

                executed_count += 1
                self.queue.remove_item(item)

            except Exception as e:
                logging.error(f"Erro ao executar item {item.file_name}: {e}")
                errors.append(f"{item.file_name}: {e}")

        if errors:
            QMessageBox.warning(
                self, "Execução Concluída com Avisos",
                f"{executed_count} alteração(ões) aplicada(s).\n\nErros:\n" + "\n".join(errors[:5])
            )
        else:
            QMessageBox.information(
                self, "Execução Concluída",
                f"Todas as {executed_count} alterações foram aplicadas com sucesso em {dest_name}!"
            )

        self.executionCompleted.emit(executed_count)
        self.accept()
