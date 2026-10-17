"""Gmsh session actions as runs (Plan 42 SG16, D9).

A Gmsh action on the resident session (Generate, a plugin, a script, a write)
runs in-process on a worker thread, so it made no job event: the Live run
panel, the Console and the one Cancel never heard of it.  An :class:`ActionHandle`
makes it look like what it is -- a run:

* it publishes ``JOB_STARTED`` / ``JOB_OUTPUT`` / ``JOB_FINISHED`` /
  ``JOB_FAILED`` / ``JOB_CANCELLED`` with ``session_action=True``, so every
  surface that follows a job follows it;
* it registers a cancel callback that ``job.cancel_active`` (the one Cancel)
  reaches through :func:`cancel_all`;
* it ends by writing an entry of the shared run listing
  (:mod:`foammesh.core.jobs.run_listing`).

There is no undo for session actions (D9): nothing here touches the history.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from foammesh.core.mesh.run_progress import PROGRESS_PREFIX

#: Marks the job's argv so ``is_mesher_job`` lets the Live run panel follow it.
ARGV_MARK = 'gmsh-session'

_lock = threading.RLock()
_active: dict[str, 'ActionHandle'] = {}


def _event(name: str) -> str:
    from foammesh.core.project import Event
    return getattr(Event, name)


def _iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class ActionHandle:
    """One running session action."""

    def __init__(self, case_path, name: str, *, cancel: Callable | None,
                 publish: Callable | None, engine_id: str = 'gmsh'):
        self.case_path = Path(case_path)
        self.name = name
        self.engine_id = engine_id
        self.job_id = f'session-{uuid.uuid4().hex[:12]}'
        self.started_at = _iso()
        self._t0 = time.monotonic()
        self._cancel = cancel
        self._publish = publish
        self.cancel_requested = False
        self.ended = False

    # -- reporting -------------------------------------------------------- #

    def _emit(self, event: str, **payload) -> None:
        if self._publish is None:
            return
        try:
            self._publish(_event(event), job_id=self.job_id, name=self.name,
                          session_action=True, **payload)
        except Exception:                       # noqa: BLE001 - never ends the action
            pass

    def start(self) -> None:
        self._emit('JOB_STARTED', argv=[ARGV_MARK, self.name],
                   cwd=str(self.case_path), mutation=False,
                   message=self.name)
        self.output(f'{self.name}: started')
        self.progress(self.name, self.name, 0.0)

    def output(self, line: str) -> None:
        self._emit('JOB_OUTPUT', line=line, lines=[line])

    def progress(self, stage_id: str, message: str, fraction: float) -> None:
        record = {'kind': 'stage', 'stage_id': stage_id, 'message': message,
                  'fraction': fraction}
        self._emit('JOB_OUTPUT', lines=[PROGRESS_PREFIX + json.dumps(record)])

    # -- cancelling ------------------------------------------------------- #

    def request_cancel(self) -> bool:
        if self.ended:
            return False
        self.cancel_requested = True
        self._emit('JOB_CANCEL_REQUESTED')
        if self._cancel is not None:
            try:
                self._cancel()
            except Exception:                   # noqa: BLE001
                pass
        return True

    # -- the end ---------------------------------------------------------- #

    def finish(self, ok: bool, message: str = '') -> str:
        """End the run; returns ``done``, ``failed`` or ``cancelled``."""
        if self.ended:
            return 'done'
        self.ended = True
        with _lock:
            _active.pop(self.job_id, None)
        outcome = ('cancelled' if self.cancel_requested
                   else 'done' if ok else 'failed')
        seconds = time.monotonic() - self._t0
        line = f'{self.name}: {outcome} in {seconds:.2f} s'
        if message and outcome != 'done':
            line += f' ({message})'
        self.output(line)
        event = {'done': 'JOB_FINISHED', 'failed': 'JOB_FAILED',
                 'cancelled': 'JOB_CANCELLED'}[outcome]
        self._emit(event, outcome=outcome, message=message)
        try:
            from foammesh.core.jobs import run_listing
            run_listing.write_entry(
                self.case_path, engine_id=self.engine_id,
                kind='session-action', name=self.name, status=outcome,
                started_at=self.started_at, ended_at=_iso(),
                message=message, run_id=self.job_id, seconds=round(seconds, 3))
        except OSError:
            pass
        return outcome


def begin(case_path, name: str, *, cancel: Callable | None = None,
          publish: Callable | None = None) -> ActionHandle:
    handle = ActionHandle(case_path, name, cancel=cancel, publish=publish)
    with _lock:
        _active[handle.job_id] = handle
    handle.start()
    return handle


def active(case_path=None) -> list[ActionHandle]:
    with _lock:
        handles = list(_active.values())
    if case_path is None:
        return handles
    where = Path(case_path)
    return [item for item in handles if item.case_path == where]


def cancel_all(case_path=None) -> int:
    """The one Cancel: stop every running session action of the case."""
    return sum(1 for handle in active(case_path) if handle.request_cancel())
