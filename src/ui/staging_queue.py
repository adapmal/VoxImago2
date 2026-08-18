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
            dst_folder = os.path.basename(os.path.dirname(self.new_value))
            return f"📦 Mover para a pasta: '{dst_folder}'"
        elif self.action_type == 'delete':
            return f"🗑️ Excluir arquivo"
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
                self.mode_info_label.setStyleSheet("color: #856404;")
            else:
                self.mode_info_label.setText("☁️ Alvo: Produção Oficial (Google Drive)")
                self.mode_info_label.setStyleSheet("color: #28A745;")
            self.btn_execute.setEnabled(self.queue.count() > 0)
            self.btn_execute_next.setEnabled(self.queue.count() > 0)

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
                elif item.action_type == 'rename':
                    details = (
                        f"<b>Arquivo Original:</b> {item.old_value or item.file_name}<br>"
                        f"<b>Novo Nome:</b> {item.new_value}<br>"
                        f"<b>Caminho:</b> {item.path}<br>"
                        f"<b>Ação:</b> ✏️ Renomear arquivo"
                    )
                else:
                    details = (
                        f"<b>Arquivo:</b> {item.file_name}<br>"
                        f"<b>Caminho:</b> {item.path}<br>"
                        f"<b>Ação:</b> {item.get_description_summary()}<br>"
                        f"<b>Descrição Anterior:</b> <i>{item.old_value or '(vazia)'}</i>"
                    )
                self.details_label.setText(details)

                # Emitir sinal para focar e iluminar o arquivo na árvore e no grid
                self.itemFocusRequested.emit(item)
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
            
        errors = []
        # Remove confirmation dialog as requested
        # dest_name = "Sandbox (L:\\Drives Compartilhados\\_TestesBanco)" if self.config_mgr.is_sandbox() else "Google Drive Oficial"
        # reply = QMessageBox.question(...)

        executed_count = 0
        errors = []

        for item in list(self.queue.items):
            try:
                self._execute_single_item(item)
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

        item = self.queue.items[0]
        try:
            self._execute_single_item(item)
            self.queue.remove_item(item)
            self.executionCompleted.emit(1)
        except Exception as e:
            logging.error(f"Erro ao executar proxima: {e}")
            QMessageBox.warning(self, "Erro", f"Falha ao executar o item: {e}")

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
                    import os
                    clean_name = item.file_name.replace("'", "\\'")
                    fpath = item.path or fid
                    current_drive = self.config_mgr.get_current_drive_id()

                    kwargs = {
                        'supportsAllDrives': True,
                        'includeItemsFromAllDrives': True,
                    }
                    if current_drive:
                        kwargs['corpora'] = 'drive'
                        kwargs['driveId'] = current_drive
                    else:
                        kwargs['corpora'] = 'allDrives'

                    # 2. Resolução Top-Down exata desde o Ano / Raiz
                    if fpath and current_drive:
                        try:
                            norm = os.path.normpath(fpath).replace('\\', '/')
                            parts = norm.split('/')
                            root_markers = ['banco de imagens', '_testesbanco']
                            start_idx = -1
                            for marker in root_markers:
                                for idx, part in enumerate(parts):
                                    if part.lower() == marker:
                                        start_idx = idx + 1
                                        break
                                if start_idx != -1:
                                    break
                                    
                            if start_idx == -1:
                                for idx, part in enumerate(parts):
                                    if part.isdigit() and len(part) == 4:
                                        start_idx = idx
                                        break

                            if start_idx != -1 and start_idx < len(parts):
                                rel_segments = parts[start_idx:]
                                folder_segments = rel_segments[:-1]
                                curr_pid = current_drive
                                path_ok = True
                                for seg in folder_segments:
                                    clean_seg = seg.replace("'", "\\'")
                                    q_seg = f"name = '{clean_seg}' and '{curr_pid}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                                    res_seg = self.drive_service.files().list(q=q_seg, fields='files(id, name)', **kwargs).execute()
                                    seg_folders = res_seg.get('files', [])
                                    if seg_folders:
                                        curr_pid = seg_folders[0]['id']
                                    else:
                                        path_ok = False
                                        break
                                        
                                if path_ok and curr_pid:
                                    q_exact = f"name = '{clean_name}' and '{curr_pid}' in parents and trashed = false"
                                    res_exact = self.drive_service.files().list(q=q_exact, fields='files(id, name, webViewLink)', **kwargs).execute()
                                    exact_matches = res_exact.get('files', [])
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
                        except Exception as e_top:
                            logging.debug(f"Falha na resolução top-down no staging: {e_top}")

                    # 3. Fallback: Desambiguação por pasta pai direta
                    parent_dir_name = os.path.basename(os.path.dirname(os.path.normpath(fpath)))
                    if parent_dir_name:
                        clean_pname = parent_dir_name.replace("'", "\\'")
                        q_folder = f"name = '{clean_pname}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                        try:
                            res_f = self.drive_service.files().list(q=q_folder, fields='files(id, name)', **kwargs).execute()
                            folders = res_f.get('files', [])
                            if folders:
                                folder_ids = [f['id'] for f in folders[:5]]
                                parents_cond = " or ".join([f"'{f_id}' in parents" for f_id in folder_ids])
                                q_file = f"name = '{clean_name}' and ({parents_cond}) and trashed = false"
                                res_file = self.drive_service.files().list(q=q_file, fields='files(id, name, webViewLink)', **kwargs).execute()
                                exact_files = res_file.get('files', [])
                                if exact_files:
                                    return exact_files[0]['id']
                        except Exception as e_f:
                            logging.debug(f"Falha na busca por pasta pai no staging: {e_f}")

                except Exception as e:
                    logging.error(f"Falha ao buscar ID do Drive para {item.file_name}: {e}")
                    return None
            return None
        return fid

    def _execute_single_item(self, item):
        drive_file_id = self._get_drive_file_id(item) if self.drive_service else None

        # 1. Processar alteração de tags / descrição
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

            # Atualizar SEMPRE localmente no banco SQLite
            if self.db_indexer and item.file_id:
                self.db_indexer.update_description(item.file_id, new_desc, commit=True)
                if item.path and item.path != item.file_id:
                    self.db_indexer.update_description(item.path, new_desc, commit=True)

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
            if self.db_indexer and item.file_id:
                self.db_indexer.cursor.execute("UPDATE files SET name = ?, name_normalized = ? WHERE file_id = ?", (new_name, new_name.lower(), item.file_id))
                self.db_indexer.conn.commit()
            if self.drive_service and drive_file_id:
                self.drive_service.files().update(
                    fileId=drive_file_id, 
                    body={'name': new_name}, 
                    supportsAllDrives=True
                ).execute()

        elif item.action_type == 'move':
            src_path = item.old_value or item.path
            dst_path = item.new_value

            # 1. Mover arquivo localmente no disco
            if src_path and os.path.exists(src_path):
                import shutil
                dst_dir = os.path.dirname(dst_path)
                os.makedirs(dst_dir, exist_ok=True)
                if os.path.normpath(src_path) != os.path.normpath(dst_path):
                    shutil.move(src_path, dst_path)
                    logging.info(f"✅ Arquivo local movido com sucesso: {src_path} -> {dst_path}")

            # 2. Atualizar no banco SQLite
            if self.db_indexer and item.file_id:
                new_parent_path = os.path.dirname(dst_path)
                self.db_indexer.cursor.execute(
                    "UPDATE files SET path = ?, parentId = ? WHERE file_id = ? OR path = ?",
                    (dst_path, new_parent_path, item.file_id, src_path)
                )
                self.db_indexer.conn.commit()

            # 3. Mover na API do Google Drive se o serviço estiver ativo
            if self.drive_service and drive_file_id:
                try:
                    f_info = self.drive_service.files().get(
                        fileId=drive_file_id, fields="parents", supportsAllDrives=True
                    ).execute()
                    current_parents = ",".join(f_info.get('parents', []))

                    dst_folder_name = os.path.basename(os.path.dirname(dst_path))
                    dst_parent_folder_name = os.path.basename(os.path.dirname(os.path.dirname(dst_path)))
                    target_parent_id = self._find_drive_folder_id(dst_folder_name, dst_parent_folder_name)

                    if target_parent_id:
                        self.drive_service.files().update(
                            fileId=drive_file_id,
                            addParents=target_parent_id,
                            removeParents=current_parents,
                            supportsAllDrives=True
                        ).execute()
                        logging.info(f"✅ Google Drive: arquivo {item.file_name} movido para a pasta '{dst_folder_name}'")
                except Exception as e:
                    logging.error(f"Erro ao mover arquivo no Google Drive: {e}")

        elif item.action_type == 'delete':
            if self.db_indexer and item.file_id:
                self.db_indexer.cursor.execute("DELETE FROM files WHERE file_id = ?", (item.file_id,))
                self.db_indexer.conn.commit()
            if self.drive_service and drive_file_id:
                self.drive_service.files().update(
                    fileId=drive_file_id, 
                    body={'trashed': True}, 
                    supportsAllDrives=True
                ).execute()

        elif item.action_type == 'rotate_90':
            if item.path and os.path.exists(item.path):
                from PIL import Image
                with Image.open(item.path) as img:
                    img = img.rotate(-90, expand=True)
                    img.save(item.path)

    def _find_drive_folder_id(self, folder_name, parent_folder_name=None):
        """Localiza o fileId de uma pasta no Google Drive pelo nome e opcionalmente pelo pai."""
        if not self.drive_service or not folder_name:
            return None
        try:
            clean_name = folder_name.replace("'", "\\'")
            query = f"name='{clean_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            kwargs = {
                'q': query,
                'supportsAllDrives': True,
                'includeItemsFromAllDrives': True,
                'fields': "files(id, name, parents)"
            }
            current_drive = self.config_mgr.get_current_drive_id()
            if current_drive:
                kwargs['corpora'] = 'drive'
                kwargs['driveId'] = current_drive
            else:
                kwargs['corpora'] = 'allDrives'
                
            res = self.drive_service.files().list(**kwargs).execute()
            folders = res.get('files', [])
            if not folders:
                return None
            if len(folders) == 1 or not parent_folder_name:
                return folders[0]['id']

            for f in folders:
                for pid in f.get('parents', []):
                    try:
                        p_info = self.drive_service.files().get(
                            fileId=pid, fields="name", supportsAllDrives=True
                        ).execute()
                        if p_info.get('name') == parent_folder_name:
                            return f['id']
                    except Exception:
                        pass
            return folders[0]['id']
        except Exception as e:
            logging.error(f"Erro ao buscar ID da pasta de destino no Drive: {e}")
            return None

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
