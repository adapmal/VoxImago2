"""Interface em 3 etapas para deduplicação de fotos por hash MD5."""

from __future__ import annotations

import os
from PyQt6.QtCore import Qt, QThread, QSize, QTimer, QEvent, pyqtSignal, QItemSelection, QItemSelectionModel, QPoint, QUrl
from PyQt6.QtGui import QPixmap, QIcon, QDesktopServices, QTransform, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from src.services.hash_deduplicator import (
    MODE_ALL_VS_ALL,
    MODE_CUSTOM_VS_ALL,
    MODE_ORGANIZAR_VS_ALL,
    MODE_SELECTED_FOLDERS,
    DuplicateGroup,
    HashDedupExecuteWorker,
    HashScanWorker,
    default_banco_apagado_path,
    parse_tags,
)
from src.utils.utils import format_size
from src.ui.thumbnails import ThumbnailCache, _rotation_degrees
from src.ui.details_panel import FileDetailsPanel
from src.ui.quick_preview import open_quick_preview_dialog
from src.utils.config_manager import ConfigManager
from src.services.dedup_review import load_review, save_review


class _InstanceRowButton(QFrame):
    """Cartão clicável para cada instância (Fica ou cada Duplicado) dentro da célula da tabela."""

    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()

    def __init__(self, text: str, tooltip: str = "", active: bool = False, parent=None):
        super().__init__(parent)
        self.last_modifiers = Qt.KeyboardModifier.NoModifier
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(0)
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.label)
        if tooltip:
            self.setToolTip(tooltip)
        self.set_active(active)

    def set_active(self, active: bool):
        target_val = "true" if active else "false"
        if self.property("active") == target_val:
            return
        self.setProperty("active", target_val)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.last_modifiers = event.modifiers()
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _DedupTableWidget(QTableWidget):
    """Tabela que permite marcar/desmarcar a coluna 'Aplicar' clicando em qualquer ponto da célula."""

    def __init__(self, rows: int = 0, cols: int = 0, parent=None):
        super().__init__(rows, cols, parent)
        self._col0_press: tuple[int, Qt.CheckState] | None = None

    @staticmethod
    def _is_item_checkable(item: QTableWidgetItem | None) -> bool:
        if item is None:
            return False
        required = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        return (item.flags() & required) == required

    def _record_col0_press(self, event):
        self._col0_press = None
        mods = event.modifiers()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not (mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
        ):
            idx = self.indexAt(event.pos())
            if idx.isValid() and idx.column() == 0:
                item = self.item(idx.row(), 0)
                if self._is_item_checkable(item):
                    self._col0_press = (idx.row(), item.checkState())

    def mousePressEvent(self, event):
        self._record_col0_press(event)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._record_col0_press(event)
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        press_info = self._col0_press
        self._col0_press = None
        super().mouseReleaseEvent(event)
        mods = event.modifiers()
        if (
            press_info is not None
            and event.button() == Qt.MouseButton.LeftButton
            and not (mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
        ):
            press_row, pre_state = press_info
            idx = self.indexAt(event.pos())
            if idx.isValid() and idx.column() == 0 and idx.row() == press_row:
                item = self.item(press_row, 0)
                # Só alterna manualmente quando o Qt não alternou (clique fora do quadradinho).
                if self._is_item_checkable(item) and item.checkState() == pre_state:
                    item.setCheckState(
                        Qt.CheckState.Unchecked
                        if pre_state == Qt.CheckState.Checked
                        else Qt.CheckState.Checked
                    )


class HashDedupDialog(QDialog):
    """Assistente de deduplicação por hash MD5 com revisão em lote."""

    COPIES_PER_PAGE = 12

    def __init__(self, parent=None, indexer=None, drive_service=None, initial_folders=None):
        super().__init__(parent)
        self.indexer = indexer
        self.drive_service = drive_service
        self.db_path = indexer.db_name if indexer else os.path.join('data', 'file_index.db')
        self.groups: list[DuplicateGroup] = []
        self.scan_thread: QThread | None = None
        self.scan_worker: HashScanWorker | None = None
        self.exec_thread: QThread | None = None
        self.exec_worker: HashDedupExecuteWorker | None = None
        self._busy = False
        self._maintenance = False
        self._plan_stale = False
        self._review_load_failed = False
        self._review_restoring = False
        self._execution_in_flight = []
        self._save_future = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self._autosave_review)
        self._save_check_timer = QTimer(self)
        self._save_check_timer.setInterval(500)
        self._save_check_timer.timeout.connect(self._check_review_save)
        self._active_detail_role = 'duplicate'  # 'duplicate' ou 'keeper'
        self._active_dup_idx = 0
        self._highlighted_row: int | None = None
        self._selection_anchor_row: int | None = None
        self._hydrated_rows: set[int] = set()
        self._copy_pages: dict[int, int] = {}
        self._group_thumb_paths: dict[tuple, tuple[str, int]] = {}
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(1)
        self._render_timer.timeout.connect(self._render_batch)
        self._hydrate_timer = QTimer(self)
        self._hydrate_timer.setSingleShot(True)
        self._hydrate_timer.setInterval(16)
        self._hydrate_timer.timeout.connect(self._hydrate_visible_rows)

        self.setWindowTitle('🧹 Deduplicador de Fotos por Hash (MD5)')
        self.resize(1360, 780)
        self._build_ui()
        self._restore_review(initial_folders=initial_folders)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        note = QLabel('A comparação usa o conteúdo e as descrições no Drive, sem baixar originais. '
                      'Aguarde o InSync/Drive terminar de sincronizar. Somente arquivos do catálogo local '
                      'com correspondência única na nuvem participam; casos ignorados são relatados.')
        note.setWordWrap(True)
        layout.addWidget(note)

        # Passo 1: Escopo e Destino
        scope_box = QGroupBox('1. Escopo da Busca & Destino de Arquivamento')
        self.scope_box = scope_box
        scope_layout = QVBoxLayout(scope_box)

        self.scope_group = QButtonGroup(self)
        self.radio_organizar = QRadioButton('📁 Pastas "Organizar" × Todo o Banco de Imagens')
        self.radio_custom = QRadioButton('📂 Pasta(s) específica(s) × Todo o Banco de Imagens')
        self.radio_all = QRadioButton('🌐 Todo o Banco de Imagens × Todo o Banco de Imagens')
        self.radio_selected = QRadioButton('📂 Comparar pastas selecionadas entre si (duas ou mais, incluindo subpastas)')
        self.radio_selected.setChecked(True)
        for rb in (self.radio_selected, self.radio_organizar, self.radio_custom, self.radio_all):
            self.scope_group.addButton(rb)
            scope_layout.addWidget(rb)

        # Seletor de pastas customizadas
        custom_row = QHBoxLayout()
        self.custom_list = QListWidget()
        self.custom_list.setMaximumHeight(68)
        self.custom_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        custom_btns = QVBoxLayout()
        self.btn_add_folder = QPushButton('+ Adicionar Pasta')
        self.btn_remove_folder = QPushButton('- Remover')
        custom_btns.addWidget(self.btn_add_folder)
        custom_btns.addWidget(self.btn_remove_folder)
        custom_row.addWidget(self.custom_list, 1)
        custom_row.addLayout(custom_btns)
        scope_layout.addLayout(custom_row)

        self.radio_custom.toggled.connect(self._toggle_custom_folders)
        self.radio_selected.toggled.connect(self._toggle_custom_folders)
        self.btn_add_folder.clicked.connect(self._add_custom_folder)
        self.btn_remove_folder.clicked.connect(self._remove_custom_folder)

        opts_row = QHBoxLayout()
        self.chk_include_videos = QCheckBox('Incluir vídeos na comparação por hash')
        opts_row.addWidget(self.chk_include_videos)
        opts_row.addStretch()
        opts_row.addWidget(QLabel('Destino (Banco_Apagado):'))
        self.dest_edit = QLineEdit(default_banco_apagado_path())
        self.dest_edit.setMinimumWidth(360)
        self.btn_dest_browse = QPushButton('Procurar…')
        self.btn_dest_browse.clicked.connect(self._browse_dest)
        opts_row.addWidget(self.dest_edit)
        opts_row.addWidget(self.btn_dest_browse)
        scope_layout.addLayout(opts_row)

        scan_btn_row = QHBoxLayout()
        self.btn_scan = QPushButton('🔍 Buscar Duplicatas por Hash')
        self.btn_scan.setStyleSheet('background-color: #1565C0; color: white; font-weight: bold; padding: 6px 14px;')
        self.btn_cancel_op = QPushButton('Cancelar Operação')
        self.btn_cancel_op.setEnabled(False)
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_rescan = QPushButton('Refazer busca')
        self.btn_rescan.clicked.connect(self._start_scan)
        self.btn_clear_results = QPushButton('Limpar revisão')
        self.btn_clear_results.clicked.connect(self._clear_results)
        self.btn_cancel_op.clicked.connect(self._cancel_operation)
        scan_btn_row.addWidget(self.btn_scan)
        scan_btn_row.addWidget(self.btn_rescan)
        scan_btn_row.addWidget(self.btn_clear_results)
        scan_btn_row.addWidget(self.btn_cancel_op)
        scan_btn_row.addStretch()
        scope_layout.addLayout(scan_btn_row)

        layout.addWidget(scope_box)

        # Barra de progresso & status
        prog_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label = QLabel('Pronto para buscar duplicatas.')
        prog_row.addWidget(self.progress, 1)
        prog_row.addWidget(self.status_label, 2)
        layout.addLayout(prog_row)

        # Passo 2: Revisão & Batch
        review_box = QGroupBox('2. Revisão Rápida & Seleção em Lote (Shift / Ctrl para seleção múltipla)')
        review_layout = QVBoxLayout(review_box)
        rules = QLabel(
            'Prioridade: pasta raiz de ano mais antiga → fora de Organizar → menor profundidade → mais tags → '
            'caminho mais curto → mais antigo no Drive → ordem alfabética. '
            'Discrepâncias com a pasta raiz anual do preservado pintam a linha de vermelho e somam tag (sem mover o preservado): '
            '"DateTaken" se o Date Taken divergir, e "discrepância" se o ano no nome de alguma cópia divergir '
            '(respeitando "até YYYY" em pastas até esse ano). Dê duplo clique em Tags Finais para editar.'
        )
        rules.setWordWrap(True)
        review_layout.addWidget(rules)

        batch_row = QHBoxLayout()
        self.summary_label = QLabel('Nenhum grupo carregado.')
        self.btn_check_selected = QPushButton('☑️ Marcar Selecionados')
        self.btn_uncheck_selected = QPushButton('⬜ Desmarcar Selecionados')
        self.btn_check_all = QPushButton('✅ Marcar Todos')
        self.btn_uncheck_all = QPushButton('❌ Desmarcar Todos')
        self.btn_swap_selected = QPushButton('🔄 Inverter Quem Fica (Selecionados)')
        self.btn_reset_tags = QPushButton('Restaurar tags automáticas (selecionados)')
        self.btn_reset_tags.clicked.connect(self._reset_selected_tags)
        for btn in (
            self.btn_check_selected,
            self.btn_uncheck_selected,
            self.btn_check_all,
            self.btn_uncheck_all,
            self.btn_swap_selected,
            self.btn_reset_tags,
        ):
            btn.setEnabled(False)
        self.btn_check_selected.clicked.connect(lambda: self._set_selected_checked(True))
        self.btn_uncheck_selected.clicked.connect(lambda: self._set_selected_checked(False))
        self.btn_check_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_uncheck_all.clicked.connect(lambda: self._set_all_checked(False))
        self.btn_swap_selected.clicked.connect(self._swap_selected_keeper)

        batch_row.addWidget(self.summary_label, 1)
        batch_row.addWidget(self.btn_check_selected)
        batch_row.addWidget(self.btn_uncheck_selected)
        batch_row.addWidget(self.btn_check_all)
        batch_row.addWidget(self.btn_uncheck_all)
        batch_row.addWidget(self.btn_swap_selected)
        review_layout.addLayout(batch_row)
        review_layout.addWidget(self.btn_reset_tags)

        self.table = _DedupTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            'Aplicar',
            'Miniatura',
            'Arquivo que FICA (Prioridade)',
            'Arquivo(s) que VÃO para Banco_Apagado',
            'Tamanho',
            'Tags Finais (editável com duplo clique)',
        ])
        self.table.setStyleSheet(
            "_InstanceRowButton { background-color: transparent; border: 1px solid transparent; border-radius: 4px; }"
            "_InstanceRowButton[discrepancy=\"true\"] { background-color: #642B31; color: white; }"
            "_InstanceRowButton[discrepancy=\"true\"] QLabel { color: white; }"
            "_InstanceRowButton:hover { background-color: #2A2D32; border: 1px solid #555555; }"
            "_InstanceRowButton[active=\"true\"] { background-color: #1E3A5F; border: 1px solid #4EA1F3; }"
            "_InstanceRowButton QLabel { background: transparent; }"
            "_InstanceRowButton[active=\"true\"] QLabel { color: #FFFFFF; }"
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setIconSize(QSize(56, 56))
        self.table.verticalHeader().setDefaultSectionSize(68)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 68)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 70)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 90)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self.table.currentCellChanged.connect(self._on_current_cell_changed)
        self.table.verticalScrollBar().valueChanged.connect(lambda _v: self._hydrate_timer.start())
        review_layout.addWidget(self.table, 1)

        # Painel de detalhes acoplado à direita (reutiliza FileDetailsPanel em modo somente inspeção)
        self.inspector_widget = QWidget()
        inspector_layout = QVBoxLayout(self.inspector_widget)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(6)

        self.btn_make_keeper = QPushButton("⭐ Tornar este o que FICA")
        self.btn_make_keeper.setToolTip("Promove exatamente esta cópia selecionada para Arquivo que FICA.")
        self.btn_make_keeper.clicked.connect(self._on_make_keeper_clicked)
        self.btn_make_keeper.setVisible(False)
        inspector_layout.addWidget(self.btn_make_keeper)

        self.details_panel = FileDetailsPanel(self)
        self.details_panel.set_inspection_only(True)
        self.details_panel.setFixedWidth(360)
        inspector_layout.addWidget(self.details_panel, 1)
        self.table.installEventFilter(self)
        self.details_panel.installEventFilter(self)
        self.details_panel.thumbnail_label.installEventFilter(self)
        self.inspector_widget.setVisible(False)

        self.review_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.review_splitter.addWidget(review_box)
        self.review_splitter.addWidget(self.inspector_widget)
        self.review_splitter.setStretchFactor(0, 1)
        self.review_splitter.setStretchFactor(1, 0)
        layout.addWidget(self.review_splitter, 1)

        # Passo 3: Executar / Fechar
        footer = QHBoxLayout()
        self.btn_execute = QPushButton('🚀 Aplicar Tags no Drive e Mover Selecionados para Banco_Apagado')
        self.btn_execute.setStyleSheet('background-color: #2E7D32; color: white; font-weight: bold; padding: 8px 16px;')
        self.btn_execute.setEnabled(False)
        self.btn_execute.clicked.connect(self._start_execute)
        self.btn_close = QPushButton('Fechar')
        self.btn_recover = QPushButton('Conferir operações interrompidas')
        self.btn_recover.clicked.connect(lambda: self._start_execute(recover_only=True))
        self.btn_close.clicked.connect(self.accept)
        footer.addWidget(self.btn_execute)
        footer.addWidget(self.btn_recover)
        footer.addStretch()
        footer.addWidget(self.btn_close)
        layout.addLayout(footer)

        # Qualquer mudança de escopo/destino invalida o plano já revisado.
        for radio in (self.radio_selected, self.radio_organizar, self.radio_custom, self.radio_all):
            radio.toggled.connect(self._invalidate_plan)
        for edit in (self.dest_edit,):
            edit.textChanged.connect(self._invalidate_plan)
        self.chk_include_videos.toggled.connect(self._invalidate_plan)
        self.custom_list.model().rowsInserted.connect(self._invalidate_plan)
        self.custom_list.model().rowsRemoved.connect(self._invalidate_plan)

    def _invalidate_plan(self, *args):
        if not self._busy and not self._review_restoring:
            if self.groups:
                self._plan_stale = True
                self.status_label.setText('Escopo/destino alterado. Revisão mantida; use Refazer busca antes de executar.')
            self._update_summary()

    def _review_settings(self):
        mode = next(i for i, rb in enumerate((self.radio_selected, self.radio_organizar,
                                             self.radio_custom, self.radio_all)) if rb.isChecked())
        return dict(mode=mode, folders=[self.custom_list.item(i).text() for i in range(self.custom_list.count())],
                    destination=self.dest_edit.text(), videos=self.chk_include_videos.isChecked(),
                    drive=ConfigManager().get('production_drive_id'))

    def _save_review(self, *, wait=False):
        if self._review_load_failed or self._review_restoring:
            return
        self._save_timer.stop()
        self._save_future = save_review(self.db_path, self.groups, self._review_settings(),
                                       self._plan_stale, self._execution_in_flight)
        if wait:
            self._save_future.result()
        else:
            self._save_check_timer.start()

    def _autosave_review(self):
        if self._busy:
            return  # O término da operação salva o novo estado coerente.
        try:
            self._save_review()
        except Exception as exc:
            self.status_label.setText(f'Não foi possível salvar a revisão local: {exc}')

    def _check_review_save(self):
        if self._save_future and self._save_future.done():
            self._save_check_timer.stop()
            try:
                self._save_future.result()
            except Exception as exc:
                self.status_label.setText(f'Falha ao salvar revisão; mantenha a janela aberta: {exc}')

    def _seed_initial_folders(self, folders):
        self.radio_selected.setChecked(True)
        self.custom_list.clear()
        self._add_folders_to_list(folders)

    def _restore_review(self, initial_folders=None):
        folders_to_seed = [p for p in (initial_folders or []) if p]
        self._review_restoring = True
        try:
            saved = load_review(self.db_path)
            if not saved:
                if folders_to_seed:
                    self._seed_initial_folders(folders_to_seed)
                    if self.custom_list.count() == 1:
                        self.status_label.setText(
                            'Pasta incluída. Adicione ao menos mais uma pasta com "+ Adicionar Pasta" e clique em Buscar.'
                        )
                return
            settings = saved['settings']
            self.dest_edit.setText(settings['destination'])
            self.chk_include_videos.setChecked(settings['videos'])
            self.groups = saved['groups']
            self._plan_stale = bool(saved.get('stale')) or settings.get('drive') != ConfigManager().get('production_drive_id')
            if folders_to_seed:
                self._seed_initial_folders(folders_to_seed)
                saved_norm = [os.path.normcase(os.path.normpath(p)) for p in settings['folders']]
                curr_norm = [
                    os.path.normcase(os.path.normpath(self.custom_list.item(i).text()))
                    for i in range(self.custom_list.count())
                ]
                if self.groups and (settings['mode'] != 0 or saved_norm != curr_norm):
                    self._plan_stale = True
            else:
                (self.radio_selected, self.radio_organizar, self.radio_custom, self.radio_all)[settings['mode']].setChecked(True)
                self.custom_list.clear()
                self.custom_list.addItems(settings['folders'])
            for group in self.groups:
                if group.keeper['drive_id'] in saved.get('in_flight', []):
                    group.checked = False
                    group.plan_error = 'Execução interrompida. Confira operações interrompidas e refaça a busca antes de executar este grupo.'
            self._populate_table()
            if not self.groups and folders_to_seed and self.custom_list.count() == 1:
                self.status_label.setText(
                    'Pasta incluída. Adicione ao menos mais uma pasta com "+ Adicionar Pasta" e clique em Buscar.'
                )
            elif self._plan_stale and folders_to_seed:
                self.status_label.setText('Revisão mantida com novas pastas selecionadas. Use Refazer busca antes de executar.')
            else:
                self.status_label.setText('Revisão local retomada; as condições no Drive serão revalidadas ao executar.'
                                          if not self._plan_stale else 'Revisão retomada com escopo alterado. Use Refazer busca antes de executar.')
        except Exception as exc:
            self._review_load_failed = True
            if folders_to_seed:
                self._seed_initial_folders(folders_to_seed)
            self.status_label.setText(f'Revisão local não carregada (arquivo preservado): {exc}')
        finally:
            self._review_restoring = False

    def _clear_results(self):
        if self._busy:
            return
        if QMessageBox.question(self, 'Limpar revisão',
                                'Apagar a busca salva e suas edições manuais? Nenhum arquivo do acervo será alterado.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.groups = []
        self._plan_stale = False
        self._review_load_failed = False
        self._execution_in_flight = []
        self._clear_details_inspector()
        self._populate_table()
        self._save_review()
        self.status_label.setText('Revisão limpa. Nenhum arquivo do acervo foi alterado.')

    def _acquire_maintenance(self):
        win = self.parent().window() if self.parent() else None
        if win and hasattr(win, 'begin_maintenance_operation'):
            if not win.begin_maintenance_operation():
                return False
            self._maintenance = True
        return True

    def _release_maintenance(self):
        if self._maintenance:
            self.parent().window().end_maintenance_operation()
            self._maintenance = False

    def _toggle_custom_folders(self, checked: bool):
        enabled = not self._busy and (self.radio_selected.isChecked() or self.radio_custom.isChecked())
        self.custom_list.setEnabled(enabled)
        self.btn_add_folder.setEnabled(enabled)
        self.btn_remove_folder.setEnabled(enabled)

    def _add_folders_to_list(self, folders):
        existing = {
            os.path.normcase(os.path.normpath(self.custom_list.item(i).text()))
            for i in range(self.custom_list.count())
        }
        for folder in folders or []:
            if not folder:
                continue
            norm = os.path.normpath(folder)
            key = os.path.normcase(norm)
            if key not in existing:
                existing.add(key)
                self.custom_list.addItem(norm)

    def _add_custom_folder(self):
        start_dir = ''
        if self.custom_list.count() > 0:
            start_dir = os.path.dirname(self.custom_list.item(0).text())
        if not start_dir or not os.path.isdir(start_dir):
            roots = ConfigManager().get_resolved_scan_paths(persist=False)
            if roots and os.path.isdir(roots[0]):
                start_dir = roots[0]

        dlg = QFileDialog(self, 'Selecionar Pasta(s) para Comparação', start_dir)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
        for view in dlg.findChildren((QListView, QTreeView)):
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            folders = [p for p in dlg.selectedFiles() if p and os.path.isdir(p)]
            if folders:
                self._add_folders_to_list(folders)

    def _remove_custom_folder(self):
        for item in self.custom_list.selectedItems():
            self.custom_list.takeItem(self.custom_list.row(item))

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(self, 'Selecionar Destino Banco_Apagado', self.dest_edit.text())
        if folder:
            self.dest_edit.setText(os.path.normpath(folder))

    def _set_busy(self, busy: bool):
        self._busy = busy
        for widget in (self.radio_selected, self.radio_organizar, self.radio_custom, self.radio_all,
                       self.chk_include_videos, self.dest_edit, self.btn_dest_browse):
            widget.setEnabled(not busy)
        for widget in (self.custom_list, self.btn_add_folder, self.btn_remove_folder):
            widget.setEnabled(not busy and (self.radio_selected.isChecked() or self.radio_custom.isChecked()))
        self.btn_recover.setEnabled(not busy and not ConfigManager().is_read_only())
        self.table.setEnabled(not busy)
        self.inspector_widget.setEnabled(not busy)
        self.btn_scan.setEnabled(not busy)
        self.btn_rescan.setEnabled(not busy)
        self.btn_clear_results.setEnabled(not busy)
        self.btn_cancel_op.setEnabled(busy)
        self.btn_close.setEnabled(not busy)
        has_groups = bool(self.groups) and not busy
        self.btn_execute.setEnabled(has_groups and not self._plan_stale and any(g.checked for g in self.groups) and not ConfigManager().is_read_only())
        for btn in (
            self.btn_check_selected,
            self.btn_uncheck_selected,
            self.btn_check_all,
            self.btn_uncheck_all,
            self.btn_swap_selected,
            self.btn_reset_tags,
        ):
            btn.setEnabled(has_groups)

    def _start_scan(self):
        if self._busy:
            return
        references = []
        if self.radio_selected.isChecked():
            mode = MODE_SELECTED_FOLDERS
            custom = [self.custom_list.item(i).text() for i in range(self.custom_list.count())]
            if len(custom) < 2:
                QMessageBox.warning(self, 'Escopo', 'Adicione ao menos duas pastas para comparar entre si.')
                return
        elif self.radio_organizar.isChecked():
            mode = MODE_ORGANIZAR_VS_ALL
            custom = []
        elif self.radio_custom.isChecked():
            mode = MODE_CUSTOM_VS_ALL
            custom = [self.custom_list.item(i).text() for i in range(self.custom_list.count())]
            if not custom:
                QMessageBox.warning(self, 'Aviso', 'Adicione ao menos uma pasta específica para comparar.')
                return
        else:
            mode = MODE_ALL_VS_ALL
            custom = []

        if not self._acquire_maintenance():
            return
        self._set_busy(True)
        self.progress.setValue(0)
        self.summary_label.setText('Buscando duplicatas...')

        self.scan_thread = QThread(self)
        self.scan_worker = HashScanWorker(
            self.db_path,
            mode,
            custom_folders=custom,
            include_videos=self.chk_include_videos.isChecked(),
            reference_folders=references,
            drive_service=self.drive_service,
        )
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.progress_update.connect(self._on_progress)
        self.scan_worker.scan_finished.connect(self._on_scan_finished)
        self.scan_worker.scan_failed.connect(self._on_scan_failed)
        self.scan_thread.start()

    def _cancel_operation(self):
        if self.scan_worker:
            self.scan_worker.cancel()
        if self.exec_worker:
            self.exec_worker.cancel()

    def _on_progress(self, pct: int, msg: str):
        self.progress.setValue(max(0, min(100, pct)))
        self.status_label.setText(msg)

    def _cleanup_scan_thread(self):
        if self.scan_thread:
            self.scan_thread.quit()
            if not self.scan_thread.wait(2000):
                return False
            self.scan_thread.deleteLater()
            self.scan_thread = None
        if self.scan_worker:
            self.scan_worker = None
        return True

    def _on_scan_finished(self, groups: list[DuplicateGroup]):
        skipped = self.scan_worker.skipped if self.scan_worker else []
        if not self._cleanup_scan_thread():
            QTimer.singleShot(100, lambda: self._on_scan_finished(groups))
            return
        self._release_maintenance()
        self.groups = groups
        self._plan_stale = False
        self._review_load_failed = False
        self._execution_in_flight = []
        self._save_review()
        self._populate_table()
        if not groups:
            QMessageBox.information(self, 'Busca Concluída',
                f'Nenhum grupo elegível no escopo. {len(skipped)} arquivos ignorados por falta de correspondência segura/checksum.')
        if skipped:
            from pathlib import Path
            from src.utils.queue_store import atomic_json
            try:
                path = Path(self.db_path).resolve().parent / 'dedup' / 'last_scan_skipped.json'
                atomic_json(path, skipped)
                self.status_label.setText(f'{len(skipped)} ignorados; motivos em {path}')
            except Exception as exc:
                self.status_label.setText('Falha ao salvar relatório da análise: ' + str(exc))

    def _on_scan_failed(self, err: str):
        if not self._cleanup_scan_thread():
            QTimer.singleShot(100, lambda: self._on_scan_failed(err))
            return
        self._release_maintenance()
        self._set_busy(False)
        self._update_summary()
        self.status_label.setText('Busca interrompida. Revisão anterior preservada.')
        if 'cancelada' not in err.lower():
            QMessageBox.warning(self, 'Erro na Busca', err)

    def _populate_table(self):
        self._render_timer.stop()
        self._hydrate_timer.stop()
        self._hydrated_rows.clear()
        self._copy_pages.clear()
        self._group_thumb_paths.clear()
        self._highlighted_row = None
        self._selection_anchor_row = None
        self._set_busy(True)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.groups))
        self._render_row = 0
        self._render_timer.start()

    @staticmethod
    def _keeper_text(grp: DuplicateGroup) -> str:
        reasons = grp.discrepancy_reasons
        return (
            f"{grp.keeper.get('rel_path', grp.keeper['path'])} "
            f"(prof. {grp.keeper.get('depth', 0)})\n"
            f"Tags atuais: {grp.keeper.get('description') or '—'}"
            + (f"\nDiscrepância: {' | '.join(reasons)}" if reasons else '')
            + (f"\nBLOQUEADO: {grp.plan_error}" if grp.plan_error else '')
        )

    @staticmethod
    def _dup_text(d: dict) -> str:
        return (
            f"• {d.get('rel_path', d['path'])} (prof. {d.get('depth', 0)})"
            + (f"\n  [tags: {d['description']}]" if d.get('description') else '')
        )

    @classmethod
    def _duplicates_fallback_text(cls, grp: DuplicateGroup) -> str:
        shown = '\n'.join(cls._dup_text(d) for d in grp.duplicates[:cls.COPIES_PER_PAGE])
        return shown + (f'\n… {len(grp.duplicates)} cópias; navegue pelas páginas.'
                        if len(grp.duplicates) > cls.COPIES_PER_PAGE else '')

    def _render_light_row(self, row: int):
        grp = self.groups[row]
        chk_item = QTableWidgetItem()
        chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        if grp.plan_error:
            grp.checked = False
            chk_item.setFlags(Qt.ItemFlag.ItemIsSelectable)
            chk_item.setToolTip(grp.plan_error)
        chk_item.setCheckState(Qt.CheckState.Checked if grp.checked else Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, chk_item)

        thumb_item = QTableWidgetItem()
        self.table.setItem(row, 1, thumb_item)

        keeper_item = QTableWidgetItem(self._keeper_text(grp))
        self.table.setItem(row, 2, keeper_item)

        dup_item = QTableWidgetItem(self._duplicates_fallback_text(grp))
        self.table.setItem(row, 3, dup_item)

        self.table.setItem(row, 4, QTableWidgetItem(format_size(grp.size)))
        tags_item = QTableWidgetItem(grp.proposed_description)
        tags_item.setToolTip('Edição manual' if grp.edited_description is not None else 'União automática das tags e nomes diferentes')
        self.table.setItem(row, 5, tags_item)
        reasons = grp.discrepancy_reasons
        if reasons:
            tip = '\n'.join(reasons)
            for column in range(6):
                cell = self.table.item(row, column)
                cell.setBackground(QColor('#642B31'))
                cell.setForeground(QColor('white'))
                cell.setToolTip((cell.toolTip() + '\n' if cell.toolTip() else '') + tip)
        for column in range(1, 5):
            cell = self.table.item(row, column)
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)

        dup_lines = sum(2 if d.get('description') else 1 for d in grp.duplicates[:self.COPIES_PER_PAGE])
        self.table.setRowHeight(row, max(68, 22 * max(2, dup_lines) + 12))

    def _find_group_cached_thumbnail(self, grp: DuplicateGroup, preferred: dict | None = None) -> tuple[str, int]:
        """Caminho de cache e rotação adicional, aplicada somente no QPixmap."""
        candidates = []
        if preferred is not None:
            candidates.append(preferred)
        candidates.append(grp.keeper)
        # Trabalho limitado também para grupos com milhares de cópias.
        start = (self._active_dup_idx // self.COPIES_PER_PAGE) * self.COPIES_PER_PAGE if preferred else 0
        candidates.extend(grp.duplicates[start:start + self.COPIES_PER_PAGE])
        key = (id(grp), _rotation_degrees(preferred or grp.keeper))
        known = self._group_thumb_paths.get(key)
        if known and os.path.isfile(known[0]):
            return known
        seen_ids = set()
        for entry in candidates:
            if not entry or id(entry) in seen_ids:
                continue
            seen_ids.add(id(entry))
            delta = (key[1] - _rotation_degrees(entry)) % 360
            cached = ThumbnailCache.get_existing_thumbnail_cache_path(self._to_details_item(entry))
            if cached and os.path.isfile(cached):
                self._group_thumb_paths[key] = (cached, delta)
                return cached, delta
            tp = entry.get('thumbnailPath')
            if tp and os.path.isfile(tp):
                self._group_thumb_paths[key] = (tp, delta)
                return tp, delta
        return '', 0

    def _ensure_row_hydrated(self, row: int, *, force: bool = False):
        if row < 0 or row >= len(self.groups) or row >= self.table.rowCount():
            return
        if not force and row in self._hydrated_rows and self.table.cellWidget(row, 2) is not None:
            return
        grp = self.groups[row]

        thumb_item = self.table.item(row, 1)
        if thumb_item is not None and thumb_item.icon().isNull():
            cached, delta = self._find_group_cached_thumbnail(grp)
            if cached:
                pix = QPixmap(cached)
                if not pix.isNull():
                    if delta:
                        pix = pix.transformed(QTransform().rotate(delta), Qt.TransformationMode.SmoothTransformation)
                    thumb_item.setIcon(QIcon(pix.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))

        # Limpa texto de fallback atrás dos cartões para não vazar durante transparência
        k_item = self.table.item(row, 2)
        if k_item is not None and k_item.text():
            k_item.setText('')
        d_item = self.table.item(row, 3)
        if d_item is not None and d_item.text():
            d_item.setText('')

        keeper_container = QWidget()
        keeper_lay = QVBoxLayout(keeper_container)
        keeper_lay.setContentsMargins(2, 2, 2, 2)
        keeper_lay.setSpacing(2)
        is_keeper_active = (self.table.currentRow() == row and self._active_detail_role == 'keeper')
        keeper_card = _InstanceRowButton(
            self._keeper_text(grp),
            tooltip="Clique para ver detalhes, duplo clique para abrir no app padrão ou Espaço para ampliar.",
            active=is_keeper_active,
        )
        has_disc = grp.has_discrepancy
        keeper_card.setProperty('discrepancy', 'true' if has_disc else 'false')
        keeper_card.clicked.connect(lambda _=False, r=row: self._on_instance_clicked(r, 'keeper', 0))
        keeper_card.doubleClicked.connect(lambda _=False, r=row: self._open_instance_in_default_app(r, 'keeper', 0))
        keeper_lay.addWidget(keeper_card)
        keeper_lay.addStretch()
        self.table.setCellWidget(row, 2, keeper_container)

        dups_container = QWidget()
        dups_lay = QVBoxLayout(dups_container)
        dups_lay.setContentsMargins(2, 2, 2, 2)
        dups_lay.setSpacing(3)
        start = self._copy_pages.get(row, 0)
        end = min(start + self.COPIES_PER_PAGE, len(grp.duplicates))
        if len(grp.duplicates) > self.COPIES_PER_PAGE:
            controls = QHBoxLayout()
            previous = QPushButton('← Anteriores')
            following = QPushButton('Próximas →')
            previous.setEnabled(start > 0)
            following.setEnabled(end < len(grp.duplicates))
            previous.clicked.connect(lambda _=False, r=row, s=start: self._show_copy_page(r, s - self.COPIES_PER_PAGE))
            following.clicked.connect(lambda _=False, r=row, s=end: self._show_copy_page(r, s))
            controls.addWidget(previous)
            controls.addWidget(QLabel(f'Cópias {start + 1}–{end} de {len(grp.duplicates)}'))
            controls.addWidget(following)
            dups_lay.addLayout(controls)
        for idx in range(start, end):
            d = grp.duplicates[idx]
            is_dup_active = (
                self.table.currentRow() == row
                and self._active_detail_role == 'duplicate'
                and self._active_dup_idx == idx
            )
            dup_card = _InstanceRowButton(
                self._dup_text(d),
                tooltip="Clique para inspecionar, duplo clique para abrir no app padrão ou Espaço para ampliar.",
                active=is_dup_active,
            )
            dup_card.setProperty('discrepancy', 'true' if has_disc else 'false')
            dup_card.clicked.connect(lambda _=False, r=row, i=idx: self._on_instance_clicked(r, 'duplicate', i))
            dup_card.setProperty('duplicateIndex', idx)
            dup_card.doubleClicked.connect(lambda _=False, r=row, i=idx: self._open_instance_in_default_app(r, 'duplicate', i))
            dups_lay.addWidget(dup_card)
        dups_lay.addStretch()
        self.table.setCellWidget(row, 3, dups_container)

        col2_w = max(200, self.table.columnWidth(2) or 260)
        col3_w = max(200, self.table.columnWidth(3) or 280)
        col2_h = max(keeper_container.sizeHint().height(), keeper_lay.heightForWidth(col2_w))
        col3_h = max(dups_container.sizeHint().height(), dups_lay.heightForWidth(col3_w))
        self.table.setRowHeight(row, max(68, col2_h + 10, col3_h + 10))
        self._hydrated_rows.add(row)

    def _show_copy_page(self, row, start):
        if self._busy or not (0 <= row < len(self.groups)):
            return
        start = max(0, min(start, max(0, len(self.groups[row].duplicates) - 1)))
        self._copy_pages[row] = start // self.COPIES_PER_PAGE * self.COPIES_PER_PAGE
        self._ensure_row_hydrated(row, force=True)
        self._navigate_to_instance(row, 'duplicate', start)

    def _dehydrate_row(self, row: int):
        if row not in self._hydrated_rows:
            return
        self._hydrated_rows.discard(row)
        if row < 0 or row >= self.table.rowCount() or row >= len(self.groups):
            return
        grp = self.groups[row]
        self.table.removeCellWidget(row, 2)
        self.table.removeCellWidget(row, 3)
        thumb_item = self.table.item(row, 1)
        if thumb_item is not None and not thumb_item.icon().isNull():
            thumb_item.setIcon(QIcon())
        k_item = self.table.item(row, 2)
        if k_item is not None:
            k_item.setText(self._keeper_text(grp))
        d_item = self.table.item(row, 3)
        if d_item is not None:
            d_item.setText(self._duplicates_fallback_text(grp))

    def _hydrate_visible_rows(self):
        total = min(len(self.groups), self.table.rowCount())
        if total <= 0:
            return
        vp = self.table.viewport()
        vp_h = vp.height() if vp is not None else 0
        first_vis = self.table.rowAt(0) if vp_h > 0 else 0
        if first_vis < 0:
            first_vis = 0
        last_vis = self.table.rowAt(max(0, vp_h - 1)) if vp_h > 0 else -1
        if last_vis < first_vis:
            last_vis = min(total - 1, first_vis + 24)

        keep_lo = max(0, first_vis - 12)
        keep_hi = min(total - 1, last_vis + 18)
        prune_lo = first_vis - 45
        prune_hi = last_vis + 45
        cur_row = self.table.currentRow()

        for stale in list(self._hydrated_rows):
            if stale != cur_row and (stale < prune_lo or stale > prune_hi):
                self._dehydrate_row(stale)

        self.table.setUpdatesEnabled(False)
        try:
            for r in range(keep_lo, keep_hi + 1):
                self._ensure_row_hydrated(r)
            if 0 <= cur_row < total:
                self._ensure_row_hydrated(cur_row)
        finally:
            self.table.setUpdatesEnabled(True)

    def _render_group_row(self, row: int, *, hydrate: bool = True):
        self._hydrated_rows.discard(row)
        self._render_light_row(row)
        if hydrate:
            self._ensure_row_hydrated(row, force=True)

    def _update_row_highlights(self, row: int):
        if row < 0 or row >= self.table.rowCount() or row not in self._hydrated_rows:
            return
        cur_row = self.table.currentRow()
        w2 = self.table.cellWidget(row, 2)
        if w2:
            for card in w2.findChildren(_InstanceRowButton):
                card.set_active(cur_row == row and self._active_detail_role == 'keeper')
        w3 = self.table.cellWidget(row, 3)
        if w3:
            for card in w3.findChildren(_InstanceRowButton):
                card.set_active(
                    cur_row == row
                    and self._active_detail_role == 'duplicate'
                    and self._active_dup_idx == card.property('duplicateIndex')
                )

    def _on_instance_clicked(self, row: int, role: str, dup_idx: int):
        if row < 0 or row >= len(self.groups):
            return
        mods = getattr(self.sender(), 'last_modifiers', Qt.KeyboardModifier.NoModifier)
        has_shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        has_ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        col = 2 if role == 'keeper' else 3
        self._active_detail_role = role
        self._active_dup_idx = dup_idx

        self.table.blockSignals(True)
        try:
            sel_model = self.table.selectionModel()
            if has_shift and sel_model is not None:
                anchor = self._selection_anchor_row
                if anchor is None or anchor < 0 or anchor >= len(self.groups):
                    cur = self.table.currentRow()
                    anchor = cur if cur >= 0 else row
                r0, r1 = min(anchor, row), max(anchor, row)
                col_last = max(0, self.table.columnCount() - 1)
                sel = QItemSelection(self.table.model().index(r0, 0), self.table.model().index(r1, col_last))
                flags = QItemSelectionModel.SelectionFlag.Select if has_ctrl else QItemSelectionModel.SelectionFlag.ClearAndSelect
                sel_model.select(sel, flags | QItemSelectionModel.SelectionFlag.Rows)
                self.table.setCurrentCell(row, col, QItemSelectionModel.SelectionFlag.NoUpdate)
            elif has_ctrl and sel_model is not None:
                self._selection_anchor_row = row
                sel_model.select(
                    self.table.model().index(row, col),
                    QItemSelectionModel.SelectionFlag.Toggle | QItemSelectionModel.SelectionFlag.Rows,
                )
                self.table.setCurrentCell(row, col, QItemSelectionModel.SelectionFlag.NoUpdate)
            else:
                self._selection_anchor_row = row
                self.table.selectRow(row)
                self.table.setCurrentCell(row, col)
        finally:
            self.table.blockSignals(False)

        self.table.setFocus()
        self._refresh_details_inspector(row)

    def _render_batch(self):
        limit = min(len(self.groups), self._render_row + 300)
        self.table.setUpdatesEnabled(False)
        try:
            for row in range(self._render_row, limit):
                self._render_light_row(row)
        finally:
            self.table.setUpdatesEnabled(True)

        self._render_row = limit
        if limit == len(self.groups):
            self._render_timer.stop()
            self.table.blockSignals(False)
            self._set_busy(False)
            self._update_summary()
            self._hydrate_visible_rows()
            if self.groups and self.table.currentRow() < 0:
                self._navigate_to_instance(0, 'duplicate', 0, target_col=3)
            elif self.groups and 0 <= self.table.currentRow() < len(self.groups):
                self._refresh_details_inspector(self.table.currentRow())

    def _scroll_active_card_into_view(self, row: int, role: str, dup_idx: int):
        col = 2 if role == 'keeper' else 3
        container = self.table.cellWidget(row, col)
        if not container:
            return
        cards = container.findChildren(_InstanceRowButton)
        target = (
            cards[0]
            if role == 'keeper' and cards
            else next((c for c in cards if c.property('duplicateIndex') == dup_idx), None)
        )
        if target is None:
            return
        vp = self.table.viewport()
        top_in_vp = target.mapTo(vp, QPoint(0, 0)).y()
        bottom_in_vp = top_in_vp + target.height()
        sb = self.table.verticalScrollBar()
        if top_in_vp < 0:
            sb.setValue(max(sb.minimum(), sb.value() + top_in_vp - 4))
        elif bottom_in_vp > vp.height():
            sb.setValue(min(sb.maximum(), sb.value() + (bottom_in_vp - vp.height()) + 4))

    def _navigate_to_instance(self, row: int, role: str, dup_idx: int = 0, target_col: int | None = None):
        if row < 0 or row >= len(self.groups):
            return
        grp = self.groups[row]
        if role == 'duplicate' and grp.duplicates:
            dup_idx = max(0, min(dup_idx, len(grp.duplicates) - 1))
            col = 3 if target_col is None else target_col
        else:
            role = 'keeper'
            dup_idx = 0
            col = 2 if target_col is None else target_col

        prev_row = self.table.currentRow()
        self._selection_anchor_row = row
        self._active_detail_role = role
        self._active_dup_idx = dup_idx
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.selectRow(row)
        self.table.setCurrentCell(row, col)
        self.table.blockSignals(False)
        if 0 <= prev_row < self.table.rowCount() and prev_row != row:
            self._update_row_highlights(prev_row)
        item = self.table.item(row, col)
        if item is not None:
            self.table.scrollToItem(item)
        page = dup_idx // self.COPIES_PER_PAGE * self.COPIES_PER_PAGE
        changed_page = role == 'duplicate' and self._copy_pages.get(row, 0) != page
        if role == 'duplicate':
            self._copy_pages[row] = page
        self._ensure_row_hydrated(row, force=changed_page)
        self._refresh_details_inspector(row)
        self._scroll_active_card_into_view(row, role, dup_idx)
        self._hydrate_timer.start()

    def _handle_table_arrow_key(self, key: int, modifiers) -> bool:
        if not self.groups:
            return False
        if self.table.state() == QAbstractItemView.State.EditingState:
            return False
        if modifiers & Qt.KeyboardModifier.ShiftModifier and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            current = max(0, self.table.currentRow())
            anchor = self._selection_anchor_row if self._selection_anchor_row is not None else current
            row = max(0, min(len(self.groups) - 1, current + (1 if key == Qt.Key.Key_Down else -1)))
            selection = QItemSelection(self.table.model().index(min(anchor, row), 0),
                                       self.table.model().index(max(anchor, row), self.table.columnCount() - 1))
            flags = (QItemSelectionModel.SelectionFlag.Select if modifiers & Qt.KeyboardModifier.ControlModifier
                     else QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.table.selectionModel().select(selection, flags | QItemSelectionModel.SelectionFlag.Rows)
            self._selection_anchor_row = anchor
            self.table.setCurrentCell(row, max(0, self.table.currentColumn()), QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollToItem(self.table.item(row, 0))
            return True
        if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            return False
        if key not in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_Right):
            return False

        cur_row = self.table.currentRow()
        cur_col = self.table.currentColumn()
        if cur_row < 0:
            self._navigate_to_instance(0, 'duplicate', 0, target_col=3)
            return True

        grp = self.groups[cur_row]
        in_dup_nav = (self._active_detail_role == 'duplicate' and cur_col in (-1, 0, 1, 3))

        if key == Qt.Key.Key_Down:
            if in_dup_nav and self._active_dup_idx + 1 < len(grp.duplicates):
                self._navigate_to_instance(cur_row, 'duplicate', self._active_dup_idx + 1, target_col=3)
                return True
            if cur_row + 1 < len(self.groups):
                next_role = 'keeper' if cur_col == 2 else 'duplicate'
                next_col = cur_col if cur_col in (2, 3, 4, 5) else 3
                if next_col in (4, 5):
                    self._selection_anchor_row = cur_row + 1
                    self.table.setCurrentCell(cur_row + 1, next_col)
                else:
                    self._navigate_to_instance(cur_row + 1, next_role, 0, target_col=next_col)
            return True

        if key == Qt.Key.Key_Up:
            if in_dup_nav and self._active_dup_idx > 0:
                self._navigate_to_instance(cur_row, 'duplicate', self._active_dup_idx - 1, target_col=3)
                return True
            if cur_row > 0:
                prev_grp = self.groups[cur_row - 1]
                prev_role = 'keeper' if cur_col == 2 else 'duplicate'
                prev_idx = max(0, len(prev_grp.duplicates) - 1) if prev_role == 'duplicate' else 0
                prev_col = cur_col if cur_col in (2, 3, 4, 5) else 3
                if prev_col in (4, 5):
                    self._selection_anchor_row = cur_row - 1
                    self.table.setCurrentCell(cur_row - 1, prev_col)
                else:
                    self._navigate_to_instance(cur_row - 1, prev_role, prev_idx, target_col=prev_col)
            return True

        if key == Qt.Key.Key_Left and cur_col == 3:
            self._navigate_to_instance(cur_row, 'keeper', 0, target_col=2)
            return True

        if key == Qt.Key.Key_Right and cur_col == 2:
            self._navigate_to_instance(cur_row, 'duplicate', 0, target_col=3)
            return True

        return False

    def _open_item_in_default_app(self, item: dict | None):
        if not item:
            return
        file_path = item.get('path') or ''
        if not file_path:
            return
        norm_p = os.path.normpath(file_path)
        if not os.path.exists(norm_p):
            QMessageBox.warning(
                self,
                'Arquivo indisponível',
                f'O arquivo local não foi encontrado em:\n{norm_p}',
            )
            return
        try:
            if hasattr(os, 'startfile'):
                os.startfile(norm_p)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(norm_p))
        except Exception as exc:
            QMessageBox.warning(
                self,
                'Erro ao abrir arquivo',
                f'Não foi possível abrir o arquivo no aplicativo padrão:\n{exc}',
            )

    def _open_instance_in_default_app(self, row: int, role: str, dup_idx: int = 0):
        if row < 0 or row >= len(self.groups):
            return
        grp = self.groups[row]
        if role == 'keeper':
            item = grp.keeper
        elif grp.duplicates:
            idx = max(0, min(dup_idx, len(grp.duplicates) - 1))
            item = grp.duplicates[idx]
        else:
            item = grp.keeper
        self._open_item_in_default_app(item)

    def _open_focused_in_default_app(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.groups):
            return
        self._open_item_in_default_app(self._get_focused_group_item(self.groups[row]))

    def _on_table_cell_double_clicked(self, row: int, column: int):
        if row < 0 or row >= len(self.groups):
            return
        if column in (1, 2):
            self._open_instance_in_default_app(row, 'keeper', 0)
        elif column == 3:
            self._open_instance_in_default_app(row, 'duplicate', self._active_dup_idx)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            focus_w = self.focusWidget()
            if isinstance(focus_w, QLineEdit) or getattr(focus_w, 'toPlainText', None):
                return super().eventFilter(watched, event)
            if event.key() == Qt.Key.Key_Space:
                self._open_selected_quick_preview()
                return True
            if watched is self.table and self._handle_table_arrow_key(event.key(), event.modifiers()):
                return True
        if (
            hasattr(self, 'details_panel')
            and watched is self.details_panel.thumbnail_label
            and event.type() == QEvent.Type.MouseButtonDblClick
        ):
            self._open_focused_in_default_app()
            return True
        return super().eventFilter(watched, event)

    def _on_table_cell_clicked(self, row: int, column: int):
        if not (QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._selection_anchor_row = row
        if column == 2:
            self._active_detail_role = 'keeper'
        else:
            self._active_detail_role = 'duplicate'
            self._active_dup_idx = self._copy_pages.get(row, 0)
        self._refresh_details_inspector(row)

    def _on_current_cell_changed(self, current_row: int, current_col: int, prev_row: int, _prev_col: int):
        if prev_row != current_row and 0 <= prev_row < self.table.rowCount():
            self._update_row_highlights(prev_row)
        if current_row < 0 or current_row >= len(self.groups):
            self._clear_details_inspector()
            return
        if current_col == 2:
            self._active_detail_role = 'keeper'
        elif current_col == 3:
            self._active_detail_role = 'duplicate'
            self._active_dup_idx = self._copy_pages.get(current_row, 0)
        self._refresh_details_inspector(current_row)

    def _clear_details_inspector(self):
        prev_hl = self._highlighted_row
        self._highlighted_row = None
        if prev_hl is not None:
            self._update_row_highlights(prev_hl)
        self.btn_make_keeper.setVisible(False)
        self.details_panel.clear_details()
        self.inspector_widget.setVisible(False)

    def _to_details_item(self, entry: dict) -> dict:
        item = dict(entry)
        if not item.get('createdTime') and item.get('drive_created'):
            item['createdTime'] = item['drive_created']
        item.setdefault('id', item.get('file_id') or item.get('path'))
        item.setdefault('source', 'local')
        return item

    def _get_focused_group_item(self, grp: DuplicateGroup) -> dict | None:
        if self._active_detail_role == 'keeper':
            return grp.keeper
        if grp.duplicates:
            idx = max(0, min(self._active_dup_idx, len(grp.duplicates) - 1))
            return grp.duplicates[idx]
        return grp.keeper

    def _on_make_keeper_clicked(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.groups):
            self._promote_duplicate_to_keeper(row, self._active_dup_idx)

    def _refresh_details_inspector(self, row: int):
        if row < 0 or row >= len(self.groups):
            self._clear_details_inspector()
            return
        grp = self.groups[row]

        can_promote = (
            self._active_detail_role == 'duplicate'
            and not grp.plan_error
            and grp.keeper.get('drive_id') in grp.eligible_ids
            and bool(grp.duplicates)
        )
        self.btn_make_keeper.setVisible(can_promote)

        prev_hl = self._highlighted_row
        self._highlighted_row = row
        if prev_hl is not None and prev_hl != row:
            self._update_row_highlights(prev_hl)
        self._update_row_highlights(row)

        focused = self._get_focused_group_item(grp)
        if not focused:
            self._clear_details_inspector()
            return

        detail_item = self._to_details_item(focused)
        fallback_thumb, delta = self._find_group_cached_thumbnail(grp, preferred=focused)
        if fallback_thumb:
            detail_item['fallback_thumbnail_path'] = fallback_thumb
            detail_item['fallback_thumbnail_rotation'] = delta

        self.inspector_widget.setVisible(True)
        self.details_panel.update_details(detail_item)

    def _select_detail_role(self, row: int, role: str, dup_idx: int = 0):
        self._active_detail_role = role
        self._active_dup_idx = dup_idx
        self._refresh_details_inspector(row)

    def _open_selected_quick_preview(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.groups):
            return
        grp = self.groups[row]
        preview_items = []
        for idx, d in enumerate(grp.duplicates):
            item_d = self._to_details_item(d)
            suffix = f" #{idx + 1}" if len(grp.duplicates) > 1 else ""
            item_d['preview_role'] = f"🗑️ Duplicado{suffix} (→ Banco_Apagado)"
            preview_items.append(item_d)
        item_k = self._to_details_item(grp.keeper)
        item_k['preview_role'] = "📌 Arquivo que Fica"
        preview_items.append(item_k)

        if self._active_detail_role == 'keeper':
            start_idx = len(preview_items) - 1
        else:
            start_idx = max(0, min(self._active_dup_idx, max(0, len(grp.duplicates) - 1)))

        open_quick_preview_dialog(self, preview_items, start_index=start_idx)

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if self._busy:
            return
        if item.column() not in (0, 5):
            return
        row = item.row()
        if 0 <= row < len(self.groups):
            if item.column() == 5:
                self.groups[row].edited_description = item.text()
            else:
                grp = self.groups[row]
                if grp.plan_error and item.checkState() == Qt.CheckState.Checked:
                    self.table.blockSignals(True)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    self.table.blockSignals(False)
                    grp.checked = False
                else:
                    grp.checked = item.checkState() == Qt.CheckState.Checked
            self._update_summary()

    def _reset_selected_tags(self):
        if self._busy:
            return
        selected_rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        if not selected_rows:
            return
        self.table.blockSignals(True)
        try:
            for row in selected_rows:
                grp = self.groups[row]
                grp.edited_description = None
                tags_item = self.table.item(row, 5)
                if tags_item is not None:
                    tags_item.setText(grp.proposed_description)
                    tags_item.setToolTip('União automática das tags e nomes diferentes')
        finally:
            self.table.blockSignals(False)
        self._update_summary()

    def _set_selected_checked(self, checked: bool):
        if self._busy:
            return
        selected_rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        if not selected_rows:
            QMessageBox.information(self, 'Seleção em Lote', 'Selecione uma ou mais linhas na tabela primeiro (use Shift ou Ctrl).')
            return
        self.table.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in selected_rows:
            allowed = checked and not self.groups[row].plan_error
            self.groups[row].checked = allowed
            self.table.item(row, 0).setCheckState(state if allowed else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_summary()

    def _set_all_checked(self, checked: bool):
        if self._busy:
            return
        self.table.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row, grp in enumerate(self.groups):
            grp.checked = checked and not grp.plan_error
            self.table.item(row, 0).setCheckState(state if grp.checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_summary()

    def _promote_duplicate_to_keeper(self, row: int, dup_idx: int):
        if self._busy:
            return
        if row < 0 or row >= len(self.groups):
            return
        grp = self.groups[row]
        if grp.plan_error:
            return
        if not (0 <= dup_idx < len(grp.duplicates)):
            return
        all_items = [grp.keeper] + grp.duplicates
        target = grp.duplicates[dup_idx]
        new_dups = [it for it in all_items if it is not target]
        if any(item['drive_id'] not in grp.eligible_ids for item in new_dups):
            QMessageBox.information(
                self,
                'Pasta protegida',
                'Esta inversão moveria arquivos fora do escopo autorizado.',
            )
            return
        grp.keeper = target
        grp.duplicates = new_dups
        grp.checked = True
        grp.recompute_merged()
        self._active_detail_role = 'keeper'
        self._active_dup_idx = 0
        self.table.blockSignals(True)
        self._render_group_row(row)
        self.table.selectRow(row)
        self.table.setCurrentCell(row, 2)
        self.table.blockSignals(False)
        self._refresh_details_inspector(row)
        self._update_summary()

    def _swap_selected_keeper(self):
        if self._busy:
            return
        selected_rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        if not selected_rows:
            QMessageBox.information(self, 'Inverter', 'Selecione ao menos uma linha para alternar qual arquivo fica.')
            return
        cur_row = self.table.currentRow()
        cur_col = self.table.currentColumn()
        self.table.blockSignals(True)
        hydrate_inline = len(selected_rows) <= 25
        self.table.setUpdatesEnabled(False)
        try:
            for row in selected_rows:
                grp = self.groups[row]
                if not grp.duplicates or grp.plan_error:
                    continue
                # Rotaciona entre todas as cópias [keeper, dup1, dup2, ...] sem perder nenhuma instância
                all_items = [grp.keeper] + grp.duplicates
                new_keeper = all_items[1]
                new_dups = all_items[2:] + [all_items[0]]
                if any(item['drive_id'] not in grp.eligible_ids for item in new_dups):
                    QMessageBox.information(self, 'Pasta protegida',
                        'Esta inversão moveria arquivos fora do escopo autorizado. '
                        'Para escolher livremente entre essas pastas, use "Comparar pastas selecionadas entre si" e analise novamente.')
                    continue
                grp.keeper = new_keeper
                grp.duplicates = new_dups
                grp.checked = True
                grp.recompute_merged()
                self._render_group_row(row, hydrate=(hydrate_inline or row in self._hydrated_rows))

            # Restaurar seleção (inclusive múltiplas linhas) e foco nas mesmas linhas invertidas
            sel_model = self.table.selectionModel()
            col_last = max(0, self.table.columnCount() - 1)
            sel = QItemSelection()
            for row in selected_rows:
                sel.select(self.table.model().index(row, 0), self.table.model().index(row, col_last))
            sel_model.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
            if cur_row in selected_rows:
                self.table.setCurrentCell(cur_row, cur_col if cur_col >= 0 else 2, QItemSelectionModel.SelectionFlag.NoUpdate)
            elif selected_rows:
                self.table.setCurrentCell(selected_rows[0], 2, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.blockSignals(False)
        self._hydrate_visible_rows()
        target_inspect = cur_row if cur_row in selected_rows else selected_rows[0]
        self._refresh_details_inspector(target_inspect)
        self._update_summary()

    def _update_summary(self):
        checked_groups = [g for g in self.groups if g.checked]
        files_to_move = sum(len(g.duplicates) for g in checked_groups)
        reclaimable = sum(g.size * len(g.duplicates) for g in checked_groups)
        self.summary_label.setText(
            f'Total: {len(self.groups):,} grupo(s) | Marcados: {len(checked_groups):,} '
            f'({files_to_move:,} arquivo(s) para mover — {format_size(reclaimable)})'
        )
        self.btn_execute.setEnabled(bool(checked_groups) and not self._plan_stale and not self._busy and not ConfigManager().is_read_only())
        if not self._busy and not self._review_restoring:
            self._save_timer.start()

    def _start_execute(self, checked=False, *, recover_only=False):
        if self._busy or ConfigManager().is_read_only():
            return
        if self._plan_stale and not recover_only:
            QMessageBox.warning(self, 'Revisão desatualizada', 'Use Refazer busca após alterar o escopo/destino.')
            return
        if not self.drive_service:
            QMessageBox.warning(
                self,
                'Google Drive Desconectado',
                'Conecte-se ao Google Drive (Login) antes de executar a movimentação e gravação de tags.',
            )
            return

        checked_groups = [g for g in self.groups if g.checked]
        if not checked_groups and not recover_only:
            return

        files_count = sum(len(g.duplicates) for g in checked_groups)
        self.btn_execute.setFocus()  # Finaliza eventual edição aberta na célula antes de confirmar.
        manual_count = sum(g.edited_description is not None for g in checked_groups)
        empty_count = sum(g.edited_description is not None and not parse_tags(g.edited_description) for g in checked_groups)
        dest_root = self.dest_edit.text().strip()
        if QMessageBox.question(
            self,
            'Confirmar Deduplicação',
            ('Conferir no Drive os resultados incertos, sem repetir movimentos.\n' if recover_only else '') +
            f'Serão processados {len(checked_groups):,} grupo(s):\n'
            f'• As tags finais serão gravadas no Google Drive e no banco local;\n'
            f'• {manual_count} grupo(s) com tags manuais, substituindo a união automática; {empty_count} com descrição vazia;\n'
            f'• {sum(g.has_discrepancy for g in checked_groups)} grupo(s) marcados com discrepância (vermelho) sem mover o preservado de pasta;\n'
            f'• {files_count:,} arquivo(s) duplicado(s) serão movidos via Google Drive API para:\n  {dest_root}\n\n'
            'As descrições atuais serão relidas; alterações na nuvem bloqueiam grupos com edição manual. '
            'Não edite os arquivos por outras máquinas durante a operação. Continuar?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        if not self._acquire_maintenance():
            return
        self._execution_in_flight = [g.keeper['drive_id'] for g in checked_groups] if not recover_only else []
        try:
            # Persistir antes de qualquer efeito externo: uma queda não libera repetição cega.
            self._save_review(wait=True)
        except Exception as exc:
            self._execution_in_flight = []
            self._release_maintenance()
            QMessageBox.critical(self, 'Revisão não salva', f'Execução não iniciada: {exc}')
            return
        self._set_busy(True)
        self.progress.setValue(0)

        self.exec_thread = QThread(self)
        self.exec_worker = HashDedupExecuteWorker(
            self.db_path,
            self.groups,
            dest_root,
            self.drive_service,
            recover_only=recover_only,
        )
        self.exec_worker.moveToThread(self.exec_thread)
        self.exec_thread.finished.connect(self.exec_worker.deleteLater)
        self.exec_thread.started.connect(self.exec_worker.run)
        self.exec_worker.progress_update.connect(self._on_progress)
        self.exec_worker.execution_finished.connect(self._on_execute_finished)
        self.exec_worker.execution_failed.connect(self._on_execute_failed)
        self.exec_thread.start()

    def _cleanup_exec_thread(self):
        if self.exec_thread:
            self.exec_thread.quit()
            if not self.exec_thread.wait(2000):
                return False
            self.exec_thread.deleteLater()
            self.exec_thread = None
        if self.exec_worker:
            self.exec_worker = None
        return True

    def _on_execute_finished(self, result: dict):
        if not self._cleanup_exec_thread():
            QTimer.singleShot(100, lambda: self._on_execute_finished(result))
            return
        self._release_maintenance()
        self._retain_pending_results(result)
        win = self.parent().window() if self.parent() else None
        if win and hasattr(win, '_force_refresh_after_sync'):
            win._force_refresh_after_sync()

        errs = result.get('errors') or []
        title = 'Deduplicação interrompida — revise os avisos' if errs else (
            'Deduplicação cancelada' if result.get('cancelled') else 'Deduplicação concluída')
        msg = (
            f"{title}\n\n"
            f"• Arquivos com tags atualizadas no Drive: {result.get('tags_updated', 0):,}\n"
            f"• Arquivos duplicados movidos para Banco_Apagado: {result.get('files_moved', 0):,}\n"
            + (f"• Preservados realocados para ANO\\Organizar: {result.get('keepers_relocated', 0):,}\n"
               if result.get('keepers_relocated', 0) else '')
            + f"• Erros/avisos: {len(errs)}"
        )
        if result.get('log_path'):
            msg += f"\n\nRelatório salvo em:\n{result['log_path']}"
        if 'reconciled' in result:
            msg += f"\nOperações reconciliadas: {result['reconciled']}"
        if errs:
            msg += "\n\nPrimeiros avisos:\n" + "\n".join(errs[:5])
        if result.get('cancelled'):
            msg += '\n\nFoi recebido um pedido de cancelamento. As etapas já concluídas não foram desfeitas.'
        if result.get('notes'):
            msg += '\n\n' + '\n'.join(result['notes'])
        msg += f'\n\nRevisão mantida: {len(self.groups)} grupo(s) pendente(s).'
        if errs:
            QMessageBox.warning(self, title, msg)
        else:
            QMessageBox.information(self, title, msg)

    def _retain_pending_results(self, result, *, uncertain=False):
        completed = set(result.get('completed_groups', []))
        moved = set(result.get('completed_moves', []))
        blocked = dict(result.get('review_groups', {}))
        if uncertain:
            for keeper_id in self._execution_in_flight:
                blocked.setdefault(keeper_id, 'Execução interrompida; confira operações interrompidas e refaça a busca.')
        remaining = []
        for group in self.groups:
            keeper_id = group.keeper['drive_id']
            if keeper_id in completed:
                continue
            group.duplicates = [d for d in group.duplicates if d['drive_id'] not in moved]
            if not group.duplicates:
                continue
            updates = result.get('keeper_updates', {}).get(keeper_id, {})
            group.keeper.update(updates)
            group.year_context = [d for d in group.year_context if d['drive_id'] not in moved]
            for entry in group.year_context:
                if entry['drive_id'] == keeper_id:
                    entry.update(updates)
            if keeper_id in blocked:
                group.checked = False
                group.plan_error = blocked[keeper_id] + ' Confira operações interrompidas e refaça a busca antes de repetir.'
            remaining.append(group)
        self.groups = remaining
        self._execution_in_flight = []
        self._clear_details_inspector()
        try:
            self._save_review(wait=True)
        except Exception as exc:
            # A cópia em disco ainda guarda in_flight: retomar permanece bloqueado.
            QMessageBox.warning(self, 'Falha ao salvar revisão', f'Resultados mantidos nesta janela, mas não salvos: {exc}')
        self._populate_table()
        self.status_label.setText(f'{len(remaining)} grupo(s) mantido(s). Concluídos removidos; interrompidos exigem revisão.')

    def _on_execute_failed(self, err: str):
        result = getattr(self.exec_worker, 'last_result', {}) if self.exec_worker else {}
        if not self._cleanup_exec_thread():
            QTimer.singleShot(100, lambda: self._on_execute_failed(err))
            return
        self._release_maintenance()
        self._retain_pending_results(result, uncertain=True)
        QMessageBox.critical(self, 'Erro na Execução', err)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.groups:
            self._hydrate_timer.start()

    def reject(self):
        if not self._busy:
            super().reject()

    def done(self, result):
        if self._busy:
            return
        self.btn_close.setFocus()  # Confirma a edição em andamento antes do último salvamento.
        try:
            self._save_review(wait=True)
        except Exception as exc:
            QMessageBox.warning(self, 'Revisão não salva', f'Não foi possível salvar. A janela permanecerá aberta: {exc}')
            return
        self._save_timer.stop()
        self._save_check_timer.stop()
        super().done(result)

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        if (self.scan_thread and self.scan_thread.isRunning()) or (
            self.exec_thread and self.exec_thread.isRunning()
        ):
            QMessageBox.information(self, 'Operação em andamento', 'Aguarde ou cancele a operação antes de fechar.')
            event.ignore()
            return
        super().closeEvent(event)
