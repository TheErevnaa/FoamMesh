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
* **Geometry kernel** (Plan 48 GM5): OpenCASCADE (default) or Built-in, chosen
  per project.  An operation the chosen kernel does not have stays in the list,
  greyed, with the reason as its tool tip.  Changing the kernel after
  something was built asks first, because Gmsh cannot mix the two.
* **Remove last command** takes back the last built step: the session empties
  the model and runs the rest of its history again.
* **Patch roles** (Plan 48 WG, DP-4215): a 2D-case or wedge preset says which
  of the groups it made are the front and back faces of the one-cell-thick
  mesh. They are listed here with the category each publishes as (``empty``,
  ``wedge``, or another the person chooses), and follow a rename made in the
  physical-group table. The publisher writes those types and refuses a mesh
  that is not a planar pair one cell thick.

The session work runs on a worker thread; the panel only shows what it reports.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from foammesh.core.facade.plan48 import pa3_routes
from foammesh.core.gmsh import checkpoints
from foammesh.core.gmsh import geometry_build as gb
from foammesh.core.gmsh import layer_preview
from foammesh.core.gmsh import session_entities as entities
from foammesh.view.combo_items import set_item_enabled
from foammesh.view.workflow_controls.length_box import (
    LengthExpressionEdit, length_expression_cell,
)

_FILTER = {0: 'Points', 1: 'Curves', 2: 'Surfaces', 3: 'Volumes'}


class _Bridge(QObject):
    done = Signal(object)
    failed = Signal(str)
    removed = Signal(object)
    previewed = Signal(object)
    rolesFound = Signal(object)


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
    removedLast = Signal(object)
    kernelChanged = Signal(str)
    #: Plan 48 GM6. The line segments ``[[p0, p1], ...]`` of the last sweep
    #: preview (the arrow or axis and the layer boundaries); the viewport
    #: draws them before anything is built.
    sweepPreview = Signal(object)

    def __init__(self, controller_provider, entity_panel=None, parent=None):
        super().__init__(parent)
        self._provider = controller_provider
        self._entityPanel = entity_panel
        if entity_panel is not None and hasattr(entity_panel, 'groupsChanged'):
            # DP-5725: a rename in the group table renames the declared role
            # and the built surface the run names
            entity_panel.groupsChanged.connect(self.refreshRoles)
        self._controller = None
        self._worker = None
        self._fields: dict = {}
        self._target = None
        self._lastLoop = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self._bridge.removed.connect(self._onRemoved)
        self._bridge.previewed.connect(self._onPreviewed)
        self._bridge.rolesFound.connect(self.showRoles)
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
        self._plabel = QLineEdit(self)
        self._plabel.setObjectName('gmshBuildParamLabel')
        self._plabel.setPlaceholderText(self.tr('Label (optional)'))
        self._plabel.setAccessibleName(self.tr('Parameter label'))
        self._ppath = QLineEdit(self)
        self._ppath.setObjectName('gmshBuildParamPath')
        self._ppath.setPlaceholderText(self.tr('Path (optional)'))
        self._ppath.setAccessibleName(self.tr('Parameter path'))
        self._padd = QPushButton(self.tr('Set parameter'), self)
        self._padd.setObjectName('gmshBuildParamSet')
        self._padd.clicked.connect(self.setParameter)
        for widget in (self._pname, self._pvalue, self._plabel, self._ppath,
                       self._padd):
            row.addWidget(widget)
        layout.addLayout(row)
        self._ptable = QTableWidget(0, 3, self)
        self._ptable.setObjectName('gmshBuildParams')
        self._ptable.setAccessibleName(self.tr('Parameters'))
        self._ptable.setHorizontalHeaderLabels(
            [self.tr('Name'), self.tr('Expression'), self.tr('Value')])
        self._ptable.setMaximumHeight(110)
        layout.addWidget(self._ptable)

        # the geometry kernel (GM5) ------------------------------------- #
        kernels = QHBoxLayout()
        kernels.addWidget(QLabel(self.tr('Geometry kernel'), self))
        self._kernel = QComboBox(self)
        self._kernel.setObjectName('gmshBuildKernel')
        self._kernel.setAccessibleName(self.tr('Geometry kernel'))
        self._kernel.setToolTip(self.tr(
            'OpenCASCADE builds solids and Booleans; the built-in kernel '
            'builds from points, lines and extrusions. Gmsh cannot mix them '
            'in one model.'))
        for kernel in gb.KERNELS:
            self._kernel.addItem(self.tr(gb.KERNEL_TITLES[kernel]), kernel)
        self._kernel.currentIndexChanged.connect(self._onKernelPicked)
        kernels.addWidget(self._kernel, 1)
        layout.addLayout(kernels)

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

        # Plan 48 GM6: the layer table of an extrusion and its preview --- #
        self._layers = QWidget(self)
        self._layers.setObjectName('gmshBuildLayerTable')
        box = QFormLayout(self._layers)
        box.setContentsMargins(0, 0, 0, 0)
        grading = QHBoxLayout()
        self._grading = QComboBox(self._layers)
        self._grading.setObjectName('gmshBuildGrading')
        self._grading.setAccessibleName(self.tr('Layer grading'))
        for key, title in layer_preview.GRADINGS:
            self._grading.addItem(self.tr(title), key)
        self._gradeLayers = QLineEdit('5', self._layers)
        self._gradeLayers.setObjectName('gmshBuildGradeLayers')
        self._gradeLayers.setAccessibleName(self.tr('Number of layers'))
        self._gradeRatio = QLineEdit('1.2', self._layers)
        self._gradeRatio.setObjectName('gmshBuildGradeRatio')
        self._gradeRatio.setAccessibleName(self.tr('Growth ratio'))
        self._gradeFirst = QLineEdit('', self._layers)
        self._gradeFirst.setObjectName('gmshBuildGradeFirst')
        self._gradeFirst.setAccessibleName(self.tr('First layer thickness'))
        self._gradeFirst.setPlaceholderText(self.tr('first height'))
        self._makeTable = QPushButton(self.tr('Make table'), self._layers)
        self._makeTable.setObjectName('gmshBuildMakeTable')
        self._makeTable.clicked.connect(self.makeLayerTable)
        for widget in (self._grading, self._gradeLayers, self._gradeRatio,
                       self._gradeFirst, self._makeTable):
            grading.addWidget(widget)
        box.addRow(self.tr('Grading'), grading)
        self._table = QLineEdit(self._layers)
        self._table.setObjectName('gmshBuildLayerRows')
        self._table.setAccessibleName(self.tr('Layer table'))
        self._table.setPlaceholderText(self.tr(
            'count@cumulative height, e.g. 2@0.4, 3@1 (blank: the layer '
            'count above)'))
        self._table.textChanged.connect(self._tableChanged)
        box.addRow(self.tr('Layer table'), self._table)
        self._tableSummary = QLabel('', self._layers)
        self._tableSummary.setObjectName('gmshBuildLayerSummary')
        self._tableSummary.setWordWrap(True)
        box.addRow('', self._tableSummary)
        layout.addWidget(self._layers)

        buttons = QHBoxLayout()
        self._previewSweep = QPushButton(self.tr('Preview layers'), self)
        self._previewSweep.setObjectName('gmshBuildPreview')
        self._previewSweep.setToolTip(self.tr(
            'Draw the direction and where each layer falls before building'))
        self._previewSweep.clicked.connect(self.previewSweep)
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
        self._undo = QPushButton(self.tr('Remove last command'), self)
        self._undo.setObjectName('gmshBuildRemoveLast')
        self._undo.setToolTip(self.tr(
            'Take back the last built step; the model is rebuilt from the '
            'steps before it'))
        self._undo.clicked.connect(self.removeLast)
        for widget in (self._show, self._previewSweep, self._loop, self._undo,
                       self._build):
            buttons.addWidget(widget)
        layout.addLayout(buttons)

        self._steps = QListWidget(self)
        self._steps.setObjectName('gmshBuildSteps')
        self._steps.setAccessibleName(self.tr('Geometry built so far'))
        self._steps.setMaximumHeight(110)
        layout.addWidget(self._steps)
        # Plan 48 WG (DP-4215): the front / back / sides a preset declared.
        layout.addWidget(QLabel(self.tr('Patch roles'), self))
        self._roles = QTableWidget(0, 3, self)
        self._roles.setObjectName('gmshBuildRoles')
        self._roles.setAccessibleName(self.tr('Patch roles'))
        self._roles.setToolTip(self.tr(
            'The faces a 2D-case or wedge preset made, and the category each '
            'is published with. Rename a patch in the physical groups table; '
            'its role follows.'))
        self._roles.setHorizontalHeaderLabels(
            [self.tr('Patch'), self.tr('Role'), self.tr('Publishes as')])
        self._roles.setMaximumHeight(110)
        layout.addWidget(self._roles)
        self._status = QLabel('', self)
        self._status.setObjectName('gmshBuildStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._fillOperations()
        # Plan 48 PA3: the local API drives this panel as a person does.
        self._api = BuildApi(self)
        session = pa3_routes.announce_from_app('build', self._api)
        if session is not None:
            self.destroyed.connect(
                lambda *_a, s=session, a=self._api: pa3_routes.unregister_panel(
                    s, 'build', a))

    # -- the form ----------------------------------------------------------- #

    def _fillOperations(self) -> None:
        group = self._group.currentData()
        kernel = self.kernel()
        self._operation.blockSignals(True)
        self._operation.clear()
        first = -1
        for op in gb.operations_in(group):
            self._operation.addItem(self.tr(op.title), op.id)
            index = self._operation.count() - 1
            reason = gb.unavailable(op.id, kernel)
            if reason:
                # DP-5603: no QStandardItem wrapper (DP-1447, combo_items)
                set_item_enabled(self._operation, index, False)
                self._operation.setItemData(
                    index, self.tr(reason), Qt.ItemDataRole.ToolTipRole)
            elif first < 0:
                first = index
        if first >= 0:
            self._operation.setCurrentIndex(first)
        self._operation.blockSignals(False)
        self._fillForm()

    def kernel(self) -> str:
        return self._kernel.currentData() or gb.DEFAULT_KERNEL

    def unavailableReason(self, op_id: str) -> str:
        """Why *op_id* is greyed on the chosen kernel ('' when it is not)."""
        return gb.unavailable(op_id, self.kernel())

    def _currentController(self):
        """DP-5721. The open case's session controller, asked each time.

        The page outlives a case (it follows a new case's controller,
        WALK-2), and this panel kept the first one it was handed: after
        another case was opened, Build went into the first case's session
        and the run of the open case never saw it."""
        controller = self._provider()
        if controller is not self._controller:
            known = self._controller is not None
            self._controller = controller
            if known:
                self._followCase(controller)
        return controller

    def _followCase(self, controller) -> None:
        """Show the open case's build, not the one before it."""
        self._lastLoop = None
        self._target = None
        self._steps.clear()
        try:
            builder = controller.builder()
        except Exception:                               # noqa: BLE001
            builder = None
        if builder is not None:
            for step in builder.steps:
                self._steps.addItem(self._stepLine(step))
            index = self._kernel.findData(builder.kernel)
            if index >= 0:
                self._kernel.blockSignals(True)
                self._kernel.setCurrentIndex(index)
                self._kernel.blockSignals(False)
                self._fillOperations()
        self.showRoles()

    def showEvent(self, event):
        super().showEvent(event)
        self.syncSteps()

    def syncSteps(self) -> bool:
        """DP-5733. Show the builder's steps when they changed elsewhere.

        Remove last command in the script panel (Export page) takes a step
        off the same builder; this list kept the step until the next case
        was opened. Returns whether the list was rebuilt."""
        if self.busy():
            return False
        try:
            builder = self._currentController().builder()
        except Exception:                               # noqa: BLE001
            return False
        lines = [self._stepLine(step) for step in builder.steps]
        if lines == self.steps():
            return False
        self._steps.clear()
        for line in lines:
            self._steps.addItem(line)
        self.showRoles()
        return True

    def _builder(self):
        controller = self._currentController()
        builder = controller.builder()
        case_path = getattr(controller, 'case_path', None)
        if case_path is not None:
            checkpoints.bind(builder, case_path)
        return builder

    def _onKernelPicked(self) -> None:
        kernel = self.kernel()
        try:
            builder = self._builder()
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        if builder.kernel == kernel:
            self._fillOperations()
            return
        if not self.setKernel(kernel, confirmed=None):
            self._kernel.blockSignals(True)
            self._kernel.setCurrentIndex(self._kernel.findData(
                builder.kernel))
            self._kernel.blockSignals(False)

    def setKernel(self, kernel: str, confirmed=True) -> bool:
        """Choose the project's kernel.  With geometry built it asks first
        (*confirmed* ``None``) or goes ahead (``True``); ``False`` refuses."""
        try:
            builder = self._builder()
            if builder.kernel == kernel:
                return True
            built = [s for s in builder.steps if s.op_id != 'parameter']
            if built:
                if confirmed is None:
                    from widgets import async_message_box
                    confirmed = async_message_box.question(
                        self, self.tr('Change the geometry kernel'),
                        self.tr(checkpoints.describe_kernel_change(
                            builder, kernel)) + '\n\n'
                        + self.tr('Change it?')) == QMessageBox.Yes
                if not confirmed:
                    self._say(self.tr('The geometry kernel was not changed.'))
                    return False
            changed = builder.set_kernel(kernel, confirm=True)
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return False
        self._kernel.blockSignals(True)
        self._kernel.setCurrentIndex(self._kernel.findData(kernel))
        self._kernel.blockSignals(False)
        # Plan 48 GB: the steps the new kernel has were built again; the
        # list shows them, and the status line names the ones it refused.
        self._steps.clear()
        for step in builder.steps:
            self._steps.addItem(self._stepLine(step))
        self._fillOperations()
        self.showRoles()
        self._say(self.reimportText(changed or {}, kernel))
        self.kernelChanged.emit(kernel)
        return True

    def reimportText(self, changed: dict, kernel: str) -> str:
        """The status line after a kernel change: what was built again and
        what was refused, by name."""
        text = self.tr('Geometry kernel: %s.') % gb.KERNEL_TITLES[kernel]
        if not changed.get('cleared'):
            return text
        again = len(changed.get('reimported') or ())
        text += ' ' + (self.tr('1 step built again.') if again == 1 else
                       self.tr('%d steps built again.') % again)
        refused = changed.get('refused') or ()
        if refused:
            text += ' ' + self.tr('Refused: %s.') % '; '.join(
                '%s (step %d): %s' % (r['title'], r['step'], r['reason'])
                for r in refused)
        return text

    def _created(self, step) -> str:
        made = ' '.join(entities.chip_text(tuple(c)) for c in step.created)
        if made:
            return made
        # A loop or a parametric geometry is not a model entity; its number
        # is what a later step asks for (DP-5558).
        if getattr(step, 'geometry', None) is not None:
            return self.tr('geometry %d') % step.geometry
        if getattr(step, 'loop', None) is not None:
            return self.tr('loop %d') % step.loop
        return self.tr('nothing new')

    def _stepLine(self, step) -> str:
        return '%s: %s' % (step.title, self._created(step))

    def removeLast(self) -> None:
        if self.busy():
            return
        try:
            builder = self._builder()
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._say(self.tr('Removing the last command…'))

        def work():
            try:
                self._bridge.removed.emit(checkpoints.remove_last(builder))
            except Exception as error:                  # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=work, name='gmsh-build-undo',
                                        daemon=True)
        self._worker.start()

    def _onRemoved(self, result) -> None:
        self._worker = None
        for _ in result.get('removed', ()):
            if self._steps.count():
                self._steps.takeItem(self._steps.count() - 1)
        self._say(self.tr(result.get('text') or 'Removed.'))
        self.showRoles()
        self.removedLast.emit(result)
        if self._entityPanel is not None:
            self._entityPanel.showModel()

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
        self._layers.setVisible(op_id in gb.LAYER_TABLE_OPS)
        self._previewSweep.setVisible(
            bool(op_id) and gb.operation(op_id).kind
            in gb.GeometryBuilder.SWEEPS)
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
        if self.operationId() in gb.LAYER_TABLE_OPS \
                and self._table.text().strip():
            out['layer_table'] = self._table.text().strip()
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
            controller = self._currentController()
            parameters = controller.parameters()
            parameters.set(name, expression)
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._showParameters(parameters)
        self._say(self.tr('Parameter %s set.') % name)
        # the value also reaches Gmsh's parser, so an exported script has it
        sent = {'name': name, 'value': expression}
        if self._plabel.text().strip():
            sent['label'] = self._plabel.text().strip()
        if self._ppath.text().strip():
            sent['path'] = self._ppath.text().strip()
        self._run(('parameter', sent))

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

    # -- the layer table and the sweep preview (Plan 48 GM6) ----------------- #

    def makeLayerTable(self) -> None:
        """Fill the layer table from the grading preset."""
        try:
            first = self._gradeFirst.text().strip()
            rows = layer_preview.grade(
                self._grading.currentData(),
                int(float(self._gradeLayers.text() or 0)),
                float(self._gradeRatio.text() or 1.0),
                float(first) if first else None,
                self._totalLength())
        except (layer_preview.LayerTableRefusal, ValueError) as error:
            self._say(str(error))
            return
        self._table.setText(layer_preview.table_text(rows))

    def _totalLength(self) -> float:
        """The length the first-height preset measures against: the typed
        distance or the length of the vector, else 1."""
        values = {}
        for name in ('distance', 'thickness', 'dz', 'dx', 'dy'):
            widget = self._fields.get(name)
            if widget is None:
                continue
            text = (widget.modelText() if isinstance(
                widget, LengthExpressionEdit) else widget.text())
            try:
                values[name] = abs(float(text))
            except (TypeError, ValueError):
                continue
        for name in ('distance', 'thickness'):
            if values.get(name):
                return values[name]
        vector = [values.get(n, 0.0) for n in ('dx', 'dy', 'dz')]
        length = sum(v * v for v in vector) ** 0.5
        return length or 1.0

    def layerRows(self) -> list:
        return layer_preview.parse_table(self._table.text())

    def _tableChanged(self, text) -> None:
        if not str(text).strip():
            self._tableSummary.setText('')
            return
        try:
            info = layer_preview.summary(layer_preview.parse_table(text),
                                         self._totalLength())
        except layer_preview.LayerTableRefusal as error:
            self._tableSummary.setText(str(error))
            return
        self._tableSummary.setText(self.tr(
            '%1 layers; first %2, last %3 (growth %4).')
            .replace('%1', str(info['layers']))
            .replace('%2', '%.4g' % info['first'])
            .replace('%3', '%.4g' % info['last'])
            .replace('%4', '%.3g' % info['growth']))

    def previewSweep(self) -> None:
        if self.busy():
            return
        op_id, values = self.operationId(), self.values()
        try:
            builder = self._builder()
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._say(self.tr('Working out the layers…'))

        def work():
            try:
                self._bridge.previewed.emit(builder.preview(op_id, values))
            except Exception as error:                  # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=work, name='gmsh-build-preview',
                                        daemon=True)
        self._worker.start()

    def _onPreviewed(self, result) -> None:
        self._worker = None
        self.showSweepPreview(result)

    def showSweepPreview(self, result) -> str:
        """Draw the layers of a sweep and say how many there are."""
        self._say(self.tr('%1 layers will fall at the marks drawn.').replace(
            '%1', str(result.get('layer_count', 0))))
        self.sweepPreview.emit(result.get('segments', []))
        return self._status.text()

    def _run(self, job) -> None:
        op_id, values = job
        try:
            controller = self._currentController()
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

    # -- patch roles (Plan 48 WG, DP-4215) ---------------------------------- #

    _ROLE_TITLES = {'front': 'front', 'back': 'back',
                    'front_and_back': 'front and back', 'sides': 'sides'}

    def roleRows(self) -> list:
        """``[{'name', 'role', 'category', 'default', ...}]`` as listed."""
        try:
            return self._builder().roles()
        except Exception:                               # noqa: BLE001
            return []

    def showRoles(self, rows=None) -> None:
        """List the declared patch roles (the builder's, unless given)."""
        rows = self.roleRows() if rows is None else list(rows)
        table = self._roles
        table.blockSignals(True)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, text in enumerate((
                    row['name'], self.tr(self._ROLE_TITLES.get(
                        row['role'], row['role'])))):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(index, column, item)
            box = QComboBox(table)
            box.setObjectName('gmshBuildRoleCategory')
            box.setAccessibleName(self.tr('Publishes as: %1').replace(
                '%1', row['name']))
            for category in gb.ROLE_CATEGORIES.get(row['default'],
                                                   (row['default'],)):
                box.addItem(category, category)
            box.setCurrentIndex(max(0, box.findData(row['category'])))
            box.currentIndexChanged.connect(
                lambda _i, b=box, n=row['name']: self.setRoleCategory(
                    n, b.currentData()))
            table.setCellWidget(index, 2, box)
        table.blockSignals(False)

    def setRoleCategory(self, name: str, category: str) -> bool:
        """Choose the category the patch *name* publishes with."""
        try:
            self._builder().set_role_category(name, category)
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            self.showRoles()
            return False
        self.showRoles()
        self._say(self.tr('%1 publishes as %2.').replace('%1', name)
                  .replace('%2', category))
        return True

    def refreshRoles(self) -> None:
        """List the roles now, then follow any rename made in the group
        table (the session is asked on a worker)."""
        self.showRoles()
        try:
            builder = self._builder()
            controller = self._controller
        except Exception:                               # noqa: BLE001
            return
        if not builder.roles() or not hasattr(controller, 'physical_editor'):
            return

        def work():
            try:
                groups = [(g.dim, g.tag, g.name, g.entities)
                          for g in controller.physical_editor().groups()]
                self._bridge.rolesFound.emit(builder.follow_names(groups))
            except Exception:                           # noqa: BLE001
                pass

        threading.Thread(target=work, name='gmsh-build-roles',
                         daemon=True).start()

    def _onDone(self, step) -> None:
        self._worker = None
        created = self._created(step)
        self._steps.addItem(self._stepLine(step))
        if getattr(step, 'roles', None):
            self.refreshRoles()
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


class BuildApi:
    """What the local API does on the open Geometry build panel (Plan 48
    PA3); registered on the case session, called by ``pa3_routes``."""

    def __init__(self, panel: GmshGeometryBuildPanel):
        self._panel = panel

    @staticmethod
    def _gui(function, *args):
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread
        return on_gui_thread(function, *args)

    def show_model(self) -> dict:
        """The Show model button: draw the entities so they can be picked."""
        def run():
            panel = self._panel
            if panel._entityPanel is None:                # noqa: SLF001
                raise ValueError('there is no viewport to show the model in')
            panel.showModel()
            return {'status': panel._entityPanel.status()}  # noqa: SLF001
        return self._gui(run)

    def show_sweep_preview(self, result) -> str:
        """The viewport draws the layers of a sweep; the status line."""
        return self._gui(self._panel.showSweepPreview, result)

    def roles(self) -> list:
        """The Patch roles table: one row per declared patch."""
        return self._gui(self._panel.roleRows)

    def set_role_category(self, name: str, category: str) -> list:
        """Choose what a declared patch publishes as, as the combo does;
        the rows after the change.  A refusal raises ``ValueError``."""
        def run():
            panel = self._panel
            if not panel.setRoleCategory(name, category):
                raise ValueError(panel.status())
            return panel.roleRows()
        return self._gui(run)

    def show_roles(self) -> None:
        """Redraw the table from the project (after a route changed it)."""
        self._gui(self._panel.showRoles)
