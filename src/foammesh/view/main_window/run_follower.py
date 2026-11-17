"""A meshing run shows on the window whoever started it (DP-6454).

MEASURED on the dev build: a run started through the local API
(``workflow.run_pipeline`` or ``workflow.run_stage``) left the window's status
bar empty and the run status strip idle for the whole run. The strip, its
Cancel and the sentence a run ends with were driven only by the page code that
starts a run from its own button (``showRunStarted`` before it submits,
``showRunStatus`` when it has its result), so a run the application executed
for any other caller ran unseen.

The one place every run passes through is the facade's command lifecycle: the
session says when a queued command starts and when it ends, whether it
returned or raised (:meth:`CaseSession.observe_command_lifecycle`). This
follows that lifecycle for the meshing runs and drives the window's existing
surfaces from it -- the run status strip (running, with the stage the run is
in, and its Cancel) and the status bar -- on the GUI thread. There is no
second indicator.

A run a page started is the page's to report: the page puts its own plan on
the strip before it submits and its own verdict after, with the offers and
modals that go with them. So a command whose source is the window itself
(``gui``) is left alone here, and nothing is reported twice.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import PurePath

from PySide6.QtCore import QObject, Signal

from .run_narration import cell_limit_warnings, describe_result, newest_log

logger = logging.getLogger(__name__)

#: The commands that are a meshing run: the ones a page shows on the strip.
RUN_OPERATIONS = frozenset({
    'workflow.run_pipeline', 'workflow.run_stage', 'mesh.gmsh.run'})

#: What a job's command line is running, by the tool it names (only the tools
#: FoamMesh runs: the capability matrix reads a name here as a claim). The Gmsh
#: runner is a Python script; everything else is the OpenFOAM utility.
_STAGE_TOOLS = (
    'blockMesh', 'surfaceFeatures',
    'snappyHexMesh', 'checkMesh', 'decomposePar',
    'reconstructPar', 'redistributePar', 'extrudeMesh', 'createPatch',
    'renumberMesh', 'transformPoints')

#: How long the status bar keeps the sentence a run ended with, as the
#: page's own reports do.
END_MESSAGE_MS = 10000


def stage_of(argv) -> str:
    """The stage a job's command line runs, or ``''`` when it names none."""
    words = []
    for part in argv or ():
        for word in str(part).replace(';', ' ').split():
            words.append(PurePath(word.strip('\'"')).name)
    for word in words:
        if 'runner_v1' in word or word.startswith('gmsh'):
            return 'Gmsh'
    for word in words:
        for tool in _STAGE_TOOLS:
            if word == tool:
                return tool
    return ''


def run_title(operation: str, parameters) -> str:
    """The running sentence before the run has said which stage it is in."""
    parameters = parameters if isinstance(parameters, dict) else {}
    if operation == 'workflow.run_stage':
        stage = str(parameters.get('stage') or '').strip()
        return f'Running {stage}' if stage else 'Running a meshing stage'
    if operation == 'mesh.gmsh.run':
        return 'Running Gmsh'
    return 'Running the meshing pipeline'


def _cancelled(payload: dict) -> bool:
    """Whether the run ended because it was cancelled (as the page reads it)."""
    if payload.get('cancelled') is True:
        return True
    if str((payload.get('job') or {}).get('status') or '') == 'cancelled':
        return True
    return (str(payload.get('reason') or '')
            == 'the run was cancelled before it finished')


def refused_before_start(payload: dict) -> bool:
    """DP-6487. Whether the recovery gate refused the run before anything
    started: a stage's or the pipeline's first execution's exit kind."""
    payload = payload if isinstance(payload, dict) else {}
    if str(payload.get('exit_kind') or '') == 'recovery_pending':
        return True
    jobs = [payload.get('job')]
    jobs += [(item or {}).get('job') for item in payload.get('executions') or ()
             if isinstance(item, dict)]
    return any(isinstance(job, dict)
               and str((job.get('exit') or {}).get('kind') or '')
               == 'recovery_pending' for job in jobs)


def end_sentence(result=None, error=None) -> tuple[str, bool, str]:
    """``(sentence, failed, log)`` for a run that ended."""
    if isinstance(error, asyncio.CancelledError):
        return 'The run was cancelled.', False, ''
    if error is not None:
        details = getattr(error, 'details', None)
        if isinstance(details, dict) and details.get('cancelled') is True:
            # DP-6498. A run stopped before its stage began (while the case
            # was being split for parallel) ends as an error that says so.
            return 'The run was cancelled.', False, ''
        text = str(error).strip() or type(error).__name__
        return f'The run failed: {text}', True, ''
    payload = getattr(result, 'payload', None)
    payload = payload if isinstance(payload, dict) else {}
    log = newest_log(payload)
    run_id = str(payload.get('run_id') or '').strip()
    if _cancelled(payload):
        named = f'Run {run_id}' if run_id else 'The run'
        return f'{named} was cancelled.', False, log
    if str(getattr(result, 'status', '')) == 'failed':
        if refused_before_start(payload):
            # DP-6487. Nothing ran: the refusal's own sentence, alone.
            reason = str(payload.get('reason') or payload.get('error') or '')
            return (reason.strip() or 'The run was not started.'), True, log
        sentence = describe_result(payload, failed=True)
        reason = str(payload.get('reason') or payload.get('error') or '')
        reason = reason.strip()
        if reason:
            sentence = f'{sentence} {reason[0].upper()}{reason[1:]}'
            if not sentence.endswith('.'):
                sentence += '.'
        return sentence, True, log
    return describe_result(payload), False, log


class RunFollower(QObject):
    """Drives the window's run strip and status bar from the run lifecycle.

    Created on the GUI thread. The lifecycle and the job events may arrive on
    any thread; each is handed to the GUI thread through ``_relay`` (a queued
    connection when the sender is another thread, a direct call otherwise).
    """

    _relay = Signal(object)

    def __init__(self, window_getter, parent=None):
        super().__init__(parent)
        self._window_getter = window_getter
        #: command id -> (title, stage) of the run this follower is showing.
        self._shown: dict[str, list] = {}
        self._relay.connect(self._call)

    # -- what the session and the bus call --------------------------------- #

    def command_started(self, command) -> None:
        if not self._follows(command):
            return
        self._relay.emit(partial(
            self._started, str(command.command_id),
            run_title(command.operation, command.parameters)))

    def command_ended(self, command, result=None, error=None) -> None:
        if not self._follows(command):
            return
        self._relay.emit(partial(
            self._ended, str(command.command_id), result, error))

    def job_started(self, **payload) -> None:
        stage = stage_of(payload.get('argv'))
        if stage:
            self._relay.emit(partial(self._stage, stage, ''))

    def job_progress(self, **payload) -> None:
        progress = payload.get('progress')
        percent = None
        if isinstance(progress, dict):
            percent = progress.get('percent')
        elif payload.get('fraction') is not None:
            try:
                percent = float(payload['fraction']) * 100.0
            except (TypeError, ValueError):
                percent = None
        if percent is None:
            return
        try:
            done = f'{float(percent):.0f}%'
        except (TypeError, ValueError):
            return
        self._relay.emit(partial(self._stage, None, done))

    # -- on the GUI thread ------------------------------------------------- #

    @staticmethod
    def _follows(command) -> bool:
        if getattr(command, 'operation', '') not in RUN_OPERATIONS:
            return False
        source = getattr(command, 'source', None)
        source = getattr(source, 'value', source)
        # A page that starts a run reports it itself (see the module note).
        return source != 'gui'

    def _call(self, function) -> None:
        try:
            function()
        except RuntimeError:            # the window is being torn down
            logger.debug('run follower: window gone', exc_info=True)

    def _window(self):
        try:
            return self._window_getter()
        except RuntimeError:
            return None

    def _started(self, command_id: str, title: str) -> None:
        if self._shown:
            # A run started inside a run this is showing: the outer one is
            # what the strip is about.
            self._shown[command_id] = None
            return
        self._shown[command_id] = [title, '', '']
        self._show(title)

    def _stage(self, stage, done) -> None:
        current = next((item for item in self._shown.values() if item), None)
        if current is None:
            return
        if stage is not None:
            current[1], current[2] = stage, ''
        if done:
            current[2] = done
        title, stage, done = current
        text = f'{title}: {stage}' if stage else title
        if done:
            text = f'{text} · {done}'
        self._show(text)

    def _show(self, text: str) -> None:
        window = self._window()
        if window is None:
            return
        strip = getattr(window, '_runStatusStrip', None)
        if getattr(strip, 'state', '') == 'cancelling':
            # The user pressed Cancel; the strip says so until the run ends.
            return
        sentence = f'{text}…'
        window.showRunStarted(sentence)
        window.statusBar().showMessage(sentence)

    def _ended(self, command_id: str, result, error) -> None:
        shown = self._shown.pop(command_id, None)
        if shown is None:
            return
        if self._shown:
            # The outer run ended while an inner one was still recorded.
            self._shown.clear()
        sentence, failed, log = end_sentence(result, error)
        window = self._window()
        if window is None:
            return
        # DP-6483. A run stopped at Max global cells said so only in its
        # answer and on a tooltip; the sentence goes to the Console, whole.
        if error is None and not failed:
            from .session_activity import _console_append
            for warning in cell_limit_warnings(
                    getattr(result, 'payload', None)):
                _console_append(window, f'Refinement: {warning}')
        window.showRunStatus(sentence, failed=failed, log=log)
        window.statusBar().showMessage(sentence, END_MESSAGE_MS)


def install_run_follower(session, window_getter):
    """Follow *session*'s meshing runs on the window; returns the undo."""
    from foammesh.core.project import Event

    follower = RunFollower(window_getter)
    undo = [
        session.observe_command_lifecycle(
            follower.command_started, follower.command_ended),
        session.state.bus.subscribe(Event.JOB_STARTED, follower.job_started),
        session.state.bus.subscribe(Event.JOB_PROGRESS, follower.job_progress),
    ]

    def release():
        for forget in undo:
            try:
                forget()
            except Exception:                               # noqa: BLE001
                logger.debug('run follower release', exc_info=True)
        undo.clear()
        follower.deleteLater()

    release.follower = follower
    return release
