"""The event loop's default executor: background work that never holds the exit.

``asyncio.to_thread`` and ``loop.run_in_executor(None, ...)`` run on the loop's
default executor. qasync's own (``QThreadExecutor``) is shut down by
``QEventLoop.close()`` with ``wait=True``: its workers first run every job still
queued and then the main thread waits for all of them. MEASURED live (Plan 45,
2026-10-17): after the app was told to quit, ``main()`` returned 4-65 s later,
the main thread waiting in ``QThreadExecutor.shutdown`` on start-up probes of
the WSL runtime (``capabilities.utility``/``help``, the Gmsh and snappy probes)
-- some of them still queued at quit and only *started* after it.

This executor ends differently. On shutdown, jobs that have not started are
cancelled (nothing can await their answer once the loop has stopped). A running
job that is only waiting on a child process -- inside ``subprocess``, or in a
function marked :func:`waits_on_child_process` -- is not waited for: the
process it waits on is not killed by this, and nobody is left to read its
answer. Every other running job gets a short grace to finish, long enough for a
file write or a copy. A job still running after that is left on its daemon
thread and its answer is dropped; each job left is named in the log. The
workers are daemon threads, so the interpreter does not join them at exit
either (``concurrent.futures.ThreadPoolExecutor`` workers it would).
"""
from __future__ import annotations

import concurrent.futures
import functools
import logging
import os
import queue
import sys
import threading
import time

logger = logging.getLogger(__name__)

#: How long shutdown waits for the jobs already running, in seconds.
SHUTDOWN_GRACE_S = 1.0
#: As many workers as qasync's default executor had.
MAX_WORKERS = 10

_STOP = object()

#: Code objects of functions whose wait is a wait on a child process.
_CHILD_WAITS: set = set()
_SUBPROCESS_FILE = os.sep + 'subprocess.py'


def waits_on_child_process(function):
    """Mark *function*: a job blocked inside it only waits on a child process.

    For waits the ``subprocess`` module does not show, such as a session
    waiting for its first answer from a process it started. Returns
    *function* unchanged.
    """
    _CHILD_WAITS.add(function.__code__)
    return function


def _waiting_on_child(frame) -> bool:
    while frame is not None:
        code = frame.f_code
        if code in _CHILD_WAITS or code.co_filename.endswith(_SUBPROCESS_FILE):
            return True
        frame = frame.f_back
    return False


def _describe(fn) -> str:
    if isinstance(fn, functools.partial):
        # asyncio.to_thread submits partial(context.run, func, *args).
        if (getattr(fn.func, '__name__', '') == 'run' and fn.args
                and callable(fn.args[0])):
            return _describe(fn.args[0])
        return _describe(fn.func)
    name = getattr(fn, '__qualname__', None) or getattr(fn, '__name__', None)
    if name is None:
        return repr(fn)[:80]
    module = getattr(fn, '__module__', '') or ''
    return f'{module}.{name}' if module else name


class _Job:
    __slots__ = ('future', 'fn', 'args', 'kwargs')

    def __init__(self, future, fn, args, kwargs):
        self.future = future
        self.fn = fn
        self.args = args
        self.kwargs = kwargs


class PromptExitExecutor(concurrent.futures.Executor):
    """A thread pool whose shutdown never waits more than ``grace`` seconds."""

    def __init__(self, max_workers: int = MAX_WORKERS,
                 grace: float = SHUTDOWN_GRACE_S,
                 thread_name_prefix: str = 'foammesh-worker'):
        if max_workers <= 0:
            raise ValueError('max_workers must be greater than 0')
        self._max_workers = max_workers
        self.grace = float(grace)
        self._prefix = thread_name_prefix
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._idle = threading.Semaphore(0)
        self._threads: list[threading.Thread] = []
        self._running: dict[int, _Job] = {}
        self._finished = threading.Condition(self._lock)
        self._shutdown = False
        self._cancel_pending = True
        #: Set once shutdown has stopped waiting: answers after it are dropped.
        self._abandoned = False
        #: Descriptions of the jobs shutdown left running (for the log and tests).
        self.abandoned_jobs: list[str] = []

    # -- submitting ------------------------------------------------------ #

    def submit(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._shutdown:
                raise RuntimeError('cannot schedule new futures after shutdown')
            future = concurrent.futures.Future()
            self._queue.put(_Job(future, fn, args, kwargs))
            if not self._idle.acquire(blocking=False) and \
                    len(self._threads) < self._max_workers:
                thread = threading.Thread(
                    target=self._work,
                    name=f'{self._prefix}-{len(self._threads) + 1}',
                    daemon=True)
                self._threads.append(thread)
                thread.start()
            return future

    def _work(self) -> None:
        ident = threading.get_ident()
        while True:
            job = self._queue.get()
            if job is _STOP:
                return
            with self._lock:
                # Shutdown drains the queue while a worker may hold a job it
                # has just taken: that one has not started either.
                if self._shutdown and self._cancel_pending:
                    job.future.cancel()
                    continue
                if not job.future.set_running_or_notify_cancel():
                    self._idle.release()
                    continue
                self._running[ident] = job
            try:
                outcome = (True, job.fn(*job.args, **job.kwargs))
            except BaseException as error:              # noqa: BLE001
                outcome = (False, error)
            with self._lock:
                self._running.pop(ident, None)
                abandoned = self._abandoned
                self._finished.notify_all()
            if abandoned:
                # The loop that awaited this is closed; touching the future
                # would post into a torn-down event loop during exit.
                job = None
                return
            if outcome[0]:
                job.future.set_result(outcome[1])
            else:
                job.future.set_exception(outcome[1])
            job = None
            outcome = None
            self._idle.release()

    # -- ending ------------------------------------------------------------ #

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = True) -> None:
        """Stop: cancel what has not started; wait at most ``grace`` seconds,
        and not at all once every running job only waits on a child process."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._cancel_pending = bool(cancel_futures)
        if cancel_futures:
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not _STOP:
                    job.future.cancel()
        for _ in self._threads:
            self._queue.put(_STOP)
        if not wait:
            return
        deadline = time.monotonic() + max(0.0, self.grace)
        with self._lock:
            while self._running and not self._only_child_waits():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                # Re-checked often: a job may reach its process wait later.
                self._finished.wait(min(left, 0.02))
            if self._running:
                self._abandoned = True
                self.abandoned_jobs = [_describe(job.fn)
                                       for job in self._running.values()]
        if self.abandoned_jobs:
            logger.info('Exit did not wait for %d background job(s) still '
                        'running: %s', len(self.abandoned_jobs),
                        ', '.join(self.abandoned_jobs))

    def _only_child_waits(self) -> bool:
        """Every running job is blocked waiting on a child process."""
        frames = sys._current_frames()
        return all(_waiting_on_child(frames.get(ident))
                   for ident in self._running)


def install(loop, **options) -> PromptExitExecutor:
    """Make a :class:`PromptExitExecutor` the default executor of *loop*."""
    executor = PromptExitExecutor(**options)
    loop.set_default_executor(executor)
    return executor
