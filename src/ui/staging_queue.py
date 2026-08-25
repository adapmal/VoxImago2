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
    QPushButton, QLabel, QMessageBox, QWidget, QFrame, QSplitter,
    QAbstractItemView, QProgressDialog
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QCoreApplication
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
            dst_folder = os.path.basename(os.path.dirname(self.new_value))
            return f"📦 Mover para a pasta: '{dst_folder}'"
        elif self.action_type == 'delete':
            return f"🗑️ Excluir arquivo"
        elif self.action_type == 'create_folder':
            folder_name = os.path.basename(self.new_value)
            return f"📁🟢 Criar nova pasta (Fila): '{folder_name}'"
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
        # 1. Se for 'delete', remove qualquer 'move', 'rename' ou 'tags' pendentes anteriores desse mesmo arquivo
        if item.action_type == 'delete':
            self.items = [it for it in self.items if it.file_id != item.file_id]

        # 2. Se for 'move':
        elif item.action_type == 'move':
            # Se o arquivo já está marcado para exclusão definitiva (delete), ignora o mover
            if any(it.file_id == item.file_id and it.action_type == 'delete' for it in self.items):
                return
            # Substitui 'move' anterior
            self.items = [it for it in self.items if not (it.file_id == item.file_id and it.action_type == 'move')]

        # 3. Se for 'rename', substitui 'rename' anterior
        elif item.action_type == 'rename':
            self.items = [it for it in self.items if not (it.file_id == item.file_id and it.action_type == 'rename')]

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

    def _cleanup_staged_folder(self, item):
        if item.action_type == 'create_folder':
            fpath = item.new_value or item.path
            if fpath and os.path.isdir(fpath):
                try:
                    # Se a pasta estiver vazia e foi descartada, remove o placeholder local
                    if not os.listdir(fpath):
                        os.rmdir(fpath)
                except Exception:
                    pass

    def remove_item(self, item, discard_placeholder=False):
        if item in self.items:
            if discard_placeholder:
                self._cleanup_staged_folder(item)
            self.items.remove(item)
            self.itemRemoved.emit(item)
            self.queueChanged.emit(len(self.items))
            self._save_to_disk()

    def remove_items_batch(self, items_to_remove, discard_placeholder=False):
        changed = False
        for item in items_to_remove:
            if item in self.items:
                if discard_placeholder:
                    self._cleanup_staged_folder(item)
                self.items.remove(item)
                self.itemRemoved.emit(item)
                changed = True
        if changed:
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
    itemFocusRequested = pyqtSignal(object)  # (StagingItem) Solicitado foco no arquivo na interface principal

    def __init__(self, parent=None, drive_service=None, db_indexer=None):
        super().__init__(parent)
        self.queue = StagingQueue()
        self.drive_service = drive_service
        self.db_indexer = db_indexer
        self.config_mgr = ConfigManager()

        self.setWindowTitle("📋 Fila de Revisão de Alterações (Staging Queue)")
        self.setWindowFlags(Qt.WindowType.Window)  # Janela independente destacável
        self.setMinimumSize(780, 520)

        self._init_ui()
        self._populate_list()
        self.queue.queueChanged.connect(lambda c: self._populate_list())

    def showEvent(self, event):
        super().showEvent(event)
        self._populate_list()

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                color: #FFFFFF;
            }
            QLabel {
                color: #FFFFFF;
            }
            QListWidget {
                background-color: #252525;
                color: #FFFFFF;
                border: 1px solid #3A3A3A;
                border-radius: 6px;
            }
            QListWidget::item {
                padding: 6px;
                border-bottom: 1px solid #2F2F2F;
            }
            QListWidget::item:selected {
                background-color: #0D47A1;
                color: #FFFFFF;
            }
            QPushButton {
                background-color: #333333;
                color: #FFFFFF;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: #444444;
            }
        """)

        layout = QVBoxLayout(self)

        header_layout = QHBoxLayout()
        self.header_label = QLabel("📋 Alterações Pendentes para Revisão")
        self.header_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #FFFFFF;")
        header_layout.addWidget(self.header_label)

        header_layout.addStretch()

        self.mode_info_label = QLabel()
        self.mode_info_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        header_layout.addWidget(self.mode_info_label)

        layout.addLayout(header_layout)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.list_widget)

        self.details_label = QLabel("Selecione um item para ver o detalhamento da alteração.")
        self.details_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.details_label.setWordWrap(True)
        self.details_label.setStyleSheet("padding: 10px; background-color: #252525; color: #E0E0E0; border: 1px solid #3A3A3A; border-radius: 6px; font-size: 12px; min-height: 48px;")
        layout.addWidget(self.details_label)

        self.list_widget.currentItemChanged.connect(self._on_item_selected)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)

        btn_layout = QHBoxLayout()

        self.btn_remove_selected = QPushButton("🗑️ Remover Selecionado")
        self.btn_remove_selected.clicked.connect(self._remove_selected_item)
        btn_layout.addWidget(self.btn_remove_selected)

        self.btn_clear_all = QPushButton("🧹 Limpar Fila")
        self.btn_clear_all.clicked.connect(self._clear_queue)
        btn_layout.addWidget(self.btn_clear_all)

        btn_layout.addStretch()

        self.btn_import = QPushButton("📂 Importar")
        self.btn_import.clicked.connect(self._import_queue)
        btn_layout.addWidget(self.btn_import)

        self.btn_export = QPushButton("💾 Exportar")
        self.btn_export.clicked.connect(self._export_queue)
        btn_layout.addWidget(self.btn_export)

        btn_layout.addStretch()

        self.btn_execute_next = QPushButton("▶️ Executar Próxima")
        self.btn_execute_next.setStyleSheet(
            "background-color: #17A2B8; color: white; font-weight: bold; font-size: 13px; padding: 6px 16px;")
        self.btn_execute_next.clicked.connect(self._execute_next)
        btn_layout.addWidget(self.btn_execute_next)

        self.btn_execute = QPushButton("🚀 Executar Todas")
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
            self.btn_execute_next.setEnabled(False)
        else:
            if self.config_mgr.is_sandbox():
                self.mode_info_label.setText("🧪 Alvo: Sandbox (L:\\Drives Compartilhados\\_TestesBanco)")
                self.mode_info_label.setStyleSheet("color: #FFC107;")
            else:
                self.mode_info_label.setText("☁️ Alvo: Produção Oficial (Google Drive)")
                self.mode_info_label.setStyleSheet("color: #28A745;")
            self.btn_execute.setEnabled(self.queue.count() > 0)
            self.btn_execute_next.setEnabled(self.queue.count() > 0)

    def _populate_list(self):
        self.list_widget.clear()
        
        # Mapear arquivos que possuem 'move' na fila
        move_map = {}
        for it in self.queue.items:
            if it.action_type == 'move':
                dest_name = os.path.basename(os.path.dirname(it.new_value))
                move_map[it.file_id] = (dest_name, it.new_value)
                if it.path:
                    move_map[os.path.normpath(it.path).lower()] = (dest_name, it.new_value)

        for idx, item in enumerate(self.queue.items):
            list_item = QListWidgetItem()
            
            # Verificar se este arquivo possui movimento posterior
            has_subsequent_move = False
            moved_dest_name = ""
            if item.action_type != 'move':
                lookup_key = os.path.normpath(item.path).lower() if item.path else item.file_id
                if item.file_id in move_map:
                    has_subsequent_move = True
                    moved_dest_name = move_map[item.file_id][0]
                elif lookup_key in move_map:
                    has_subsequent_move = True
                    moved_dest_name = move_map[lookup_key][0]

            if item.action_type == 'move':
                orig_folder = os.path.basename(os.path.dirname(item.old_value or item.path))
                dest_folder = os.path.basename(os.path.dirname(item.new_value))
                text = f"{idx+1}. 📦 {item.file_name} ➜ Mover: [{orig_folder}] ➔ [{dest_folder}]"
            elif item.action_type == 'rename':
                move_suffix = f"  [📦 Movido para: {moved_dest_name}]" if has_subsequent_move else ""
                text = f"{idx+1}. ✏️ {item.old_value or item.file_name} ➜ Renomear para [{item.new_value}]{move_suffix}"
            elif item.action_type == 'delete':
                text = f"{idx+1}. 🗑️ {item.file_name} ➜ Excluir do acervo"
            elif item.action_type == 'create_folder':
                parent_name = os.path.basename(os.path.dirname(item.new_value or item.path))
                text = f"{idx+1}. 📁🟢 Criar Nova Pasta: [{parent_name}] ➔ [{item.file_name}]"
            else:
                move_suffix = f"  [📦 Movido para: {moved_dest_name}]" if has_subsequent_move else ""
                text = f"{idx+1}. 🏷️ {item.file_name} ➜ {item.get_description_summary()}{move_suffix}"

            list_item.setText(text)
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            self.list_widget.addItem(list_item)

        self.header_label.setText(f"📋 Alterações Pendentes para Revisão ({self.queue.count()} itens)")
        self._update_mode_label()

    def _on_item_selected(self, current, previous):
        if current:
            item = current.data(Qt.ItemDataRole.UserRole)
            if item:
                # Checar se possui transferência posterior
                subsequent_note = ""
                if item.action_type != 'move':
                    lookup_key = os.path.normpath(item.path).lower() if item.path else item.file_id
                    for it in self.queue.items:
                        if it.action_type == 'move':
                            if it.file_id == item.file_id or (it.path and os.path.normpath(it.path).lower() == lookup_key):
                                dst_folder = os.path.normpath(os.path.dirname(it.new_value))
                                subsequent_note = f"<br><br><span style='color: #17A2B8; font-weight: bold;'>📦 Transferência posterior:</span> Este arquivo foi movido na fila para a pasta: <b>{dst_folder}</b>"
                                break

                if item.action_type == 'move':
                    details = (
                        f"<b>Arquivo:</b> {item.file_name}<br>"
                        f"<b>Origem:</b> {item.old_value or item.path}<br>"
                        f"<b>Destino:</b> {item.new_value}<br>"
                        f"<b>Ação:</b> 📦 Mover arquivo para nova pasta"
                    )
                elif item.action_type == 'delete':
                    details = (
                        f"<b>Arquivo:</b> {item.file_name}<br>"
                        f"<b>Caminho:</b> {item.path}<br>"
                        f"<b>Ação:</b> 🗑️ Excluir arquivo do acervo"
                    )
                elif item.action_type == 'create_folder':
                    parent_dir = os.path.dirname(item.new_value or item.path)
                    details = (
                        f"<b>Nome da Nova Pasta:</b> {item.file_name}<br>"
                        f"<b>Pasta Pai (Local):</b> {parent_dir}<br>"
                        f"<b>Caminho Completo:</b> {item.new_value or item.path}<br>"
                        f"<b>Ação:</b> 📁🟢 Criar Nova Pasta no Google Drive e no Disco"
                    )
                elif item.action_type == 'rename':
                    details = (
                        f"<b>Arquivo Original:</b> {item.old_value or item.file_name}<br>"
                        f"<b>Novo Nome:</b> {item.new_value}<br>"
                        f"<b>Caminho:</b> {item.path}<br>"
                        f"<b>Ação:</b> ✏️ Renomear arquivo"
                        f"{subsequent_note}"
                    )
                else:
                    details = (
                        f"<b>Arquivo:</b> {item.file_name}<br>"
                        f"<b>Caminho:</b> {item.path}<br>"
                        f"<b>Ação:</b> {item.get_description_summary()}<br>"
                        f"<b>Descrição Anterior:</b> <i>{item.old_value or '(vazia)'}</i>"
                        f"{subsequent_note}"
                    )
                self.details_label.setText(details)

                # Emitir sinal para focar e iluminar o arquivo na árvore e no grid
                self.itemFocusRequested.emit(item)
                return
        self.details_label.setText("Selecione um item para ver o detalhamento da alteração.")

    def _on_selection_changed(self):
        selected_items = self.list_widget.selectedItems()
        count = len(selected_items)
        if count == 0:
            self.btn_execute_next.setText("▶️ Executar Próxima")
            self.btn_remove_selected.setText("🗑️ Remover Selecionado")
        elif count == 1:
            self.btn_execute_next.setText("▶️ Executar Selec.")
            self.btn_remove_selected.setText("🗑️ Remover Selecionado")
        else:
            self.btn_execute_next.setText(f"▶️ Executar Selec. ({count})")
            self.btn_remove_selected.setText(f"🗑️ Remover Selecionados ({count})")

    def _remove_selected_item(self):
        selected = self.list_widget.selectedItems()
        if selected:
            items_to_remove = []
            for it_widget in selected:
                try:
                    item = it_widget.data(Qt.ItemDataRole.UserRole)
                    if item:
                        items_to_remove.append(item)
                except RuntimeError:
                    pass
            if items_to_remove:
                self.queue.remove_items_batch(items_to_remove)

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

        reply = QMessageBox.question(
            self, "Confirmar Execução",
            f"Você tem certeza de que deseja executar todas as {total} alterações pendentes da fila?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        executed_count = 0
        errors = []
        executed_items = []

        progress = QProgressDialog("Preparando execução...", "Cancelar", 0, total, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()

        for idx, item in enumerate(list(self.queue.items)):
            if progress.wasCanceled():
                logging.info("Execução da fila cancelada pelo usuário.")
                break
            progress.setLabelText(f"Executando ({idx+1}/{total}):\n{item.file_name}")
            progress.setValue(idx)
            QCoreApplication.processEvents()

            try:
                self._execute_single_item(item)
                executed_count += 1
                executed_items.append(item)
            except Exception as e:
                logging.error(f"Erro ao executar item {item.file_name}: {e}")
                errors.append(f"{item.file_name}: {e}")

        progress.setValue(total)
        progress.close()

        if executed_items:
            self.queue.remove_items_batch(executed_items)

        if errors:
            QMessageBox.warning(
                self, "Execução Concluída com Avisos",
                f"{executed_count} alteração(ões) aplicada(s).\n\nErros:\n" + "\n".join(errors[:5])
            )
        else:
            try:
                # Mostrar mensagem não intrusiva de sucesso por 2 segundos na StatusBar principal
                self.parent().window().status_bar.showMessage(f"Sucesso! {executed_count} alteração(ões) aplicada(s).", 2000)
            except Exception:
                pass

        self.executionCompleted.emit(executed_count)
        self.accept()

    def _execute_next(self):
        if self.config_mgr.is_read_only() or self.queue.count() == 0:
            return

        selected_widgets = self.list_widget.selectedItems()
        if selected_widgets:
            target_items_set = {w.data(Qt.ItemDataRole.UserRole) for w in selected_widgets if w.data(Qt.ItemDataRole.UserRole)}
            items_to_execute = [it for it in self.queue.items if it in target_items_set]
        else:
            items_to_execute = [self.queue.items[0]]

        executed_count = 0
        errors = []
        executed_items = []
        
        total = len(items_to_execute)
        progress = None
        if total > 1:
            progress = QProgressDialog("Preparando execução...", "Cancelar", 0, total, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.setValue(0)
            progress.show()

        for idx, item in enumerate(items_to_execute):
            if progress:
                if progress.wasCanceled():
                    logging.info("Execução cancelada pelo usuário.")
                    break
                progress.setLabelText(f"Executando ({idx+1}/{total}):\n{item.file_name}")
                progress.setValue(idx)
                QCoreApplication.processEvents()

            try:
                self._execute_single_item(item)
                executed_items.append(item)
                executed_count += 1
            except Exception as e:
                logging.error(f"Erro ao executar item {item.file_name}: {e}")
                errors.append(f"{item.file_name}: {e}")

        if progress:
            progress.setValue(total)
            progress.close()

        if executed_items:
            self.queue.remove_items_batch(executed_items)

        if errors:
            QMessageBox.warning(self, "Aviso", f"{executed_count} item(ns) executado(s).\n\nErros:\n" + "\n".join(errors[:5]))
        self.executionCompleted.emit(executed_count)

    def _get_drive_file_id(self, item):
        fid = item.file_id
        if fid and ('/' in fid or '\\' in fid or fid[1:3] == ':\\'):
            # 1. Verificar se o banco de dados já possui o link direto gravado
            if self.db_indexer:
                try:
                    self.db_indexer.ensure_conn()
                    self.db_indexer.cursor.execute(
                        "SELECT webContentLink FROM files WHERE file_id = ? OR path = ? LIMIT 1",
                        (fid, item.path or fid)
                    )
                    row = self.db_indexer.cursor.fetchone()
                    if row and row[0] and '/d/' in row[0]:
                        extracted_id = row[0].split('/d/')[1].split('/')[0]
                        if extracted_id:
                            return extracted_id
                except Exception as e_db:
                    logging.debug(f"Erro ao buscar link no banco: {e_db}")

            if self.drive_service:
                try:
                    fpath = item.path or fid
                    clean_name = item.file_name.replace("'", "\\'")
                    parent_dir = os.path.dirname(fpath)
                    
                    # 2. Usar o resolvedor hierárquico à prova de maiúsculas/minúsculas e acentos
                    parent_drive_id = self._resolve_drive_folder_id_by_path(parent_dir)
                    if parent_drive_id:
                        current_drive = self.config_mgr.get_current_drive_id()
                        kwargs = {'supportsAllDrives': True, 'includeItemsFromAllDrives': True}
                        if current_drive:
                            kwargs['corpora'] = 'drive'
                            kwargs['driveId'] = current_drive
                        else:
                            kwargs['corpora'] = 'allDrives'

                        q_exact = f"name = '{clean_name}' and '{parent_drive_id}' in parents and trashed = false"
                        res_exact = self.drive_service.files().list(q=q_exact, fields='files(id, name, webViewLink)', **kwargs).execute()
                        exact_matches = res_exact.get('files', [])
                        
                        # Se não encontrar por nome exato (ex: case mismatch .jpg vs .JPG), faz busca normalizada
                        if not exact_matches:
                            q_all = f"'{parent_drive_id}' in parents and trashed = false"
                            res_all = self.drive_service.files().list(q=q_all, fields='files(id, name, webViewLink)', **kwargs).execute()
                            from src.database.search import SearchEngine
                            norm_fname = SearchEngine(None).normalize_text(item.file_name)
                            for cand in res_all.get('files', []):
                                if SearchEngine(None).normalize_text(cand.get('name', '')) == norm_fname:
                                    exact_matches = [cand]
                                    break

                        if exact_matches:
                            matched_id = exact_matches[0]['id']
                            wlink = exact_matches[0].get('webViewLink')
                            if wlink and self.db_indexer:
                                try:
                                    self.db_indexer.cursor.execute("UPDATE files SET webContentLink = ? WHERE file_id = ? OR path = ?", (wlink, fid, fpath))
                                    self.db_indexer.conn.commit()
                                except Exception:
                                    pass
                            return matched_id
                except Exception as e:
                    logging.error(f"Falha ao buscar ID do Drive para {item.file_name}: {e}")
                    return None
            return None
        return fid

    def _execute_single_item(self, item):
        if self.db_indexer:
            self.db_indexer.ensure_conn()
        drive_file_id = self._get_drive_file_id(item) if self.drive_service else None

        # 1. Processar alteração de tags / descrição
        if item.action_type in ('add_tags', 'remove_tags', 'set_description'):
            lookup_id = item.file_id or item.path
            canon_id = os.path.normcase(os.path.normpath(lookup_id)) if lookup_id else None
            
            # Buscar a descrição VIVA mais recente do banco SQLite no momento da execução (F11)
            current_live_desc = item.old_value or ''
            if self.db_indexer and canon_id:
                self.db_indexer.ensure_conn()
                self.db_indexer.cursor.execute(
                    "SELECT description FROM files WHERE file_id = ? OR path = ? LIMIT 1",
                    (canon_id, canon_id)
                )
                r_live = self.db_indexer.cursor.fetchone()
                if r_live and r_live[0] is not None:
                    current_live_desc = r_live[0]

            if item.action_type == 'add_tags':
                tags = [t.strip() for t in item.new_value.split(',') if t.strip()]
                existing_tags = [t.strip() for t in current_live_desc.split(',') if t.strip()]
                for t in tags:
                    if t not in existing_tags:
                        existing_tags.append(t)
                new_desc = ", ".join(existing_tags)
            elif item.action_type == 'remove_tags':
                tags_to_rem = [t.strip().lower() for t in item.new_value.split(',') if t.strip()]
                existing_tags = [t.strip() for t in current_live_desc.split(',') if t.strip()]
                existing_tags = [t for t in existing_tags if t.lower() not in tags_to_rem]
                new_desc = ", ".join(existing_tags)
            elif item.action_type == 'set_description':
                new_desc = item.new_value

            # Atualizar SEMPRE localmente no banco SQLite estritamente pela chave canônica única
            if self.db_indexer and canon_id:
                from src.database.search import SearchEngine
                norm_engine = SearchEngine(None)
                norm_desc = norm_engine.normalize_text(new_desc)
                self.db_indexer.update_description(canon_id, new_desc, commit=False)
                self.db_indexer.cursor.execute(
                    "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ?",
                    (new_desc, norm_desc, canon_id)
                )
                self.db_indexer.conn.commit()

            # Atualizar na API do Google Drive se o serviço de drive estiver ativo
            if self.drive_service and drive_file_id:
                try:
                    self.drive_service.files().update(
                        fileId=drive_file_id, 
                        body={'description': new_desc}, 
                        supportsAllDrives=True
                    ).execute()
                    logging.info(f"✅ Google Drive atualizado com sucesso: {item.file_name} -> {new_desc}")
                except Exception as e:
                    logging.error(f"Erro na API Drive ao atualizar desc {drive_file_id}: {e}")
                    raise e

        elif item.action_type == 'rename':
            new_name = item.new_value
            old_path = item.path or item.old_value
            if not old_path or not os.path.exists(old_path):
                logging.warning(f"Arquivo de origem para renomear não encontrado: {old_path}")
                return

            parent_dir = os.path.dirname(old_path)
            new_path = os.path.normpath(os.path.join(parent_dir, new_name))
            old_norm = os.path.normcase(os.path.normpath(old_path))
            new_norm = os.path.normcase(new_path)

            if old_norm != new_norm:
                # Checagem estrita de colisão (F3): nunca sobrescrever arquivo existente
                if os.path.exists(new_path):
                    raise RuntimeError(f"Já existe um arquivo ou pasta com o nome '{new_name}' em '{parent_dir}'. Renomeação abortada para evitar perda de dados.")

                from src.utils.utils import safe_move_file
                success, err_msg = safe_move_file(old_path, new_path, retries=3, delay=0.5)
                if not success:
                    raise RuntimeError(f"Erro ao renomear localmente: {err_msg}")
                logging.info(f"✅ Arquivo/pasta local renomeado: {old_path} -> {new_path}")

            # Atualizar no banco SQLite com chave canônica normcase(normpath)
            if self.db_indexer:
                self.db_indexer.cursor.execute(
                    "UPDATE files SET name = ?, name_normalized = ?, path = ?, file_id = ? WHERE file_id = ? OR path = ?", 
                    (new_name, new_name.lower(), new_norm, new_norm, old_norm, old_norm)
                )
                self.db_indexer.cursor.execute(
                    "UPDATE search_index SET name = ?, normalized_name = ?, file_id = ? WHERE file_id = ?",
                    (new_name, new_name.lower(), new_norm, old_norm)
                )

                # Se for diretório, atualizar recursivamente os caminhos dos arquivos filhos com ESCAPE (F12)
                if os.path.isdir(new_path) or os.path.isdir(old_path):
                    escaped_old = old_norm.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%')
                    self.db_indexer.cursor.execute(
                        "SELECT file_id, path, parentId FROM files WHERE path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'",
                        (f"{escaped_old}\\\\%", f"{escaped_old}/%")
                    )
                    child_rows = self.db_indexer.cursor.fetchall()
                    for c_fid, c_path, c_parent in child_rows:
                        if not c_path:
                            continue
                        rel = os.path.relpath(c_path, old_norm)
                        new_c_path = os.path.normcase(os.path.normpath(os.path.join(new_norm, rel)))
                        new_c_parent = os.path.normcase(os.path.normpath(os.path.dirname(new_c_path)))
                        self.db_indexer.cursor.execute(
                            "UPDATE files SET path = ?, parentId = ?, file_id = ? WHERE file_id = ? OR path = ?",
                            (new_c_path, new_c_parent, new_c_path, c_fid, c_path)
                        )
                        self.db_indexer.cursor.execute(
                            "UPDATE search_index SET file_id = ? WHERE file_id = ?",
                            (new_c_path, c_fid)
                        )

                self.db_indexer.conn.commit()

        elif item.action_type == 'move':
            src_path = item.old_value or item.path
            dst_path = item.new_value
            if not src_path or not os.path.exists(src_path):
                logging.warning(f"Arquivo de origem para mover não encontrado: {src_path}")
                return

            norm_src = os.path.normcase(os.path.normpath(src_path))
            norm_dst = os.path.normcase(os.path.normpath(dst_path))

            # 0. Proteção contra Loop Infinito (mover pasta para dentro de si mesma)
            if os.path.isdir(src_path):
                if norm_dst == norm_src or norm_dst.startswith(norm_src + os.sep):
                    raise RuntimeError(f"Não é permitido mover uma pasta para dentro de si mesma: {src_path} -> {dst_path}")

            # 1. Mover arquivo localmente no disco com proteção de File Lock / InSync
            if norm_src != norm_dst:
                dst_dir = os.path.dirname(dst_path)
                os.makedirs(dst_dir, exist_ok=True)
                
                # Se o arquivo destino já existe no local, resolve o conflito de nome
                if os.path.exists(dst_path):
                    base, ext = os.path.splitext(dst_path)
                    counter = 1
                    while True:
                        candidate_path = f"{base}_{counter}{ext}"
                        if not os.path.exists(candidate_path):
                            dst_path = candidate_path
                            norm_dst = os.path.normcase(os.path.normpath(dst_path))
                            break
                        counter += 1
                    item.new_value = dst_path
                    logging.warning(f"⚠️ Conflito detectado! Renomeando arquivo de destino para evitar sobreposição: {os.path.basename(dst_path)}")

                from src.utils.utils import safe_move_file
                success, err_msg = safe_move_file(src_path, dst_path, retries=3, delay=0.5)
                if not success:
                    raise RuntimeError(f"Falha ao mover arquivo local: {err_msg}")
                logging.info(f"✅ Arquivo local movido com sucesso: {src_path} -> {dst_path}")

            # 2. Atualizar no banco SQLite com chave canônica normcase(normpath)
            if self.db_indexer:
                new_parent_path = os.path.normcase(os.path.normpath(os.path.dirname(dst_path)))
                new_name = os.path.basename(dst_path)
                from src.database.search import SearchEngine
                norm_engine = SearchEngine(None)
                norm_name = norm_engine.normalize_text(new_name)
                self.db_indexer.cursor.execute(
                    "UPDATE files SET path = ?, parentId = ?, name = ?, name_normalized = ?, file_id = ? WHERE file_id = ? OR path = ?",
                    (norm_dst, new_parent_path, new_name, new_name.lower(), norm_dst, norm_src, norm_src)
                )
                self.db_indexer.cursor.execute(
                    "UPDATE search_index SET name = ?, normalized_name = ?, file_id = ? WHERE file_id = ?",
                    (new_name, norm_name, norm_dst, norm_src)
                )

                # Se for um diretório, atualizar recursivamente os caminhos de todos os arquivos e subpastas filhos
                if os.path.isdir(dst_path) or os.path.isdir(src_path):
                    escaped_src = norm_src.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%')
                    self.db_indexer.cursor.execute(
                        "SELECT file_id, path, parentId FROM files WHERE path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'",
                        (f"{escaped_src}\\\\%", f"{escaped_src}/%")
                    )
                    child_rows = self.db_indexer.cursor.fetchall()
                    for c_fid, c_path, c_parent in child_rows:
                        if not c_path:
                            continue
                        rel = os.path.relpath(c_path, norm_src)
                        new_c_path = os.path.normcase(os.path.normpath(os.path.join(norm_dst, rel)))
                        new_c_parent = os.path.normcase(os.path.normpath(os.path.dirname(new_c_path)))
                        self.db_indexer.cursor.execute(
                            "UPDATE files SET path = ?, parentId = ?, file_id = ? WHERE file_id = ? OR path = ?",
                            (new_c_path, new_c_parent, new_c_path, c_fid, c_path)
                        )
                        self.db_indexer.cursor.execute(
                            "UPDATE search_index SET file_id = ? WHERE file_id = ?",
                            (new_c_path, c_fid)
                        )

                self.db_indexer.conn.commit()

        elif item.action_type == 'delete':
            del_path = item.path or item.old_value
            if not del_path:
                return

            # 1. Enviar para a Lixeira do Windows nativa
            if os.path.exists(del_path):
                from src.utils.utils import send_to_recycle_bin
                moved_to_trash = send_to_recycle_bin(del_path)
                if not moved_to_trash:
                    raise RuntimeError(f"Não foi possível mover para a Lixeira do Windows: '{del_path}'. Exclusão abortada por segurança.")
                logging.info(f"🗑️ Arquivo/pasta local movido para a Lixeira do Windows: '{del_path}'")

            # 2. Apagar no banco de dados SQLite local (inclusive arquivos filhos se for pasta) com ESCAPE (F12)
            if self.db_indexer:
                norm_del = os.path.normcase(os.path.normpath(del_path))
                escaped_del = norm_del.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%')
                self.db_indexer.cursor.execute(
                    "DELETE FROM files WHERE file_id = ? OR path = ? OR path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' OR parentId = ? OR parentId LIKE ? ESCAPE '\\' OR parentId LIKE ? ESCAPE '\\'",
                    (norm_del, norm_del, f"{escaped_del}\\\\%", f"{escaped_del}/%", norm_del, f"{escaped_del}\\\\%", f"{escaped_del}/%")
                )
                self.db_indexer.cursor.execute(
                    "DELETE FROM search_index WHERE file_id = ? OR file_id LIKE ? ESCAPE '\\' OR file_id LIKE ? ESCAPE '\\'",
                    (norm_del, f"{escaped_del}\\\\%", f"{escaped_del}/%")
                )
                self.db_indexer.conn.commit()

        elif item.action_type == 'create_folder':
            target_folder_path = item.new_value or item.path
            if target_folder_path:
                norm_p = os.path.normcase(os.path.normpath(target_folder_path))
                os.makedirs(norm_p, exist_ok=True)
                if self.db_indexer:
                    now = int(time.time())
                    self.db_indexer.cursor.execute(
                        "INSERT OR REPLACE INTO files (file_id, name, path, mimeType, source, description, parentId, createdTime, modifiedTime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (norm_p, os.path.basename(norm_p), norm_p, 'folder', 'local', '', os.path.dirname(norm_p), now, now)
                    )
                    self.db_indexer.conn.commit()
                    if parent_drive_id:
                        folder_metadata = {
                            'name': os.path.basename(norm_p),
                            'mimeType': 'application/vnd.google-apps.folder',
                            'parents': [parent_drive_id]
                        }
                        res = self.drive_service.files().create(
                            body=folder_metadata,
                            fields='id',
                            supportsAllDrives=True
                        ).execute()
                        new_folder_id = res.get('id')
                        if not hasattr(self, '_drive_folder_id_cache'):
                            self._drive_folder_id_cache = {}
                        self._drive_folder_id_cache[norm_p.lower()] = new_folder_id
                        if self.db_indexer and new_folder_id:
                            self.db_indexer.cursor.execute(
                                "UPDATE files SET file_id = ? WHERE path = ?",
                                (new_folder_id, norm_p)
                            )
                            self.db_indexer.conn.commit()

        elif item.action_type == 'rotate_90':
            if item.path and os.path.exists(item.path):
                from PIL import Image
                with Image.open(item.path) as img:
                    img = img.rotate(-90, expand=True)
                    img.save(item.path)

    def _resolve_drive_folder_id_by_path(self, full_folder_path, create_if_missing=False):
        """
        Resolve o fileId exato da pasta no Google Drive navegando nível por nível
        a partir da raiz do Drive Compartilhado, evitando ambiguidades entre anos e pastas com nomes iguais.
        """
        if not self.drive_service or not full_folder_path:
            return None

        norm = os.path.normpath(full_folder_path).lower()
        if not hasattr(self, '_drive_folder_id_cache'):
            self._drive_folder_id_cache = {}
            
        if norm in self._drive_folder_id_cache:
            return self._drive_folder_id_cache[norm]

        from src.utils.utils import resolve_drive_folder_id_by_path
        drive_id = self.config_mgr.get_current_drive_id() or '0AOB-ISqqs76_Uk9PVA'
        folder_id = resolve_drive_folder_id_by_path(self.drive_service, full_folder_path, drive_id, create_if_missing=create_if_missing)
        if folder_id:
            self._drive_folder_id_cache[norm] = folder_id
        return folder_id

    def _export_queue(self):
        from PyQt6.QtWidgets import QFileDialog
        import json
        if self.queue.count() == 0:
            return
        
        filepath, _ = QFileDialog.getSaveFileName(self, "Exportar Fila", "", "JSON Files (*.json)")
        if filepath:
            try:
                data = []
                for it in self.queue.items:
                    data.append({
                        'file_id': it.file_id,
                        'file_name': it.file_name,
                        'path': it.path,
                        'action_type': it.action_type,
                        'old_value': it.old_value,
                        'new_value': it.new_value,
                        'timestamp': it.timestamp
                    })
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                QMessageBox.information(self, "Sucesso", "Fila exportada com sucesso.")
            except Exception as e:
                QMessageBox.warning(self, "Erro", f"Falha ao exportar: {e}")

    def _import_queue(self):
        from PyQt6.QtWidgets import QFileDialog
        import json
        
        filepath, _ = QFileDialog.getOpenFileName(self, "Importar Fila", "", "JSON Files (*.json)")
        if filepath:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                count = 0
                for d in data:
                    it = StagingItem(
                        d.get('file_id'), d.get('file_name', ''), d.get('path', ''),
                        d.get('action_type'), d.get('old_value', ''), d.get('new_value', '')
                    )
                    it.timestamp = d.get('timestamp', time.time())
                    self.queue.add_item(it)
                    count += 1
                
                QMessageBox.information(self, "Sucesso", f"{count} itens importados com sucesso.")
            except Exception as e:
                QMessageBox.warning(self, "Erro", f"Falha ao importar: {e}")
