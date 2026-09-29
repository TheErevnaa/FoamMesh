"""Shared meshing-method chooser backed exclusively by facade operations."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QGroupBox, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QTabWidget, QVBoxLayout, QWidget,
)

from foammesh.core.naming import humanise_option
from foammesh.view.widgets.folder_header import FolderHeader
from foammesh.view.workflow_controls.field_group_page import (
    ExecutionPreferencesPage,
)
from foammesh.view.workflow_controls.warning_text import format_warnings
from foammesh.view.facade_client import FailedResult, query, submit


#: Kept as the name this module has always exported; the definition moved to
#: ``facade_client`` in C31-12 so that one helper owns the whole view layer's
#: write scheduling and there is exactly one synchronous fallback to justify.
_FailedResult = FailedResult


def _submit(client, operation, parameters, on_result):
    """Run one facade operation on whichever path its handler needs.

    ``mesh.engine.probe``/``select`` and the engine run operations have async
    facade handlers, so a real client must use the awaitable ``run`` path -
    ``run_sync`` raises ``RuntimeError`` for them by design.  Test doubles
    that only implement ``run_sync`` keep working synchronously.  Facade
    errors are delivered to the callback as a failed result instead of
    escaping into the event loop.
    """
    return submit(client, operation, parameters, then=on_result)


class MeshingMethodPage(QWidget):
    # DP-226. The table that lived here spelled the second mesher
    # `Snappy Hex Mesh`, while the modal reporting every run called it
    # `Snappy` and the readiness banner called it `snappyHexMesh`. One
    # rule spells a stored name and it knows both meshers; an engine
    # registered without a name still degrades visibly, because the
    # rule falls back to sentence case over the id it was given.

    #: Plan 28. Only the two solvers a user can pick; 'unselected' is a state,
    #: not an option, and is shown by leaving both unchecked.
    SOLVER_LABELS = {
        'openfoam': 'OpenFOAM',
        'su2': 'SU2',
    }

    #: How long to wait before asking again when a probe answers 'pending'.
    #: A cold WSL boot takes tens of seconds; polling twice a second would
    #: just move the stall into the GUI thread.
    PROBE_RETRY_MS = 2000

    engineChanged = Signal(str)
    probeFinished = Signal(bool)
    # WP-13 / F-32. ``targetSolverChanged`` was declared and emitted here and
    # connected nowhere, so it read like a notification the rest of the window
    # acts on when nothing ever did. The page already calls ``refresh()`` after
    # the solver is set, which is what actually updates what the user sees;
    # a signal with no consumer only makes the wiring look richer than it is.

    @staticmethod
    def _engine_ids() -> tuple[str, ...]:
        from foammesh.core.engine.registry import ENGINE_REGISTRY
        return ENGINE_REGISTRY.ids()

    def __init__(self, facade_client, parent=None):
        super().__init__(parent)
        self._client = facade_client
        self._tasks = set()
        self._probeGeneration = 0
        # A probe answer describes this machine, not this page. Keeping
        # it means opening the page again costs nothing; the Re-probe
        # button and a change in the engine list are what discard it.
        self._lastProbe = {}
        self._probedEngines = ()
        self.setObjectName('meshingMethodPage')
        # Plan 32 section 4.1. The page is `2. Mesh setup` in the outline and
        # says so at its head. The object name, the token and the class name
        # are unchanged: they are addressed by saved cases, by the strict-GUI
        # harness and by eight call sites, and none of them is read by a user.
        self.setAccessibleName(self.tr('Mesh setup'))
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Mesh setup'))
        title.setObjectName('meshingMethodTitle')
        layout.addWidget(title)

        # Plan 28. The solver comes first because it decides what follows: SU2
        # reads four cell families and snappyHexMesh produces a fifth, so the
        # engine list below is a consequence of this choice rather than an
        # independent one.
        # E5. The solver sentence used to sit between the last solver radio
        # and the first engine radio, where it read as a caption for the
        # engine below it, and the engine radios had no group label at all.
        # Each explanation now lives inside the group it explains.
        solverBox = QGroupBox(self.tr('Mesh for'), self)
        solverBox.setObjectName('targetSolverBox')
        solverLayout = QVBoxLayout(solverBox)
        layout.addWidget(solverBox)
        self._targetSolver = 'unselected'
        self._solverGroup = QButtonGroup(self)
        self._solverButtons = {}
        for solver_id, label in self.SOLVER_LABELS.items():
            button = QRadioButton(label, self)
            button.setObjectName(solver_id + 'TargetSolver')
            button.setProperty('targetSolver', solver_id)
            button.clicked.connect(
                lambda _checked=False, chosen=solver_id:
                self.chooseTargetSolver(chosen))
            self._solverGroup.addButton(button)
            self._solverButtons[solver_id] = button
            solverLayout.addWidget(button)
        self._solverReason = QLabel(self)
        self._solverReason.setWordWrap(True)
        self._solverReason.setObjectName('targetSolverReason')
        self._solverReason.setProperty('foammeshTone', 'muted')
        # F5. Choosing a solver clears this sentence, and the whole button
        # column jumped up under the cursor. Two lines of height are kept
        # whether or not there is anything to say.
        self._solverReason.setMinimumHeight(
            2 * self._solverReason.fontMetrics().lineSpacing())
        solverLayout.addWidget(self._solverReason)

        engineBox = QGroupBox(self.tr('Meshing engine'), self)
        engineBox.setObjectName('meshingEngineBox')
        engineLayout = QVBoxLayout(engineBox)
        layout.addWidget(engineBox)
        self._group = QButtonGroup(self)
        self._buttons = {}
        self._reasons = {}
        self._incompatible = {}
        for engine_id in self._engine_ids():
            label = humanise_option(engine_id)
            button = QRadioButton(label, self)
            button.setProperty('engineId', engine_id)
            self._group.addButton(button)
            self._buttons[engine_id] = button
            engineLayout.addWidget(button)
            reason = QLabel(self)
            reason.setWordWrap(True)
            reason.setObjectName(engine_id + 'AvailabilityReason')
            # E6. A version string and a twelve-character fingerprint were
            # drawn at the same weight as the choice they annotate.
            reason.setProperty('foammeshTone', 'muted')
            self._reasons[engine_id] = reason
            engineLayout.addWidget(reason)
        # Plan 33 SETUP-05. `Apply method` stood here, the one control on a
        # four-button page that moved the work forward, beside a footer whose
        # Proceed is what every other step is committed with. MEASURED: the
        # footer press applied the method itself, so the two did the same
        # thing and the page's own button was the one a reader had to guess
        # about -- and the settings below it were saved by one of them and
        # not the other. There is one commit now, `commitSelection`, and the
        # footer is what calls it. A press that cannot go forward says why
        # through the same route every other refusal takes.
        for engine_button in self._buttons.values():
            engine_button.toggled.connect(
                lambda _checked=False: self._syncExecutionRoute())

        # Plan 33 SETUP-04. The settings that govern the run were folded away
        # behind `Advanced` together with three buttons that open read-only
        # reports, so the middle column of `2. Mesh setup` showed a reader two
        # radio groups and a fold, and the core count the run reads was two
        # presses from sight. The settings are the page now, under the name of
        # what they are; the reports keep the fold, because a report is not a
        # setting and nothing on this page has to be read before meshing.
        self._execution = ExecutionPreferencesPage(facade_client, self)
        # DP-155/157. A section of a page does not scroll itself and does not
        # carry a commit pair of its own.
        self._execution.setEmbedded(True)
        layout.addWidget(self._execution)

        self._diagnosticsBody = QWidget(self)
        self._diagnosticsBody.setObjectName('meshingDiagnosticsBody')
        diagnosticsLayout = QVBoxLayout(self._diagnosticsBody)
        diagnosticsLayout.setContentsMargins(0, 0, 0, 0)
        self._runtimeDiagnostics = QPushButton(
            self.tr('OpenFOAM runtime diagnostics…'),
            self._diagnosticsBody)
        self._runtimeDiagnostics.clicked.connect(self._show_runtime_diagnostics)
        diagnosticsLayout.addWidget(self._runtimeDiagnostics)
        self._effectiveDictionaries = QPushButton(
            self.tr('Effective meshing setup…'), self._diagnosticsBody)
        self._effectiveDictionaries.clicked.connect(
            self._show_effective_dictionaries)
        diagnosticsLayout.addWidget(self._effectiveDictionaries)
        self._reprobe = QPushButton(self.tr('Re-probe runtimes'),
                                    self._diagnosticsBody)
        self._reprobe.clicked.connect(lambda: self.refresh(force=True))
        diagnosticsLayout.addWidget(self._reprobe)

        self._diagnostics = FolderHeader(self.tr('Diagnostics'), self)
        self._diagnostics.setObjectName('meshingMethodDiagnosticsHeader')
        # Not a settings fold. SETUP-04 left the settings on the page and put
        # the runtime probe reports behind this one, and section 1.1 routes
        # reports to an on-demand view. A fold opens open everywhere it holds
        # editors (W-O2, `FolderHeader`); this one holds none, and says so.
        self._diagnostics.setChecked(False)
        # DP-186/187. A header read aloud has to say which disclosure it is.
        self._diagnostics.setAccessibleName(
            self.tr('Diagnostics for the meshing runtimes'))
        layout.addWidget(self._diagnostics)
        layout.addWidget(self._diagnosticsBody)
        self._diagnostics.setContents(self._diagnosticsBody)
        layout.addStretch(1)
        self.refresh()

    # -- the Advanced disclosure ------------------------------------------- #

    def executionPanel(self):
        """The embedded execution settings, reachable without a private name.

        Plan 32 W1. The wizard and the tests have to be able to ask this page
        for the panel it hosts; before W1 they reached it as an outline row.
        """
        return self._execution

    def diagnosticsHeader(self):
        """The folded disclosure the three report buttons live under."""
        return self._diagnostics

    def runtimeDiagnosticsButton(self):
        """The OpenFOAM runtime report, which a Gmsh route is not offered."""
        return self._runtimeDiagnostics

    def advancedHeader(self):
        """The fold this page still has, for the harnesses that open it.

        Plan 33 SETUP-04 left one fold on the page and the settings outside
        it, so `Advanced` is now the diagnostics fold; the walkthrough scripts
        open it to reach the report buttons.
        """
        return self._diagnostics

    def setAdvancedOpen(self, open_: bool) -> None:
        """Open or close the diagnostics fold."""
        self._diagnostics.setChecked(bool(open_))

    def isAdvancedOpen(self) -> bool:
        """True while the diagnostics fold is showing."""
        return self._diagnostics.isChecked()

    def _syncExecutionRoute(self) -> None:
        """Tell the settings which mesher they are being read for.

        Plan 33 SETUP-02 and SETUP-04. The route decides both which settings
        a run reads and which report is worth opening: `openfoam.runtime.
        diagnostics` describes the runtime a Gmsh mesh never goes near.
        """
        engine_id = self._checkedEngineId()
        self._execution.setEngine(engine_id)
        self._runtimeDiagnostics.setVisible(engine_id != 'gmsh')

    def hasPendingExecutionEdits(self) -> bool:
        """True when the Advanced band holds an edit nobody has committed.

        The band has no Apply of its own (DP-157), so the page it sits on is
        what has to notice, and the forward action is what has to commit.
        """
        return self._execution.is_dirty

    def applyPendingExecution(self):
        """Commit whatever the Advanced band is holding."""
        return self._execution.apply()

    async def savePendingExecution(self) -> bool:
        """Commit the Advanced band and say whether the case took it.

        DP-251. `applyPendingExecution` schedules the write and answers a
        task that completes before the facade replies (DP-227), so a caller
        that has to decide whether to move on learns nothing from it. The
        forward press is exactly such a caller: MEASURED on the guided walk,
        `serial` was typed here, the press applied the method, the method
        change refreshed this page, the refresh reloaded the band -- and the
        case still read `auto`. So the band is saved, awaited, and a refusal
        is answered False rather than walked past.
        """
        return await self._execution.save()

    def _track(self, task):
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def refresh(self, *, force=False):
        self._probeGeneration += 1
        generation = self._probeGeneration
        # Plan 32 W1. The execution band narrows its decomposition methods
        # from a runtime probe that is usually still cold on the first render,
        # so every revisit of this page re-reads it. Before W1 the band was a
        # row of its own and did this on its own show.
        # DP-339. A revisit of this page is not a write, so the band
        # keeps an edit that has not reached the case yet.
        self._execution.reload(discard_pending=False)
        result = query(self._client, 'mesh.engine.list').payload
        current = result['current_engine']
        # Absent on an older payload or a test double, and permissive when
        # absent: that is exactly the pre-Plan-28 behaviour.
        self._targetSolver = str(result.get('target_solver') or 'unselected')
        self._incompatible = {}
        for descriptor in result['engines']:
            engine_id = descriptor['engine_id']
            self._buttons[engine_id].setToolTip(descriptor.get('summary', ''))
            self._incompatible[engine_id] = str(
                descriptor.get('incompatible_reason') or '')
        engine_ids = tuple(sorted(self._incompatible))
        if force or engine_ids != self._probedEngines:
            self._lastProbe.clear()
        self._probedEngines = engine_ids
        self._refresh_target_solver()
        # R130. MEASURED: click OpenFOAM, click snappyHexMesh, press Apply
        # method -- nothing was applied and both engine radios were empty.
        # `chooseTargetSolver()` sets the solver through an async facade call
        # and calls this from the reply; this used to clear every engine radio
        # and re-check only the engine the facade currently holds, which on a
        # fresh case is none. Any engine picked between the solver click and
        # the reply was wiped without a word. The facade is authoritative
        # about what it *holds*, not about what the user has just pointed at,
        # so a pick the solver rule still allows survives the redraw.
        pending = self._checkedEngineId()
        self._group.setExclusive(False)
        for button in self._buttons.values():
            button.setChecked(False)
        self._group.setExclusive(True)
        if current in self._buttons:
            self._buttons[current].setChecked(True)
        elif pending in self._buttons and not self._incompatible.get(pending):
            self._buttons[pending].setChecked(True)
        for engine_id, button in self._buttons.items():
            reason = self._incompatible.get(engine_id, '')
            if reason:
                # No probe: whether this machine can run the engine is beside
                # the point when its output could not be read anyway, and a
                # "runtime available" line under a disabled button reads as a
                # contradiction.
                button.setEnabled(False)
                self._reasons[engine_id].setText(reason)
                continue
            cached = self._lastProbe.get(engine_id)
            if cached is not None:
                self._apply_probe(cached, current, engine_id)
                continue
            button.setEnabled(current == engine_id)
            self._reasons[engine_id].setText(self.tr('Probing the runtime…'))
            self._submit_probe(engine_id, current, generation, force)
        self._syncExecutionRoute()

    def _checkedEngineId(self) -> str:
        """Which engine radio is checked right now, if any (R130/R131)."""
        button = self._group.checkedButton()
        return '' if button is None else str(button.property('engineId') or '')

    def _submit_probe(self, engine_id, current, generation, force):
        self._track(_submit(
            self._client, 'mesh.engine.probe',
            {'engine_id': engine_id, 'timeout_seconds': 3,
             'refresh': bool(force)},
            lambda probe, selected=engine_id, cycle=generation:
            self._apply_probe_if_current(probe, current, selected, cycle)))

    def _refresh_target_solver(self) -> None:
        self._solverGroup.setExclusive(False)
        for solver_id, button in self._solverButtons.items():
            button.setChecked(solver_id == self._targetSolver)
        self._solverGroup.setExclusive(True)
        blocked = [humanise_option(engine_id)
                   for engine_id, reason in sorted(self._incompatible.items())
                   if reason]
        if self._targetSolver == 'unselected':
            self._solverReason.setText(self.tr(
                'Choose the solver this mesh is for. Every meshing method is '
                'offered until you do.'))
        elif blocked:
            self._solverReason.setText(self.tr(
                '{0} cannot be used for this solver.').format(
                    ', '.join(blocked)))
        else:
            self._solverReason.setText('')

    def targetSolver(self) -> str:
        return self._targetSolver

    def isEngineOffered(self, engine_id: str) -> bool:
        """Whether the solver rule allows this engine.

        Separate from whether its runtime is present: an engine that cannot
        produce a readable mesh is wrong on every machine, and one whose
        runtime is missing here is right everywhere else.
        """
        return not self._incompatible.get(engine_id, '')

    def engineReason(self, engine_id: str) -> str:
        label = self._reasons.get(engine_id)
        return label.text() if label is not None else ''

    def chooseTargetSolver(self, solver: str):
        """Record the target solver, then re-derive everything below it."""
        def on_result(result):
            if getattr(result, 'status', '') != 'accepted':
                self._solverReason.setText(
                    str(getattr(result, 'message', '')
                        or self.tr('The target solver could not be set.')))
                self._refresh_target_solver()
                return
            self.refresh()
            stranded = (result.payload or {}).get('engine_incompatible_reason')
            if stranded:
                # Not repaired here: which engine to move to is the user's
                # call, and silently switching would discard their settings.
                QMessageBox.warning(
                    self, self.tr('Meshing method'),
                    self.tr('The selected meshing method cannot serve '
                            'this solver.'
                            '\n\n{0}\n\n'
                            'Choose another method before meshing.'
                            ).format(stranded))

        return self._track(_submit(
            self._client, 'mesh.target_solver.set',
            {'target_solver': str(solver)}, on_result))

    def _apply_probe_if_current(self, probe, current, engine_id, generation):
        """Ignore late probe results from an obsolete refresh cycle."""
        if generation != self._probeGeneration:
            return
        probes = (getattr(probe, 'payload', None) or {}).get('probes', [])
        if probes and all(item.get('status') == 'pending' for item in probes):
            # The runtime is still being measured next to the write queue.
            # Say so and ask again shortly, rather than reporting a cold
            # WSL boot as a missing runtime.
            self._reasons[engine_id].setText(
                self.tr('Still probing the runtime…'))
            QTimer.singleShot(
                self.PROBE_RETRY_MS, self,
                lambda: self._retry_probe(engine_id, current, generation))
            return
        self._lastProbe[engine_id] = probe
        self._apply_probe(probe, current, engine_id)

    def _retry_probe(self, engine_id, current, generation):
        if generation != self._probeGeneration:
            return
        self._submit_probe(engine_id, current, generation, False)

    def _apply_probe(self, probe, current, engine_id):
        if self._incompatible.get(engine_id):
            return
        probes = (probe.payload or {}).get('probes', [])
        available = any(item.get('available') for item in probes)
        self._buttons[engine_id].setEnabled(
            available or current == engine_id)
        reason = next((item.get('reason') for item in probes
                       if item.get('reason')), '')
        if available:
            # Name what the probe actually found rather than asserting the
            # runtime is fine: a version and profile the user can check.
            selected = next(
                (item for item in probes if item.get('available')), {})
            version = str(selected.get('version') or '').strip()
            profile = str(selected.get('profile_id') or '').strip()
            parts = [part for part in (version, profile) if part]
            text = self.tr('Runtime {0}').format(' via '.join(parts)) if parts else ''
            fingerprint = str(selected.get('runtime_fingerprint') or '')[:12]
            # E6. The fingerprint identifies a build for a bug report; it is
            # not something to weigh when choosing an engine. It moves to
            # where someone looking for it will still find it.
            label = self.tr('Runtime {0}').format(version) if version else text
            # DP-50. An engine can be available and still have something worth
            # saying: Gmsh runs without OpenFOAM's checkMesh, and the mesh it
            # publishes then goes unvalidated. This line was overwritten with
            # the runtime version whenever the engine was usable, so the one
            # advisory the probe had to offer was dropped in exactly the case
            # it was written for. The advisory is the selected profile's own,
            # not the first one found: a reason belonging to some other
            # profile that failed to probe describes a runtime the user is not
            # about to use.
            advisory = str(selected.get('reason') or '').strip()
            if advisory:
                label = f'{label} · {advisory}' if label else advisory
            self._reasons[engine_id].setText(label)
            self._reasons[engine_id].setToolTip(
                text + (self.tr(' · fingerprint {0}').format(fingerprint)
                        if fingerprint else ''))
        else:
            self._reasons[engine_id].setText(
                reason or self.tr('The required runtime is unavailable.'))
            self._reasons[engine_id].setToolTip('')
        self.probeFinished.emit(available)

    def _show_runtime_diagnostics(self):
        # Plan 35 CR6. The window opens at once and fills in when the facade
        # answers; it also carries the WSL health and the one place
        # `wsl --shutdown` is offered.
        from foammesh.app import app
        from foammesh.view.main_window.wsl_health_bar import show_runtime_diagnostics
        dialog = show_runtime_diagnostics(
            self, self._client, getattr(app, 'wslHealth', None))
        self._track(getattr(dialog, '_submitted', None))
        return dialog

    def _show_effective_dictionaries(self):
        checked = self._group.checkedButton()
        if (checked is not None
                and checked.property('engineId') != 'snappy'):
            self._show_effective_plan()
            return

        def show(result):
            if getattr(result, 'status', '') != 'accepted':
                QMessageBox.warning(
                    self, self.tr('Effective meshing setup'),
                    str(getattr(result, 'message', '')
                        or self.tr('Generate dictionaries first.')))
                return
            payload = result.payload or {}
            from foammesh.app import app
            window = getattr(app, 'window', None)
            if window is not None and hasattr(window, 'showEffectiveSetup'):
                window.showEffectiveSetup(payload)
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(self.tr('Effective meshing setup'))
            dialog.resize(1000, 760)
            layout = QVBoxLayout(dialog)
            tabs = QTabWidget(dialog)
            layout.addWidget(tabs)

            links = payload.get('source_links') or {}
            warnings = payload.get('warnings') or []
            overview_lines = [
                f"Target: {payload.get('target', '')}",
                f"Configuration: {payload.get('configuration_sha256', '')}",
                f"Last run: {payload.get('last_run_configuration_sha256') or 'none'}",
                f"Stale since last run: {payload.get('stale_since_last_run', False)}",
                'Stale stages: ' + ', '.join(
                    name for name, stale in
                    (payload.get('stale_stages') or {}).items() if stale),
                '',
                'Source fields:',
                *(f"{section}: {', '.join(fields)}"
                  for section, fields in sorted(links.items())),
            ]
            if warnings:
                overview_lines.extend(['', 'Validation warnings:',
                                       *format_warnings(warnings)])
            overview = QPlainTextEdit(dialog)
            overview.setReadOnly(True)
            overview.setPlainText('\n'.join(overview_lines))
            tabs.addTab(overview, self.tr('Provenance'))

            for name, content in sorted(
                    (payload.get('current') or {}).items()):
                editor = QPlainTextEdit(dialog)
                editor.setReadOnly(True)
                editor.setPlainText(content)
                tabs.addTab(editor, name)
            for name, content in sorted(
                    (payload.get('diffs') or {}).items()):
                if not content:
                    continue
                editor = QPlainTextEdit(dialog)
                editor.setReadOnly(True)
                editor.setPlainText(content)
                tabs.addTab(editor, self.tr('Diff: {0}').format(name))
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Close, parent=dialog)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.exec()

        self._track(_submit(
            self._client, 'workflow.effective_dictionaries', {}, show))

    def _show_effective_plan(self):
        def show(result):
            if getattr(result, 'status', '') != 'accepted':
                QMessageBox.warning(
                    self, self.tr('Effective engine plan unavailable'),
                    str(getattr(result, 'message', '')
                        or self.tr('The engine plan is not available.')))
                return
            from foammesh.app import app
            window = getattr(app, 'window', None)
            if window is not None and hasattr(
                    window, 'showEffectiveEnginePlan'):
                window.showEffectiveEnginePlan(result.payload or {})
                return
            QMessageBox.information(
                self, self.tr('Effective engine plan'),
                str((result.payload or {}).get('plan_digest') or
                    self.tr('Engine plan derived.')))

        self._track(_submit(self._client, 'mesh.plan.derive', {}, show))

    async def _run(self, operation, parameters):
        """One facade call, awaited, with a refusal shaped like a result.

        Plan 30 WP-08: a write from a view module goes on the scheduler or it
        does not happen, and a client with no awaitable `run` is answered with
        a refusal rather than served from the GUI thread.
        """
        runner = getattr(self._client, 'run', None)
        if runner is None:
            return FailedResult(
                RuntimeError('This window cannot reach the case.'))
        try:
            return await runner(operation, parameters)
        except Exception as error:                           # noqa: BLE001
            return FailedResult(error)

    async def commitSelection(self):
        """Save what this page holds and say whether it can be left.

        Plan 33 SETUP-05. This is the whole commit of `2. Mesh setup`: the
        target solver, the meshing method, and the resource settings below
        them, in that order, awaited, with the first refusal answered rather
        than walked past. It returns `(accepted, reason)` -- the caller is
        the footer's Proceed, and the reason is what the window says on the
        status bar when the press cannot move.

        R131 lives here now. MEASURED before Plan 33: with no engine radio
        checked the page's own button was a silent no-op, and the version
        that replaced it wrote an inline hint under a button that no longer
        exists. A press that will not move says why in the one place this
        window says it.
        """
        button = self._group.checkedButton()
        if button is None:
            return False, self.tr(
                'Choose a meshing method before moving on.')
        engine_id = str(button.property('engineId') or '')
        if not button.isEnabled():
            return False, self.tr(
                'This meshing method cannot run here. {0}').format(
                    self.engineReason(engine_id))
        solver = self._targetSolver
        if solver and solver != 'unselected':
            result = await self._run('mesh.target_solver.set',
                                     {'target_solver': solver})
            if getattr(result, 'status', 'accepted') != 'accepted':
                return False, str(getattr(result, 'message', '')
                                  or self.tr('The solver could not be set.'))
        if self._execution.is_dirty and not await self._execution.save():
            return False, self.tr(
                'The meshing resources on this page were refused, so nothing '
                'was saved.')
        preview = await self._run('mesh.engine.select',
                                  {'engine_id': engine_id, 'dry_run': True})
        if getattr(preview, 'status', 'accepted') != 'accepted':
            message = str(getattr(preview, 'message', ''))
            self._reasons[engine_id].setText(message)
            return False, message
        document = (preview.payload or {}).get('preview') or {}
        confirmation_required = bool(document.get('confirmation_required'))
        if confirmation_required and not self._confirmMethodChange(document):
            self.refresh()
            return False, ''
        result = await self._run('mesh.engine.select', {
            'engine_id': engine_id,
            'confirmed': confirmation_required,
            'retained_artifacts': document.get('retained_artifacts', []),
        })
        if getattr(result, 'status', 'accepted') != 'accepted':
            message = str(getattr(result, 'message', ''))
            self._reasons[engine_id].setText(message)
            return False, message
        self.engineChanged.emit(engine_id)
        return True, ''

    def _confirmMethodChange(self, preview) -> bool:
        artifacts = preview.get('retained_artifacts') or []
        detail = '\n'.join(f'- {item}' for item in artifacts)
        prompt = self.tr(
            'Changing the meshing method will retain the previous '
            'engine settings but mark its generated artifacts stale.')
        if detail:
            prompt += self.tr('\n\nAffected retained artifacts:\n') + detail
        choice = QMessageBox.question(
            self, self.tr('Change meshing method'), prompt,
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return choice == QMessageBox.StandardButton.Yes
