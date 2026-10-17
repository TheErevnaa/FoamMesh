"""The script of the model in the Gmsh session (Plan 41 GN19).

Four things, one panel, no dialog box:

* **Script view.**  The Python and ``.geo`` equivalent of what the session did,
  from the calls it recorded (:mod:`foammesh.core.gmsh.script_export`), with
  "Save script".  Read-only for Python.
* **Edit and reload (G-12).**  The ``.geo`` view is editable; "Reload model
  from this script" writes it beside the project and opens it in the session.
  "Open .geo" opens a script of the user's own.
* **Run script (T-08, Plan 41 Q5).**  A Python script or ``.geo`` lines run on
  the live model.  The script is on screen and a confirm strip in the panel
  asks first; nothing runs on a single press.
* **ONELAB parameters (S-01).**  The ``DefineConstant`` parameters of an opened
  ``.geo`` as controls (number, choice, text); "Run again" (or every change,
  when "Re-run on change" is on) clears the model and reads the script again
  with the values.

Every call to the session runs on a worker thread; the answer arrives on a
signal.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget)

from foammesh.view.theming.metrics import CompactDoubleSpinBox
from foammesh.core.gmsh import script_export as export

PYTHON, GEO = 'python', 'geo'
NO_SESSION = ('There is no Gmsh session yet; generate or build something on '
              'the Gmsh pages first.')


class GmshScriptPanel(QWidget):
    """Script view, editor, console and ONELAB parameters of the session."""

    #: A session call finished: ``(what, result)``.
    finished = Signal(str, object)
    #: A call failed: the sentence.
    failed = Signal(str)
    _done = Signal(str, object)

    def __init__(self, controller_getter, parent=None, *, case_path=None):
        super().__init__(parent)
        self.setObjectName('gmshScriptPanel')
        self._controller_getter = controller_getter
        self._case_path = case_path
        self._busy = False
        self._geoPath = ''
        self._params: dict = {}
        self._paramControls: dict = {}
        self._pending = None
        self._script = export.Script('')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self._language = QComboBox(self)
        self._language.setObjectName('gmshScriptLanguage')
        self._language.setAccessibleName(self.tr('Script format'))
        self._language.addItem(self.tr('Python'), PYTHON)
        self._language.addItem(self.tr('.geo'), GEO)
        self._refreshButton = QPushButton(self.tr('Refresh'), self)
        self._refreshButton.setObjectName('gmshScriptRefresh')
        self._save = QPushButton(self.tr('Save script…'), self)
        self._save.setObjectName('gmshScriptSave')
        row.addWidget(self._language)
        row.addWidget(self._refreshButton)
        row.addWidget(self._save)
        row.addStretch(1)
        layout.addLayout(row)

        self._note = QLabel('', self)
        self._note.setObjectName('gmshScriptNote')
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        self._view = QPlainTextEdit(self)
        self._view.setObjectName('gmshScriptView')
        self._view.setAccessibleName(self.tr('Script of the model'))
        self._view.setMinimumHeight(140)
        layout.addWidget(self._view)

        row = QHBoxLayout()
        self._reload = QPushButton(self.tr('Reload model from this script'),
                                   self)
        self._reload.setObjectName('gmshScriptReload')
        self._open = QPushButton(self.tr('Open .geo…'), self)
        self._open.setObjectName('gmshScriptOpen')
        row.addWidget(self._reload)
        row.addWidget(self._open)
        row.addStretch(1)
        layout.addLayout(row)

        # ONELAB parameters of the opened script
        self._paramsHost = QWidget(self)
        self._paramsHost.setObjectName('gmshScriptParameters')
        self._paramsForm = QFormLayout(self._paramsHost)
        self._paramsForm.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._paramsHost)
        row = QHBoxLayout()
        self._rerun = QCheckBox(self.tr('Re-run on change'), self)
        self._rerun.setObjectName('gmshScriptRerunOnChange')
        self._rerun.setChecked(True)
        self._runAgain = QPushButton(self.tr('Run again'), self)
        self._runAgain.setObjectName('gmshScriptRunAgain')
        row.addWidget(self._rerun)
        row.addWidget(self._runAgain)
        row.addStretch(1)
        self._paramsButtons = QWidget(self)
        self._paramsButtons.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._paramsButtons)

        # the console
        heading = QLabel(self.tr('Run a script on the model'), self)
        heading.setObjectName('gmshScriptConsoleHeading')
        layout.addWidget(heading)
        row = QHBoxLayout()
        self._consoleLanguage = QComboBox(self)
        self._consoleLanguage.setObjectName('gmshScriptConsoleLanguage')
        self._consoleLanguage.setAccessibleName(self.tr('Console script format'))
        self._consoleLanguage.addItem(self.tr('Python (gmsh API)'), PYTHON)
        self._consoleLanguage.addItem(self.tr('.geo lines'), GEO)
        row.addWidget(self._consoleLanguage)
        row.addStretch(1)
        layout.addLayout(row)
        self._console = QPlainTextEdit(self)
        self._console.setObjectName('gmshScriptConsole')
        self._console.setAccessibleName(self.tr('Script to run'))
        self._console.setPlaceholderText(self.tr(
            'gmsh.model.occ.addSphere(0, 0, 0, 1)\n'
            'gmsh.model.occ.synchronize()'))
        self._console.setMaximumHeight(120)
        layout.addWidget(self._console)
        self._run = QPushButton(self.tr('Run script…'), self)
        self._run.setObjectName('gmshScriptRun')
        layout.addWidget(self._run)

        # the confirm strip: the script is on screen above, this asks
        self._confirm = QWidget(self)
        self._confirm.setObjectName('gmshScriptConfirm')
        strip = QHBoxLayout(self._confirm)
        strip.setContentsMargins(0, 0, 0, 0)
        self._confirmText = QLabel(self.tr(
            'Run the script above on the model in the session? It can '
            'change or delete the model.'), self._confirm)
        self._confirmText.setWordWrap(True)
        self._confirmYes = QPushButton(self.tr('Run'), self._confirm)
        self._confirmYes.setObjectName('gmshScriptConfirmRun')
        self._confirmNo = QPushButton(self.tr('Cancel'), self._confirm)
        self._confirmNo.setObjectName('gmshScriptConfirmCancel')
        strip.addWidget(self._confirmText, 1)
        strip.addWidget(self._confirmYes)
        strip.addWidget(self._confirmNo)
        self._confirm.setVisible(False)
        layout.addWidget(self._confirm)

        self._result = QLabel('', self)
        self._result.setObjectName('gmshScriptResult')
        self._result.setWordWrap(True)
        self._result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._result)

        self._language.currentIndexChanged.connect(self.refresh)
        self._refreshButton.clicked.connect(self.refresh)
        # DP-385: `clicked` hands over its flag, which is not a path.
        self._save.clicked.connect(lambda: self.saveScript())
        self._reload.clicked.connect(self.reloadFromView)
        self._open.clicked.connect(self.chooseGeo)
        self._runAgain.clicked.connect(self.runAgain)
        self._run.clicked.connect(self.askToRun)
        self._confirmYes.clicked.connect(self.confirmRun)
        self._confirmNo.clicked.connect(self.cancelRun)
        self._done.connect(self._finish)
        self._showParameters([])
        self.refresh()

    # -- the script ----------------------------------------------------------- #

    def language(self) -> str:
        return str(self._language.currentData())

    def selectLanguage(self, language: str) -> None:
        index = self._language.findData(language)
        if index >= 0:
            self._language.setCurrentIndex(index)

    def casePath(self) -> Path | None:
        if self._case_path:
            return Path(self._case_path)
        session = self._session()
        return Path(session.case_path) if session is not None else None

    def _session(self):
        controller = self._controller_getter()
        return getattr(controller, 'session', None) if controller else None

    def scriptText(self) -> str:
        return self._view.toPlainText()

    def refresh(self, *_args) -> None:
        """Rebuild the view from the calls the session recorded."""
        case = self.casePath()
        calls = export.read_calls(case) if case else []
        geo = self.language() == GEO
        script = export.to_geo(calls) if geo else export.to_python(calls)
        self._script = script
        self._view.setPlainText(script.text if calls else '')
        self._view.setReadOnly(not geo)
        self._reload.setEnabled(geo and bool(calls) and not self._busy)
        self._save.setEnabled(bool(calls))
        if not calls:
            self._note.setText(self.tr(
                'Nothing has been recorded yet; the script appears as the '
                'model is built and meshed.'))
        elif script.rebuildable:
            self._note.setText(self.tr(
                '{0} calls. This script rebuilds the model in bare Gmsh.'
            ).format(script.calls))
        else:
            self._note.setText(self.tr(
                '{0} calls, {1} not in this script: {2}').format(
                    script.calls, len(script.gaps), '; '.join(script.gaps[:3])))
        self._note.setProperty(
            'foammeshStatus', '' if (not calls or script.rebuildable)
            else 'warning')

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def saveScript(self, path: str | None = None) -> str:
        """Write the view to a file; the path (empty when cancelled)."""
        geo = self.language() == GEO
        if not path:
            case = self.casePath()
            start = str((case / 'exports') if case else Path.home())
            path, _filter = QFileDialog.getSaveFileName(
                self, self.tr('Save script'), start,
                self.tr('Gmsh script (*.geo)') if geo
                else self.tr('Python (*.py)'))
        if not path:
            return ''
        export.save_text(path, self.scriptText())
        self._result.setText(self.tr('Saved {0}.').format(path))
        return path

    # -- edit and reload ------------------------------------------------------ #

    def _runtime(self, path: str | Path) -> str:
        session = self._session()
        return session.runtime_path(path) if session is not None else str(path)

    def reloadFromView(self) -> None:
        """Write the edited ``.geo`` beside the project and open it."""
        case = self.casePath()
        if case is None:
            self._fail(self.tr(NO_SESSION))
            return
        self.openGeo(export.save_text(export.model_geo_path(case),
                                      self.scriptText()))

    def chooseGeo(self) -> None:
        case = self.casePath()
        path, _filter = QFileDialog.getOpenFileName(
            self, self.tr('Open .geo'), str(case or Path.home()),
            self.tr('Gmsh script (*.geo)'))
        if path:
            self.openGeo(path)

    def openGeo(self, path: str, values: dict | None = None) -> None:
        """Replace the model with the script *path* (ONELAB values optional)."""
        session = self._session()
        if session is None:
            self._fail(self.tr(NO_SESSION))
            return
        self._geoPath = str(path)
        runtime = self._runtime(path)
        if values:
            self._background('open', lambda: session.call(
                'session.onelab_run', runtime, values))
        else:
            def work():
                opened = session.call('session.open', runtime)
                opened['parameters'] = session.call(
                    'session.onelab_parameters')['parameters']
                return opened
            self._background('open', work)

    # -- ONELAB parameters ---------------------------------------------------- #

    def parameterNames(self) -> list:
        return list(self._paramControls)

    def parameterValues(self) -> dict:
        values = {}
        for name, control in self._paramControls.items():
            if isinstance(control, QDoubleSpinBox):
                values[name] = control.value()
            elif isinstance(control, QComboBox):
                values[name] = control.currentData()
            else:
                values[name] = control.text()
        return values

    def setParameter(self, name: str, value) -> None:
        control = self._paramControls[name]
        if isinstance(control, QDoubleSpinBox):
            control.setValue(float(value))
        elif isinstance(control, QComboBox):
            control.setCurrentIndex(max(0, control.findData(value)))
        else:
            control.setText(str(value))

    def _showParameters(self, parameters: list) -> None:
        while self._paramsForm.rowCount():
            self._paramsForm.removeRow(0)
        self._paramControls = {}
        for item in parameters:
            name = item['name']
            values = item.get('values') or [0]
            choices = item.get('choices') or []
            if item['type'] == 'string':
                control = QLineEdit(str(values[0]), self)
                control.editingFinished.connect(self._parameterChanged)
            elif choices:
                control = QComboBox(self)
                labels = item.get('labels') or {}
                for choice in choices:
                    control.addItem(str(labels.get(str(choice), choice)),
                                    choice)
                control.setCurrentIndex(max(0, control.findData(values[0])))
                control.currentIndexChanged.connect(self._parameterChanged)
            else:
                control = CompactDoubleSpinBox(self)
                control.setDecimals(6)
                low = item.get('min')
                high = item.get('max')
                control.setRange(-1e12 if low is None else float(low),
                                 1e12 if high is None else float(high))
                if item.get('step'):
                    control.setSingleStep(float(item['step']))
                control.setValue(float(values[0]))
                control.valueChanged.connect(self._parameterChanged)
            control.setObjectName('gmshScriptParam_' + name.replace('/', '_'))
            control.setAccessibleName(item.get('label') or name)
            if item.get('readOnly'):
                control.setEnabled(False)
            self._paramControls[name] = control
            self._paramsForm.addRow(item.get('label') or name, control)
        shown = bool(parameters)
        self._paramsHost.setVisible(shown)
        self._paramsButtons.setVisible(shown)

    def _parameterChanged(self, *_args) -> None:
        if self._rerun.isChecked() and not self._busy:
            self.runAgain()

    def runAgain(self) -> None:
        if not self._geoPath:
            self._fail(self.tr('Open a .geo first.'))
            return
        self.openGeo(self._geoPath, self.parameterValues())

    # -- run a script --------------------------------------------------------- #

    def consoleLanguage(self) -> str:
        return str(self._consoleLanguage.currentData())

    def setConsole(self, language: str, text: str) -> None:
        index = self._consoleLanguage.findData(language)
        if index >= 0:
            self._consoleLanguage.setCurrentIndex(index)
        self._console.setPlainText(text)

    def askToRun(self) -> None:
        """Show the confirm strip; the script is the text above it."""
        if not self._console.toPlainText().strip():
            self._fail(self.tr('Type the script to run first.'))
            return
        # The panel's own Gmsh session (None until the model is open), read
        # once as the other handlers do; not the facade's session(), which
        # raises with no case open (DP-361).
        gmsh_session = self._session()
        if gmsh_session is None:
            self._fail(self.tr(NO_SESSION))
            return
        self._pending = (self.consoleLanguage(),
                         self._console.toPlainText())
        self._confirm.setVisible(True)
        self._run.setEnabled(False)
        self._console.setReadOnly(True)

    def cancelRun(self) -> None:
        self._pending = None
        self._confirm.setVisible(False)
        self._run.setEnabled(True)
        self._console.setReadOnly(False)

    def confirmRun(self) -> None:
        pending, self._pending = self._pending, None
        self.cancelRun()
        session = self._session()
        if not pending or session is None:
            return
        language, text = pending
        op = 'session.script' if language == PYTHON else 'session.geo_lines'
        self._background('run', lambda: session.call(op, text))

    # -- the worker ----------------------------------------------------------- #

    def _background(self, what: str, work) -> None:
        if self._busy:
            return
        self._busy = True
        self._result.setText(self.tr('Working…'))
        self._run.setEnabled(False)
        # Plan 42 SG16 (D9): a script action is a run like any other.
        from .generate_panel import beginSessionAction
        self._handle = beginSessionAction(
            self._controller_getter(), self.tr('Script: %s') % what)

        def thread():
            try:
                self._done.emit(what, work())
            except Exception as error:                       # noqa: BLE001
                self._done.emit(what, error)

        threading.Thread(target=thread, name='gmsh-script', daemon=True
                         ).start()

    def _finish(self, what: str, outcome) -> None:
        self._busy = False
        handle, self._handle = getattr(self, '_handle', None), None
        if handle is not None:
            handle.finish(not isinstance(outcome, Exception),
                          str(outcome) if isinstance(outcome, Exception)
                          else '')
        self._run.setEnabled(not self._confirm.isVisible())
        if isinstance(outcome, Exception):
            self._fail(str(outcome))
            return
        if what == 'open':
            self._showParameters(outcome.get('parameters') or [])
        entities = outcome.get('entities')
        if isinstance(entities, dict):
            entities = [entities.get(str(d), entities.get(d, 0))
                        for d in range(4)]
        if isinstance(entities, list) and len(entities) == 4:
            self._result.setText(self.tr(
                'Done. The model has {0:,} points, {1:,} curves, '
                '{2:,} surfaces, {3:,} volumes.').format(
                    *(int(count or 0) for count in entities)))
        else:
            self._result.setText(self.tr('Done.'))
        self.finished.emit(what, outcome)
        self.refresh()

    def _fail(self, sentence: str) -> None:
        self._result.setText(sentence)
        self.failed.emit(sentence)
