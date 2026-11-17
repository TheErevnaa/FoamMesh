#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import platform
import shutil
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QRect

logger = logging.getLogger(__name__)

_backgroundTasks = set()

#: Plan 39 VI14b. How long a rename of a directory is retried when Windows
#: says it is in use. A virus scanner or the search indexer keeps a handle on
#: a freshly written file for a moment after the writer closed it; the rename
#: of the folder holding it is refused until the handle is gone.
RENAME_RETRY_SECONDS = 4.0
RENAME_RETRY_FIRST_DELAY = 0.05


def rename_retrying(source, target, *, seconds=None, sleep=time.sleep,
                    clock=time.monotonic):
    """``Path(source).rename(target)``, retried briefly on a sharing refusal.

    Only ``PermissionError`` is retried (``[WinError 5]`` access denied and
    ``[WinError 32]`` sharing violation both are one): every other error, and
    a refusal that outlasts ``seconds``, is raised as it came, so a real
    holder is never hidden behind a pause. The wait grows from 50 ms.
    """
    limit = RENAME_RETRY_SECONDS if seconds is None else seconds
    began = clock()
    delay = RENAME_RETRY_FIRST_DELAY
    while True:
        try:
            return Path(source).rename(target)
        except PermissionError:
            if clock() - began + delay > limit:
                raise
            sleep(delay)
            delay = min(delay * 2, 0.5)


#: The prefix of a folder renamed out of the way and deleted afterwards.
DEFERRED_DELETE_PREFIX = 'delete_me_'
#: Plan 50 CX1 (DP-6202). How often the delete of a renamed folder is tried,
#: and the pause between tries. A killed parallel run has one process per
#: core still letting go of its files on the Windows drive.
DELETE_ATTEMPTS = 3
DELETE_RETRY_SECONDS = 0.5

#: Renamed folders a delete is working on now (resolved path strings): the
#: sweep leaves them to it rather than deleting the same tree twice.
_IN_FLIGHT: set = set()
_IN_FLIGHT_LOCK = threading.Lock()


def on_loop_thread() -> bool:
    """Is an asyncio loop running in this thread (the owner loop, which in
    the window is the GUI thread)? Plan 50 CX1: no long delete or wait there."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _claim(target) -> str | None:
    key = os.path.normcase(str(Path(target).absolute()))
    with _IN_FLIGHT_LOCK:
        if key in _IN_FLIGHT:
            return None
        _IN_FLIGHT.add(key)
    return key


def _release(key) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(key)


def _delete_renamed(target, onerror=None, *, attempts=None, sleep=time.sleep) -> bool:
    """Delete a folder already renamed out of the way; ``True`` once gone.

    Plan 50 CX1 (DP-6202). The background delete used to run once, with
    errors not ignored and nobody waiting on the task: a folder it could not
    delete stayed in the case for good, and the error was never seen. It is
    retried now, and what is still left is logged and swept by
    :func:`sweep_deferred_deletes` at the next clean-up of the same folder.
    """
    target = Path(target)
    key = _claim(target)
    try:
        return _delete_claimed(target, onerror, attempts=attempts, sleep=sleep)
    finally:
        if key is not None:
            _release(key)


def _delete_held(target, onerror, key) -> bool:
    """The background delete of a tree :func:`rmtree` claimed for it."""
    try:
        return _delete_claimed(Path(target), onerror, attempts=None,
                               sleep=time.sleep)
    finally:
        if key is not None:
            _release(key)


def _delete_claimed(target: Path, onerror, *, attempts, sleep) -> bool:
    tries = DELETE_ATTEMPTS if attempts is None else max(1, int(attempts))
    error = None
    for attempt in range(tries):
        try:
            if onerror is not None and attempt == tries - 1:
                shutil.rmtree(target, False, onerror)
            else:
                shutil.rmtree(target)
        except FileNotFoundError:
            return True
        except OSError as caught:
            error = caught
            if attempt + 1 < tries:
                sleep(DELETE_RETRY_SECONDS)
            continue
        if not target.exists():
            return True
    if not target.exists():
        return True
    logger.warning('could not delete %s (%s); it is left for the next clean-up',
                   target, error)
    return False


def sweep_deferred_deletes(directory) -> list[str]:
    """Delete the ``delete_me_*`` folders left in *directory*; never raises.

    Plan 50 CX1 (DP-6202). Returns the names still there afterwards. A
    folder a background delete is working on now is left to it, and on a
    thread running an asyncio loop (the GUI thread) nothing is deleted: the
    sweep is for a worker thread.
    """
    folder = Path(directory)
    try:
        leftovers = [item for item in folder.iterdir()
                     if item.name.startswith(DEFERRED_DELETE_PREFIX)]
    except OSError:
        return []
    if on_loop_thread():
        return [item.name for item in leftovers]
    left = []
    for item in leftovers:
        key = _claim(item)
        if key is None:
            left.append(item.name)      # being deleted right now
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            _release(key)
        if item.exists():
            left.append(item.name)
    return left


def discard_tree(path, *, rename_seconds=None) -> bool:
    """Remove *path* as :func:`rmtree` does, never raising; ``True`` if gone.

    Plan 50 CX1 (DP-6200). When Windows refuses the rename, what can be
    deleted in place is deleted and the rest is left for the caller to name
    and for the next clean-up to remove. On a thread running an asyncio loop
    (the GUI thread) the rename is not waited on and nothing is deleted in
    place: the renamed tree goes to a background delete, as before.
    """
    p = Path(path)
    on_loop = on_loop_thread()
    try:
        rmtree(p, rename_seconds=0 if on_loop else rename_seconds)
    except OSError as error:
        if on_loop:
            logger.warning('could not move %s out of the way (%s); left for '
                           'the next clean-up', p, error)
            return not p.exists()
        logger.warning('could not move %s out of the way (%s); deleting in place',
                       p, error)
        try:
            shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    return not p.exists()


def rmtree(path, ignore_errors=False, onerror=None, *, rename_seconds=None):
    """Delete file tree immediately and safely in the background

    This function renames the path into temporary one and delete the path in the background

    Args:
        path:
        ignore_errors: a refused rename is not raised either
        onerror: passed to the last delete attempt
        rename_seconds: how long a refused rename is retried
            (``RENAME_RETRY_SECONDS`` by default)

    Returns:

    """
    p = Path(path)
    if not p.exists():
        return

    target = p.parent / (DEFERRED_DELETE_PREFIX + str(uuid.uuid4()))
    try:
        rename_retrying(p, target, seconds=rename_seconds)
    except OSError:
        if ignore_errors:
            return
        raise

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # This function might be called in a thread
        loop = None

    if loop is not None:
        # Claimed now, not when the worker starts: a sweep on another
        # thread in between must not delete the same tree as well.
        coro = asyncio.to_thread(_delete_held, target, onerror, _claim(target))
        task = asyncio.create_task(coro)

        _backgroundTasks.add(task)

        task.add_done_callback(_backgroundTasks.discard)
    else:  # loop is None
        _delete_renamed(target, onerror)


def getFit(window: QRect, display: QRect) -> QRect:
    x, y, width, height = window.getRect()

    if x < display.topLeft().x():
        x = display.topLeft().x()
    if y < display.topLeft().y():
        y = display.topLeft().y()

    # Note that "width" and "height" are scaled by "QT_SCALE_FACTOR"
    if width > display.width():
        width = display.width()
    if height > display.height() - 40:  # "40" for window title bar
        height = display.height() - 40

    if x + width > display.right() + 1:
        x = display.right() + 1 - width
    if y + height > display.bottom() + 1:
        y = display.bottom() + 1 - height

    return QRect(x, y, width, height)


def copyOrLink(source: Path, target: Path):
    if platform.system() == 'Windows':
        shutil.copy(source, target)
    else:
        target.symlink_to(os.path.relpath(source, target.parent))  # "walk_up" option for pathlib.Path.relative_to() is not available in python 3.9
