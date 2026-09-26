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

Plan 33 EXPORT-04. What the dialog asks is now :class:`ExportForm`, the same
object the Export step mounts in the settings column. The window is a way of
reaching the form from the menu; it is not a second copy of what an export
asks for, and the two can no longer drift apart.
"""
from PySide6.QtWidgets import QDialog, QVBoxLayout

from .export_dialog_ui import Ui_ExportDialog
from .export_form import ExportForm


class ExportDialog(QDialog):
    def __init__(self, parent, entries=(), recommended: str = 'openfoam',
                 target_solver_name: str = ''):
        super().__init__(parent)
        self._ui = Ui_ExportDialog()
        self._ui.setupUi(self)

        self.setWindowTitle(self.tr('Export mesh'))

        # DP-202. The form used to declare a `Run solver after export`
        # checkbox that this line hid on every open, and an
        # `isRunAfterExportChecked()` that no caller in the product ever
        # asked. FoamMesh meshes; it does not run a solver. A control the
        # reader can never see is not a setting, and an accessor whose only
        # readers are tests asserting the value permanent invisibility
        # guarantees is not an answer. Both are gone from the form.

        self._ui.ok.setEnabled(False)

        self._form = ExportForm(self._ui.path, entries, recommended)
        layout = QVBoxLayout(self._ui.path)
        layout.addWidget(self._form)
        # R42. This used to call `hideValidationMessage()`, in the one dialog
        # where the location is typed rather than browsed: the sentence that
        # says "<location> is not a folder." was hidden exactly where it was
        # needed, so a disabled OK looked like a dead OK. Two clicks on it
        # did nothing at all -- no message, nothing written, nothing logged.

        # The names the rest of the application has always reached this
        # dialog by, pointing at the controls the form now owns.
        self._pathWidget = self._form.destinationWidget()
        self._format = self._form.formatBox()
        self._reason = self._form.refusalLabel()
        self._entries = self._form.entries()

        if target_solver_name:
            self.setWindowTitle(
                self.tr('Export mesh for {0}').format(target_solver_name))

        # EXPORT-02. `setMinimumWidth(500)` and `resize(500, ...)` used to
        # stand here. MEASURED at the application font: the widgets inside
        # this window ask for about 775 px and the window declared itself
        # 500 wide, so the destination rows -- which stated no height of
        # their own -- were drawn 9 and 10 px tall against a 15 px line, and
        # the label of one row overlapped the field of the next. A window
        # takes its size from what is in it; the rows state their own floor
        # in `export_form`, which is where both hosts of this form get it.

        self._connectSignalsSlots()
        self._pathChanged(self._form.projectPath())

    # ------------------------------------------------------------------ #
    # formats
    # ------------------------------------------------------------------ #

    def selectedEntry(self):
        return self._form.selectedEntry()

    def selectedFormat(self) -> str:
        return self._form.selectedFormat()

    # ------------------------------------------------------------------ #
    # destination
    # ------------------------------------------------------------------ #

    def projectPath(self):
        """The destination, carrying the chosen format's suffix."""
        return self._form.projectPath()

    def dimensionality(self) -> str:
        """`''` for the mesh as it is, else `'plane'` or `'wedge'`."""
        return self._form.dimensionality()

    def extrudeOptions(self):
        """The regions and the extrusion, when a 2D shape was chosen."""
        return self._form.extrudeOptions()

    def _connectSignalsSlots(self):
        self._form.pathChanged.connect(self._pathChanged)
        self._form.formatChanged.connect(self._formatChanged)

    def _formatChanged(self, _entry_id: str = ''):
        self._pathChanged(self._form.projectPath())

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
