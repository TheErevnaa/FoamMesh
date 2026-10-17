"""The section's cancel, reachable from the one Cancel (Plan 42 SG12).

The Section tool stops its own worker (`CutTool.cancelSection`).  The footer
Cancel (`job.cancel_active`) knows no tool, so a tool registers a callback
here and :func:`cancel_all` calls every one.  Panel Cancel stays scoped to
the section; the footer stops it too, with the run.
"""
from __future__ import annotations

import threading
import weakref
from typing import Callable

_lock = threading.RLock()
_callbacks: dict[int, Callable] = {}


def register(owner: object, callback: Callable[[], bool]) -> None:
    """Call *callback* on :func:`cancel_all`; it returns whether it stopped
    something.  One callback per *owner*."""
    held = (weakref.WeakMethod(callback) if hasattr(callback, '__self__')
            else (lambda: callback))
    with _lock:
        _callbacks[id(owner)] = held


def unregister(owner: object) -> None:
    with _lock:
        _callbacks.pop(id(owner), None)


def cancel_all() -> int:
    """Stop every running section; how many had something to stop."""
    with _lock:
        callbacks = list(_callbacks.values())
    stopped = 0
    for held in callbacks:
        callback = held()
        if callback is None:
            continue
        try:
            if callback():
                stopped += 1
        except Exception:                                   # noqa: BLE001
            continue
    return stopped
