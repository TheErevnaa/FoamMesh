"""Build geometry on the Gmsh model (Plan 41 GN3).

Gmsh's Geometry > Elementary entities > Add and Transform, with every entity
field picked in the viewport rather than typed as a tag:

* **Parameters**: named values (``R = 25``); any number field takes an
  expression that names them (``2*R + 1``).
* **Operation**: a group (Points, Curves, Surfaces and volumes, Primitives,
  Transform, Extrude, Boolean, Delete, Presets) and one operation in it; the
  form below is made from :mod:`foammesh.core.gmsh.geometry_build`.
* An entity field has **Pick** (it starts the viewport pick on the shared
  entity panel, with the filter of that field), the chips of what was picked,
  and Clear.  **Use last result** fills a loop field from the loop just made.
* **Build** runs the operation on the session, redraws the model, and lists
  the step.  A refusal says why and changes nothing.

The session work runs on a worker thread; the panel only shows what it reports.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.core.gmsh import geometry_build as gb
from foammesh.core.gmsh import session_entities as entities
from foammesh.view.workflow_controls.length_box import (
    LengthExpressionEdit, length_expression_cell,
)

_FILTER = {0: 'Points', 1: 'Curves', 2: 'Surfaces', 3: 'Volumes'}


class _Bridge(QObject):
    done = Signal(object)
    failed = Signal(str)


class _EntityField(QWidget):
    """Pick / chips / Clear for one entity-list field."""

    pickRequested = Signal(object)          # this field

    def __init__(self, spec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.value: tuple = ()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._button = QPushButton(self.tr('Pick'), self)
        self._button.setObjectName('gmshBuildPick_' + spec.name)
        self._button.setAccessibleName(
            self.tr('Pick %s in the viewport') % spec.title)
        self._button.clicked.connect(lambda: self.pickRequested.emit(self))
        self._chips = QLabel(self.tr('nothing picked'), self)
        self._chips.setObjectName('gmshBuildChips_' + spec.name)
        self._chips.setWordWrap(True)
        self._clear = QPushButton(self.tr('Clear'), self)
        self._clear.setObjectName('gmshBuildClear_' + spec.name)
        self._clear.clicked.connect(lambda: self.set(()))
        row.addWidget(self._button)
        row.addWidget(self._chips, 1)
        row.addWidget(self._clear)

    def set(self, picked) -> None:
        self.value = tuple(tuple(e) for e in picked)
        self._chips.setText(
            ' '.join(entities.chip_text(e) for e in self.value)
            or self.tr('nothing picked'))

    def filterName(self):
        return _FILTER.get(self.spec.dim)


class GmshGeometryBuildPanel(QWidget):
    """Parameters, an operation form, Build, and the list of steps."""

    built = Signal(object)

    def __init__(self, controller_provider, entity_panel=None, parent=None):
        super().__init__(parent)
        self._provider = controller_provider
        self._entityPanel = entity_panel
        self._controller = None
        self._worker = None
        self._fields: dict = {}
        self._target = None
        self._lastLoop = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self.setObjectName('gmshGeometryBuildPanel')
        if entity_panel is not None:
            entity_panel.pickAccepted.connect(self._onPicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # parameters ---------------------------------------------------- #
        layout.addWidget(QLabel(self.tr('Parameters'), self))
        row = QHBoxLayout()
        self._pname = QLineEdit(self)
        self._pname.setObjectName('gmshBuildParamName')
        self._pname.setPlaceholderText(self.tr('Name, for example R'))
        self._pname.setAccessibleName(self.tr('Parameter name'))
        self._pvalue = QLineEdit(self)
        self._pvalue.setObjectName('gmshBuildParamValue')
        self._pvalue.setPlaceholderText(self.tr('Value or expression'))
        self._pvalue.setAccessibleName(self.tr('Parameter value'))
        self._padd = QPushButton(self.tr('Set parameter'), self)
        self._padd.setObjectName('gmshBuildParamSet')
        self._padd.clicked.connect(self.setParameter)
        for widget in (self._pname, self._pvalue, self._padd):
            row.addWidget(widget)
        layout.addLayout(row)
        self._ptable = QTableWidget(0, 3, self)
        self._ptable.setObjectName('gmshBuildParams')
        self._ptable.setAccessibleName(self.tr('Parameters'))
        self._ptable.setHorizontalHeaderLabels(
            [self.tr('Name'), self.tr('Expression'), self.tr('Value')])
        self._ptable.setMaximumHeight(110)
        layout.addWidget(self._ptable)

        # the operation ------------------------------------------------- #
        pick = QHBoxLayout()
        self._group = QComboBox(self)
        self._group.setObjectName('gmshBuildGroup')
        self._group.setAccessibleName(self.tr('Kind of operation'))
        for group in gb.GROUPS:
            if group != 'Parameter':
                self._group.addItem(self.tr(group), group)
        self._operation = QComboBox(self)
        self._operation.setObjectName('gmshBuildOperation')
        self._operation.setAccessibleName(self.tr('Operation'))
        pick.addWidget(self._group)
        pick.addWidget(self._operation, 1)
        layout.addLayout(pick)
        self._group.currentIndexChanged.connect(self._fillOperations)
        self._operation.currentIndexChanged.connect(self._fillForm)
        self._form = QFormLayout()
        self._formHost = QWidget(self)
        self._formHost.setLayout(self._form)
        layout.addWidget(self._formHost)

        buttons = QHBoxLayout()
        self._show = QPushButton(self.tr('Show model'), self)
        self._show.setObjectName('gmshBuildShow')
        self._show.setToolTip(self.tr(
            'Draw the model so entities can be picked'))
        self._show.clicked.connect(self.showModel)
        self._build = QPushButton(self.tr('Build'), self)
        self._build.setObjectName('gmshBuildRun')
        self._build.clicked.connect(self.build)
        self._loop = QPushButton(self.tr('Use last loop'), self)
        self._loop.setObjectName('gmshBuildUseLoop')
        self._loop.setToolTip(self.tr(
            'Fill the loop field with the loop made last'))
        self._loop.clicked.connect(self.useLastLoop)
        for widget in (self._show, self._loop, self._build):
            buttons.addWidget(widget)
        layout.addLayout(buttons)

        self._steps = QListWidget(self)
        self._steps.setObjectName('gmshBuildSteps')
        self._steps.setAccessibleName(self.tr('Geometry built so far'))
        self._steps.setMaximumHeight(110)
        layout.addWidget(self._steps)
        self._status = QLabel('', self)
        self._status.setObjectName('gmshBuildStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._fillOperations()

    # -- the form ----------------------------------------------------------- #

    def _fillOperations(self) -> None:
        group = self._group.currentData()
        self._operation.blockSignals(True)
        self._operation.clear()
        for op in gb.operations_in(group):
            self._operation.addItem(self.tr(op.title), op.id)
        self._operation.blockSignals(False)
        self._fillForm()

    def operationId(self) -> str:
        return self._operation.currentData()

    def selectOperation(self, op_id: str) -> None:
        op = gb.operation(op_id)
        self._group.setCurrentIndex(self._group.findData(op.group))
        self._operation.setCurrentIndex(self._operation.findData(op_id))

    def _fillForm(self) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._fields = {}
        op_id = self._operation.currentData()
        if not op_id:
            return
        for spec in gb.operation(op_id).fields:
            if spec.kind == gb.ENTITIES:
                widget = _EntityField(spec, self)
                widget.pickRequested.connect(self._requestPick)
            elif spec.kind == gb.BOOLEAN:
                widget = QCheckBox(self)
                widget.setChecked(bool(spec.default))
            elif spec.kind == gb.CHOICE:
                widget = QComboBox(self)
                widget.addItems(list(spec.choices))
                widget.setCurrentText(str(spec.default))
            else:
                # UXB3c: a length is typed in the project display unit;
                # values() hands the operation model units.
                widget = (LengthExpressionEdit(self) if spec.length
                          else QLineEdit(self))
                default = spec.default
                text = '' if default in (None, '') else (
                    ('%g' % default) if isinstance(default, float)
                    else str(default))
                if spec.length:
                    widget.setModelText(text)
                else:
                    widget.setText(text)
                if spec.help:
                    widget.setToolTip(spec.help)
            widget.setObjectName('gmshBuildField_' + spec.name)
            widget.setAccessibleName(spec.title)
            self._fields[spec.name] = widget
            if spec.length:
                self._form.addRow(self.tr(spec.title),
                                  length_expression_cell(widget, self))
            else:
                self._form.addRow(self.tr(spec.title), widget)

    def values(self) -> dict:
        out = {}
        for name, widget in self._fields.items():
            if isinstance(widget, _EntityField):
                out[name] = widget.value
            elif isinstance(widget, QCheckBox):
                out[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                out[name] = widget.currentText()
            elif isinstance(widget, LengthExpressionEdit):
                out[name] = widget.modelText()
            else:
                out[name] = widget.text()
        return out

    def setValue(self, name: str, value) -> None:
        widget = self._fields[name]
        if isinstance(widget, _EntityField):
            widget.set(value)
        elif isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QComboBox):
            widget.setCurrentText(str(value))
        elif isinstance(widget, LengthExpressionEdit):
            widget.setModelText(str(value))
        else:
            widget.setText(str(value))

    # -- picking ------------------------------------------------------------ #

    def _requestPick(self, field) -> None:
        if self._entityPanel is None:
            self._say(self.tr('There is no viewport to pick in.'))
            return
        self._target = field
        if not self._entityPanel.pickFor(field.filterName()):
            self._target = None
            self._say(self._entityPanel.status())
            return
        self._say(self.tr('Pick %s in the viewport; Enter accepts.')
                  % field.spec.title.lower())

    def _onPicked(self, chosen) -> None:
        field, self._target = self._target, None
        if field is None:
            return
        field.set(chosen)
        self._say(self.tr('%d picked for %s.')
                  % (len(chosen), field.spec.title.lower()))

    # -- parameters --------------------------------------------------------- #

    def setParameter(self) -> None:
        name, expression = self._pname.text().strip(), self._pvalue.text()
        try:
            controller = self._controller = (
                self._controller or self._provider())
            parameters = controller.parameters()
            parameters.set(name, expression)
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._showParameters(parameters)
        self._say(self.tr('Parameter %s set.') % name)
        # the value also reaches Gmsh's parser, so an exported script has it
        self._run(('parameter', {'name': name, 'value': expression}))

    def _showParameters(self, parameters) -> None:
        rows = parameters.rows()
        self._ptable.setRowCount(len(rows))
        for row, (name, text, value) in enumerate(rows):
            for column, cell in enumerate((name, text, '%g' % value)):
                self._ptable.setItem(row, column, QTableWidgetItem(cell))

    # -- running ------------------------------------------------------------ #

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def status(self) -> str:
        return self._status.text()

    def steps(self) -> list:
        return [self._steps.item(i).text() for i in range(self._steps.count())]

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def showModel(self) -> None:
        if self._entityPanel is not None:
            self._entityPanel.showModel()

    def useLastLoop(self) -> None:
        if self._lastLoop is None:
            self._say(self.tr('No loop has been made yet.'))
            return
        for name in ('loops', 'shells'):
            if name in self._fields:
                self.setValue(name, str(self._lastLoop))
                return
        self._say(self.tr('This operation has no loop field.'))

    def build(self) -> None:
        if self.busy():
            return
        self._run((self.operationId(), self.values()))

    def _run(self, job) -> None:
        op_id, values = job
        try:
            controller = self._controller = (
                self._controller or self._provider())
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._say(self.tr('Building…'))

        def work():
            try:
                self._bridge.done.emit(controller.build(op_id, values))
            except Exception as error:                  # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=work, name='gmsh-build',
                                        daemon=True)
        self._worker.start()

    def _onDone(self, step) -> None:
        self._worker = None
        created = ' '.join(entities.chip_text(tuple(c))
                           for c in step.created) or self.tr('nothing new')
        self._steps.addItem('%s: %s' % (step.title, created))
        loop = getattr(step, 'loop', None)
        if loop is not None:
            self._lastLoop = loop
        self._say(self.tr('%s: %s.') % (step.title, created))
        self.built.emit(step)
        if self._entityPanel is not None and step.op_id != 'parameter':
            self._entityPanel.showModel()

    def _onFailed(self, message: str) -> None:
        self._worker = None
        self._say(message)
