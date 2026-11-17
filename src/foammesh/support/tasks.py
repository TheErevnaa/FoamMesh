"""Fire-and-forget tasks that are never lost silently (Plan 50 IP19, ST-9).

``asyncio.create_task(work())`` with the task thrown away leaves the loop as
its only holder, and the loop holds tasks weakly: a task nobody references can
be collected half-way through, and an exception inside one surfaces -- if at
all -- as "Task exception was never retrieved" whenever the collector gets to
it. Either way the person at the window saw nothing; the run, reload or probe
they started simply never finished (DP-6361).

:func:`spawn` is the one way GUI code starts work it does not await. It keeps
a strong reference until the task is done, logs an exception with its
traceback, and says on the status line that the work stopped. Cancellation is
not a fault and is not reported.
"""
from __future__ import annotations

import asyncio
import logging
import warnings
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

#: Tasks started by :func:`spawn` and not yet finished. The strong reference
#: is the point: the loop alone keeps only a weak one.
_running: set[asyncio.Future] = set()

#: Says a sentence to the person. The main window installs its status line
#: (:func:`set_reporter`); before it does, a dead task is logged only.
_reporter: Callable[[str], Any] | None = None


def set_reporter(reporter: Callable[[str], Any] | None) -> None:
    """Install where a dead task is reported (the window's status line)."""
    global _reporter
    _reporter = reporter


def running() -> tuple[asyncio.Future, ...]:
    """The tasks :func:`spawn` still holds."""
    _drop_the_stranded()
    return tuple(_running)


def _drop_the_stranded() -> None:
    """Let go of tasks whose event loop was closed under them.

    Such a task can never finish, so its done callback never lets go of it
    and it would be held, and counted as running, for the life of the process.
    """
    stranded = [task for task in _running
                if not task.done() and task.get_loop().is_closed()]
    _running.difference_update(stranded)


def spawn(work: Awaitable, *, what: str = '',
          loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Future:
    """Start *work* without awaiting it, held until it is done.

    *what* names the work for the person ("Loading the mesh") when it fails.
    *work* is a coroutine or any awaitable; a future is held as it is.
    With no running loop (and no *loop*) it uses the thread's event loop,
    as ``asyncio.ensure_future`` did; when that cannot take the task either it
    raises RuntimeError and closes *work*, so the coroutine is not reported
    as never awaited.
    """
    try:
        if loop is None:
            loop = _loop()
        task = asyncio.ensure_future(work, loop=loop)
    except RuntimeError:
        close = getattr(work, 'close', None)
        if callable(close):
            close()
        raise
    label = what or _name(work)
    _drop_the_stranded()
    _running.add(task)
    task.add_done_callback(lambda done: _finished(done, label))
    return task


def _loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_closed():
            raise RuntimeError('no event loop to start the task on') from None
        return loop


def _name(work) -> str:
    code = getattr(work, 'cr_code', None) or getattr(work, 'gi_code', None)
    name = getattr(code, 'co_qualname', None) or getattr(code, 'co_name', None)
    return name or type(work).__name__


def _finished(task: asyncio.Future, what: str) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    logger.error('%s stopped: %s', what, error,
                 exc_info=(type(error), error, error.__traceback__))
    reporter = _reporter
    if reporter is None:
        return
    try:
        reporter(f'{what} stopped: {error}')
    except Exception:  # the report must never hide the log line above
        logger.exception('Could not report that %s stopped', what)
