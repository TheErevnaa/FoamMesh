#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The 3D export chooser: which mesh file, and where to write it.

Plan 28 WP3. This dialog used to ask only for a destination, because the page
behind it could write exactly one thing -- an OpenFOAM case directory. An SU2
project could therefore be meshed in this application and never exported from
it; ``foammesh export <case> su2`` was the only way out, which is not a thing
a person using a mesher is supposed to have to know.

The format list is the facade's, per case: SU2 reads "ready" only when this
mesh's own census says SU2 can open it, and otherwise carries the sentence
that says how much of the mesh is not readable and what to do about it.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QVBoxLayout

from widgets.new_project_widget import NewProjectWidget

from .export_dialog_ui import Ui_ExportDialog


class ExportDialog(QDialog):
    def __init__(self, parent, entries=(), recommended: str = 'openfoam',
                 target_solver_name: str = ''):
        super().__init__(parent)
        self._ui = Ui_ExportDialog()
        self._ui.setupUi(self)

        self._entries = tuple(entries)
        self._pathWidget = NewProjectWidget(self._ui.path, suffix=None)

        self.setWindowTitle(self.tr('Export Mesh'))
        self._ui.run.setVisible(False)

        self._ui.ok.setEnabled(False)

        layout = QVBoxLayout(self._ui.path)
        layout.addWidget(self._pathWidget)
        # R42. This used to call `hideValidationMessage()`, in the one dialog
        # where the location is typed rather than browsed: the sentence that
        # says "<location> is not a folder." was hidden exactly where it was
        # needed, so a disabled OK looked like a dead OK. Two clicks on it
        # did nothing at all -- no message, nothing written, nothing logged.

        self._format = QComboBox(self)
        self._reason = QLabel(self)
        self._reason.setWordWrap(True)
        if self._entries:
            # Above the destination, because it decides what the destination
            # means: a directory for an OpenFOAM case, one file for the rest.
            self._ui.verticalLayout.insertWidget(0, QLabel(self.tr('Format')))
            self._ui.verticalLayout.insertWidget(1, self._format)
            self._ui.verticalLayout.insertWidget(2, self._reason)
            self._fillFormats(recommended)
        if target_solver_name:
            self.setWindowTitle(
                self.tr('Export Mesh for {0}').format(target_solver_name))

        # G7. A floor as well as a starting size: the dialog no longer
        # grows with the text inside it, so it must not collapse either.
        self.setMinimumWidth(500)
        self.resize(500, self.size().height())

        self._connectSignalsSlots()

    # ------------------------------------------------------------------ #
    # formats
    # ------------------------------------------------------------------ #

    def _fillFormats(self, recommended: str) -> None:
        """Every format stays listed; an unusable one is disabled, not hidden.

        Hiding SU2 from a snappyHexMesh project would leave the user hunting
        for a format that is not missing but refused, and never reading why.
        """
        chosen = -1
        for index, entry in enumerate(self._entries):
            label = entry['label']
            if not entry['available']:
                label = self.tr('{0}  (not available)').format(label)
            self._format.addItem(label, entry['entry_id'])
            item = self._format.model().item(index)
            if item is not None and not entry['available']:
                item.setEnabled(False)
            self._format.setItemData(index, entry['reason'] or entry['notes'],
                                     Qt.ItemDataRole.ToolTipRole)
            if entry['entry_id'] == recommended and entry['available']:
                chosen = index
        if chosen < 0:
            chosen = next((index for index, entry in enumerate(self._entries)
                           if entry['available']), 0)
        self._format.setCurrentIndex(chosen)
        self._formatChanged(chosen)

    def selectedEntry(self):
        entry_id = self._format.currentData()
        for entry in self._entries:
            if entry['entry_id'] == entry_id:
                return entry
        return None

    def selectedFormat(self) -> str:
        entry = self.selectedEntry()
        return entry['entry_id'] if entry else 'openfoam'

    # ------------------------------------------------------------------ #
    # destination
    # ------------------------------------------------------------------ #

    def projectPath(self):
        """The destination, carrying the chosen format's suffix.

        A directory destination (the OpenFOAM case) takes no suffix; a file
        one does, and appending it here is what keeps ``duct`` from being
        written as an extension-less file the solver will not recognise.
        """
        path = self._pathWidget.projectPath()
        entry = self.selectedEntry()
        if path is None or entry is None:
            return path
        suffix = entry.get('suffix') or ''
        if entry.get('destination') == 'file' and suffix and path.suffix != suffix:
            return path.with_name(path.name + suffix)
        return path

    def isRunAfterExportChecked(self):
        return self._ui.run.isChecked()

    def _connectSignalsSlots(self):
        self._pathWidget.pathChanged.connect(self._pathChanged)
        self._format.currentIndexChanged.connect(self._formatChanged)

    def _formatChanged(self, index: int):
        entry = self._entries[index] if 0 <= index < len(self._entries) else None
        if entry is None:
            self._reason.setText('')
            return
        self._reason.setText(entry['reason'] or entry['notes'] or '')
        # R125. An OpenFOAM case is a folder that gets created; every other
        # format on this list is one file with an extension. The destination
        # preview said "folder" for both until it was told.
        self._pathWidget.setDestination(entry.get('destination') or 'directory',
                                        entry.get('suffix') or '')
        self._pathChanged(self._pathWidget.projectPath())

    def _pathChanged(self, path):
        entry = self.selectedEntry()
        usable = entry is None or entry['available']
        self._ui.ok.setEnabled(path is not None and usable)
        # R42. A disabled OK that keeps its accent fill is indistinguishable
        # from a slow export, so the button carries the reason it cannot be
        # pressed rather than leaving the user clicking it.
        if self._ui.ok.isEnabled():
            self._ui.ok.setToolTip('')
        else:
            self._ui.ok.setToolTip(
                self._pathWidget.validationMessage()
                or (entry['reason'] if entry and not usable else '')
                or self.tr('Give the export a name and a folder that exists.'))
