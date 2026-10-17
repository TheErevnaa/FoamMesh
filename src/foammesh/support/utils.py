#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import os
import platform
import shutil
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QRect

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


def rmtree(path, ignore_errors=False, onerror=None):
    """Delete file tree immediately and safely in the background

    This function renames the path into temporary one and delete the path in the background

    Args:
        path:
        ignore_errors:
        onerror:

    Returns:

    """
    p = Path(path)
    if not p.exists():
        return

    target = p.parent / ('delete_me_' + str(uuid.uuid4()))
    rename_retrying(p, target)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # This function might be called in a thread
        loop = None

    if loop is not None:
        coro = asyncio.to_thread(shutil.rmtree, target, ignore_errors, onerror)
        task = asyncio.create_task(coro)

        _backgroundTasks.add(task)

        task.add_done_callback(_backgroundTasks.discard)
    else:  # loop is None
        shutil.rmtree(target, ignore_errors, onerror)


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
