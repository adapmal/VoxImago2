'''
Fila de Revisão de Alterações (Staging Queue) para o VoxImago v2.1
Permite acumular alterações em lote, visualizar o preview de modificações (Diff)
e realizar a execução segura no Google Drive e Banco de Dados.
'''

import os
import time
import logging
from pathlib import Path
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QWidget, QFrame, QSplitter,
    QAbstractItemView, QPlainTextEdit, QProgressBar, QListView
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer, QThread
from src.utils.queue_store import QueueStore, atomic_json, validated_records
from src.utils.config_manager import ConfigManager
from src.utils.staging_schema import (
    MAX_QUEUE_FILE_BYTES,
    MAX_QUEUE_ITEMS,
    StagingItem,
    physical_item_path,
)

QUEUE_CACHE_FILE = os.path.join('config', 'staging_queue.json')


class _StagingSignals(QObject):
    queueChanged = pyqtSignal(int)  # Emitido com o número total de itens pendentes
    itemAdded = pyqtSignal(object)
    itemRemoved = pyqtSignal(object)
    cleared = pyqtSignal()


class QueueAutosaveWorker(QThread):
    failed = pyqtSignal(str)

    def __init__(self, store):
        super().__init__()
        self.store = store

    def run(self):
        try:
            self.store.autosave()
        except Exception as exc:
            self.failed.emit(str(exc))


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
            cls._instance.busy = False
            cls._instance.load_error = None
            cls._instance._autosave_worker = None
            cls._instance._autosave_pending = False
            cls._instance.store = QueueStore(os.path.join('data', 'staging_queue.db'), QUEUE_CACHE_FILE)
            cls._instance._load_from_disk()
            cls._instance.timer = QTimer(cls._instance.signals)
            cls._instance.timer.setInterval(5 * 60 * 1000)
            cls._instance.timer.timeout.connect(cls._instance.request_autosave)
            cls._instance.timer.start()
            if cls._instance.load_error:
                QTimer.singleShot(0, cls._instance._show_load_error)
        return cls._instance

    def __init__(self, *args, **kwargs):
        pass

    def _save_to_disk(self):
        try:
            if self.load_error:
                raise ValueError(self.load_error)
            self.store.save([it.to_dict() for it in self.items])
            self._tag_index = None
            return True
        except Exception as e:
            logging.error(f"Erro ao salvar fila no disco: {e}")
            if not self.load_error:
                self.items = [StagingItem.from_dict(row) for row in self.store.load()]
            self._tag_index = None
            QMessageBox.critical(None, 'Fila não salva', str(e))
            return False

    def _load_from_disk(self):
        self._tag_index = None
        try:
            self.items = [StagingItem.from_dict(row) for row in self.store.initialize()]
        except Exception as exc:
            self.load_error = str(exc)
            logging.exception('Fila preservada no disco, mas não pôde ser carregada.')

    def _show_load_error(self):
        QMessageBox.critical(None, 'Recuperação da fila necessária',
            'Não foi possível carregar a fila. Novas edições estão bloqueadas para preservar os dados.\n'
            'Abra a Fila e use Recuperar autosave.\n\n' + self.load_error)

    def can_edit(self):
        if self.busy or self.load_error:
            QMessageBox.warning(None, 'Fila indisponível', self.load_error or 'Aguarde a operação atual da fila terminar.')
            return False
        return True

    def request_autosave(self):
        if self.busy or self.load_error or (self._autosave_worker and self._autosave_worker.isRunning()):
            self._autosave_pending = True
            return
        self._autosave_pending = False
        self._autosave_worker = QueueAutosaveWorker(self.store)
        self._autosave_worker.failed.connect(lambda error: QMessageBox.warning(None, 'Falha no autosave', error))
        self._autosave_worker.start()

    def release(self):
        self.busy = False
        if self._autosave_pending:
            QTimer.singleShot(0, self.request_autosave)

    def effective_description(self, file_item):
        from src.services.batch_tags import _identity_key, effective_description
        if self._tag_index is None:
            self._tag_index = {}
            for item in self.items:
                if item.action_type in ('set_description', 'add_tags', 'remove_tags'):
                    for val in (item.file_id, item.path):
                        k = _identity_key(val)
                        if k:
                            self._tag_index.setdefault(k, []).append(item)
        fid = file_item.get('file_id') or file_item.get('id')
        fpath = physical_item_path(file_item)
        ops = self._tag_index.get(_identity_key(fid)) or (self._tag_index.get(_identity_key(fpath)) if fpath else None) or []
        return effective_description(file_item.get('description', '') or '', ops)

    @staticmethod
    def _get_queue_records(data):
        # Lista simples = formato usado por todas as versoes anteriores.
        if isinstance(data, list):
            records = data
        # Envelope opcional para uma futura evolucao do formato.
        elif isinstance(data, dict) and isinstance(data.get('items'), list):
            records = data['items']
        else:
            raise ValueError('Formato da fila invalido: esperada uma lista de itens.')
        if len(records) > MAX_QUEUE_ITEMS:
            raise ValueError(f'Fila excede o limite de {MAX_QUEUE_ITEMS} itens.')
        return records

    def add_item(self, item):
        if not self.can_edit():
            return False
        try:
            item.validate()
        except Exception as exc:
            logging.error(f"Item recusado pela validacao da fila: {exc}")
            return False

        from src.services.contextual_queue import queue_conflict, ACTIVE
        conflict = queue_conflict(item, self.items)
        if conflict:
            QMessageBox.warning(None, 'Conflito na fila', conflict)
            return False
        if any(it.file_id == item.file_id and it.execution_state in ACTIVE for it in self.items):
            QMessageBox.warning(None, 'Arquivo em confirmação', 'Aguarde a confirmação no Drive antes de preparar novas ações neste arquivo.')
            return False

        # 2. Se for 'move':
        if item.action_type == 'move':
            # Se o arquivo já está marcado para exclusão definitiva (delete), ignora o mover
            if any(it.file_id == item.file_id and it.action_type == 'delete' for it in self.items):
                return
            # Substitui 'move' anterior
            self.items = [it for it in self.items if not (it.file_id == item.file_id and it.action_type == 'move')]

        # 3. Se for 'rename', substitui 'rename' anterior
        elif item.action_type == 'rename':
            self.items = [it for it in self.items if not (it.file_id == item.file_id and it.action_type == 'rename')]

        self.items.append(item)
        if not self._save_to_disk():
            self.queueChanged.emit(len(self.items))
            return False
        self.itemAdded.emit(item)
        self.queueChanged.emit(len(self.items))
        return True

    def add_batch_tags(self, files_list, tags_to_add="", tags_to_remove="", db_path=None):
        if not self.can_edit():
            return 0
        from src.services.batch_tags import (
            ACTIVE_STATES,
            _identity_key,
            parse_delimited_tags,
            prepare_batch_with_stats,
        )
        added = parse_delimited_tags(tags_to_add) if tags_to_add else []
        removed = {t.lower() for t in parse_delimited_tags(tags_to_remove)} if tags_to_remove else set()
        resolved_db = db_path or str(Path(self.store.path).resolve().parent / 'file_index.db')
        if Path(resolved_db).exists():
            previous_items = list(self.items)
            try:
                records, _partner_count, changed_count = prepare_batch_with_stats(
                    resolved_db,
                    files_list,
                    [it.to_dict() for it in self.items],
                    added,
                    removed,
                    lambda: False,
                    lambda _d, _t: None,
                )
                if records is not None:
                    self.items = [StagingItem.from_dict(r) for r in records]
                    self._tag_index = None
                    saved = self._save_to_disk()
                    if not saved and self.load_error:
                        self.items = previous_items
                    self.queueChanged.emit(len(self.items))
                    return changed_count if saved else 0
            except ValueError as exc:
                QMessageBox.warning(None, 'Lote não aplicado', str(exc))
                return 0
            except Exception as exc:
                logging.warning(f"Fallback em add_batch_tags (usando modo simples): {exc}")
                self.items = previous_items

        from src.services.raw_tag_sync import is_raw_filename, RAW_TAG_LABEL, RAW_ONLY_TAGS_FOLDED
        protected_keys = {
            _identity_key(v)
            for it in self.items
            if it.execution_state in ACTIVE_STATES or it.action_type == 'delete'
            for v in (it.file_id, it.path) if v
        }
        previous_items = list(self.items)
        count = 0
        for file_item in files_list:
            fid = file_item.get('file_id') or file_item.get('id')
            fname = file_item.get('name', 'Sem nome')
            fpath = physical_item_path(file_item)
            if {_identity_key(v) for v in (fid, fpath) if v} & protected_keys:
                continue
            old_desc = file_item.get('description', '')
            is_raw = is_raw_filename(fpath or fname)

            eff_add = list(added)
            if is_raw and not any(t.lower() in RAW_ONLY_TAGS_FOLDED for t in eff_add) and RAW_TAG_LABEL.lower() not in removed:
                eff_add.append(RAW_TAG_LABEL)
            if not is_raw:
                eff_add = [t for t in eff_add if t.lower() not in RAW_ONLY_TAGS_FOLDED]

            if eff_add:
                item_add = StagingItem(fid, fname, fpath, 'add_tags', old_value=old_desc, new_value=', '.join(eff_add))
                self.items.append(item_add)
                count += 1

            if removed:
                item_rem = StagingItem(fid, fname, fpath, 'remove_tags', old_value=old_desc, new_value=', '.join(parse_delimited_tags(tags_to_remove)))
                self.items.append(item_rem)
                count += 1

        self._tag_index = None
        saved = self._save_to_disk()
        if not saved and self.load_error:
            self.items = previous_items
        self.queueChanged.emit(len(self.items))
        return count if saved else 0

    def _cleanup_staged_folder(self, item):
        # Pastas pendentes são virtuais. Nunca apagar uma pasta real ao cancelar,
        # inclusive placeholders físicos deixados por versões anteriores.
        pass

    def remove_item(self, item, discard_placeholder=False):
        if not self.can_edit():
            return
        if item in self.items:
            if discard_placeholder:
                self._cleanup_staged_folder(item)
            self.items.remove(item)
            if not self._save_to_disk():
                return False
            self.itemRemoved.emit(item)
            self.queueChanged.emit(len(self.items))

    def remove_items_batch(self, items_to_remove, discard_placeholder=False):
        if not self.can_edit():
            return
        removed = []
        for item in items_to_remove:
            if item in self.items:
                if discard_placeholder:
                    self._cleanup_staged_folder(item)
                self.items.remove(item)
                removed.append(item)
        if removed:
            if not self._save_to_disk():
                return False
            for item in removed:
                self.itemRemoved.emit(item)
            self.queueChanged.emit(len(self.items))

    def clear(self, discard_placeholder=True):
        if not self.can_edit():
            return
        if discard_placeholder:
            for item in list(self.items):
                self._cleanup_staged_folder(item)
        self.items.clear()
        if not self._save_to_disk():
            return False
        self.cleared.emit()
        self.queueChanged.emit(len(self.items))

    def count(self):
        return len(self.items)

    def replace_description(self, item, matching_ids):
        items = [item] if item else []
        return self.replace_descriptions(items, matching_ids)

    def replace_descriptions(self, items, matching_ids):
        if not self.can_edit():
            return False
        from src.services.batch_tags import ACTIVE_STATES, _identity_key
        norm_matching = {_identity_key(m) for m in (matching_ids or ()) if m}
        # Não sobrescrever arquivo que já tenha operação ativa na fila
        for old in self.items:
            if old.execution_state in ACTIVE_STATES:
                old_keys = {_identity_key(v) for v in (old.file_id, old.path) if v}
                if old_keys & norm_matching:
                    QMessageBox.warning(
                        None,
                        'Arquivo em confirmação',
                        'Aguarde a confirmação no Drive antes de preparar novas ações neste arquivo.',
                    )
                    return False

        previous_items = list(self.items)
        self.items = [
            old for old in self.items
            if not (
                old.action_type in ('set_description', 'add_tags', 'remove_tags')
                and old.execution_state not in ACTIVE_STATES
                and (
                    _identity_key(old.file_id) in norm_matching
                    or (old.path and _identity_key(old.path) in norm_matching)
                )
            )
        ]
        for item in items:
            if item:
                self.items.append(item)
        self._tag_index = None
        saved = self._save_to_disk()
        if not saved and self.load_error:
            self.items = previous_items
        self.queueChanged.emit(len(self.items))
        return saved

    def complete_execution(self, item):
        self.store.complete_execution(item.operation_id)
        self.items = [old for old in self.items if old.operation_id != item.operation_id]
        self._tag_index = None


class QueueExecutionReportDialog(QDialog):
    """
    Janela expansível e rolável para exibição completa de erros de execução da fila.
    Permite selecionar, copiar todos os erros e abrir a pasta de logs.
    """
    def __init__(self, parent=None, executed_count=0, errors=None):
        super().__init__(parent)
        self.setWindowTitle("⚠️ Relatório de Execução da Fila")
        self.setMinimumSize(680, 450)
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Cabeçalho com ícones e status
        err_count = len(errors or [])
        header_text = (
            f"<h3 style='margin: 0; padding-bottom: 4px;'>📊 Execução da Fila Concluída com Avisos</h3>"
            f"<b>✅ {executed_count} alteração(ões) aplicada(s) com sucesso.</b><br>"
            f"<span style='color: #ff5555; font-weight: bold;'>❌ {err_count} alteração(ões) encontraram erros e permaneceram salvas na fila:</span>"
        )
        lbl_header = QLabel(header_text)
        lbl_header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(lbl_header)

        # Caixa de texto de erros rolável e copiável
        self.text_errors = QPlainTextEdit()
        self.text_errors.setReadOnly(True)
        
        error_lines = []
        for i, err in enumerate(errors or [], 1):
            error_lines.append(f"[{i}] {err}")
        
        self.raw_error_text = "\n".join(error_lines)
        self.text_errors.setPlainText(self.raw_error_text)
        self.text_errors.setStyleSheet("""
            QPlainTextEdit {
                background-color: #1e1e1e;
                color: #f8f8f2;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 8px;
            }
        """)
        layout.addWidget(self.text_errors)

        # Botões de Ação
        btn_layout = QHBoxLayout()
        
        self.lbl_copied = QLabel("")
        self.lbl_copied.setStyleSheet("color: #50fa7b; font-weight: bold;")
        btn_layout.addWidget(self.lbl_copied)
        
        btn_layout.addStretch()

        btn_copy = QPushButton("📋 Copiar Todos os Erros")
        btn_copy.clicked.connect(self._copy_to_clipboard)
        btn_layout.addWidget(btn_copy)

        btn_logs = QPushButton("📁 Abrir Pasta de Logs")
        btn_logs.clicked.connect(self._open_logs_folder)
        btn_layout.addWidget(btn_logs)

        btn_close = QPushButton("Fechar")
        btn_close.setDefault(True)
        btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(btn_close)

        layout.addLayout(btn_layout)

    def _copy_to_clipboard(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.raw_error_text)
        self.lbl_copied.setText("✅ Copiado para a área de transferência!")
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(3000, lambda: self.lbl_copied.setText(""))

    def _open_logs_folder(self):
        import subprocess
        log_dir = os.path.abspath("logs")
        os.makedirs(log_dir, exist_ok=True)
        try:
            os.startfile(log_dir)
        except Exception:
            try:
                subprocess.Popen(["explorer", log_dir])
            except Exception:
                pass


class StagingQueueDialog(QDialog):
    executionCompleted = pyqtSignal(int)  # Emitido com o número de alterações aplicadas
    executionStarted = pyqtSignal()
    executionFinished = pyqtSignal()
    itemFocusRequested = pyqtSignal(object)  # (StagingItem) Solicitado foco no arquivo na interface principal

    def __init__(self, parent=None, drive_service=None, db_indexer=None):
        super().__init__(parent)
        self.queue = StagingQueue()
        self.drive_service = drive_service
        self.db_indexer = db_indexer
        self.config_mgr = ConfigManager()
        self._execution_active = False
        self._context_worker = None
        self._raw_sync_worker = None
        self._list_loading = False
        self._list_timer = QTimer(self)
        self._list_timer.setInterval(1)
        self._list_timer.timeout.connect(self._populate_list_batch)

        self.setWindowTitle("📋 Fila de Revisão de Alterações (Staging Queue)")
        self.setWindowFlags(Qt.WindowType.Window)  # Janela independente destacável
        self.setMinimumSize(780, 520)

        self._init_ui()
        self.queue.queueChanged.connect(lambda c: self._populate_list() if self.isVisible() else None)
        self.recovery_button = QPushButton('Recuperar autosave…', self)
        self.recovery_button.clicked.connect(self._recover_autosave)
        self.layout().addWidget(self.recovery_button)

    def _recover_autosave(self):
        from PyQt6.QtWidgets import QFileDialog
        if self.queue.busy or (self.queue._autosave_worker and self.queue._autosave_worker.isRunning()):
            QMessageBox.warning(self, 'Fila ocupada', 'Aguarde a operação atual terminar.')
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Escolher autosave (confira a data)',
            str(self.queue.store.backup_dir.resolve()), 'Autosaves (*.json)')
        if not path:
            return
        try:
            from src.utils.queue_store import read_queue_json
            records = read_queue_json(path)
            answer = QMessageBox.question(self, 'Recuperar fila',
                f'Restaurar {len(records):,} operações desta cópia? A fila atual será substituída. '
                'Operações já executadas serão filtradas; resultados incertos continuarão bloqueados.')
            if answer != QMessageBox.StandardButton.Yes:
                return
            if not self.queue.load_error:
                self.queue.store.autosave()
            records = self.queue.store.restore(path, records=records)
            self.queue.load_error = None
            self.queue.items = [StagingItem.from_dict(row) for row in records]
            self.queue._tag_index = None
            self.queue.queueChanged.emit(self.queue.count())
        except Exception as exc:
            QMessageBox.critical(self, 'Recuperação não realizada', str(exc))

    def showEvent(self, event):
        super().showEvent(event)
        self._populate_list()

    def hideEvent(self, event):
        # A reabertura começa com uma nova visão da fila, nunca com callbacks antigos.
        self._list_timer.stop()
        self._list_items = ()
        self._move_map = {}
        super().hideEvent(event)

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

        self.list_progress = QProgressBar(self)
        self.list_progress.setAccessibleName("Progresso da montagem da fila")
        self.list_progress.hide()
        layout.addWidget(self.list_progress)

        self.list_widget = QListWidget()
        self.list_widget.setLayoutMode(QListView.LayoutMode.Batched)
        self.list_widget.setBatchSize(200)
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

        context_controls = QHBoxLayout()
        self.btn_sync_raw = QPushButton('📷 Sincronizar RAW ↔ Cópias na Fila')
        self.btn_sync_raw.setToolTip(
            'Aplica retroativamente aos itens de tags já pendentes na fila: '
            'acrescenta a tag RAW nos arquivos brutos e traz/sincroniza pares RAW ↔ JPG/PNG correspondentes.'
        )
        self.btn_sync_raw.setStyleSheet(
            "QPushButton { background-color: #2E5A88; color: white; font-weight: bold; } "
            "QPushButton:hover { background-color: #3B71A8; } "
            "QPushButton:disabled { background-color: #2A2A2A; color: #777777; }"
        )
        self.btn_sync_raw.clicked.connect(self._sync_raw_in_queue)
        context_controls.addWidget(self.btn_sync_raw)

        self.btn_check_cloud = QPushButton('Conferir sincronização / operações interrompidas')
        self.btn_check_cloud.clicked.connect(lambda: self._start_context_execution(check_only=True))
        self.btn_cancel_context = QPushButton('Cancelar execução (preservar concluídas)')
        self.btn_cancel_context.setEnabled(False)
        self.btn_cancel_context.clicked.connect(self._cancel_active_worker)
        context_controls.addWidget(self.btn_check_cloud)
        context_controls.addWidget(self.btn_cancel_context)
        layout.addLayout(context_controls)
        recovery_controls = QHBoxLayout()
        self.btn_release_context = QPushButton('Revisar tentativa incerta…')
        self.btn_release_context.clicked.connect(self._release_context)
        self.btn_context_history = QPushButton('Histórico de execução')
        self.btn_context_history.clicked.connect(self._context_history)
        recovery_controls.addWidget(self.btn_release_context)
        recovery_controls.addWidget(self.btn_context_history)
        layout.addLayout(recovery_controls)

        self._update_mode_label()

    def _update_mode_label(self):
        can_sync_raw = (
            self.queue.count() > 0
            and not self._list_loading
            and not self._execution_active
            and not getattr(self.queue, 'busy', False)
            and self._raw_sync_worker is None
        )
        if hasattr(self, 'btn_sync_raw'):
            self.btn_sync_raw.setEnabled(can_sync_raw)
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
            enabled = self.queue.count() > 0 and not self._list_loading and not self._execution_active and not getattr(self.queue, 'busy', False)
            self.btn_execute.setEnabled(enabled)
            self.btn_execute_next.setEnabled(enabled)

    def _populate_list(self):
        """Agenda a montagem; não faz I/O nem executa operações da fila."""
        self._list_timer.stop()
        self._list_loading = True
        self._list_items = tuple(self.queue.items)
        self._move_map = {}
        self._list_position = 0
        self._list_phase = 'clear'
        self._list_steps = 0
        self.list_progress.setRange(0, max(1, self.list_widget.count() + 2 * len(self._list_items)))
        self.list_progress.setValue(0)
        self.list_progress.setFormat("Preparando lista… %p%")
        self.list_progress.show()
        self.header_label.setText(f"📋 Montando fila ({len(self._list_items):,} operações)…")
        self.list_widget.clearSelection()
        self.list_widget.setCurrentRow(-1)
        self._set_list_controls_enabled(False)
        self._list_timer.start()

    def _set_list_controls_enabled(self, enabled):
        for widget in (self.list_widget, self.btn_remove_selected, self.btn_clear_all,
                       self.btn_import, self.btn_export, self.recovery_button, self.btn_sync_raw):
            widget.setEnabled(enabled)
        self._update_mode_label()

    def _populate_list_batch(self):
        # Widgets Qt permanecem na thread gráfica. Cada lote devolve o controle
        # ao event loop; não usar processEvents (permitiria reentrância aqui).
        deadline = time.perf_counter() + 0.008
        processed = 0
        while processed < 200 and time.perf_counter() < deadline:
            if self._list_phase == 'clear':
                if self.list_widget.count():
                    del_item = self.list_widget.takeItem(self.list_widget.count() - 1)
                    del del_item
                else:
                    self._list_phase = 'map'
                    self.list_progress.setFormat("Preparando operações… %p%")
                    continue
            elif self._list_phase == 'map':
                if self._list_position == len(self._list_items):
                    self._list_phase = 'rows'
                    self._list_position = 0
                    continue
                it = self._list_items[self._list_position]
                if it.action_type == 'move':
                    value = (os.path.basename(os.path.dirname(it.new_value)), it.new_value)
                    self._move_map[it.file_id] = value
                    if it.path:
                        self._move_map[os.path.normpath(it.path).lower()] = value
                self._list_position += 1
            else:
                if self._list_position == len(self._list_items):
                    self._list_timer.stop()
                    self._list_loading = False
                    self.list_progress.setValue(self.list_progress.maximum())
                    self.list_progress.hide()
                    self.header_label.setText(
                        f"📋 Alterações Pendentes para Revisão ({len(self._list_items)} itens)")
                    self._list_items = ()
                    self._move_map = {}
                    self.details_label.setText(getattr(self, '_context_report_message', '') or
                                               "Selecione um item para ver o detalhamento da alteração.")
                    self._set_list_controls_enabled(True)
                    return
                self._append_list_row(self._list_position, self._list_items[self._list_position])
                self._list_position += 1
            processed += 1
            self._list_steps += 1
        self.list_progress.setValue(self._list_steps)
        if self._list_phase == 'rows':
            self.list_progress.setFormat(
                f"Montando fila: {self._list_position:,}/{len(self._list_items):,} operações — %p%")

    def _append_list_row(self, idx, item):
        move_map = self._move_map
        list_item = QListWidgetItem()
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

        state = getattr(item, 'execution_state', 'pending')
        labels = dict(blocked='BLOQUEADO', applying='RESULTADO A CONFERIR', local_applied='APLICADO LOCALMENTE',
                      waiting_cloud='AGUARDANDO DRIVE', uncertain='RESULTADO INCERTO')
        if state in labels:
            text += ' — ' + labels[state]
        list_item.setText(text)
        list_item.setToolTip(getattr(item, 'execution_message', ''))
        list_item.setData(Qt.ItemDataRole.UserRole, item)
        self.list_widget.addItem(list_item)

    def _on_item_selected(self, current, previous):
        if self._list_loading:
            self.details_label.setText("Aguarde a montagem da fila para selecionar uma operação.")
            return
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
                self.queue.remove_items_batch(items_to_remove, discard_placeholder=True)

    def _cancel_active_worker(self):
        if self._context_worker:
            self._context_worker.requestInterruption()
        if self._raw_sync_worker:
            self._raw_sync_worker.requestInterruption()

    def _sync_raw_in_queue(self):
        if self.queue.count() == 0 or self._raw_sync_worker is not None:
            return
        if not self._begin_execution():
            return
        from src.services.batch_tags import QueueRawSyncWorker
        db_path = (
            self.db_indexer.db_name
            if (self.db_indexer and getattr(self.db_indexer, 'db_name', None))
            else str(Path(self.queue.store.path).resolve().parent / 'file_index.db')
        )
        self._set_list_controls_enabled(False)
        self.btn_check_cloud.setEnabled(False)
        self.btn_release_context.setEnabled(False)
        self.btn_context_history.setEnabled(False)
        self.btn_cancel_context.setEnabled(True)
        self.list_progress.setRange(0, max(1, self.queue.count()))
        self.list_progress.setValue(0)
        self.list_progress.setFormat('Sincronizando RAW ↔ Cópias na fila… %v/%m')
        self.list_progress.show()

        worker = QueueRawSyncWorker(
            db_path,
            [it.to_dict() for it in self.queue.items],
            self.queue.store,
            self,
        )
        self._raw_sync_worker = worker
        self._raw_sync_result = None
        self._raw_sync_error = None
        self._raw_sync_cancelled = False

        def on_progress(done, total):
            self.list_progress.setRange(0, max(1, total))
            self.list_progress.setValue(done)
            self.list_progress.setFormat(f'Sincronizando RAW ↔ Cópias na fila: {done:,}/{total:,}')

        def on_saving():
            self.btn_cancel_context.setEnabled(False)
            self.list_progress.setFormat('Salvando fila atualizada…')

        def on_succeeded(new_records, stats):
            self._raw_sync_result = (new_records, stats)

        def on_failed(message):
            self._raw_sync_error = message

        def on_cancelled():
            self._raw_sync_cancelled = True

        def on_finished():
            worker.deleteLater()
            self._raw_sync_worker = None
            self.list_progress.hide()
            self._finish_execution()
            self.btn_check_cloud.setEnabled(True)
            self.btn_release_context.setEnabled(True)
            self.btn_context_history.setEnabled(True)
            self.btn_cancel_context.setEnabled(False)

            if self._raw_sync_error:
                self._set_list_controls_enabled(True)
                QMessageBox.critical(self, 'Falha ao sincronizar RAW na fila', self._raw_sync_error)
                return
            if self._raw_sync_cancelled or not self._raw_sync_result:
                self._set_list_controls_enabled(True)
                self.details_label.setText('Sincronização RAW ↔ Cópias cancelada. Fila anterior preservada.')
                return

            new_records, stats = self._raw_sync_result
            if stats.get('total_changed', 0) == 0:
                self._set_list_controls_enabled(True)
                skipped = stats.get('skipped_protected', 0)
                extra = f'\n({skipped} par(es) pulado(s) por estarem aguardando confirmação ou exclusão.)' if skipped else ''
                QMessageBox.information(
                    self,
                    'Fila já sincronizada',
                    f'Todos os itens de tags pendentes na fila já possuem a tag RAW nos arquivos brutos '
                    f'e os pares correspondentes atualizados.{extra}',
                )
            else:
                parts = []
                if stats.get('raw_tags_added'):
                    parts.append(f"• {stats['raw_tags_added']:,} arquivo(s) bruto(s) receberam a tag RAW")
                if stats.get('partners_added'):
                    parts.append(f"• {stats['partners_added']:,} par(es) RAW ↔ Cópia trazido(s) para a fila")
                if stats.get('existing_updated'):
                    parts.append(f"• {stats['existing_updated']:,} item(ns) já na fila receberam tags do par")
                if stats.get('skipped_protected'):
                    parts.append(f"• {stats['skipped_protected']:,} par(es) pulado(s) por estarem protegidos/excluídos")
                summary = '\n'.join(parts)
                self._context_report_message = (
                    f"📷 Sincronização RAW ↔ Cópias concluída na fila ({stats['total_changed']:,} item(ns) atualizado(s)/adicionado(s))."
                )
                self.queue.items = [StagingItem.from_dict(r) for r in new_records]
                self.queue._tag_index = None
                self.queue.queueChanged.emit(self.queue.count())
                if not self._list_loading:
                    self._set_list_controls_enabled(True)
                QMessageBox.information(
                    self,
                    'RAW ↔ Cópias Sincronizados na Fila',
                    f'Fila atualizada com sucesso:\n\n{summary}\n\n'
                    f'Total atual na fila: {self.queue.count():,} item(ns).',
                )

        worker.progress.connect(on_progress)
        worker.saving.connect(on_saving)
        worker.succeeded.connect(on_succeeded)
        worker.failed.connect(on_failed)
        worker.cancelled.connect(on_cancelled)
        worker.finished.connect(on_finished)
        worker.start()

    def _clear_queue(self):
        if self.queue.count() > 0:
            reply = QMessageBox.question(
                self, "Limpar Fila",
                "Tem certeza de que deseja cancelar todas as alterações pendentes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.queue.clear(discard_placeholder=True)

    def _execute_queue(self):
        if QMessageBox.question(self, 'Aprovar fila',
                f'Executar {self.queue.count()} operações? Mudanças físicas serão feitas somente agora; '
                'ações dependentes aguardarão confirmação no Drive.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._start_context_execution([i.operation_id for i in self.queue.items])

    def _execute_next(self):
        selected = [w.data(Qt.ItemDataRole.UserRole) for w in self.list_widget.selectedItems()]
        selected = [i for i in selected if i] or self.queue.items[:1]
        if selected and QMessageBox.question(self, 'Aprovar selecionados',
                f'Executar/reconciliar {len(selected)} operações selecionadas?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._start_context_execution([i.operation_id for i in selected])

    def _context_history(self):
        import json
        with self.queue.store.connect() as conn:
            rows = conn.execute('SELECT operation_id,state,payload,created FROM context_events ORDER BY seq DESC LIMIT 200').fetchall()
        dialog = QDialog(self)
        dialog.setWindowTitle('Histórico — últimas 200 etapas (sem reescrever eventos antigos)')
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        output = QPlainTextEdit(dialog)
        output.setReadOnly(True)
        output.setPlainText('\n\n'.join(f'{time.ctime(stamp)} | {state} | {op}\n' + json.dumps(json.loads(payload), ensure_ascii=False, indent=2)
                                       for op, state, payload, stamp in rows))
        layout.addWidget(output)
        dialog.exec()

    def _release_context(self):
        selected = [w.data(Qt.ItemDataRole.UserRole) for w in self.list_widget.selectedItems()]
        if selected and QMessageBox.question(self, 'Confirmar conferência manual',
                'Você conferiu os arquivos e pastas destas tentativas interrompidas? '
                'O aplicativo verificará o disco e o Drive: poderá reconhecer uma criação/exclusão já concluída '
                'ou liberar nova tentativa quando comprovar ausência de efeito. '
                'Não haverá desfazimento nem repetição de ação física agora. Sem comprovação, mantém o bloqueio.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._start_context_execution([i.operation_id for i in selected if i], release=True)

    def _start_context_execution(self, selected=(), check_only=False, release=False):
        from src.services.contextual_queue import ContextQueueWorker
        if (not self.db_indexer or self.config_mgr.is_read_only()
                or not self.queue.can_edit() or not self._begin_execution()):
            return
        self._context_result = None
        self._context_failure = None
        self._set_list_controls_enabled(False)
        self.btn_execute.setEnabled(False)
        self.btn_execute_next.setEnabled(False)
        self.btn_check_cloud.setEnabled(False)
        self.btn_release_context.setEnabled(False)
        self.btn_context_history.setEnabled(False)
        self.btn_cancel_context.setEnabled(True)
        self.list_progress.show()
        self._context_worker = ContextQueueWorker(self.queue.store.path, self.queue.store.legacy_path,
            self.db_indexer.db_name, self.drive_service, selected, check_only, self, release=release)
        self._context_worker.progress.connect(self._context_progress)
        self._context_worker.result.connect(lambda result: setattr(self, '_context_result', result))
        self._context_worker.failed.connect(lambda message: setattr(self, '_context_failure', message))
        self._context_worker.finished.connect(self._context_finished)
        self._context_worker.start()

    def _context_progress(self, value, total, name):
        self.list_progress.setRange(0, max(total, 1))
        self.list_progress.setValue(value)
        self.list_progress.setFormat(f'{value}/{total} — {name}')

    def _context_finished(self):
        try:
            self.queue.items = [StagingItem.from_dict(r) for r in self.queue.store.load()]
            self.queue._tag_index = None
        except Exception as exc:
            self._context_failure = str(exc)
        self._context_worker.deleteLater()
        self._context_worker = None
        self._finish_execution()
        self.btn_check_cloud.setEnabled(True)
        self.btn_release_context.setEnabled(True)
        self.btn_context_history.setEnabled(True)
        self.btn_cancel_context.setEnabled(False)
        self.queue.queueChanged.emit(self.queue.count())
        result = self._context_result or {}
        self.executionCompleted.emit(result.get('completed', 0) + result.get('applied', 0))
        self._populate_list()
        message = self._context_failure or (
            f'Confirmados no Drive: {result.get("completed", 0)}; aguardando/conferência: {result.get("waiting", 0)}. '
            f'Tentativas liberadas após revisão: {result.get("released", 0)}. '
            'Operações dependentes permanecem na fila para nova aprovação quando liberadas.\n'
            + '\n'.join(result.get('errors', [])[:15]))
        self._context_report_message = message
        self.details_label.setText(message)
        if self._context_failure:
            QMessageBox.warning(self, 'Execução interrompida', message)

    def done(self, result):
        if not self._execution_active:
            super().done(result)

    def closeEvent(self, event):
        if self._execution_active:
            event.ignore()
        else:
            super().closeEvent(event)

    def _begin_execution(self):
        if self._execution_active or self._list_loading:
            return False
        autosave = getattr(self.queue, '_autosave_worker', None)
        if autosave and autosave.isRunning():
            QMessageBox.information(self, 'Autosave em andamento', 'Aguarde o autosave terminar e execute novamente.')
            return False
        if not self.queue.can_edit():
            return False
        win = self.parent().window() if self.parent() else None
        if win and hasattr(win, 'begin_queue_execution'):
            if not win.begin_queue_execution():
                return False
        self._execution_active = True
        self.queue.busy = True
        self.executionStarted.emit()
        return True

    def _finish_execution(self):
        if not self._execution_active:
            return
        self._execution_active = False
        self.queue.release()
        win = self.parent().window() if self.parent() else None
        if win and hasattr(win, 'end_queue_execution'):
            win.end_queue_execution()
        self.executionFinished.emit()

    def _export_queue(self):
        if self.queue.busy:
            QMessageBox.warning(self, 'Fila ocupada', 'Aguarde a operação terminar para exportar um estado completo.')
            return
        from PyQt6.QtWidgets import QFileDialog
        import json
        if self.queue.count() == 0:
            return
        
        filepath, _ = QFileDialog.getSaveFileName(self, "Exportar Fila", "", "JSON Files (*.json)")
        if filepath:
            try:
                data = [it.to_dict() for it in self.queue.items]
                validated_records(data)
                atomic_json(filepath, data)
                QMessageBox.information(self, "Sucesso", "Fila exportada com sucesso.")
            except Exception as e:
                QMessageBox.warning(self, "Erro", f"Falha ao exportar: {e}")

    def _import_queue(self):
        if not self.queue.can_edit():
            return
        from PyQt6.QtWidgets import QFileDialog
        import json
        
        filepath, _ = QFileDialog.getOpenFileName(self, "Importar Fila", "", "JSON Files (*.json)")
        if filepath:
            try:
                if os.path.getsize(filepath) > MAX_QUEUE_FILE_BYTES:
                    raise ValueError('Arquivo da fila excede o limite de 64 MB.')
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                records = self.queue._get_queue_records(data)
                imported = [StagingItem.from_dict(d) for d in records]
                combined = list(self.queue.items)
                known = {item.operation_id for item in combined}
                count = 0
                # Verificar quais operation_ids já constam na tabela execution para gerar novo ID limpo ao importar
                exec_ids = set()
                try:
                    with self.queue.store.connect() as conn_check_c:
                        exec_ids = {r[0] for r in conn_check_c.execute('SELECT id FROM execution')}
                except Exception:
                    exec_ids = set()

                for item in imported:
                    if item.operation_id in exec_ids:
                        raise ValueError('Importação contém uma operação já executada ou incerta. O ID não será trocado para permitir repetição.')
                    if item.operation_id in known:
                        continue
                    from src.services.contextual_queue import queue_conflict
                    conflict = queue_conflict(item, combined)
                    if conflict:
                        raise ValueError(conflict)
                    if item.action_type in ('rename', 'move'):
                        if item.action_type == 'move' and any(old.file_id == item.file_id and old.action_type == 'delete' for old in combined):
                            continue
                        combined = [old for old in combined if not (old.file_id == item.file_id and old.action_type == item.action_type)]
                    combined.append(item)
                    known.add(item.operation_id)
                    count += 1
                self.queue.busy = True
                try:
                    self.queue.store.save([item.to_dict() for item in combined])
                    self.queue.items = combined
                    self.queue._tag_index = None
                finally:
                    self.queue.release()
                self.queue.queueChanged.emit(self.queue.count())
                message = f"{count} itens importados com sucesso."
                QMessageBox.information(self, "Importação Concluída", message)
            except Exception as e:
                QMessageBox.warning(self, "Erro", f"Falha ao importar: {e}")
