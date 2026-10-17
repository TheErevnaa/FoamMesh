"""Plan 40 UXE1 (F7b). The Keyboard shortcuts dialog: search, rebind, print.

Opens from Help > Keyboard shortcuts and from F1.  Every command of the
registry is a row; type in the box to filter, pick a row, press the new keys
in the editor.  A key another command has is refused with that command's
name, so the table never holds a clash.  The sheet button prints the table or
saves it as a PDF.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QKeySequenceEdit,
    QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from foammesh.view.main_window import shortcut_registry as registry_module
from foammesh.view.main_window.shortcut_registry import GROUPS


class ShortcutsDialog(QDialog):
    ID_ROLE = Qt.ItemDataRole.UserRole

    def __init__(self, parent=None, registry=None):
        super().__init__(parent)
        self._registry = registry or registry_module.REGISTRY
        self.setObjectName('shortcutsDialog')
        self.setWindowTitle(self.tr('Keyboard shortcuts'))
        self.resize(640, 560)

        self._search = QLineEdit(self)
        self._search.setObjectName('shortcutsSearch')
        self._search.setPlaceholderText(
            self.tr('Type a command or a key to filter the list'))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self.applyFilter)

        self._tree = QTreeWidget(self)
        self._tree.setObjectName('shortcutsTree')
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(
            [self.tr('Command'), self.tr('Keys'), self.tr('Works')])
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.header().setStretchLastSection(False)
        self._tree.setColumnWidth(0, 300)
        self._tree.setColumnWidth(1, 170)
        self._tree.currentItemChanged.connect(self._currentChanged)

        self._name = QLabel(self.tr('Pick a command to give it other keys.'))
        self._name.setObjectName('shortcutsPicked')
        self._editor = QKeySequenceEdit(self)
        self._editor.setObjectName('shortcutsEditor')
        self._editor.setEnabled(False)
        self._editor.editingFinished.connect(self._editorFinished)
        self._clear = QPushButton(self.tr('Clear'), self)
        self._clear.setObjectName('shortcutsClear')
        self._clear.setToolTip(self.tr('Leave this command without a key'))
        self._clear.clicked.connect(self._clearPicked)
        self._reset = QPushButton(self.tr('Reset'), self)
        self._reset.setObjectName('shortcutsReset')
        self._reset.setToolTip(self.tr('Give this command its own keys back'))
        self._reset.clicked.connect(self._resetPicked)
        self._resetAll = QPushButton(self.tr('Reset all'), self)
        self._resetAll.setObjectName('shortcutsResetAll')
        self._resetAll.clicked.connect(self._resetEverything)
        self._message = QLabel('')
        self._message.setObjectName('shortcutsMessage')
        self._message.setWordWrap(True)

        edit = QHBoxLayout()
        edit.addWidget(self._editor, 1)
        for button in (self._clear, self._reset, self._resetAll):
            edit.addWidget(button)

        self._print = QPushButton(self.tr('Print sheet…'), self)
        self._print.setObjectName('shortcutsPrint')
        self._print.clicked.connect(self.printSheet)
        self._pdf = QPushButton(self.tr('Save sheet as PDF…'), self)
        self._pdf.setObjectName('shortcutsPdf')
        self._pdf.clicked.connect(self.saveSheetAsPdf)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(self._print)
        bottom.addWidget(self._pdf)
        bottom.addStretch(1)
        bottom.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self._search)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._name)
        layout.addLayout(edit)
        layout.addWidget(self._message)
        layout.addLayout(bottom)

        self._unsubscribe = self._registry.subscribe(lambda _id: self.refresh())
        self.refresh()

    def closeEvent(self, event):
        self._unsubscribe()
        super().closeEvent(event)

    # ---- the list --------------------------------------------------------

    def refresh(self) -> None:
        picked = self.pickedId()
        self._tree.blockSignals(True)
        self._tree.clear()
        groups = {}
        for group in GROUPS:
            item = QTreeWidgetItem(self._tree, [self.tr(group)])
            item.setFirstColumnSpanned(True)
            groups[group] = item
        for command in self._registry.commands():
            row = QTreeWidgetItem(groups[command.group], [
                self.tr(command.title),
                self._registry.display(command.id) or self.tr('Not set'),
                self.tr(registry_module._WHERE.get(command.scope, ''))])
            row.setData(0, self.ID_ROLE, command.id)
            if self._registry.isChanged(command.id):
                font = row.font(1)
                font.setBold(True)
                row.setFont(1, font)
                row.setToolTip(1, self.tr('Changed from the default: %s')
                               % ', '.join(self._registry.default(command.id)))
        fixed = QTreeWidgetItem(self._tree, [self.tr('Keys that belong to a widget')])
        fixed.setFirstColumnSpanned(True)
        for where, key, what in registry_module.FIXED_KEYS:
            QTreeWidgetItem(fixed, [f'{where}: {what}', key, self.tr('fixed')])
        self._tree.expandAll()
        self._tree.blockSignals(False)
        self.applyFilter(self._search.text())
        if picked:
            self.pick(picked)

    def applyFilter(self, text: str) -> int:
        """Hide the rows that do not match *text*; returns the rows shown."""
        needle = (text or '').strip().lower()
        shown = 0
        for index in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(index)
            visible = 0
            for child in range(group.childCount()):
                row = group.child(child)
                hay = ' '.join(row.text(c) for c in range(3)).lower()
                match = not needle or needle in hay
                row.setHidden(not match)
                visible += int(match)
            group.setHidden(visible == 0)
            shown += visible
        return shown

    def visibleRows(self) -> list:
        """The command ids showing now, in order."""
        out = []
        for index in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(index)
            if group.isHidden():
                continue
            for child in range(group.childCount()):
                row = group.child(child)
                command_id = row.data(0, self.ID_ROLE)
                if command_id and not row.isHidden():
                    out.append(command_id)
        return out

    # ---- picking and editing --------------------------------------------

    def pickedId(self) -> str:
        item = self._tree.currentItem()
        return (item.data(0, self.ID_ROLE) or '') if item is not None else ''

    def pick(self, command_id: str) -> bool:
        for index in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(index)
            for child in range(group.childCount()):
                row = group.child(child)
                if row.data(0, self.ID_ROLE) == command_id:
                    self._tree.setCurrentItem(row)
                    return True
        return False

    def _currentChanged(self, current, _previous) -> None:
        command_id = (current.data(0, self.ID_ROLE) or '') if current else ''
        self._editor.setEnabled(bool(command_id))
        self._clear.setEnabled(bool(command_id))
        self._reset.setEnabled(bool(command_id))
        if not command_id:
            self._name.setText(self.tr('Pick a command to give it other keys.'))
            self._editor.clear()
            return
        command = self._registry.command(command_id)
        self._name.setText(self.tr('Keys for "%s"') % self.tr(command.title))
        keys = self._registry.keys(command_id)
        self._editor.setKeySequence(QKeySequence(keys[0]) if keys
                                    else QKeySequence())
        self._message.setText('')

    def _editorFinished(self) -> None:
        command_id = self.pickedId()
        sequence = self._editor.keySequence()
        if not command_id or sequence.isEmpty():
            return
        self.assign(command_id, sequence.toString(registry_module.PORTABLE))

    def assign(self, command_id: str, key: str) -> tuple:
        """Make *key* the command's first key; the others stay."""
        keys = list(self._registry.keys(command_id))
        wanted = registry_module.normalise(key)
        rest = [k for k in keys[1:] if k != wanted]
        ok, why = self._registry.assign(command_id, [wanted] + rest)
        self._message.setText(why)
        if not ok:
            current = self._registry.keys(command_id)
            self._editor.setKeySequence(QKeySequence(current[0]) if current
                                        else QKeySequence())
        return ok, why

    def _clearPicked(self) -> None:
        command_id = self.pickedId()
        if command_id:
            self._registry.assign(command_id, [])
            self._editor.clear()

    def _resetPicked(self) -> None:
        command_id = self.pickedId()
        if command_id:
            self._registry.reset(command_id)
            self._message.setText('')
            self._currentChanged(self._tree.currentItem(), None)

    def _resetEverything(self) -> None:
        self._registry.resetAll()
        self._message.setText(self.tr('Every command has its own keys again.'))

    # ---- the sheet -------------------------------------------------------

    def printSheet(self) -> bool:
        from PySide6.QtGui import QTextDocument
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if QPrintDialog(printer, self).exec() != QDialog.DialogCode.Accepted:
            return False
        document = QTextDocument()
        document.setHtml(self._registry.sheet_html())
        document.print_(printer)
        return True

    def saveSheetAsPdf(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr('Save the shortcut sheet'),
            'foammesh-shortcuts.pdf', self.tr('PDF (*.pdf)'))
        if not path:
            return False
        return self._registry.write_sheet_pdf(path)


def show(window) -> ShortcutsDialog:
    """Open (or raise) the one dialog of *window*."""
    existing = getattr(window, '_shortcutsDialog', None)
    if existing is not None:
        try:
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return existing
            existing.deleteLater()
        except RuntimeError:
            pass
    dialog = ShortcutsDialog(window)
    window._shortcutsDialog = dialog
    dialog.show()
    return dialog
