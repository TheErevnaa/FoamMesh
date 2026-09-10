"""Shared meshing-method chooser backed exclusively by facade operations."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QGroupBox, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QTabWidget, QVBoxLayout, QWidget,
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
    #: Display names for registered engines. An engine absent here is offered
    #: under its own id rather than hidden, so registering an engine without
    #: adding a label degrades visibly instead of silently.
    ENGINE_LABELS = {
        'snappy': 'Snappy Hex Mesh',
        'gmsh': 'Gmsh',
    }

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
        self.setAccessibleName(self.tr('Meshing method'))
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Meshing Method'))
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
            label = self.ENGINE_LABELS.get(engine_id, engine_id)
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
        self._apply = QPushButton(self.tr('Apply Method'), self)
        # G2. Of four identical full-width buttons this is the only one that
        # advances the workflow; the other three open read-only reports.
        self._apply.setProperty('foammeshRole', 'primary')
        self._apply.clicked.connect(self._apply_selection)
        layout.addWidget(self._apply)
        # R131. MEASURED: pressing Apply Method with no engine radio checked
        # did nothing whatsoever -- no dialog, no message, no status line --
        # so the button read as broken rather than as waiting for an input.
        # This line is where that is said. It is kept at the height of one
        # line whether or not there is anything in it, so pressing the button
        # does not shift the panel under the cursor.
        self._applyHint = QLabel(self)
        self._applyHint.setWordWrap(True)
        self._applyHint.setObjectName('meshingMethodApplyHint')
        self._applyHint.setProperty('foammeshTone', 'muted')
        self._applyHint.setMinimumHeight(
            self._applyHint.fontMetrics().lineSpacing())
        layout.addWidget(self._applyHint)

        diagnosticsBox = QGroupBox(self.tr('Diagnostics'), self)
        diagnosticsBox.setObjectName('meshingDiagnosticsBox')
        diagnosticsLayout = QVBoxLayout(diagnosticsBox)
        self._runtimeDiagnostics = QPushButton(
            self.tr('OpenFOAM Runtime Diagnostics...'), self)
        self._runtimeDiagnostics.clicked.connect(self._show_runtime_diagnostics)
        diagnosticsLayout.addWidget(self._runtimeDiagnostics)
        self._effectiveDictionaries = QPushButton(
            self.tr('Effective Meshing Setup...'), self)
        self._effectiveDictionaries.clicked.connect(
            self._show_effective_dictionaries)
        diagnosticsLayout.addWidget(self._effectiveDictionaries)
        self._reprobe = QPushButton(self.tr('Re-probe Runtimes'), self)
        self._reprobe.clicked.connect(lambda: self.refresh(force=True))
        diagnosticsLayout.addWidget(self._reprobe)
        layout.addWidget(diagnosticsBox)
        layout.addStretch(1)
        self.refresh()

    def _track(self, task):
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def refresh(self, *, force=False):
        self._probeGeneration += 1
        generation = self._probeGeneration
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
        # R130. MEASURED: click OpenFOAM, click Snappy Hex Mesh, press Apply
        # Method -- nothing was applied and both engine radios were empty.
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
            self._reasons[engine_id].setText(self.tr('Probing the runtime...'))
            self._submit_probe(engine_id, current, generation, force)

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
        blocked = [self.ENGINE_LABELS.get(engine_id, engine_id)
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
                    self, self.tr('Meshing Method'),
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
                self.tr('Still probing the runtime...'))
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
            self._reasons[engine_id].setText(
                self.tr('Runtime {0}').format(version) if version else text)
            self._reasons[engine_id].setToolTip(
                text + (self.tr(' · fingerprint {0}').format(fingerprint)
                        if fingerprint else ''))
        else:
            self._reasons[engine_id].setText(
                reason or self.tr('The required runtime is unavailable.'))
            self._reasons[engine_id].setToolTip('')
        self.probeFinished.emit(available)

    def _show_runtime_diagnostics(self):
        def show(result):
            if getattr(result, 'status', '') != 'accepted':
                QMessageBox.warning(
                    self, self.tr('OpenFOAM Runtime'),
                    str(getattr(result, 'message', '')
                        or self.tr('Runtime diagnostics failed.')))
                return
            selected = (result.payload or {}).get('selected_profile')
            if not selected:
                profiles = (result.payload or {}).get('profiles') or []
                reason = '\n'.join(
                    str(item.get('reason') or item.get('profile_id'))
                    for item in profiles)
                QMessageBox.warning(
                    self, self.tr('OpenFOAM Runtime'),
                    reason or self.tr('No qualified runtime is available.'))
                return
            utilities = selected.get('utilities') or {}
            lines = [
                f"Profile: {selected.get('profile_id', '')}",
                f"Distribution: {selected.get('distribution', '')}",
                f"User: {selected.get('user', '')}",
                f"Bashrc: {selected.get('bashrc', '')}",
                f"Project: {selected.get('project', '')}",
                f"Version: {selected.get('version', '')}",
                f"Build: {selected.get('wm_options', '')}",
                f"MPI: {selected.get('mpi_identity', '')}",
                f"Fingerprint: {selected.get('fingerprint', '')}",
                '',
                'Utilities:',
                *(f"  {name}: {path}"
                  for name, path in sorted(utilities.items())),
            ]
            QMessageBox.information(
                self, self.tr('OpenFOAM Runtime Diagnostics'),
                '\n'.join(lines))

        self._track(_submit(
            self._client, 'openfoam.runtime.diagnostics', {}, show))

    def _show_effective_dictionaries(self):
        checked = self._group.checkedButton()
        if (checked is not None
                and checked.property('engineId') != 'snappy'):
            self._show_effective_plan()
            return

        def show(result):
            if getattr(result, 'status', '') != 'accepted':
                QMessageBox.warning(
                    self, self.tr('Effective Dictionaries'),
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
            dialog.setWindowTitle(self.tr('Effective OpenFOAM Dictionaries'))
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
                    self, self.tr('Effective Engine Plan'),
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
                self, self.tr('Effective Engine Plan'),
                str((result.payload or {}).get('plan_digest') or
                    self.tr('Engine plan derived.')))

        self._track(_submit(self._client, 'mesh.plan.derive', {}, show))

    def _apply_selection(self):
        button = self._group.checkedButton()
        if button is None:
            # R131. Pressing this with nothing checked used to be a no-op with
            # no facade call and no feedback of any kind.
            self._applyHint.setText(self.tr(
                'Choose a meshing engine above, then press Apply Method.'))
            return
        self._applyHint.setText('')
        engine_id = button.property('engineId')

        def on_preview(result):
            if getattr(result, 'status', 'accepted') == 'failed':
                self._reasons[engine_id].setText(result.message)
                return
            preview = result.payload['preview']
            confirmation_required = bool(
                preview.get('confirmation_required'))
            if confirmation_required:
                artifacts = preview.get('retained_artifacts') or []
                detail = '\n'.join(f'- {item}' for item in artifacts)
                prompt = self.tr(
                    'Changing the meshing method will retain the previous '
                    'engine settings but mark its generated artifacts stale.')
                if detail:
                    prompt += self.tr('\n\nAffected retained artifacts:\n') + detail
                choice = QMessageBox.question(
                    self, self.tr('Change Meshing Method'), prompt,
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if choice != QMessageBox.StandardButton.Yes:
                    self.refresh()
                    return
            self._track(_submit(self._client, 'mesh.engine.select', {
                'engine_id': engine_id,
                'confirmed': confirmation_required,
                'retained_artifacts': preview.get('retained_artifacts', []),
            }, on_select))

        def on_select(result):
            if result.status == 'accepted':
                self.engineChanged.emit(engine_id)
            else:
                self._reasons[engine_id].setText(result.message)

        self._track(_submit(
            self._client, 'mesh.engine.select',
            {'engine_id': engine_id, 'dry_run': True}, on_preview))
