"""The one Cancel, for the parts of a Gmsh Run to end no job owns (DP-5743).

A Gmsh Run to end is one operation: prepare the geometry, write the job, run
Gmsh (a job), publish the mesh, check it (a job). Only the two jobs were
reachable from the footer's Cancel. MEASURED on the ef_save window walk
(2026-10-24): Cancel pressed while the mesh was being published answered
"Nothing was running, so nothing was stopped." and the run went on to finish.

A run holds a :class:`PipelineToken` for its case. Cancel sets it, and the run
checks it between its steps, up to the moment the new mesh replaces the case
mesh. From that moment the run is committing: a Cancel then can no longer
leave the case mesh as it was, so it is refused and says why.
"""
from __future__ import annotations

import threading
from pathlib import Path

_lock = threading.Lock()
_active: dict[str, 'PipelineToken'] = {}


class PipelineCancelled(Exception):
    """The user's Cancel reached a run between two of its steps."""


def _key(case_path) -> str:
    return str(Path(case_path).resolve()).lower()


class PipelineToken:
    """One running pipeline: cancelled, committing, or neither."""

    def __init__(self, case_path):
        self.case_path = Path(case_path)
        self.cancelled = False
        self.committing = False

    def check(self) -> None:
        """Raise :class:`PipelineCancelled` if the user has cancelled."""
        if self.cancelled:
            raise PipelineCancelled('the run was cancelled before it finished')

    def commit(self) -> None:
        """The case mesh is about to change: no Cancel stops the run now.

        Raises :class:`PipelineCancelled` instead when a Cancel came first.
        """
        with _lock:
            if self.cancelled:
                raise PipelineCancelled(
                    'the run was cancelled before it finished')
            self.committing = True


def start(case_path) -> PipelineToken:
    token = PipelineToken(case_path)
    with _lock:
        _active[_key(case_path)] = token
    return token


def finish(token: PipelineToken) -> None:
    with _lock:
        key = _key(token.case_path)
        if _active.get(key) is token:
            del _active[key]


def current(case_path) -> PipelineToken | None:
    with _lock:
        return _active.get(_key(case_path))


def cancel_all(case_path) -> dict:
    """Cancel this case's pipeline: ``{'stopped': n, 'committing': n}``."""
    with _lock:
        token = _active.get(_key(case_path))
        if token is None:
            return {'stopped': 0, 'committing': 0}
        if token.committing:
            return {'stopped': 0, 'committing': 1}
        token.cancelled = True
        return {'stopped': 1, 'committing': 0}
