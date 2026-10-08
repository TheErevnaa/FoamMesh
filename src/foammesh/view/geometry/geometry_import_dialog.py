#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog,
                               QLabel, QListWidgetItem, QMessageBox)

from foammesh.app import app
from foammesh.core.geometry.measure import format_size, union_bounds
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

#: Formats whose size the dialog can read before import: a VTK reader gives
#: the box. A CAD file needs the OpenCASCADE import itself to be measured.
MEASURABLE = {'.stl', '.obj'}

#: The split dialog's own slider and validator: whole degrees, 0 to 180.
FEATURE_ANGLE_RANGE = (0, 180)


def parseFeatureAngle(text) -> int:
    """The split feature angle, in whole degrees, or a plain refusal.

    DP-637 (field audit 0924 D-SH-03). The field used to be read with a bare
    ``float()`` outside any ``try``: ``60deg`` raised out of the import slot
    with nothing on screen, after the CAD half of a mixed selection had
    already been imported, and an empty field quietly meant "no split".
    """
    low, high = FEATURE_ANGLE_RANGE
    refusal = ValueError(
        f'Feature angle must be a whole number of degrees from {low} to '
        f'{high}; "{str(text or "").strip()}" is not. Untick "Split surface" '
        'to import without splitting.')
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        raise refusal from None
    if not low <= value <= high:
        raise refusal
    return value


def _caseSources():
    """``(engine id, store entries)`` of the open case, empty when unknown."""
    engine, entries = '', []
    try:
        from foammesh.core.engine.registry import configured_engine_id
        engine = configured_engine_id(app.db)
    except Exception:                                      # noqa: BLE001
        pass
    project = getattr(app, 'project', None)
    if project is not None and getattr(project, 'path', None) is not None:
        try:
            from foammesh.core.geometry import GeometryArtifactStore
            entries = GeometryArtifactStore(project.path).entries()
        except (OSError, ValueError):
            entries = []
    return engine, entries


def _mixesCadAndSurfaces(engine) -> bool:
    from foammesh.core.engine.registry import ENGINE_REGISTRY
    if engine not in ENGINE_REGISTRY.ids():
        return True
    return getattr(ENGINE_REGISTRY.get(engine), 'mixes_cad_and_surfaces', True)


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
        #: DP-1182. The box of each chosen surface file, in file units, read
        #: once when the files are chosen and re-scaled on a unit change.
        self._measured = {}
        self._sizeLabel = QLabel(self)
        self._sizeLabel.setObjectName('modelSize')
        self._sizeLabel.setWordWrap(True)
        self._sizeLabel.setVisible(False)
        layout = self._ui.verticalLayout
        layout.insertWidget(layout.indexOf(self._ui.unitRow) + 1,
                            self._sizeLabel)

        for label, value in UNIT_CHOICES:
            self._ui.unit.addItem(label, value)
        self._ui.unit.setCurrentIndex(
            self._ui.unit.findData(app.settings.getRecentImportUnit()))
        self._ui.buttonBox.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        # DP-637. Letters cannot be typed; what a paste or an empty field
        # leaves is refused at OK, before anything is imported.
        self._ui.featureAngle.setValidator(
            QIntValidator(*FEATURE_ANGLE_RANGE, self))

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
        self._ui.unit.currentIndexChanged.connect(self._showSize)

    def _openFileDialog(self):
        self._dialog = QFileDialog(self, self.tr('Select geometry file'), app.settings.getRecentImportDirectory(), 'Geometry (*.stl *.obj *.step *.stp *.iges *.igs *.brep);;Surface (*.stl *.obj);;CAD (*.step *.stp *.iges *.igs *.brep)')
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
        self._measured = {path: _fileBounds(path) for path in self.files()
                          if path.suffix.lower() in MEASURABLE}
        self._suggestUnit()
        self._showSize()

    def sizeText(self) -> str:
        """The chosen files' size in the chosen unit, or why it is not known.

        DP-1182. The dialog said which unit a diagonal suggested and nothing
        else, so a user could not check the one number that settles the
        unit -- how big the part is -- before committing to it. Every
        surface file chosen is measured (their combined box), scaled by the
        unit they are declared in and written the way the rest of the
        window writes a length. A CAD file is measured by the import itself,
        and the line says that rather than leaving it out silently.
        """
        paths = self.files()
        if not paths:
            return ''
        cad = [path for path in paths if path.suffix.lower() not in MEASURABLE]
        bounds = union_bounds(box for box in self._measured.values()
                              if box is not None)
        size = ''
        if bounds is not None:
            unit = self._ui.unit.currentData() or 'm'
            size = format_size(bounds, UNIT_TO_M.get(unit, 1.0))
        if size:
            text = self.tr('Size: {0}').format(size)
            if len(paths) > 1:
                text = self.tr('Size of the surface files together: {0}'
                               ).format(size)
            if cad:
                text += self.tr('. CAD files are measured on import.')
            return text
        if cad:
            return self.tr('The size of a CAD file is shown on the Geometry '
                           'page once it is imported.')
        return self.tr('The size could not be read from the chosen files.')

    def _showSize(self, *_args):
        text = self.sizeText()
        self._sizeLabel.setText(text)
        self._sizeLabel.setVisible(bool(text))

    def refusal(self):
        """Why OK cannot import this selection, or ``None``."""
        if self._ui.splitSurface.isChecked():
            try:
                parseFeatureAngle(self._ui.featureAngle.text())
            except ValueError as error:
                return str(error)
        # DP-638. Refused here, before the first file goes in: the page
        # imports the CAD half of a selection before the surfaces.
        engine, entries = _caseSources()
        if not _mixesCadAndSurfaces(engine):
            from foammesh.core.geometry.store import gmsh_mixed_sources_refusal
            return gmsh_mixed_sources_refusal(entries, self.files())
        return None

    def accept(self):
        refusal = self.refusal()
        if refusal:
            QMessageBox.warning(self, self.tr('Import geometry'), refusal)
            return
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

        diagonal = None
        for path in paths:
            # The boxes read when the files were chosen (DP-1182), so the
            # file is not read a second time for the hint.
            box = (self._measured[path] if path in self._measured
                   else _fileBounds(path))
            diagonal = _diagonalOf(box)
            if diagonal is not None:
                break
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


def _fileBounds(path):
    """The box of one STL/OBJ file in file units, or ``None``."""
    from vtkmodules.vtkIOGeometry import vtkOBJReader, vtkSTLReader

    suffix = Path(path).suffix.lower()
    if suffix == '.stl':
        reader = vtkSTLReader()
    elif suffix == '.obj':
        reader = vtkOBJReader()
    else:
        return None
    try:
        reader.SetFileName(str(path))
        reader.Update()
        data = reader.GetOutput()
        if data is None or data.GetNumberOfPoints() == 0:
            return None
        return tuple(float(value) for value in data.GetBounds())
    except Exception:                                          # noqa: BLE001
        # A file the reader cannot open is the import's problem to report,
        # not the unit hint's. Fall through and leave the combo alone.
        return None


def _diagonalOf(bounds):
    import math

    if bounds is None:
        return None
    x0, x1, y0, y1, z0, z1 = bounds
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _diagonal(paths):
    """Bounding-box diagonal of the first readable surface, in file units."""
    for path in paths:
        diagonal = _diagonalOf(_fileBounds(path))
        if diagonal is not None:
            return diagonal
    return None
