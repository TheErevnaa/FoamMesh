#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import functools
import contextvars
import weakref
from concurrent.futures import ThreadPoolExecutor


_pool = ThreadPoolExecutor(1)  # To make only one thread running for VTK

# Keyed by the loop itself, not by ``id(loop)``.  DP-347: CPython reuses the
# address of a collected loop almost immediately -- three hundred loops made
# and closed in a row produced six distinct ids -- so an id key hands a fresh
# loop the lock built for a dead one, and that lock can still be held.
_vtk_locks: 'weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]' = (
    weakref.WeakKeyDictionary())


# Copied from asyncio.to_thread
async def _to_vtk_thread(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(_pool, func_call)


_holdRendering = False

# Bumped every time the hold is taken, so a caller can tell its own hold from
# a later one.  DP-347: the ``finally`` of a call abandoned by a closed loop
# runs whenever that coroutine is finally collected, which can be long after
# another loop has legitimately taken the hold.
_holdGeneration = 0


def holdRendering():
    """Holds VTK rendering while VTK is working in background

    Returns:
        int: the generation of this hold, for the caller to release

    Raises:
        AssertionError: Rendering is already on hold
    """
    global _holdRendering
    global _holdGeneration

    if _holdRendering:
        raise AssertionError

    _holdRendering = True
    _holdGeneration += 1

    return _holdGeneration


def resumeRendering():
    """Resume VTK rendering

    Raises:
        AssertionError: Rendering is not hold
    """
    global _holdRendering

    if not _holdRendering:
        raise AssertionError

    _holdRendering = False


def isRenderingHold():
    return _holdRendering


def _forgetClosedLoops():
    """Drop the lock of any loop that has been closed.

    Weak keys alone are not enough.  A loop closed with a call still in flight
    is kept alive by that call's own pending task, so its entry -- and its
    still-held lock -- outlive it for as long as the task is referenced.  The
    loop being closed is the fact that matters, and it is readable directly.
    """
    for loop in [loop for loop in list(_vtk_locks) if loop.is_closed()]:
        _vtk_locks.pop(loop, None)


def _dropStrandedHold(mine):
    """Clear a hold that no live loop owns any more.

    DP-347.  ``_holdRendering`` is process-wide and is set between the lock
    being taken and released.  A loop closed while a call was in flight never
    runs that call's ``finally``, so the flag stays set for the life of the
    process -- and ``rendering_widget`` reads it to decide whether to render,
    so the viewport stops updating and nothing says why.  Once the closed
    loops are forgotten, "no lock but mine is held" is a usable reading of
    "no live call owns the hold".
    """
    global _holdRendering

    if not _holdRendering:
        return
    if any(lock.locked() for lock in list(_vtk_locks.values())
           if lock is not mine):
        return

    _holdRendering = False


async def vtk_run_in_thread(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    # Locks are loop-bound.  Keeping one per live loop makes the helper safe in
    # the desktop loop and in isolated pytest/qasync loops.
    _forgetClosedLoops()
    lock = _vtk_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        _dropStrandedHold(lock)
        generation = holdRendering()
        try:
            return await _to_vtk_thread(func, *args, **kwargs)
        finally:
            # Only release the hold this call took.  A call whose loop closed
            # under it reaches here when its coroutine is collected, by which
            # time the hold may belong to somebody else.
            if _holdRendering and _holdGeneration == generation:
                resumeRendering()
