"""Load Mesh format chooser with the §13.4 visible-disabled availability policy."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout,
)

from foammesh.core.import_export import MeshImportEntry


class MeshImportDialog(QDialog):
    """Every mesh import format stays visible; absent converters are disabled
    with the exact capability reason, so nothing unavailable looks runnable."""

    def __init__(self, entries: tuple[MeshImportEntry, ...], parent=None):
        super().__init__(parent)
        self._entries = tuple(entries)
        self.setWindowTitle(self.tr('Load mesh'))
        self.resize(480, 360)
        layout = QVBoxLayout(self)
        self._list = QListWidget()
        for entry in self._entries:
            maturity = self.tr('Stable') if entry.maturity == 'stable' else self.tr('Experimental')
            item = QListWidgetItem(f'{entry.label} — {maturity}')
            item.setData(Qt.ItemDataRole.UserRole, entry.entry_id)
            if not entry.available:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(entry.reason)
            self._list.addItem(item)
        layout.addWidget(self._list)
        self._details = QLabel()
        self._details.setWordWrap(True)
        layout.addWidget(self._details)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText(self.tr('Load'))
        self._ok.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._list.currentRowChanged.connect(self._selectionChanged)

    def _selectionChanged(self, row: int):
        entry = self._entries[row] if 0 <= row < len(self._entries) else None
        available = entry is not None and entry.available
        self._ok.setEnabled(available)
        if entry is None:
            details = ''
        else:
            maturity = self.tr('Stable') if entry.maturity == 'stable' else self.tr('Experimental')
            details = self.tr('Maturity: {0}\nProvider: {1}').format(maturity, entry.provider)
            if entry.reason:
                details += f'\n{entry.reason}'
        self._details.setText(details)

    def selected_entry(self) -> MeshImportEntry | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._entries) and self._entries[row].available:
            return self._entries[row]
        return None
