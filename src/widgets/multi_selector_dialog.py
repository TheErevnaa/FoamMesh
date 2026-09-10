#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
from dataclasses import dataclass
from enum import IntEnum, auto

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidgetItem, QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, Signal


def _natural_key(text: str):
    """Sort `pipe_2` before `pipe_10` (G5).

    A plain alphabetical sort puts `pipe_10` between `pipe_1` and `pipe_2`,
    which for the numbered solids an STL split produces is the one ordering
    nobody expects. Digit runs compare as numbers, everything else folded.
    """
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', text)]

from .multi_selector_dialog_ui import Ui_MultiSelectorDialog


class ListDataRole(IntEnum):
    USER_DATA = Qt.UserRole
    FILTERING_TEXT = auto()
    SELECTION_FLAG = auto()
    REMOVABLE = auto()


@dataclass
class SelectorItem:
    label: str  # Text to display in the list
    text: str   # Text for filtering
    value: str  # The key of item
    removable: bool = True
    kind: str = 'surface_group'
    geometry_ids: tuple[str, ...] = ()
    status: str = 'valid'
    status_label: str = 'Available'


class MultiSelectorDialog(QDialog):
    itemsSelected = Signal(list)
    currentItemChanged = Signal(str)
    selectedSetChanged = Signal(list)
    hoverPreviewChanged = Signal(str)
    selectionAccepted = Signal(list)
    selectionRejected = Signal()
    selectionRestored = Signal(list)

    def __init__(self, parent, title, items: list[SelectorItem], selectedItems,
                 selection_service=None):
        """Constructs a new SelectorDialog

        Args:
            title: Window title of the dialog
            items: List of items
            selectedItems: List of values for the selected item
        """
        super().__init__(parent)
        self._ui = Ui_MultiSelectorDialog()
        self._ui.setupUi(self)
        self._ui.filter.setAccessibleName(self.tr('Filter geometry scopes'))
        self._ui.list.setAccessibleName(self.tr('Available geometry scopes'))
        self._ui.selectedList.setAccessibleName(
            self.tr('Selected geometry scopes'))
        self._ui.add.setAccessibleName(self.tr('Add selected geometry scopes'))
        self._ui.remove.setAccessibleName(
            self.tr('Remove selected geometry scopes'))
        self._selectionService = selection_service
        self._selectionSnapshot = (
            selection_service.snapshot() if selection_service else None)
        self._editorToken = None

        self.setWindowTitle(title)
        self._installHeaders()
        # G5. The surfaces arrived in whatever order the catalogue held them
        # -- `pipe_3`, `pipe_1`, `pipe_2` for a three-solid STL. With thirty
        # patches an unordered list is not a list, it is a search.
        items = sorted(items, key=lambda data: _natural_key(data.label))
        for data in items:
            label = (
                data.label if data.status == 'valid'
                else f'{data.label} — {data.status_label}')
            item = QListWidgetItem(label)
            item.setData(ListDataRole.USER_DATA, data.value)
            item.setData(ListDataRole.FILTERING_TEXT, data.text.lower())
            item.setData(ListDataRole.SELECTION_FLAG, False)
            item.setData(ListDataRole.REMOVABLE, data.removable)
            item.setToolTip(
                f'{data.status_label}; stable ID: {data.value}')
            if data.status != 'valid':
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)

            self._ui.list.addItem(item)
            if data.value in selectedItems:
                self._addSelectedItem(item)
        self._updateCounts()

        if selection_service is not None:
            from foammesh.core.selection import (
                SelectionEntity, SelectionKind, SelectionStatus)
            for data in items:
                # Opening an editor must not steal an entity from the geometry
                # or prepared-geometry catalogue that owns its lifecycle.
                if selection_service.entity(data.value) is None:
                    selection_service.register(SelectionEntity(
                        data.value, data.label, SelectionKind(data.kind),
                        tuple(data.geometry_ids) or (data.value,),
                        SelectionStatus(data.status),
                        owner='scope-dialog', removable=data.removable))
            self._editorToken = selection_service.attach_editor(
                self._viewportSelection,
                (data.value for data in items if data.status == 'valid'))
            selection_service.select(
                value for value in selectedItems
                if selection_service.entity(value) is not None
                and selection_service.entity(value).status.value == 'valid')
        self._connectSignalsSlots()

    def selectedItems(self):
        return [(self._ui.list.item(self._ui.selectedList.item(i).data(Qt.UserRole)).data(ListDataRole.USER_DATA),
                 self._ui.selectedList.item(i).text())
                for i in range(self._ui.selectedList.count())]

    def accept(self):
        selected = self.selectedItems()
        self.itemsSelected.emit(selected)
        self.selectionAccepted.emit(selected)
        if self._selectionService is not None:
            self._selectionService.select(value for value, _label in selected)
            self._detachEditor()
        super().accept()

    def reject(self):
        if self._selectionService is not None and self._selectionSnapshot is not None:
            self._selectionService.restore(self._selectionSnapshot)
            restored = [
                (value, self._labelFor(value))
                for value in self._selectionSnapshot.selected_ids
                if self._labelFor(value)]
            self.selectionRestored.emit(restored)
            self._detachEditor()
        self.selectionRejected.emit()
        super().reject()

    def _installHeaders(self):
        """Give each list a heading that counts what is under it (G5).

        Two unlabelled boxes either side of `Add >>` left which one was which
        to be inferred from the arrows, and there was no way to take every
        surface at once short of dragging through the whole list.
        """
        self._availableHeading = QLabel(self)
        self._selectAll = QCheckBox(self.tr('All'), self)
        self._selectAll.setTristate(True)
        self._selectAll.setToolTip(
            self.tr('Select every surface the filter leaves visible'))
        self._selectAll.setAccessibleName(self.tr('Select all surfaces'))
        self._selectAll.clicked.connect(self._selectAllClicked)

        header = QWidget(self)
        headerLayout = QHBoxLayout(header)
        headerLayout.setContentsMargins(0, 0, 0, 0)
        headerLayout.addWidget(self._availableHeading)
        headerLayout.addStretch(1)
        headerLayout.addWidget(self._selectAll)
        self._ui.verticalLayout.insertWidget(0, header)

        # `selectedList` sits straight in the row, so it needs a column of
        # its own before it can carry a heading.
        self._selectedHeading = QLabel(self)
        row = self._ui.horizontalLayout
        index = row.indexOf(self._ui.selectedList)
        column = QWidget(self)
        columnLayout = QVBoxLayout(column)
        columnLayout.setContentsMargins(0, 0, 0, 0)
        columnLayout.addWidget(self._selectedHeading)
        row.removeWidget(self._ui.selectedList)
        columnLayout.addWidget(self._ui.selectedList)
        row.insertWidget(index, column)

    def _updateCounts(self):
        available = sum(
            1 for i in range(self._ui.list.count())
            if not self._ui.list.item(i).isHidden())
        self._availableHeading.setText(
            self.tr('Available ({0})').format(available))
        self._selectedHeading.setText(
            self.tr('Selected ({0})').format(self._ui.selectedList.count()))
        self._syncSelectAll()

    def _selectableRows(self):
        for i in range(self._ui.list.count()):
            item = self._ui.list.item(i)
            if item.isHidden() or not (item.flags() & Qt.ItemIsEnabled):
                continue
            yield item

    def _selectAllClicked(self, _checked=False):
        rows = list(self._selectableRows())
        # A tri-state box the user clicks always resolves to all-or-nothing;
        # `PartiallyChecked` is something only the list itself reports.
        wantAll = not all(item.isSelected() for item in rows) if rows else False
        self._ui.list.clearSelection()
        if wantAll:
            for item in rows:
                item.setSelected(True)
        self._syncSelectAll()

    def _syncSelectAll(self):
        rows = list(self._selectableRows())
        chosen = sum(1 for item in rows if item.isSelected())
        if not rows or not chosen:
            state = Qt.CheckState.Unchecked
        elif chosen == len(rows):
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self._selectAll.setEnabled(bool(rows))
        blocked = self._selectAll.blockSignals(True)
        self._selectAll.setCheckState(state)
        self._selectAll.blockSignals(blocked)

    def _connectSignalsSlots(self):
        self._ui.filter.textChanged.connect(self._filterChanged)
        self._ui.list.itemSelectionChanged.connect(self._syncSelectAll)
        self._ui.list.itemDoubleClicked.connect(self._addClicked)
        self._ui.add.clicked.connect(self._addClicked)
        self._ui.remove.clicked.connect(self._removeClicked)
        self._ui.selectedList.itemDoubleClicked.connect(self._removeClicked)
        self._ui.list.currentItemChanged.connect(self._currentChanged)
        self._ui.list.itemEntered.connect(self._hovered)
        self._ui.selectedList.currentItemChanged.connect(
            self._selectedCurrentChanged)

    def _filterChanged(self):
        text = self._ui.filter.text().lower()
        for i in range(self._ui.list.count()):
            item = self._ui.list.item(i)
            item.setHidden(text not in item.data(ListDataRole.FILTERING_TEXT) or item.data(ListDataRole.SELECTION_FLAG))
        self._updateCounts()

    def _addClicked(self):
        for item in self._ui.list.selectedItems():
            self._addSelectedItem(item)
        self._selectionChanged()

    def _removeClicked(self):
        for item in self._ui.selectedList.selectedItems():
            if not item.data(ListDataRole.REMOVABLE):
                continue
            i = self._ui.list.item(item.data(Qt.UserRole))
            i.setData(ListDataRole.SELECTION_FLAG, False)
            i.setHidden(False)
            self._ui.selectedList.takeItem(self._ui.selectedList.row(item))
        self._selectionChanged()

    def _addSelectedItem(self, item):
        item.setData(ListDataRole.SELECTION_FLAG, True)
        item.setHidden(True)
        item.setSelected(False)

        itemToAdd = QListWidgetItem(item.text())
        itemToAdd.setData(Qt.UserRole, self._ui.list.row(item))
        itemToAdd.setData(
            ListDataRole.REMOVABLE,
            item.data(ListDataRole.REMOVABLE))
        itemToAdd.setToolTip(item.toolTip())
        if not item.data(ListDataRole.REMOVABLE):
            itemToAdd.setFlags(itemToAdd.flags() & ~Qt.ItemIsSelectable)

        self._ui.selectedList.addItem(itemToAdd)

    def _selectionChanged(self):
        self._updateCounts()
        selected = self.selectedItems()
        self.selectedSetChanged.emit(selected)
        if self._selectionService is not None:
            self._selectionService.select(value for value, _label in selected)

    def _currentChanged(self, item, _prior):
        value = (
            str(item.data(ListDataRole.USER_DATA)) if item is not None else '')
        self.currentItemChanged.emit(value)
        self._preview(value)

    def _selectedCurrentChanged(self, item, _prior):
        if item is None:
            self._preview('')
            return
        source = self._ui.list.item(item.data(Qt.UserRole))
        value = str(source.data(ListDataRole.USER_DATA))
        self.currentItemChanged.emit(value)
        self._preview(value)

    def _hovered(self, item):
        value = str(item.data(ListDataRole.USER_DATA))
        self.hoverPreviewChanged.emit(value)
        self._preview(value)

    def _preview(self, value):
        if self._selectionService is not None:
            self._selectionService.preview((value,) if value else ())

    def _viewportSelection(self, stable_ids):
        selected = set(stable_ids)
        self._ui.selectedList.clear()
        for index in range(self._ui.list.count()):
            item = self._ui.list.item(index)
            value = str(item.data(ListDataRole.USER_DATA))
            item.setData(ListDataRole.SELECTION_FLAG, value in selected)
            item.setHidden(value in selected)
            if value in selected:
                self._addSelectedItem(item)
        self._updateCounts()
        self.selectedSetChanged.emit(self.selectedItems())

    def _labelFor(self, stable_id):
        for index in range(self._ui.list.count()):
            item = self._ui.list.item(index)
            if str(item.data(ListDataRole.USER_DATA)) == str(stable_id):
                return item.text()
        return ''

    def _detachEditor(self):
        if self._selectionService is not None and self._editorToken:
            self._selectionService.detach_editor(self._editorToken)
            self._selectionService.clear_preview()
            self._editorToken = None
