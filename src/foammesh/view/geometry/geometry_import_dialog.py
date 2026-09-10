#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QListWidgetItem

from foammesh.app import app
from foammesh.core.geometry.units import UNIT_TO_M, suggest_unit
from .geometry_import_dialog_ui import Ui_ImportDialog


#: Offered in the order an engineer thinks of them, not alphabetically.
UNIT_CHOICES = (
    ('Millimetres (mm)', 'mm'),
    ('Metres (m)', 'm'),
    ('Centimetres (cm)', 'cm'),
    ('Inches (in)', 'inch'),
    ('Feet (ft)', 'ft'),
    ('Micrometres (µm)', 'um'),
)

#: Formats that declare their own length unit. Asking about them would invite
#: a user to overrule the file, which is how a correct model gets scaled wrong.
#: BREP is not one of them: it carries geometry only, so it has to be asked.
SELF_DESCRIBING = {'.step', '.stp', '.iges', '.igs'}


class ImportDialog(QDialog):
    """Choose files, say what unit they are in, and optionally split them.

    **The unit is the point.** FoamMesh works in SI metres and an STL carries
    no unit at all, so importing a millimetre part as written puts the whole
    case a thousand times out of scale -- every size, every refinement level,
    every layer thickness computed from it. `units.py` has said so in its own
    docstring from the start ("STL files carry no units, so FoamMesh must let
    the user declare the import unit") and shipped a `suggest_unit` heuristic
    for exactly this dialog, which nothing ever called.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_ImportDialog()
        self._ui.setupUi(self)

        self._dialog = None

        for label, value in UNIT_CHOICES:
            self._ui.unit.addItem(label, value)
        self._ui.unit.setCurrentIndex(
            self._ui.unit.findData(app.settings.getRecentImportUnit()))
        self._ui.buttonBox.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        self._connectSignalsSlots()

    def files(self):
        return [Path(self._ui.files.item(i).text()) for i in range(self._ui.files.count())]

    def unit(self) -> str:
        """The declared unit, or ``'m'`` when the files declare their own.

        A STEP file states its unit and the importer honours it; returning the
        combo's value for one would scale it twice.
        """
        if all(path.suffix.lower() in SELF_DESCRIBING for path in self.files()):
            return 'm'
        return self._ui.unit.currentData() or 'm'

    def featureAngle(self):
        return self._ui.featureAngle.text() if self._ui.splitSurface.isChecked() else None

    def _connectSignalsSlots(self):
        self._ui.select.clicked.connect(self._openFileDialog)

    def _openFileDialog(self):
        self._dialog = QFileDialog(self, self.tr('Select Geometry File'), app.settings.getRecentImportDirectory(), 'Geometry (*.stl *.obj *.step *.stp *.iges *.igs *.brep);;Surface (*.stl *.obj);;CAD (*.step *.stp *.iges *.igs *.brep)')
        self._dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        self._dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        self._dialog.filesSelected.connect(self._filesSelected)
        self._dialog.open()

    def _filesSelected(self, files):
        self._ui.files.clear()
        for f in files:
            self._ui.files.addItem(QListWidgetItem(f))

        self._ui.buttonBox.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

        app.settings.updateRecentImportDirectory(Path(files[0]).parent)
        self._suggestUnit()

    def accept(self):
        # Remembered so the next import opens on what this one used; the
        # measured suggestion still overrides it once files are chosen.
        app.settings.updateRecentImportUnit(self._ui.unit.currentData() or 'mm')
        super().accept()

    def _suggestUnit(self):
        """Read the model's size and propose the unit that makes it plausible.

        A guess, offered rather than applied silently: the combo is preselected
        and the reasoning is shown beside it, so a user who disagrees changes
        one control instead of discovering the mistake three steps later in a
        refinement level that makes no sense.
        """
        paths = self.files()
        selfDescribing = all(
            path.suffix.lower() in SELF_DESCRIBING for path in paths)
        self._ui.unitRow.setVisible(not selfDescribing)
        if selfDescribing:
            self._ui.unitHint.setText('')
            return

        diagonal = _diagonal(paths)
        if diagonal is None:
            self._ui.unitHint.setText('')
            return
        suggestion = suggest_unit(diagonal)
        if suggestion.unit in UNIT_TO_M:
            index = self._ui.unit.findData(suggestion.unit)
            if index >= 0:
                self._ui.unit.setCurrentIndex(index)
        self._ui.unitHint.setText(
            suggestion.message if suggestion.confident
            else self.tr('{0} Please check.').format(suggestion.message))


def _diagonal(paths):
    """Bounding-box diagonal of the first readable surface, in file units."""
    import math

    from vtkmodules.vtkIOGeometry import vtkOBJReader, vtkSTLReader

    for path in paths:
        suffix = path.suffix.lower()
        if suffix == '.stl':
            reader = vtkSTLReader()
        elif suffix == '.obj':
            reader = vtkOBJReader()
        else:
            continue
        try:
            reader.SetFileName(str(path))
            reader.Update()
            data = reader.GetOutput()
            if data is None or data.GetNumberOfPoints() == 0:
                continue
            x0, x1, y0, y1, z0, z1 = data.GetBounds()
            return math.dist((x0, y0, z0), (x1, y1, z1))
        except Exception:                                          # noqa: BLE001
            # A file the reader cannot open is the import's problem to report,
            # not the unit hint's. Fall through and leave the combo alone.
            continue
    return None
