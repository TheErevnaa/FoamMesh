"""Open or merge a file on the Gmsh model (Plan 41 GN5).

Gmsh's ``File > Open`` and ``File > Merge`` with every format the build reads
(``.geo``, STEP/IGES/BREP/XAO, STL/OBJ/PLY/OFF, ``.msh`` of any version, MED,
CGNS, UNV, BDF, Medit, VTK, NIfTI), and ``Mesh > Reclassify 2D``, ``Create
topology`` and ``Create geometry`` as actions on the model that is there:

* **File** and **Browse**; **Open** replaces the model, **Merge** adds to it.
* **Unit** and **Scale** for STEP, IGES, BREP (the unit those formats carry is
  converted to the one chosen; BREP carries none).
* **Reclassify** takes every argument of ``classifySurfaces`` (angle, boundary,
  for reparametrisation, curve angle, export discrete) and optionally runs
  ``createGeometry`` after it; **Create topology** and **Create geometry** are
  buttons of their own.  **Close into a volume** is the recipe that turns a
  closed surface mesh back into something Gmsh can mesh in 3D.

The session work runs on a worker thread; the panel only shows what it reports.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell
from foammesh.core.gmsh import import_formats as formats


class _Bridge(QObject):
    done = Signal(str, object)
    failed = Signal(str)


class GmshImportPanel(QWidget):
    """File > Open / Merge and the Reclassify / topology / geometry actions."""

    opened = Signal(object)

    def __init__(self, controller_provider, entity_panel=None, parent=None):
        super().__init__(parent)
        self._provider = controller_provider
        self._entityPanel = entity_panel
        self._controller = None
        self._worker = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self.setObjectName('gmshImportPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self._path = QLineEdit(self)
        self._path.setObjectName('gmshImportPath')
        self._path.setAccessibleName(self.tr('File to open or merge'))
        self._path.setPlaceholderText(self.tr('A file Gmsh reads'))
        self._browse = QPushButton(self.tr('Browse…'), self)
        self._browse.setObjectName('gmshImportBrowse')
        self._browse.clicked.connect(self._chooseFile)
        row.addWidget(self._path, 1)
        row.addWidget(self._browse)
        layout.addLayout(row)

        mode = QHBoxLayout()
        self._open = QRadioButton(self.tr('Open (formats only Gmsh reads)'), self)
        self._open.setObjectName('gmshImportOpen')
        self._open.setChecked(True)
        self._merge = QRadioButton(self.tr('Merge (add to the model)'), self)
        self._merge.setObjectName('gmshImportMerge')
        mode.addWidget(self._open)
        mode.addWidget(self._merge)
        layout.addLayout(mode)

        form = QFormLayout()
        self._unit = QComboBox(self)
        self._unit.setObjectName('gmshImportUnit')
        self._unit.setAccessibleName(self.tr('Target unit for STEP and IGES'))
        for value, label in formats.UNITS:
            self._unit.addItem(self.tr(label), value)
        self._unit.setToolTip(self.tr(
            'STEP and IGES carry their unit and are converted to this one. '
            'BREP carries none: use the scale factor.'))
        self._scale = CompactDoubleSpinBox(self)
        self._scale.setObjectName('gmshImportScale')
        self._scale.setAccessibleName(self.tr('Scale factor on import'))
        self._scale.setDecimals(9)
        self._scale.setRange(1e-9, 1e9)
        self._scale.setValue(1.0)
        # Plan 42 SG4 (DP-1417). The prepared model is in the shared unit.
        self._unit.setCurrentIndex(self._unit.findData(formats.SHARED_UNIT))
        form.addRow(self.tr('Unit'), self._unit)
        form.addRow(self.tr('Scale'), self._scale)
        layout.addLayout(form)

        self._classifyOnImport = QCheckBox(
            self.tr('Reclassify a surface (STL, OBJ, PLY, OFF) as it opens'),
            self)
        self._classifyOnImport.setObjectName('gmshImportClassifyOnOpen')
        layout.addWidget(self._classifyOnImport)
        self._load = QPushButton(self.tr('Open'), self)
        self._load.setObjectName('gmshImportRun')
        self._load.clicked.connect(self.run)
        layout.addWidget(self._load)
        self._merge.toggled.connect(
            lambda on: self._load.setText(
                self.tr('Merge') if on else self.tr('Open')))

        layout.addWidget(QLabel(self.tr('Reclassify 2D'), self))
        cform = QFormLayout()
        self._angle = CompactDoubleSpinBox(self)
        self._angle.setObjectName('gmshReclassifyAngle')
        self._angle.setAccessibleName(self.tr('Classification angle'))
        self._angle.setRange(0.1, 180.0)
        self._angle.setValue(self._sharedAngle())
        self._curve = CompactDoubleSpinBox(self)
        self._curve.setObjectName('gmshReclassifyCurveAngle')
        self._curve.setAccessibleName(self.tr('Curve angle'))
        self._curve.setRange(0.1, 180.0)
        self._curve.setValue(180.0)
        self._curve.setToolTip(self.tr(
            'Curves are also split where they turn by more than this; 180 '
            'degrees does not split them'))
        self._boundary = QCheckBox(self.tr('Curves on the boundary'), self)
        self._boundary.setObjectName('gmshReclassifyBoundary')
        self._boundary.setChecked(True)
        self._reparam = QCheckBox(self.tr('For reparametrisation'), self)
        self._reparam.setObjectName('gmshReclassifyReparam')
        self._reparam.setChecked(True)
        self._export = QCheckBox(self.tr('Export discrete entities'), self)
        self._export.setObjectName('gmshReclassifyExport')
        self._export.setChecked(True)
        self._geometry = QCheckBox(self.tr('Create geometry after'), self)
        self._geometry.setObjectName('gmshReclassifyCreateGeometry')
        self._geometry.setChecked(True)
        cform.addRow(self.tr('Angle'), unit_cell(self._angle, 'deg'))
        cform.addRow(self.tr('Curve angle'), unit_cell(self._curve, 'deg'))
        # Plan 48 TL: Gmsh 4.15.2 can leave createTopology (and a subset
        # Reclassify) hanging; both stop at this limit and the session stays.
        self._timeLimit = CompactDoubleSpinBox(self)
        self._timeLimit.setObjectName('gmshImportTimeLimit')
        self._timeLimit.setAccessibleName(self.tr('Time limit'))
        self._timeLimit.setDecimals(0)
        self._timeLimit.setRange(0.0, 86400.0)
        self._timeLimit.setValue(300.0)
        self._timeLimit.setToolTip(self.tr(
            'Reclassify and Create topology stop after this many seconds if '
            'Gmsh 4.15.2 does not return (it can hang in createTopology); '
            'the session is restarted with the model as it was. 0 waits '
            'without a limit.'))
        cform.addRow(self.tr('Time limit'), unit_cell(self._timeLimit, 's'))
        for widget in (self._boundary, self._reparam, self._export,
                       self._geometry):
            cform.addRow(widget)
        layout.addLayout(cform)
        actions = QHBoxLayout()
        self._reclassify = QPushButton(self.tr('Reclassify'), self)
        self._reclassify.setObjectName('gmshReclassifyRun')
        self._reclassify.clicked.connect(self.reclassify)
        self._topology = QPushButton(self.tr('Create topology'), self)
        self._topology.setObjectName('gmshCreateTopology')
        self._topology.clicked.connect(self.createTopology)
        self._createGeometry = QPushButton(self.tr('Create geometry'), self)
        self._createGeometry.setObjectName('gmshCreateGeometry')
        self._createGeometry.clicked.connect(self.createGeometry)
        self._close = QPushButton(self.tr('Close into a volume'), self)
        self._close.setObjectName('gmshCloseIntoVolume')
        self._close.setToolTip(self.tr(
            'Drop the volumes, reclassify and parametrise the surfaces, and '
            'close them with one surface loop and one volume, so the model '
            'can be meshed in 3D again'))
        self._close.clicked.connect(self.closeIntoVolume)
        for widget in (self._reclassify, self._topology, self._createGeometry,
                       self._close):
            actions.addWidget(widget)
        layout.addLayout(actions)

        self._status = QLabel('', self)
        self._status.setObjectName('gmshImportStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    # -- state -------------------------------------------------------------- #

    @staticmethod
    def _sharedAngle() -> float:
        """The project feature angle, the shared value (SG4)."""
        from foammesh.core.facade.feature_angle import normalise
        try:
            from foammesh.app import app
            return normalise(app.db.getValue('mesh/featureAngle'))
        except Exception:                                   # noqa: BLE001
            return normalise(None)

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def timeLimit(self) -> float:
        return float(self._timeLimit.value())

    def showTimeLimit(self, limits: dict) -> None:
        """Show the project's limit for Reclassify / Create topology."""
        value = limits.get('topology', limits.get('reclassify'))
        if value is not None:
            self._timeLimit.setValue(float(value))

    def _applyTimeLimit(self, controller) -> None:
        """Hand the typed limit to the project (Plan 48 TL)."""
        setter = getattr(controller, 'set_time_limit', None)
        if setter is None:
            return
        for kind in ('reclassify', 'topology'):
            setter(kind, self.timeLimit())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        try:
            controller = self._controller or self._provider()
            limits = getattr(controller, 'time_limits', None)
            if limits is not None and not self.busy():
                self.showTimeLimit(limits())
        except Exception:                               # noqa: BLE001
            pass

    def status(self) -> str:
        return self._status.text()

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def setPath(self, path) -> None:
        self._path.setText(str(path))

    def setMode(self, mode: str) -> None:
        (self._merge if mode == 'merge' else self._open).setChecked(True)
        self._load.setText(self.tr('Merge') if mode == 'merge'
                           else self.tr('Open'))

    def mode(self) -> str:
        return 'merge' if self._merge.isChecked() else 'open'

    def _chooseFile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr('Open or merge a file'), '',
            formats.file_filter(self.mode()))
        if path:
            self._path.setText(path)

    def reclassifyValues(self) -> dict:
        return {'angle': self._angle.value(),
                'boundary': self._boundary.isChecked(),
                'reparametrize': self._reparam.isChecked(),
                'curve_angle': self._curve.value(),
                'export_discrete': self._export.isChecked(),
                'create_geometry': self._geometry.isChecked()}

    # -- actions ------------------------------------------------------------ #

    def run(self) -> None:
        """Open or Merge the file in the field."""
        if self.busy():
            return
        path = self._path.text().strip()
        options = {}
        try:
            carries_unit = (formats.classify(path).kind == 'cad'
                            and formats.extension(path) != '.xao')
        except formats.ImportRefusal:
            carries_unit = True      # plan_open names the refusal below
        if carries_unit:
            options = {'unit': self._unit.currentData(),
                       'scaling': self._scale.value()}
        if self._classifyOnImport.isChecked():
            values = self.reclassifyValues()
            options['classify_angle'] = values.pop('angle')
            options.update(values)
        try:
            formats.refuse_open(path, self.mode())
            formats.plan_open(path, self.mode(), **options)
        except formats.ImportRefusal as error:
            self._say(str(error))
            return
        self._start('open', lambda c: c.open_model(path, self.mode(),
                                                   **options))

    def reclassify(self) -> None:
        if self.busy():
            return
        values = self.reclassifyValues()
        try:
            formats.reclassify_arguments(**values)
        except formats.ImportRefusal as error:
            self._say(str(error))
            return
        self._start('reclassify', lambda c: c.reclassify(**values))

    def createTopology(self) -> None:
        if not self.busy():
            self._start('topology', lambda c: c.create_topology())

    def createGeometry(self) -> None:
        if not self.busy():
            self._start('geometry', lambda c: c.create_geometry())

    def closeIntoVolume(self) -> None:
        if self.busy():
            return
        values = self.reclassifyValues()
        self._start('close', lambda c: c.remesh_surfaces(
            angle=values['angle'], curve_angle=values['curve_angle']))

    def _start(self, what: str, call) -> None:
        try:
            controller = self._controller = (
                self._controller or self._provider())
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._say(self.tr('Working…'))
        if what in ('reclassify', 'topology'):
            try:
                self._applyTimeLimit(controller)
            except Exception as error:                  # noqa: BLE001
                self._say(str(error))
                return

        def work():
            try:
                self._bridge.done.emit(what, call(controller))
            except Exception as error:                  # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=work, name='gmsh-import',
                                        daemon=True)
        self._worker.start()

    def _onDone(self, what: str, result) -> None:
        self._worker = None
        result = result or {}
        after = result.get('after') or result.get('entities') or {}
        if what == 'open':
            counts = result.get('entities') or {}
            text = self.tr(
                '%(kind)s opened: %(p)s points, %(c)s curves, %(s)s surfaces, '
                '%(v)s volumes; %(n)d nodes, %(e)d elements.') % {
                    'kind': result.get('kind', ''), 'p': counts.get('0', 0),
                    'c': counts.get('1', 0), 's': counts.get('2', 0),
                    'v': counts.get('3', 0), 'n': result.get('nodes', 0),
                    'e': result.get('elements', 0)}
            self._say(text)
        else:
            self._say(self.tr('Done: %s surfaces, %s volumes.')
                      % (after.get('2', 0), after.get('3', 0)))
        self.opened.emit(result)
        if self._entityPanel is not None:
            self._entityPanel.showModel()

    def _onFailed(self, message: str) -> None:
        self._worker = None
        self._say(message)
