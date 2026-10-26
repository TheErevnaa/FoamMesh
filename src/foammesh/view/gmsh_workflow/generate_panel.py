"""The Generate actions as a panel (Plan 41 GN12).

Pick an action, set the controls it takes, run it on the resident Gmsh
session, and read the stage timeline.  The controls come from
``core/gmsh/generate_actions.py`` and the option catalogue, so an action shows
exactly the Gmsh options it can take.  An option is sent only when its box is
ticked: a value left alone is the value the project or the Options list set.

The session work runs on a worker thread; the page shows "running" and the
Cancel button ends it (the session is then rebuilt from its log).
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from foammesh.core import display_unit
from foammesh.view.theming.metrics import CompactSpinBox
from foammesh.view.workflow_controls.length_box import (
    ModelLengthBox, UnitLabel, format_model_units,
)
from foammesh.core.gmsh import generate_actions as actions
from foammesh.core.gmsh import option_catalogue as catalogue

from .shared_pick import SharedPick

#: name -> (label, kind, default, extra); Plan 48 PA2: the tables are in
#: ``core/gmsh/generate_actions.py`` so the local API publishes the same ones.
ARGUMENTS = actions.ARGUMENTS

#: Where an action's argument means something other than the table above.
ARGUMENT_OVERRIDES = actions.ARGUMENT_OVERRIDES

#: The quality the Poor button looks below until the user types another.
#: A name, not a literal: it is the value in the box, not a label on it.
DEFAULT_BELOW = '0.1'

#: The pick filter each entity argument starts with.
PICK_FILTERS = actions.PICK_FILTERS
ELEMENT_PICK_FILTERS = actions.ELEMENT_PICK_FILTERS


def beginSessionAction(controller, name: str):
    """Start the run entry of a Gmsh session action (Plan 42 SG16)."""
    from foammesh.core.facade.plan48.pa2_routes import begin_session_action

    return begin_session_action(controller, name)


class _Bridge(QObject):
    finished = Signal(object)
    failed = Signal(str)
    #: GM7: (the line to fill, the text) / (the message).
    filled = Signal(object, str)
    pickFailed = Signal(str)


class _PickLine(QWidget):
    """A list of entities or elements: typed, or picked in the viewport.

    ``Pick`` starts the application's Pick tool with the filter chosen beside
    it. For elements the picked entities are turned into their element tags;
    ``Poor`` fills the tags of the elements below a quality.
    """

    pickRequested = Signal(object)
    poorRequested = Signal(object, float)

    def __init__(self, name: str, elements: bool, parent=None):
        super().__init__(parent)
        self.name = name
        self.elements = elements
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(self)
        self.edit.setObjectName('gmshGenerate_' + name)
        box.addWidget(self.edit, 1)
        self.filter = QComboBox(self)
        self.filter.setObjectName('gmshGeneratePickFilter_' + name)
        self.filter.setAccessibleName(self.tr('Pick filter'))
        for word in (ELEMENT_PICK_FILTERS if elements else PICK_FILTERS):
            self.filter.addItem(self.tr(word), word)
        self.filter.setCurrentIndex(self.filter.findData('Surfaces'))
        box.addWidget(self.filter)
        self.pick = QPushButton(self.tr('Pick'), self)
        self.pick.setObjectName('gmshGeneratePick_' + name)
        self.pick.setAccessibleName(self.tr('Pick in the viewport'))
        self.pick.clicked.connect(lambda _c=False: self.pickRequested.emit(
            self))
        box.addWidget(self.pick)
        self.below = None
        self.poor = None
        if elements:
            self.below = QLineEdit(self)
            self.below.setObjectName('gmshGenerateBelow')
            self.below.setText(DEFAULT_BELOW)
            self.below.setMaximumWidth(48)
            self.below.setAccessibleName(self.tr('Quality below'))
            box.addWidget(self.below)
            self.poor = QPushButton(self.tr('Poor'), self)
            self.poor.setObjectName('gmshGeneratePoor')
            self.poor.setToolTip(self.tr(
                'Fill the tags of the elements whose quality (minSICN) is '
                'below the number beside this button'))
            self.poor.clicked.connect(self._emitPoor)
            box.addWidget(self.poor)

    def poorLimit(self) -> float:
        """The quality the Poor button looks below (0.1 when unreadable)."""
        try:
            return float(self.below.text())
        except ValueError:
            return 0.1

    def _emitPoor(self, _checked=False) -> None:
        self.poorRequested.emit(self, self.poorLimit())

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)

    def setAccessibleName(self, name: str) -> None:
        self.edit.setAccessibleName(name)


class GmshGeneratePanel(QWidget):
    """Run Gmsh's Mesh-module actions on the project's resident session."""

    stageFinished = Signal(object)
    #: Plan 48 PA2. Every run ends here, whatever its outcome: a dict with
    #: ``outcome`` (done, failed, cancelled), ``stage`` (or ``None``),
    #: ``message`` (the status line) and ``job_id``.
    runEnded = Signal(object)

    def __init__(self, controller_provider, parent=None):
        super().__init__(parent)
        self._provider = controller_provider
        self._job_id = ''
        self._api = GenerateApi(self)
        self._controller = None
        self._worker = None
        self._handle = None
        self._bridge = _Bridge(self)
        self._bridge.finished.connect(self._onFinished)
        self._bridge.failed.connect(self._onFailed)
        self._bridge.filled.connect(self._onFilled)
        self._bridge.pickFailed.connect(self._onPickFailed)
        self._picker = None
        self._pick_line = None
        self._argument_widgets: dict = {}
        self._option_widgets: dict = {}
        self.setObjectName('gmshGeneratePanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._action = QComboBox(self)
        self._action.setObjectName('gmshGenerateAction')
        self._action.setAccessibleName(self.tr('Generate action'))
        for item in actions.ACTIONS.values():
            self._action.addItem(item.title, item.id)
        self._action.currentIndexChanged.connect(self._rebuildForm)
        layout.addWidget(self._action)

        self._help = QLabel(self)
        self._help.setObjectName('gmshGenerateHelp')
        self._help.setWordWrap(True)
        layout.addWidget(self._help)

        self._form_host = QWidget(self)
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_host)

        row = QHBoxLayout()
        self._run = QPushButton(self.tr('Run'), self)
        self._run.setObjectName('gmshGenerateRun')
        self._run.clicked.connect(self.runSelected)
        row.addWidget(self._run)
        self._cancel = QPushButton(self.tr('Cancel'), self)
        self._cancel.setObjectName('gmshGenerateCancel')
        self._cancel.setEnabled(False)
        self._cancel.clicked.connect(self.cancelRunning)
        row.addWidget(self._cancel)
        row.addStretch(1)
        layout.addLayout(row)

        self._status = QLabel(self)
        self._status.setObjectName('gmshGenerateStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._table = QTableWidget(0, 5, self)
        self._table.setObjectName('gmshGenerateTimeline')
        self._table.setAccessibleName(self.tr('Stage timeline'))
        self._table.setHorizontalHeaderLabels([
            self.tr('Stage'), self.tr('Time (s)'), self.tr('Nodes'),
            self.tr('Elements'), self.tr('Result')])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setMinimumHeight(120)
        layout.addWidget(self._table)
        self._rebuildForm()
        self.registerApi()

    # -- the form ------------------------------------------------------------ #

    def currentActionId(self) -> str:
        return self._action.currentData()

    def _clearForm(self) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._argument_widgets.clear()
        self._option_widgets.clear()

    def _rebuildForm(self, *_args) -> None:
        chosen = actions.action(self.currentActionId())
        self._help.setText(chosen.help)
        self._clearForm()
        for name in chosen.arguments:
            label, kind, default, extra = ARGUMENT_OVERRIDES.get(
                (chosen.id, name)) or ARGUMENTS[name]
            self._argument_widgets[name] = widget = self._argumentEditor(
                kind, default, extra, label, name)
            self._form.addRow(self.tr(label), widget)
        for name in chosen.options:
            option = catalogue.get(name)
            if option is None:
                continue        # an option this Gmsh build lacks is not offered
            self._addOptionRow(option)
        self._applyAlgorithm()

    # -- the High-order tools: what each algorithm leaves active (GM7) ------- #

    def _applyAlgorithm(self) -> None:
        """Gmsh's window greys the fields an algorithm does not use."""
        chooser = self._argument_widgets.get('algorithm')
        if chooser is None:
            return
        try:
            chooser.currentIndexChanged.disconnect(self._applyAlgorithm)
        except (RuntimeError, TypeError):
            pass
        chooser.currentIndexChanged.connect(self._applyAlgorithm)
        row = next((r for r in actions.REGULARIZE_ALGORITHMS
                    if r[0] == chooser.currentData()), None)
        active = set(row[3]) if row else set()
        for name, (use, editor, _option) in self._option_widgets.items():
            on = name in active
            use.setEnabled(on)
            editor.setEnabled(on)
            if not on:
                use.setChecked(False)
        self._run.setEnabled(bool(row and row[2]) and not self.busy())

    def _argumentEditor(self, kind, default, extra, label, name=''):
        if kind == 'fixed':
            widget = QLineEdit(self)
            widget.setText(str(default))
            widget.setEnabled(False)
            widget.setToolTip(self.tr(
                'Gmsh 4.15.2 has no option for this field of its window, '
                'and its API runs with %s. It cannot be changed here.')
                % default)
        elif kind == 'text' and name in ('entities', 'elements'):
            widget = _PickLine(name, name == 'elements', self)
            widget.pickRequested.connect(self._startPick)
            widget.poorRequested.connect(self._findPoor)
        elif kind == 'bool':
            widget = QCheckBox(self)
            widget.setChecked(bool(default))
        elif kind == 'int':
            widget = CompactSpinBox(self)
            widget.setRange(*extra)
            widget.setValue(int(default))
        elif kind == 'choice':
            widget = QComboBox(self)
            for title, value in extra:
                widget.addItem(title, value)
        else:
            widget = QLineEdit(self)
            widget.setText(str(default))
        widget.setAccessibleName(self.tr(label))
        return widget

    def _addOptionRow(self, option) -> None:
        holder = QWidget(self)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        use = QCheckBox(self.tr('Set'), holder)
        use.setAccessibleName(self.tr('Set %s') % option.name)
        if option.kind == 'bool':
            editor = QCheckBox(holder)
            editor.setChecked(bool(option.default))
        elif option.kind == 'enum':
            editor = QComboBox(holder)
            for value, text in option.choices:
                editor.addItem(f'{text} ({value})', value)
            index = editor.findData(int(option.default))
            editor.setCurrentIndex(max(0, index))
        elif option.is_length:
            # UXB3c. A length: shown and typed in the project display unit,
            # sent to Gmsh in model units.
            editor = ModelLengthBox(holder)
            editor.setModelValue(float(option.default))
        else:
            editor = QLineEdit(holder)
            editor.setText(str(option.default))
        editor.setToolTip(option.help)
        editor.setAccessibleName(option.name)
        layout.addWidget(use)
        layout.addWidget(editor, 1)
        if isinstance(editor, ModelLengthBox):
            unit = UnitLabel(display_unit.current(), holder)
            editor.unit_label = unit
            layout.addWidget(unit)
        self._option_widgets[option.name] = (use, editor, option)
        self._form.addRow(option.label, holder)

    def values(self) -> dict:
        """What the form says: arguments always, options only when ticked."""
        values: dict = {}
        for name, widget in self._argument_widgets.items():
            if not widget.isEnabled():
                continue            # a fixed field of Gmsh's window
            if isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            else:
                text = widget.text().strip()
                if name in ('angle', 'curve_angle', 'timeout'):
                    try:
                        values[name] = float(text)
                    except ValueError:
                        values[name] = text
                elif text or name not in ('entities', 'elements',
                                          'delete_edges'):
                    values[name] = text
        for name, (use, editor, option) in self._option_widgets.items():
            if not use.isChecked():
                continue
            if isinstance(editor, QCheckBox):
                values[name] = 1 if editor.isChecked() else 0
            elif isinstance(editor, QComboBox):
                values[name] = editor.currentData()
            elif isinstance(editor, ModelLengthBox):
                values[name] = format_model_units(editor.modelValue())
            else:
                values[name] = editor.text().strip()
        return values

    # -- picking entities and elements (GM7) ---------------------------------- #

    def set_picker(self, picker) -> None:
        """Use *picker* (``pickFor`` / ``pickAccepted`` / ``status``) instead of
        the application's Pick tool."""
        if self._picker is not None:
            try:
                self._picker.pickAccepted.disconnect(self._onPicked)
            except (RuntimeError, TypeError):
                pass
        self._picker = picker
        picker.pickAccepted.connect(self._onPicked)

    def _pickTool(self):
        if self._picker is None:
            self.set_picker(SharedPick('gmsh.generate', parent=self))
        return self._picker

    def _startPick(self, line) -> None:
        self._pick_line = line
        tool = self._pickTool()
        if not tool.pickFor(line.filter.currentData()):
            self._pick_line = None
            self._status.setText(getattr(tool, 'status', lambda: '')()
                                 or self.tr('Nothing can be picked yet.'))
            return
        self._status.setText(self.tr('Pick in the viewport; Enter accepts.'))

    def _onPicked(self, chosen) -> None:
        line, self._pick_line = self._pick_line, None
        if line is None:
            return
        picked = [(int(e[0]), int(e[1])) for e in chosen]
        if line.elements and picked and all(d == 4 for d, _t in picked):
            # picked cells are element tags already (Plan 48 PK)
            text = actions.compress_tags([t for _d, t in picked])
            line.setText(text)
            self._status.setText(self.tr('%d elements picked.') % len(picked))
            return
        if not line.elements:
            line.setText(', '.join(f'{d}:{t}' for d, t in picked))
            self._status.setText(self.tr('%d picked.') % len(picked))
            return
        self._elementsOf(line, {'entities': [list(p) for p in picked]})

    def _findPoor(self, line, limit: float) -> bool:
        return self._elementsOf(line, {'below': limit})

    def _elementsOf(self, line, query: dict) -> bool:
        """Fill *line* with the tags the session lists for *query*; False
        (the reason in the status line) when it cannot start."""
        try:
            session = self.controller().session
        except Exception as error:                   # noqa: BLE001
            self._status.setText(str(error))
            return False
        if session is None or not session.alive:
            self._status.setText(self.tr(
                'There is no mesh yet; generate one first.'))
            return False

        def work():
            try:
                answer = session.call('session.list_mesh_elements', **query)
                text = actions.compress_tags(answer.get('elements') or [])
                if answer.get('truncated'):
                    text += ' ...'
                self._bridge.filled.emit(line, text)
            except Exception as error:               # noqa: BLE001
                self._bridge.pickFailed.emit(str(error))

        threading.Thread(target=work, name='gmsh-elements',
                         daemon=True).start()
        return True

    def _onFilled(self, line, text: str) -> None:
        line.setText(text)
        self._status.setText(self.tr('%d element tags.') % (
            len(actions.element_tags(text.replace(' ...', ''))) if text
            else 0))

    def _onPickFailed(self, message: str) -> None:
        self._status.setText(message)

    # -- running -------------------------------------------------------------- #

    def controller(self):
        if self._controller is None:
            self._controller = self._provider()
        return self._controller

    def chooseAction(self, action_id: str) -> bool:
        """Select *action_id* in the list; ``False`` when there is no such one."""
        index = self._action.findData(action_id)
        if index < 0:
            return False
        self._action.setCurrentIndex(index)
        return True

    def focusRun(self) -> None:
        self._run.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def runSelected(self) -> None:
        if self.busy():
            return
        self.startRun(self.currentActionId(), self.values(),
                      self._action.currentText())

    def startRun(self, action_id: str, values: dict, title: str) -> str:
        """Play *action_id* with *values* on the resident session.

        The one start of a run: the Run button, the digit keys and the local
        API all come here. Returns ``''`` when the run started, else the
        reason it did not (also shown in the status line).
        """
        if self.busy():
            return self.tr('%s is still running.') % self._action.currentText()
        try:
            controller = self.controller()
        except Exception as error:                   # noqa: BLE001
            self._status.setText(str(error))
            return str(error)
        try:
            actions.plan(action_id, values)       # refuse before any wait
        except actions.ActionRefusal as error:
            self._status.setText(str(error))
            return str(error)
        self._status.setText(self.tr('Running %s…') % title)
        # Plan 42 SG16 (D9): the action is a run -- Live run, Console, strip
        # and the one Cancel hear of it.
        self._handle = beginSessionAction(controller, title)
        self._job_id = getattr(self._handle, 'job_id', '')
        self._run.setEnabled(False)
        self._cancel.setEnabled(True)
        self._watchEscape(True)

        def work():
            try:
                self._bridge.finished.emit(controller.run(action_id, values))
            except Exception as error:               # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=work, name='gmsh-generate',
                                        daemon=True)
        self._worker.start()
        return ''

    # Plan 41 I-06: Escape aborts the run, as in Gmsh's own window.  It is an
    # application filter that exists only while a run does, not a shortcut: a
    # shortcut on Escape would be ambiguous with the console's find bar, and
    # the window's focus-mode exit already owns the key.  A key typed into a
    # text field or meant for an open popup is left to it.
    def _watchEscape(self, on: bool) -> None:
        app = QApplication.instance()
        if app is None:
            return
        if on:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
                and self._cancel.isEnabled() and self.window().isVisible()):
            if QApplication.activePopupWidget() is None and not isinstance(
                    QApplication.focusWidget(),
                    (QLineEdit, QAbstractSpinBox, QComboBox)):
                self.cancelRunning()
                return True
        return False

    def cancelRunning(self) -> None:
        # The one Cancel: the same route as the strip's (D9).
        handle = self._handle
        if handle is not None and handle.request_cancel():
            return
        controller = self._controller
        if controller is not None:
            controller.cancel()

    def _finish(self) -> None:
        self._run.setEnabled(True)
        self._cancel.setEnabled(False)
        self._watchEscape(False)

    def _endHandle(self, ok: bool, message: str = '') -> str:
        handle, self._handle = self._handle, None
        return handle.finish(ok, message) if handle is not None else ''

    def _announce(self, outcome: str, stage) -> None:
        self.runEnded.emit({'outcome': outcome, 'stage': stage,
                            'message': self._status.text(),
                            'job_id': self._job_id})

    def _onFinished(self, stage) -> None:
        self._finish()
        outcome = self._endHandle(bool(stage.ok), stage.error or '')
        if outcome == 'cancelled':
            self._addStageRow(stage)
            self._status.setText(self.tr(
                '%s: cancelled; the session was rebuilt from its log.')
                % stage.title)
            self._recover()
            self._announce('cancelled', stage)
            return
        self._addStageRow(stage)
        self._status.setText(
            self.tr('%s: done in %.2f s') % (stage.title, stage.seconds)
            if stage.ok else stage.error)
        self.stageFinished.emit(stage)
        self._announce('done' if stage.ok else 'failed', stage)

    def _recover(self) -> None:
        controller = self._controller
        if controller is not None:
            try:
                controller.recover()
            except Exception as error:               # noqa: BLE001
                self._status.setText(f'{self._status.text()} ({error})')

    def _onFailed(self, message: str) -> None:
        self._finish()
        outcome = self._endHandle(False, message)
        self._status.setText(self.tr('Cancelled; the session was rebuilt '
                                     'from its log.')
                             if outcome == 'cancelled' else message)
        controller = self._controller
        if controller is not None:
            try:
                controller.recover()
            except Exception as error:               # noqa: BLE001
                self._status.setText(f'{message} ({error})')
        self._announce('cancelled' if outcome == 'cancelled' else 'failed',
                       None)

    def _addStageRow(self, stage) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        elements = sum(int(v) for v in stage.after.get('elements', {}).values())
        cells = [stage.title, f'{stage.seconds:.2f}',
                 str(stage.after.get('nodes', '')), str(elements) if
                 stage.after else '',
                 self.tr('ok') if stage.ok else stage.error]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._table.setItem(row, column, item)
        self._table.scrollToBottom()

    def timelineRows(self) -> int:
        return self._table.rowCount()

    # -- the local API (Plan 48 PA2) ------------------------------------------ #

    def api(self):
        return self._api

    def statusText(self) -> str:
        return self._status.text()

    def cancelEnabled(self) -> bool:
        return self._cancel.isEnabled()

    def locked(self) -> bool:
        """True while a result-locked page holds this panel read-only."""
        owner = self.parent()
        while owner is not None:
            is_read_only = getattr(owner, 'isReadOnly', None)
            try:
                if callable(is_read_only) and is_read_only():
                    return True
            except Exception:                        # noqa: BLE001
                pass
            owner = owner.parent()
        return False

    def timelineCells(self) -> list:
        """The stage timeline as the table shows it: a list of text rows."""
        return [[self._table.item(r, c).text() if self._table.item(r, c)
                 else '' for c in range(self._table.columnCount())]
                for r in range(self._table.rowCount())]

    def setValues(self, action_id: str, values: dict) -> bool:
        """Put *values* in the form of *action_id* as a person would.

        The form starts from the action's defaults; arguments are set, and
        an option is ticked and set. ``False`` when there is no such action.
        """
        if not self.chooseAction(action_id):
            return False
        self._rebuildForm()
        values = values or {}
        # Arguments first (the algorithm decides which options are active),
        # whatever order the caller listed them in (DP-4872).
        for name, value in values.items():
            widget = self._argument_widgets.get(name)
            if widget is not None:
                self._setArgument(name, widget, value)
        for name, value in values.items():
            held = self._option_widgets.get(name)
            if held is not None and name not in self._argument_widgets:
                self._setOption(*held, value)
        return True

    def _setArgument(self, name, widget, value) -> None:
        if not widget.isEnabled():
            return                      # a fixed field of Gmsh's window
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(value))
        elif isinstance(widget, QComboBox):
            widget.setCurrentIndex(max(0, widget.findData(value)))
        else:
            if name in ('entities', 'elements') and not isinstance(
                    value, str):
                value = (actions.compress_tags(value) if name == 'elements'
                         else ', '.join(f'{d}:{t}' for d, t in value))
            widget.setText(str(value))

    def _setOption(self, use, editor, option, value) -> None:
        if not use.isEnabled():
            return
        use.setChecked(True)
        if isinstance(editor, QCheckBox):
            editor.setChecked(bool(int(value)))
        elif isinstance(editor, QComboBox):
            editor.setCurrentIndex(max(0, editor.findData(int(value))))
        elif isinstance(editor, ModelLengthBox):
            editor.setModelValue(float(value))
        else:
            editor.setText(str(value))

    def registerApi(self) -> bool:
        """Tell the case session this panel is in a window (idempotent)."""
        from foammesh.core.facade.plan48 import pa2_routes

        session = pa2_routes.case_session_of()
        if session is None:
            return False
        pa2_routes.register(session, pa2_routes.GENERATE_ATTRIBUTE,
                            self._api)
        if not getattr(self, '_apiBound', False):
            self._apiBound = True
            self.destroyed.connect(
                lambda *_a, s=session, a=self._api: pa2_routes.unregister(
                    s, pa2_routes.GENERATE_ATTRIBUTE, a))
        return True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.registerApi()


class GenerateApi:
    """What the Generate panel does for the local API.

    A run is the panel's own: the form is filled, the run is started by the
    same code as the Run button and the digit keys, and it ends in the same
    status line, timeline row and session action (so ``job.cancel_active``
    reaches it). Methods refuse with ``ValueError(reason)``.
    """

    def __init__(self, panel):
        self._panel = panel

    def _gui(self, function, *args):
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread
        return on_gui_thread(function, *args)

    def run(self, action_id: str, values: dict, timeout: float = 24 * 3600.0):
        """Run one action and wait for it; the dict ``runEnded`` carried."""
        import time
        from PySide6.QtCore import QCoreApplication, QThread

        done = threading.Event()
        box: dict = {}

        def ended(info):
            box['info'] = info
            done.set()

        def start():
            panel = self._panel
            if panel.locked():
                raise ValueError(panel.tr('Locked — unlock to edit'))
            if panel.busy():
                raise ValueError(panel.tr('%s is still running.')
                                 % panel._action.currentText())
            if not panel.setValues(action_id, values):
                raise ValueError(f'"{action_id}" is not a Generate action')
            panel.runEnded.connect(ended)
            reason = panel.startRun(action_id, values,
                                    panel._action.currentText())
            if reason:
                panel.runEnded.disconnect(ended)
                raise ValueError(reason)

        self._gui(start)
        application = QCoreApplication.instance()
        on_gui = (application is not None
                  and QThread.currentThread() is application.thread())
        end = time.monotonic() + timeout
        try:
            while not done.is_set() and time.monotonic() < end:
                if on_gui:
                    application.processEvents()
                done.wait(0.01)
        finally:
            try:
                self._gui(self._panel.runEnded.disconnect, ended)
            except (RuntimeError, TypeError):
                pass
        if 'info' not in box:
            raise ValueError('the run did not finish in time')
        return box['info']

    def state(self) -> dict:
        def read():
            panel = self._panel
            return {'running': panel.busy(), 'status': panel.statusText(),
                    'action': panel.currentActionId(),
                    'help': panel._help.text(),
                    'values': panel.values(),
                    'cancel_enabled': panel.cancelEnabled(),
                    'run_enabled': panel._run.isEnabled(),
                    'locked': panel.locked(),
                    'timeline': panel.timelineCells()}
        return self._gui(read)

    def _line(self, argument):
        line = self._panel._argument_widgets.get(argument)
        if not isinstance(line, _PickLine):
            raise ValueError(
                'the chosen action (%s) has no %s list to pick'
                % (self._panel.currentActionId(), argument))
        return line

    def pick(self, argument: str, filter_name=None) -> dict:
        """The Pick button of the entity or element list of the form."""
        def run():
            panel = self._panel
            line = self._line(argument)
            if filter_name:
                index = line.filter.findData(filter_name)
                if index < 0:
                    raise ValueError('pick filter %r is not one of %s'
                                     % (filter_name, ', '.join(
                                         line.filter.itemData(i) for i in
                                         range(line.filter.count()))))
                line.filter.setCurrentIndex(index)
            panel._startPick(line)
            if panel._pick_line is not line:
                raise ValueError(panel.statusText())
            return {'status': panel.statusText(),
                    'filter': line.filter.currentData()}
        return self._gui(run)

    def poor(self, below=None, timeout: float = 600.0) -> dict:
        """The Poor button: the tags of the elements below a quality."""
        import time
        from PySide6.QtCore import QCoreApplication, QThread

        done = threading.Event()
        box: dict = {}

        def filled(line, text):
            box['text'] = text
            done.set()

        def failed(message):
            box['error'] = message
            done.set()

        def start():
            panel = self._panel
            line = self._line('elements')
            if line.below is None:
                raise ValueError('the chosen action has no Poor button')
            if below is not None:
                line.below.setText(str(below))
            panel._bridge.filled.connect(filled)
            panel._bridge.pickFailed.connect(failed)
            if not panel._findPoor(line, line.poorLimit()):
                panel._bridge.filled.disconnect(filled)
                panel._bridge.pickFailed.disconnect(failed)
                raise ValueError(panel.statusText())

        self._gui(start)
        application = QCoreApplication.instance()
        on_gui = (application is not None
                  and QThread.currentThread() is application.thread())
        end = time.monotonic() + timeout
        try:
            while not done.is_set() and time.monotonic() < end:
                if on_gui:
                    application.processEvents()
                done.wait(0.01)
        finally:
            def drop():
                for signal, slot in ((self._panel._bridge.filled, filled),
                                     (self._panel._bridge.pickFailed,
                                      failed)):
                    try:
                        signal.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass
            self._gui(drop)
        if 'error' in box:
            raise ValueError(box['error'])
        if 'text' not in box:
            raise ValueError('the element search did not finish in time')

        def read():
            return {'text': box['text'], 'status': self._panel.statusText()}
        return self._gui(read)

