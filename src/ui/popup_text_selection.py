"""Habilita seleção e cópia de texto em todos os popups e diálogos."""
from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QDialog, QLabel, QMessageBox

_FILTER_ATTR = '_popup_text_selection_filter'


def _is_selectable_dialog_label(label: QLabel) -> bool:
    if type(label) is not QLabel:
        return False
    if label.objectName() in ('qt_msgboxex_icon_label', 'thumbnail_label'):
        return False
    if label.buddy() is not None:
        return False
    pixmap = label.pixmap()
    if pixmap is not None and not pixmap.isNull():
        return False
    # Evita transformar caixas de miniatura com tamanho fixo em campos selecionáveis.
    if (not label.text() and label.minimumSize() == label.maximumSize()
            and label.maximumWidth() < 16777215):
        return False
    # Não intercepta cliques/arrastos de células dentro de tabelas, listas ou árvores.
    parent = label.parentWidget()
    while parent is not None and not isinstance(parent, QDialog):
        if isinstance(parent, QAbstractItemView):
            return False
        parent = parent.parentWidget()
    return True


def _apply_selectable_flags(label: QLabel, extra_flags) -> None:
    current = label.textInteractionFlags()
    target = current | extra_flags
    if target == current:
        return
    prev_focus = label.focusPolicy()
    label.setTextInteractionFlags(target)
    if prev_focus == Qt.FocusPolicy.TabFocus:
        label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    elif prev_focus in (Qt.FocusPolicy.StrongFocus, Qt.FocusPolicy.WheelFocus):
        label.setFocusPolicy(prev_focus)


def _messagebox_flags(box: QMessageBox):
    return (
        box.textInteractionFlags()
        | Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )


def _enable_dialog_label(label: QLabel, window, msg_flags=None) -> None:
    if isinstance(window, QMessageBox):
        if label.objectName() == 'qt_msgboxex_icon_label':
            return
        _apply_selectable_flags(label, msg_flags if msg_flags is not None else _messagebox_flags(window))
    elif _is_selectable_dialog_label(label):
        _apply_selectable_flags(label, Qt.TextInteractionFlag.TextSelectableByMouse)


def enable_selectable_popup_text(widget) -> None:
    """Ativa seleção por mouse/teclado em QMessageBox e QLabels padrão de diálogo."""
    if isinstance(widget, QMessageBox):
        flags = _messagebox_flags(widget)
        if widget.textInteractionFlags() != flags:
            widget.setTextInteractionFlags(flags)
        for label in widget.findChildren(QLabel):
            _enable_dialog_label(label, widget, flags)
        return
    if isinstance(widget, QDialog):
        for label in widget.findChildren(QLabel):
            _enable_dialog_label(label, widget)
        return
    if type(widget) is QLabel and isinstance(widget.window(), QDialog):
        _enable_dialog_label(widget, widget.window())


class PopupTextSelectionFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show:
            if isinstance(obj, QDialog):
                enable_selectable_popup_text(obj)
            elif type(obj) is QLabel:
                win = obj.window()
                if isinstance(win, QDialog):
                    _enable_dialog_label(obj, win)
        return super().eventFilter(obj, event)


def install_popup_text_selection(app=None):
    app = app or QApplication.instance()
    if app is None:
        return None
    existing = getattr(app, _FILTER_ATTR, None)
    if existing is not None:
        return existing
    flt = PopupTextSelectionFilter(app)
    app.installEventFilter(flt)
    setattr(app, _FILTER_ATTR, flt)
    return flt
