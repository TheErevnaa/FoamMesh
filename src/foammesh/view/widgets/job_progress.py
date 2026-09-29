"""Single compact progress surface for long-running case jobs (§7.2).

Shows operation name, elapsed time, Cancel, and Show log for every job the
:class:`JobManager` publishes on the event bus.  Cancel kills the original
process tree through the manager; the widget itself never touches processes.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from foammesh.core.project import Event
from foammesh.view.theming.metrics import FORM_MARGIN


@dataclass
class _ActiveJob:
    name: str
    started: float
    cancelling: bool = False
    #: Latest progress line, so a long job says what it is doing rather than
    #: only how long it has been doing it. A surface check once ran for over
    #: seven hours showing nothing but a rising clock.
    stage: str = ''
    #: Plan 35 CR5 step 1: seconds of silence when the manager asked
    #: [Keep waiting] / [Stop]; ``None`` while output is flowing.
    idle_seconds: float | None = None


@dataclass
class JobProgressTracker:
    """Qt-free projection of job events into one displayable progress state."""

    _jobs: dict[str, _ActiveJob] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def on_started(self, job_id: str, name: str, *, now: float | None = None):
        self._jobs[job_id] = _ActiveJob(name, time.monotonic() if now is None else now)
        self._order.append(job_id)

    def on_cancel_requested(self, job_id: str):
        job = self._jobs.get(job_id)
        if job is not None:
            job.cancelling = True

    def on_progress(self, job_id: str, message: str):
        job = self._jobs.get(job_id)
        if job is not None and message:
            job.stage = str(message)

    def on_idle(self, job_id: str, state: str, idle_seconds: float = 0.0):
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.idle_seconds = float(idle_seconds or 0.0) if state == 'idle' else None

    @property
    def idle(self) -> bool:
        job_id = self.current_job_id
        return bool(job_id and self._jobs[job_id].idle_seconds is not None)

    def on_finished(self, job_id: str):
        self._jobs.pop(job_id, None)
        if job_id in self._order:
            self._order.remove(job_id)

    @property
    def active(self) -> bool:
        return bool(self._jobs)

    @property
    def current_job_id(self) -> str | None:
        return self._order[-1] if self._order else None

    @property
    def cancelling(self) -> bool:
        job_id = self.current_job_id
        return bool(job_id and self._jobs[job_id].cancelling)

    def label(self) -> str:
        job_id = self.current_job_id
        if job_id is None:
            return ''
        job = self._jobs[job_id]
        name = job.name
        if job.stage:
            name = f'{name} — {job.stage}'
        if job.cancelling:
            name = f'{name} (cancelling…)'
        elif job.idle_seconds is not None:
            minutes = max(1, int(job.idle_seconds // 60))
            name = f'{name} — no output for {minutes} min'
        others = len(self._jobs) - 1
        return f'{name} (+{others} more)' if others > 0 else name

    def elapsed_seconds(self, *, now: float | None = None) -> int:
        job_id = self.current_job_id
        if job_id is None:
            return 0
        current = time.monotonic() if now is None else now
        return max(0, int(current - self._jobs[job_id].started))

    @staticmethod
    def format_elapsed(seconds: int) -> str:
        minutes, remainder = divmod(max(0, seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f'{hours:d}:{minutes:02d}:{remainder:02d}'
        return f'{minutes:d}:{remainder:02d}'


class JobProgressWidget(QWidget):
    """Status-bar widget bound to the shared event bus and job manager."""

    def __init__(self, events, job_manager, show_log, parent=None):
        super().__init__(parent)
        self._tracker = JobProgressTracker()
        self._jobManager = job_manager

        layout = QHBoxLayout(self)
        layout.setContentsMargins(FORM_MARGIN, 0, FORM_MARGIN, 0)
        self._label = QLabel()
        self._elapsed = QLabel()
        self._cancel = QPushButton(self.tr('Cancel'))
        # Plan 35 CR5 step 1: a silent job is asked about, never killed.
        self._keepWaiting = QPushButton(self.tr('Keep waiting'))
        self._keepWaiting.hide()
        self._showLog = QPushButton(self.tr('Show log'))
        for widget in (self._label, self._elapsed, self._keepWaiting, self._cancel,
                       self._showLog):
            layout.addWidget(widget)

        self._cancel.clicked.connect(self._cancelCurrent)
        self._keepWaiting.clicked.connect(self._keepWaitingCurrent)
        self._showLog.clicked.connect(show_log)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)

        self._unsubscribes = [
            events.subscribe(Event.JOB_STARTED, self._onStarted),
            events.subscribe(Event.JOB_PROGRESS, self._onProgress),
            events.subscribe(Event.JOB_CANCEL_REQUESTED, self._onCancelRequested),
            events.subscribe(Event.JOB_IDLE, self._onIdle),
            events.subscribe(Event.JOB_FINISHED, self._onFinished),
            events.subscribe(Event.JOB_FAILED, self._onFinished),
            events.subscribe(Event.JOB_CANCELLED, self._onFinished),
        ]
        self.hide()

    def shutdown(self):
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes = []
        self._timer.stop()

    def _onStarted(self, *, job_id, name, **_kwargs):
        self._tracker.on_started(job_id, name)
        self._timer.start()
        self._refresh()

    def _onProgress(self, *, job_id, message=None, **_kwargs):
        self._tracker.on_progress(job_id, message or '')
        self._refresh()

    def _onCancelRequested(self, *, job_id, **_kwargs):
        self._tracker.on_cancel_requested(job_id)
        self._refresh()

    def _onIdle(self, *, job_id, state='idle', idle_seconds=0.0, **_kwargs):
        self._tracker.on_idle(job_id, state, idle_seconds)
        self._refresh()

    def _keepWaitingCurrent(self):
        job_id = self._tracker.current_job_id
        if job_id is None:
            return
        keep = getattr(self._jobManager, 'keep_waiting', None)
        if keep is None or not keep(job_id):
            self._tracker.on_idle(job_id, 'kept_waiting')
        self._refresh()

    def _onFinished(self, *, job_id, **_kwargs):
        self._tracker.on_finished(job_id)
        if not self._tracker.active:
            self._timer.stop()
        self._refresh()

    def _cancelCurrent(self):
        job_id = self._tracker.current_job_id
        if job_id is None:
            return
        self._tracker.on_cancel_requested(job_id)
        self._refresh()
        asyncio.create_task(self._cancel_anywhere(job_id))

    async def _cancel_anywhere(self, job_id):
        """Cancel whether the work is a subprocess or in-process diagnostics.

        The manager only knows about processes it launched. Surface checks run
        in this process and register a budget instead, so both have to be
        asked -- otherwise Cancel silently does nothing for the one kind of
        work that has actually been seen to run away.
        """
        from foammesh.core.geometry.diagnostics import budget as budget_module

        if budget_module.cancel(job_id):
            return
        await self._jobManager.cancel(job_id)

    def _refresh(self):
        if not self._tracker.active:
            self.hide()
            return
        self._label.setText(self._tracker.label())
        self._elapsed.setText(self._tracker.format_elapsed(self._tracker.elapsed_seconds()))
        self._cancel.setEnabled(not self._tracker.cancelling)
        idle = self._tracker.idle and not self._tracker.cancelling
        self._keepWaiting.setVisible(idle)
        self._cancel.setText(self.tr('Stop') if idle else self.tr('Cancel'))
        self.show()
