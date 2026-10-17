"""Geometry > Construct (Plan 42 SG21, D6).

Combine the solids that were imported, make primitives, and move things, on
the Gmsh OpenCASCADE kernel, before the engine is chosen.  The result is
written as STEP and brought in through the shared import, so snappyHexMesh and
Gmsh both mesh a constructed part.

* The table lists the solid models of the case and the shapes made here.  The
  *Object* and *Tool* columns are what the next operation acts on.
* **Kind** (Boolean, Primitive, Transform) and **Operation** choose the step;
  the fields below are made from the Plan 41 catalogue, so a number may be an
  expression over the parameters of the Gmsh route.
* **Apply** runs the step; **Add to the case** writes the STEP and imports it.

The work runs on a worker thread; the panel only shows what it reports.  A
mesh (STL) cannot be combined and is listed as such.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from foammesh.core.geometry import construct as core
from foammesh.core.gmsh import geometry_build as gb

KINDS = (('a', 'Boolean'), ('b', 'Primitive'), ('c', 'Transform'))
_OBJECT, _TOOL = 1, 2


class _Bridge(QObject):
    done = Signal(object)
    failed = Signal(str)


class ConstructPanel(QWidget):
    """The table of shapes, the operation form, and the buttons."""

    #: (STEP path, geometry ids of the sources it was made from)
    resultReady = Signal(str, list)

    def __init__(self, entries_provider, work_dir_provider, session_provider=None,
                 parent=None):
        super().__init__(parent)
        self._entries = entries_provider
        self._workDir = work_dir_provider
        self._sessionProvider = session_provider
        self._construction = None
        self._sources: dict = {}        # label -> entry (solid models)
        self._loaded: dict = {}         # label -> entry, already on the session
        self._worker = None
        self._fields: dict = {}
        self._exports = 0
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self.setObjectName('geometryConstructPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget(0, 3, self)
        self._table.setObjectName('constructShapes')
        self._table.setAccessibleName(self.tr('Shapes to construct from'))
        self._table.setHorizontalHeaderLabels(
            [self.tr('Shape'), self.tr('Object'), self.tr('Tool')])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(0, 200)
        self._table.setMinimumHeight(110)
        layout.addWidget(self._table)

        pick = QHBoxLayout()
        self._kind = QComboBox(self)
        self._kind.setObjectName('constructKind')
        self._kind.setAccessibleName(self.tr('Kind of operation'))
        for phase, title in KINDS:
            self._kind.addItem(self.tr(title), phase)
        self._operation = QComboBox(self)
        self._operation.setObjectName('constructOperation')
        self._operation.setAccessibleName(self.tr('Operation'))
        pick.addWidget(self._kind)
        pick.addWidget(self._operation, 1)
        layout.addLayout(pick)
        self._kind.currentIndexChanged.connect(self._fillOperations)
        self._operation.currentIndexChanged.connect(self._fillForm)

        self._form = QFormLayout()
        host = QWidget(self)
        host.setLayout(self._form)
        layout.addWidget(host)

        buttons = QHBoxLayout()
        self._apply = QPushButton(self.tr('Apply'), self)
        self._apply.setObjectName('constructApply')
        self._apply.clicked.connect(self.apply)
        self._add = QPushButton(self.tr('Add to the case'), self)
        self._add.setObjectName('constructAdd')
        self._add.setToolTip(self.tr(
            'Write what is left as a STEP file and import it like any other '
            'geometry'))
        self._add.clicked.connect(self.addToCase)
        self._reset = QPushButton(self.tr('Start over'), self)
        self._reset.setObjectName('constructReset')
        self._reset.clicked.connect(self.startOver)
        for button in (self._apply, self._add, self._reset):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self._remove = QCheckBox(
            self.tr('Remove the imported geometries it was made from'), self)
        self._remove.setObjectName('constructRemoveSources')
        self._remove.setChecked(True)
        layout.addWidget(self._remove)

        self._steps = QListWidget(self)
        self._steps.setObjectName('constructSteps')
        self._steps.setAccessibleName(self.tr('Steps so far'))
        self._steps.setMaximumHeight(90)
        layout.addWidget(self._steps)
        self._warning = QLabel('', self)
        self._warning.setObjectName('constructNamesWarning')
        self._warning.setWordWrap(True)
        layout.addWidget(self._warning)
        self._status = QLabel('', self)
        self._status.setObjectName('constructStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._fillOperations()
        self.refresh()

    # -- the table ----------------------------------------------------------- #

    def refresh(self) -> None:
        """Read the case's solid models again and redraw the table."""
        ticked = self._ticked()
        self._sources = {}
        self._meshes = []
        for entry in self._entries():
            name = entry.get('name') or entry.get('geometry_id')
            if entry.get('cad_artifact') and core.is_solid_source(
                    entry['cad_artifact']):
                self._sources[str(name)] = entry
            else:
                self._meshes.append(str(name))
        made = self._made()
        rows = ([(n, True) for n in self._sources if n not in self._loaded
                 ] + [(n, True) for n in made]
                + [(n, False) for n in self._meshes])
        self._table.setRowCount(len(rows))
        for row, (name, usable) in enumerate(rows):
            item = QTableWidgetItem(name if usable else
                                    self.tr('%s (a mesh: cannot be combined)')
                                    % name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, usable)
            self._table.setItem(row, 0, item)
            for column in (_OBJECT, _TOOL):
                cell = QTableWidgetItem()
                flags = Qt.ItemFlag.ItemIsEnabled
                if usable:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                cell.setFlags(flags)
                cell.setCheckState(
                    Qt.CheckState.Checked
                    if (name, column) in ticked and usable
                    else Qt.CheckState.Unchecked)
                self._table.setItem(row, column, cell)
        risky = core.names_at_risk(self._sources.values())
        self._warning.setText(
            self.tr('Surface names typed on %s are not kept: the constructed '
                    'part is written as STEP, which holds no face names.')
            % ', '.join(str(n) for n in risky) if risky else '')

    def _made(self) -> list:
        c = self._construction
        if c is None:
            return []
        return list(c.labels())

    def _ticked(self) -> set:
        out = set()
        for row in range(self._table.rowCount()):
            name = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if not self._table.item(row, 0).data(
                    Qt.ItemDataRole.UserRole + 1):
                continue
            for column in (_OBJECT, _TOOL):
                cell = self._table.item(row, column)
                if cell is not None and cell.checkState() == \
                        Qt.CheckState.Checked:
                    out.add((name, column))
        return out

    def shapes(self) -> list:
        return [self._table.item(r, 0).data(Qt.ItemDataRole.UserRole)
                for r in range(self._table.rowCount())]

    def tick(self, name: str, role: str, on: bool = True) -> None:
        column = _OBJECT if role == 'object' else _TOOL
        for row in range(self._table.rowCount()):
            if self._table.item(row, 0).data(Qt.ItemDataRole.UserRole) == name:
                self._table.item(row, column).setCheckState(
                    Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                return
        raise KeyError(name)

    def _chosen(self, column) -> list:
        return [n for n, c in sorted(self._ticked()) if c == column]

    # -- the form ------------------------------------------------------------ #

    def kind(self) -> str:
        return self._kind.currentData()

    def selectOperation(self, op_id: str) -> None:
        phase = core.operation_phase(op_id)
        self._kind.setCurrentIndex(self._kind.findData(phase))
        self._operation.setCurrentIndex(self._operation.findData(op_id))

    def operationId(self) -> str:
        return self._operation.currentData()

    def _fillOperations(self) -> None:
        self._operation.blockSignals(True)
        self._operation.clear()
        for op_id in core.PHASES[self.kind()]:
            self._operation.addItem(self.tr(gb.operation(op_id).title), op_id)
        self._operation.blockSignals(False)
        self._fillForm()

    def _fillForm(self) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._fields = {}
        op_id = self.operationId()
        if not op_id:
            return
        for spec in core.operation_fields(op_id):
            if spec.kind == gb.BOOLEAN:
                widget = QCheckBox(self)
                widget.setChecked(bool(spec.default))
            else:
                widget = QLineEdit(self)
                default = spec.default
                widget.setText('' if default in (None, '') else (
                    ('%g' % default) if isinstance(default, float)
                    else str(default)))
            widget.setObjectName('constructField_' + spec.name)
            widget.setAccessibleName(spec.title)
            self._fields[spec.name] = widget
            self._form.addRow(self.tr(spec.title), widget)
        phase = core.operation_phase(op_id)
        if phase in ('a', 'c'):
            text = (self.tr('Keep the tools') if phase == 'a'
                    else self.tr('Keep the original (make a copy)'))
            box = QCheckBox(self)
            box.setObjectName('constructKeep')
            box.setAccessibleName(text)
            self._fields['_keep'] = box
            self._form.addRow(text, box)

    def values(self) -> dict:
        out = {}
        for name, widget in self._fields.items():
            if name == '_keep':
                continue
            out[name] = (widget.isChecked() if isinstance(widget, QCheckBox)
                         else widget.text())
        return out

    def setValue(self, name: str, value) -> None:
        widget = self._fields[name]
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        else:
            widget.setText(str(value))

    def setKeep(self, on: bool) -> None:
        self._fields['_keep'].setChecked(on)

    def _keep(self) -> bool:
        box = self._fields.get('_keep')
        return bool(box is not None and box.isChecked())

    # -- running ------------------------------------------------------------- #

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def status(self) -> str:
        return self._status.text()

    def steps(self) -> list:
        return [self._steps.item(i).text() for i in range(self._steps.count())]

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def _construct(self):
        if self._construction is None:
            work = Path(self._workDir())
            provider = self._sessionProvider or (
                lambda: core.default_session(work / 'session'))
            self._construction = core.Construction(provider)
        return self._construction

    def apply(self) -> None:
        if self.busy():
            return
        op_id = self.operationId()
        objects, tools = self._chosen(_OBJECT), self._chosen(_TOOL)
        values, keep = self.values(), self._keep()
        self._say(self.tr('Working…'))
        self._start(lambda: self._do(op_id, objects, tools, values, keep))

    def _start(self, job) -> None:
        def work():
            try:
                self._bridge.done.emit(job())
            except Exception as error:                     # noqa: BLE001
                self._bridge.failed.emit(str(error) or error.__class__.__name__)

        self._worker = threading.Thread(target=work, name='geometry-construct',
                                        daemon=True)
        self._worker.start()

    def _bring_in(self, construction, names) -> list:
        """Import the sources named, as they are first used."""
        mapped = []
        for name in names:
            entry = self._sources.get(name)
            if entry is None or name in self._loaded:
                mapped.append(name)
                continue
            label = construction.add_source(name, entry['cad_artifact'])
            self._loaded[name] = entry
            mapped.append(label)
        return mapped

    def _do(self, op_id, objects, tools, values, keep):
        construction = self._construct()
        phase = core.operation_phase(op_id)
        if phase == 'a':
            objects = self._bring_in(construction, objects)
            tools = self._bring_in(construction, tools)
            label = construction.boolean(op_id, objects, tools,
                                         keep_tools=keep)
            return ('%s: %s' % (gb.operation(op_id).title, label))
        if phase == 'b':
            label = construction.primitive(op_id, values)
            return ('%s: %s' % (gb.operation(op_id).title, label))
        names = self._bring_in(construction, objects)
        done = construction.transform(op_id, names, values, copy=keep)
        return ('%s: %s' % (gb.operation(op_id).title, ', '.join(done)))

    def _onDone(self, result) -> None:
        self._worker = None
        if isinstance(result, dict):                       # an export
            sources = [self._loaded[n].get('geometry_id')
                       for n in self._loaded]
            self._say(self.tr('Written as %s; importing it.')
                      % Path(result['path']).name)
            self.resultReady.emit(
                result['path'],
                [s for s in sources if s] if self._remove.isChecked() else [])
            return
        self._steps.addItem(str(result))
        self._say(str(result))
        self.refresh()

    def _onFailed(self, message: str) -> None:
        self._worker = None
        self._say(message)

    def addToCase(self) -> None:
        if self.busy():
            return
        construction = self._construction
        if construction is None or not construction.labels():
            self._say(self.tr('Nothing has been constructed yet.'))
            return
        self._exports += 1
        path = Path(self._workDir()) / ('constructed_%d.step' % self._exports)
        self._say(self.tr('Writing the STEP file…'))
        self._start(lambda: construction.export(path))

    def startOver(self) -> None:
        if self.busy():
            return
        construction, self._construction = self._construction, None
        self._loaded = {}
        self._steps.clear()
        if construction is not None:
            construction.close()
        self._say(self.tr('Started over.'))
        self.refresh()

    def closeSession(self) -> None:
        construction, self._construction = self._construction, None
        if construction is not None:
            construction.close()


class ConstructDialog(QDialog):
    """Geometry > Construct, in a window of its own."""

    def __init__(self, panel: ConstructPanel, parent=None):
        super().__init__(parent)
        self.setObjectName('geometryConstructDialog')
        self.setWindowTitle(self.tr('Construct geometry'))
        self.setModal(False)
        self.panel = panel
        panel.setParent(self)
        layout = QVBoxLayout(self)
        layout.addWidget(panel)
        self.resize(560, 640)
