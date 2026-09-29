"""Plan 35 CR6. The unreachable-runtime banner and the runtime diagnostics.

``WslHealthBar`` sits across the top of the window and is shown only while the
WSL health monitor says the runtime is ``unreachable``:

    The OpenFOAM runtime (OpenFOAM13Runtime) is not answering: <reason>.
    [Retry connection] [Restart WSL runtime] [Open diagnostics]

[Restart WSL runtime] runs ``wsl --terminate <distro>`` and probes again, but
only after the user has read that every job running in that distribution stops.

``RuntimeDiagnosticsDialog`` is the "OpenFOAM runtime diagnostics" window. It
is non-modal and opens at once; the profile the facade reports (a WSL round
trip, off the GUI thread) and the health monitor's recent history fill in when
they arrive. It is the one place ``wsl --shutdown`` is offered, behind a
warning that it stops every distribution on the machine.

Nothing here calls ``wsl.exe``: the monitor does, on a worker thread.
"""
from __future__ import annotations

import asyncio
import datetime
import logging

import shiboken6
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from foammesh.core.jobs.wsl_health import UNREACHABLE
from foammesh.core.project.events import Event
from foammesh.view.main_window.recovery_offer import _Bar

logger = logging.getLogger(__name__)


async def confirm(parent, title: str, text: str) -> bool:
    """Ask before a destructive WSL action. Tests replace this."""
    from widgets.async_message_box import AsyncMessageBox
    return await AsyncMessageBox().confirm(parent, title, text)


def _clock(stamp) -> str:
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return 'never'
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime('%H:%M:%S')
    except (OverflowError, OSError, ValueError):
        return str(stamp)


def profile_lines(payload: dict | None) -> list[str]:
    """The selected runtime profile, as the diagnostics dialog always showed it."""
    payload = payload or {}
    selected = payload.get('selected_profile')
    if not selected:
        profiles = payload.get('profiles') or []
        reasons = [str(item.get('reason') or item.get('profile_id'))
                   for item in profiles]
        return reasons or ['No qualified runtime is available.']
    utilities = selected.get('utilities') or {}
    return [
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
        *(f"  {name}: {path}" for name, path in sorted(utilities.items())),
    ]


def health_lines(monitor) -> list[str]:
    """What the health monitor knows: state, last answer, recent changes."""
    if monitor is None:
        return ['WSL health: not watched (no WSL runtime is configured).']
    lines = [
        f'WSL health: {monitor.state}'
        + (f' ({monitor.reason})' if monitor.reason else ''),
        f'  Distribution: {monitor.distribution}',
        f'  Last probe: {_clock(monitor.last_probe_at)}',
        f'  Last answer: {_clock(monitor.last_alive_at)}',
    ]
    history = list(getattr(monitor, 'history', ()))[-8:]
    if history:
        lines.append('  Recent changes:')
        lines.extend(f'    {_clock(when)}  {state}' + (f'  {reason}' if reason else '')
                     for when, state, reason in history)
    return lines


class _Tasks:
    """Keeps the scheduled recovery coroutines alive until they finish."""

    def __init__(self):
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coroutine):
        try:
            task = asyncio.ensure_future(coroutine)
        except RuntimeError:        # no loop: nothing can be scheduled
            coroutine.close()
            return None
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def cancel(self):
        for task in tuple(self.tasks):
            task.cancel()
        self.tasks.clear()


class RuntimeDiagnosticsDialog(QDialog):
    """The runtime profile, the WSL health, and [Shut down WSL...]."""

    def __init__(self, health=None, parent=None):
        super().__init__(parent)
        self.setObjectName('runtimeDiagnosticsDialog')
        self.setWindowTitle(self.tr('OpenFOAM runtime diagnostics'))
        self.setModal(False)
        self._health = health
        self._tasks = _Tasks()
        self._profile = [self.tr('Asking the runtime...')]
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit(self)
        self.text.setObjectName('runtimeDiagnosticsText')
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        warning = QLabel(self.tr(
            'Shutting WSL down stops every WSL distribution on this computer, '
            'including any job running in them and any other program using WSL.'),
            self)
        warning.setWordWrap(True)
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.shutdownButton = QPushButton(self.tr('Shut down WSL…'), self)
        self.shutdownButton.setObjectName('wslShutdown')
        self.shutdownButton.setEnabled(self._monitor() is not None)
        self.shutdownButton.clicked.connect(self._shutdownClicked)
        buttons.addButton(self.shutdownButton, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.resize(620, 460)
        self._render()

    def _monitor(self):
        return getattr(self._health, 'current', None) if self._health is not None else None

    def setProfile(self, result) -> None:
        if getattr(result, 'status', '') != 'accepted':
            self._profile = [str(getattr(result, 'message', '')
                                 or self.tr('Runtime diagnostics failed.'))]
        else:
            self._profile = profile_lines(getattr(result, 'payload', None))
        self._render()

    def _render(self) -> None:
        if not shiboken6.isValid(self):
            return
        self.text.setPlainText('\n'.join(
            [*self._profile, '', *health_lines(self._monitor())]))

    def _shutdownClicked(self) -> None:
        self._tasks.spawn(self.shutdownWsl())

    async def shutdownWsl(self) -> str | None:
        monitor = self._monitor()
        if monitor is None:
            return None
        if not await confirm(self, self.tr('Shut down WSL'), self.tr(
                'This runs "wsl --shutdown": every WSL distribution on this '
                'computer stops, including any job running in them and any '
                'other program using WSL. Continue?')):
            return None
        self.shutdownButton.setEnabled(False)
        try:
            return await monitor.shutdown_all()
        finally:
            if shiboken6.isValid(self):
                self.shutdownButton.setEnabled(True)
                self._render()

    def closeEvent(self, event):
        self._tasks.cancel()
        super().closeEvent(event)


def show_runtime_diagnostics(parent, client, health=None) -> RuntimeDiagnosticsDialog:
    """Open the diagnostics window now and fill it when the facade answers."""
    from foammesh.view.facade_client import submit
    dialog = RuntimeDiagnosticsDialog(health, parent)
    dialog.show()
    dialog.raise_()

    def answered(result):
        if shiboken6.isValid(dialog):
            dialog.setProfile(result)
    if client is not None:
        dialog._submitted = submit(client, 'openfoam.runtime.diagnostics', {},
                                   then=answered)
    return dialog


class WslHealthBar(_Bar):
    """The runtime is not answering. [Retry] [Restart WSL runtime] [Diagnostics]."""

    def __init__(self, health, events=None, *, on_diagnostics=None, parent=None):
        super().__init__('wslHealthBar', parent)
        self.setAccessibleName(self.tr('OpenFOAM runtime not answering'))
        self._health = health
        self._tasks = _Tasks()
        self._onDiagnostics = on_diagnostics
        self.retryButton = self._button(
            self.tr('Retry connection'), 'wslRetryConnection', self._retryClicked)
        self.restartButton = self._button(
            self.tr('Restart WSL runtime'), 'wslRestartRuntime', self._restartClicked)
        self.diagnosticsButton = self._button(
            self.tr('Open diagnostics'), 'wslOpenDiagnostics', self._diagnosticsClicked)
        self._unsubscribe = None
        if events is not None:
            self._unsubscribe = events.subscribe(Event.WSL_HEALTH_CHANGED,
                                                 self._changed)
        monitor = self._monitor()
        self.setState(monitor.snapshot() if monitor is not None else {})

    def _monitor(self):
        return getattr(self._health, 'current', None)

    def _changed(self, event=None, **snapshot) -> None:
        if not shiboken6.isValid(self):
            return
        monitor = self._monitor()
        # Only the distribution the app uses puts the banner up.
        if monitor is not None and snapshot.get('distribution') not in (
                None, monitor.distribution):
            return
        self.setState(snapshot)

    def setState(self, snapshot: dict) -> None:
        state = snapshot.get('state')
        if state != UNREACHABLE:
            self.hide()
            return
        distribution = snapshot.get('distribution') or 'WSL'
        reason = str(snapshot.get('reason') or '').rstrip('.')
        self._text.setText(self.tr(
            'The OpenFOAM runtime ({0}) is not answering{1}. Runs cannot start '
            'until it does.').format(distribution, f': {reason}' if reason else ''))
        self._busy(False)
        self.show()

    def _busy(self, busy: bool) -> None:
        for button in (self.retryButton, self.restartButton):
            button.setEnabled(not busy)

    def _retryClicked(self) -> None:
        self._tasks.spawn(self.retryConnection())

    def _restartClicked(self) -> None:
        self._tasks.spawn(self.restartRuntime())

    def _diagnosticsClicked(self) -> None:
        if self._onDiagnostics is not None:
            self._onDiagnostics()

    async def retryConnection(self) -> str | None:
        monitor = self._monitor()
        if monitor is None:
            return None
        self._busy(True)
        self._text.setText(self.tr('Asking the OpenFOAM runtime again…'))
        try:
            return await monitor.reconnect()
        finally:
            if shiboken6.isValid(self):
                self.setState(monitor.snapshot())

    async def restartRuntime(self) -> str | None:
        monitor = self._monitor()
        if monitor is None:
            return None
        running = monitor.jobs_running
        jobs = (self.tr(' {0} job(s) are running there now and will stop.').format(running)
                if running else '')
        if not await confirm(self, self.tr('Restart WSL runtime'), self.tr(
                'This runs "wsl --terminate {0}". Every program in that '
                'distribution stops, including any mesher or checkMesh that is '
                'running.{1} Continue?').format(monitor.distribution, jobs)):
            return None
        self._busy(True)
        self._text.setText(self.tr('Restarting {0}…').format(monitor.distribution))
        try:
            return await monitor.restart_runtime()
        finally:
            if shiboken6.isValid(self):
                self.setState(monitor.snapshot())

    def dispose(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
        self._tasks.cancel()
        if shiboken6.isValid(self):
            self.hide()
            self.setParent(None)
            self.deleteLater()
