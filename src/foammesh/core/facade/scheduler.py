"""Single-owner asyncio command scheduler with human GUI priority."""
from __future__ import annotations

import asyncio
import inspect
import itertools
import time
from collections.abc import Callable

from .instrumentation import OwnerLoopMonitor

#: DP-34. How long the worker may sit between two commands before `submit`
#: stops believing in it. Picking up the next item off the queue takes
#: microseconds, so a worker that is neither running a command nor finished
#: after this long is not slow -- it is one whose step the loop refused to
#: enter, and it will never run again.
_STALL_SECONDS = 5.0


class CommandScheduler:
    """Serialize mutations without holding the lock for external job lifetimes."""

    def __init__(self, *, monitor: OwnerLoopMonitor | None = None):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._counter = itertools.count()
        self._worker: asyncio.Task | None = None
        self._closed = False
        #: DP-34. Whether the worker is inside a command right now, and when it
        #: last was not. Together they separate "waiting for a long mesh" from
        #: "never coming back", which `submit` could not tell apart before.
        self._busy = False
        self._idle_since = time.monotonic()
        #: DP-37. The future of the command in flight, so a worker abandoned
        #: while busy does not leave its caller waiting on a result nobody is
        #: computing any more.
        self._active: asyncio.Future | None = None
        self.monitor = monitor

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def submit(self, callback: Callable, *, human_priority: bool = False):
        if self._closed:
            raise RuntimeError('command scheduler is closed')
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self._queue.put((0 if human_priority else 1, next(self._counter), callback, future))
        if self._worker is None or self._worker.done() or await self._worker_is_lost():
            # DP-34. A worker that is lost is left where it is rather than
            # cancelled: cancelling schedules another step into the same task,
            # which is the thing that could not be entered in the first place.
            # It holds nothing -- the queue is the scheduler's state, not the
            # task's -- so the replacement picks up exactly where it stopped.
            self._abandon_worker()
            self._idle_since = time.monotonic()
            self._worker = loop.create_task(self._run(), name='foammesh-command-scheduler')
        return await future

    def _abandon_worker(self) -> None:
        """Let go of a worker that will never run again, and say so.

        DP-37. The first version of this recovery only replaced a worker that
        was lost *between* commands, because a worker that is inside one is
        normally just slow. But the refusal that loses a worker can arrive at
        any point, including while it is awaiting a dialog the user has just
        answered -- MEASURED, with the scheduler stopped at ``await value``
        and its awaited future already finished. Replacing it there leaves the
        command's own caller waiting on a result nobody will ever compute, so
        the caller is told instead: a failed command is recoverable, a call
        that never returns is not.
        """
        active = self._active
        self._active = None
        self._busy = False
        if active is not None and not active.done():
            active.set_exception(RuntimeError(
                'the command scheduler lost its worker while this command was '
                'running, so the command did not finish'))

    async def _worker_is_lost(self) -> bool:
        """Whether the worker task exists but will never run another command.

        DP-34, MEASURED. A modal warning opened from inside a scheduled
        command's callback spun a nested Qt loop, which asked the loop to step
        the scheduler while the caller's own task was still on the stack.
        Python refused -- *"Cannot enter into task ... while another task is
        being executed"* -- and the step was dropped. The task stayed pending
        with nothing scheduled to resume it, so `self._worker.done()` stayed
        False, so `submit` kept handing work to a worker that was gone. Every
        write after that waited on a future nothing would ever set: the window
        went on answering, and nothing a user did was ever applied again.

        The cause is fixed where it belongs, in the view's callback delivery.
        This is the second line: whatever loses the worker, the next command
        replaces it instead of joining the queue behind it forever.
        """
        worker = self._worker
        if worker is None or worker.done():
            return False
        if not self._busy:
            return (time.monotonic() - self._idle_since) > _STALL_SECONDS
        # DP-37. Busy is normally the one state that must never be disturbed:
        # a twenty-minute mesh is waiting, not lost. But a refused wakeup has
        # a fingerprint that tells the two apart exactly, with no timeout and
        # no guessing -- a task that is still pending while the future it is
        # awaiting has already finished. In the normal case that is a wakeup
        # sitting in the loop's callback queue, so one turn of the loop
        # settles it; a wakeup that was *dropped* is still there afterwards,
        # because nothing will ever retry it.
        if not self._wakeup_was_dropped(worker):
            return False
        await asyncio.sleep(0)
        return (self._worker is worker and not worker.done() and self._busy
                and self._wakeup_was_dropped(worker))

    @staticmethod
    def _wakeup_was_dropped(worker: asyncio.Task) -> bool:
        """Whether the task is waiting on a future that has already finished.

        Read through ``getattr`` because it is CPython's own bookkeeping. If a
        future runtime stops offering it the scheduler loses this one recovery
        and keeps every other behaviour, which is the right way round.
        """
        waiter = getattr(worker, '_fut_waiter', None)
        return waiter is not None and waiter.done()

    async def _run(self):
        try:
            while not self._queue.empty():
                _, _, callback, future = await self._queue.get()
                if not self._is_worker_of_record():
                    # DP-37. A worker that was abandoned and then woke up
                    # anyway must not take work from its replacement, and must
                    # not touch the shared flags it no longer owns.
                    self._queue.task_done()
                    return
                self._busy = True
                self._active = future
                try:
                    if future.cancelled():
                        continue
                    started = time.perf_counter()
                    value = callback()
                    if inspect.isawaitable(value):
                        # Async callbacks time their own blocking sections; the
                        # awaited span legitimately includes off-loop waits.
                        value = await value
                    elif self.monitor is not None:
                        self.monitor.record('owner_loop_slice',
                                            (time.perf_counter() - started) * 1000.0)
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
                else:
                    if not future.done():
                        future.set_result(value)
                finally:
                    if self._is_worker_of_record():
                        self._busy = False
                        self._active = None
                        self._idle_since = time.monotonic()
                    self._queue.task_done()
        finally:
            # DP-34. Only the worker of record clears the slot. A worker that
            # was replaced while lost must not blank out its replacement if it
            # ever does wake up.
            if self._is_worker_of_record():
                self._worker = None
                self._busy = False
                self._active = None

    def _is_worker_of_record(self) -> bool:
        return self._worker is asyncio.current_task()

    async def close(self):
        self._closed = True
        if self._worker is not None:
            await self._worker
