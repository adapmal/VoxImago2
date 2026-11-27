"""Seleção de revisão: Shift/Ctrl, clique na célula Aplicar e Espaço em lote."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QTableWidget


class ReviewTable(QTableWidget):
    def __init__(self, rows=0, columns=0, parent=None):
        super().__init__(rows, columns, parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._press = None

    @staticmethod
    def checkable(item):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        return item is not None and item.flags() & flags == flags

    def mousePressEvent(self, event):
        self._press = None
        index = self.indexAt(event.pos())
        if (event.button() == Qt.MouseButton.LeftButton and index.column() == 0
                and not event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)):
            item = self.item(index.row(), 0)
            if self.checkable(item):
                self._press = (index.row(), item.checkState())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        press, self._press = self._press, None
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.pos())
        if press and index.column() == 0 and index.row() == press[0]:
            item = self.item(press[0], 0)
            if self.checkable(item) and item.checkState() == press[1]:
                item.setCheckState(Qt.CheckState.Unchecked if press[1] == Qt.CheckState.Checked else Qt.CheckState.Checked)

    def set_selected_checked(self, checked=None):
        items = [self.item(index.row(), 0) for index in self.selectionModel().selectedRows()]
        items = [item for item in items if self.checkable(item)]
        if checked is None:
            checked = any(item.checkState() != Qt.CheckState.Checked for item in items)
        for item in items:
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.set_selected_checked()
            event.accept()
            return
        super().keyPressEvent(event)
