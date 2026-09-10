"""Unified Case Tools export chooser and completion surfaces (§13.4).

One consistent availability policy: every format stays visible; unavailable
formats are disabled with the exact capability reason, so the UI never
presents an unavailable exporter as runnable.
"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QVBoxLayout,
)

from foammesh.core.import_export import ExportEntry


def format_size(total_bytes: int) -> str:
    value = float(max(0, total_bytes))
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB':
            return f'{value:,.1f} {unit}' if unit != 'B' else f'{int(value):,} B'
        value /= 1024


class CaseExportDialog(QDialog):
    def __init__(self, entries: tuple[ExportEntry, ...], parent=None):
        super().__init__(parent)
        self._entries = tuple(entries)
        self.setWindowTitle(self.tr('Export Case'))
        self.resize(560, 460)
        layout = QVBoxLayout(self)

        self._list = QListWidget()
        for entry in self._entries:
            label = entry.label
            if entry.maturity == 'experimental':
                label = self.tr('{0}  [experimental]').format(label)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry.entry_id)
            if not entry.available:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(entry.reason)
            else:
                item.setToolTip(entry.notes)
            self._list.addItem(item)
        layout.addWidget(self._list)

        self._details = QLabel()
        self._details.setWordWrap(True)
        layout.addWidget(self._details)

        self._options = QGroupBox(self.tr('Write settings'))
        options_layout = QVBoxLayout(self._options)
        self._ascii = QRadioButton(self.tr('ASCII'))
        self._binary = QRadioButton(self.tr('Binary'))
        self._ascii.setChecked(True)
        self._compression = QCheckBox(self.tr('Compress written files'))
        self._commit = QCheckBox(self.tr('Keep these write settings in controlDict'))
        for widget in (self._ascii, self._binary, self._compression, self._commit):
            options_layout.addWidget(widget)
        self._options.setVisible(False)
        layout.addWidget(self._options)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText(self.tr('Export'))
        self._ok.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._list.currentRowChanged.connect(self._selectionChanged)

    def _selectionChanged(self, row: int):
        entry = self._entries[row] if 0 <= row < len(self._entries) else None
        if entry is None or not entry.available:
            self._ok.setEnabled(False)
            self._details.setText(entry.reason if entry else '')
            self._options.setVisible(False)
            return
        self._ok.setEnabled(True)
        details = entry.notes
        if entry.maturity == 'experimental':
            details = self.tr('Experimental format. {0}').format(details)
        self._details.setText(details)
        self._options.setVisible(entry.entry_id == 'openfoam_format')

    def selected_entry(self) -> ExportEntry | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._entries) and self._entries[row].available:
            return self._entries[row]
        return None

    def format_convert_options(self) -> dict:
        return {'write_format': 'binary' if self._binary.isChecked() else 'ascii',
                'compression': self._compression.isChecked(),
                'commit_settings': self._commit.isChecked()}


class ExportCompletionDialog(QDialog):
    """§13.4 completion: exact output path, size, warnings, Open Folder."""

    def __init__(self, destination, total_bytes: int, warnings=(), parent=None):
        super().__init__(parent)
        self._destination = destination
        self.setWindowTitle(self.tr('Export Complete'))
        self.resize(560, 300)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr('Output: {0}').format(destination)))
        layout.addWidget(QLabel(self.tr('Size: {0}').format(format_size(total_bytes))))
        if warnings:
            text = QPlainTextEdit('\n'.join(str(item) for item in warnings))
            text.setReadOnly(True)
            layout.addWidget(QLabel(self.tr('Warnings:')))
            layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        open_folder = QPushButton(self.tr('Open Folder'))
        buttons.addButton(open_folder, QDialogButtonBox.ButtonRole.ActionRole)
        open_folder.clicked.connect(self._openFolder)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _openFolder(self):
        from pathlib import Path
        target = Path(self._destination)
        folder = target if target.is_dir() else target.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
