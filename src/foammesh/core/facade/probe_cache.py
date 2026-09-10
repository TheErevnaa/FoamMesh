"""Answers from foreign runtimes, kept off the caller's thread (Plan 30 WP-08).

A runtime probe is a read that boots something: ``wsl -- which gmsh`` starts a
distribution, ``foamVersion`` sources a profile. Measured cold, that is about
thirty seconds. Two things follow, and this module is both of them.

The work runs on a worker thread, so the event loop -- and therefore every
dialog waiting to be drawn -- is never inside it. And the answer is kept for a
while, so the second dialog does not pay for the first one's discovery. A
caller that cannot wait names a deadline; the deadline ends its *wait*, not the
probe, so the next caller finds the same probe still running rather than
starting a second one.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeAnswer:
    """What a probe had to say, and whether it had said it yet."""

    value: object = None
    ready: bool = False
    cached: bool = False


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


class ProbeCache:
    """Time-limited answers keyed by whatever makes two probes different.

    The key is the caller's business: an engine probe now answers differently
    per target solver (Gmsh needs ``checkMesh`` only when OpenFOAM is the
    target), so it keys by ``(engine_id, target_solver)`` and a target switch
    does not read a stale verdict back (F-40).

    Mapping-shaped on purpose: ``clear`` and ``pop`` keep working for the
    invalidation call sites that already exist elsewhere.
    """

    def __init__(self, ttl_seconds: float = 300.0, *, clock=time.monotonic):
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._entries: dict = {}
        self._inflight: dict = {}

    # -- mapping-ish surface ---------------------------------------------- #

    def get(self, key, default=None):
        """The stored answer, or ``default`` once it is older than the TTL."""
        entry = self._entries.get(key)
        if entry is None:
            return default
        stored_at, value = entry
        if self._ttl >= 0 and self._clock() - stored_at > self._ttl:
            self._entries.pop(key, None)
            return default
        return value

    def store(self, key, value) -> None:
        self._entries[key] = (self._clock(), value)

    __setitem__ = store

    def pop(self, key, default=None):
        entry = self._entries.pop(key, None)
        return default if entry is None else entry[1]

    def clear(self) -> None:
        """Forget every answer. In-flight probes keep running and re-fill."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    # -- the part that matters -------------------------------------------- #

    async def answer(self, key, factory, *, timeout=None,
                     refresh: bool = False) -> ProbeAnswer:
        """Run ``factory`` off the event loop, at most once per key at a time.

        Returns immediately from cache when there is a fresh answer. Otherwise
        one worker thread runs the probe and every caller waits on that same
        task, so a second dialog opening mid-probe costs a wait, not a second
        boot of the runtime.
        """
        if refresh:
            self.pop(key, None)
        else:
            cached = self.get(key, _MISSING)
            if cached is not _MISSING:
                return ProbeAnswer(cached, ready=True, cached=True)

        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.ensure_future(asyncio.to_thread(factory))
            self._inflight[key] = task

            def _remember(finished, key=key):
                if self._inflight.get(key) is finished:
                    self._inflight.pop(key, None)
                if finished.cancelled() or finished.exception() is not None:
                    return
                self.store(key, finished.result())

            task.add_done_callback(_remember)

        try:
            if timeout is not None and timeout > 0:
                value = await asyncio.wait_for(asyncio.shield(task), timeout)
            else:
                value = await asyncio.shield(task)
        except asyncio.TimeoutError:
            return ProbeAnswer(None, ready=False, cached=False)
        return ProbeAnswer(value, ready=True, cached=False)

    def in_flight(self, key) -> bool:
        task = self._inflight.get(key)
        return task is not None and not task.done()
