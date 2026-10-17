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

#: name -> (label, kind, default, extra)
ARGUMENTS = {
    'method': ('Method', 'choice', '',
               [(title, method) for method, title, _ in
                actions.OPTIMIZE_METHODS]),
    'niter': ('Passes', 'int', 1, (1, 1000)),
    'force': ('Also discrete entities', 'bool', False, None),
    'entities': ('Entities (dimension:tag, ...)', 'text', '', None),
    'order': ('Order', 'int', 2, (1, 3)),
    'steps': ('Times', 'int', 1, (1, 10)),
    'parts': ('Partitions', 'int', 2, (1, 100000)),
    'dim': ('Up to dimension', 'int', 3, (1, 3)),
    'nodes': ('Nodes', 'bool', True, None),
    'elements': ('Elements', 'bool', True, None),
    'visible': ('Visible', 'bool', True, None),
    'empty': ('Only empty', 'bool', True, None),
    'recursive': ('With their boundaries', 'bool', True, None),
    'angle': ('Angle (degrees)', 'text', '40', None),
    'curve_angle': ('Curve angle (degrees)', 'text', '180', None),
    'boundary': ('Boundary curves', 'bool', True, None),
    'reparametrization': ('For reparametrization', 'bool', False, None),
    'export_discrete': ('Export discrete', 'bool', True, None),
}


def beginSessionAction(controller, name: str):
    """Start the run entry of a Gmsh session action (Plan 42 SG16)."""
    from foammesh.core.jobs import session_actions

    if controller is None or not hasattr(controller, 'case_path'):
        return None
    publish = None
    try:
        from foammesh.app import app
        publish = app.events.publish
    except Exception:                                # noqa: BLE001
        pass
    return session_actions.begin(
        controller.case_path, name, cancel=controller.cancel,
        publish=publish)


class _Bridge(QObject):
    finished = Signal(object)
    failed = Signal(str)


class GmshGeneratePanel(QWidget):
    """Run Gmsh's Mesh-module actions on the project's resident session."""

    stageFinished = Signal(object)

    def __init__(self, controller_provider, parent=None):
        super().__init__(parent)
        self._provider = controller_provider
        self._controller = None
        self._worker = None
        self._handle = None
        self._bridge = _Bridge(self)
        self._bridge.finished.connect(self._onFinished)
        self._bridge.failed.connect(self._onFailed)
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
            label, kind, default, extra = ARGUMENTS[name]
            self._argument_widgets[name] = widget = self._argumentEditor(
                kind, default, extra, label)
            self._form.addRow(self.tr(label), widget)
        for name in chosen.options:
            option = catalogue.get(name)
            if option is None:
                continue        # an option this Gmsh build lacks is not offered
            self._addOptionRow(option)

    def _argumentEditor(self, kind, default, extra, label):
        if kind == 'bool':
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
            if isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            else:
                text = widget.text().strip()
                if name in ('angle', 'curve_angle'):
                    try:
                        values[name] = float(text)
                    except ValueError:
                        values[name] = text
                elif text or name != 'entities':
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
        try:
            controller = self.controller()
        except Exception as error:                   # noqa: BLE001
            self._status.setText(str(error))
            return
        action_id, values = self.currentActionId(), self.values()
        try:
            actions.plan(action_id, values)       # refuse before any wait
        except actions.ActionRefusal as error:
            self._status.setText(str(error))
            return
        self._status.setText(self.tr('Running %s…')
                             % self._action.currentText())
        # Plan 42 SG16 (D9): the action is a run -- Live run, Console, strip
        # and the one Cancel hear of it.
        self._handle = beginSessionAction(
            controller, self._action.currentText())
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

    def _onFinished(self, stage) -> None:
        self._finish()
        outcome = self._endHandle(bool(stage.ok), stage.error or '')
        if outcome == 'cancelled':
            self._addStageRow(stage)
            self._status.setText(self.tr(
                '%s: cancelled; the session was rebuilt from its log.')
                % stage.title)
            self._recover()
            return
        self._addStageRow(stage)
        self._status.setText(
            self.tr('%s: done in %.2f s') % (stage.title, stage.seconds)
            if stage.ok else stage.error)
        self.stageFinished.emit(stage)

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
