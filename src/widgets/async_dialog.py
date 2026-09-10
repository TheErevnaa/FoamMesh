#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Await a dialog without a nested blocking event loop.

``dialog.exec()`` spins a nested modal loop that blocks the shared qasync
owner loop, stalling facade commands and the desktop API. ``asyncExec`` shows
the dialog window-modally with ``open()`` and resolves on ``finished``, so the
owner loop keeps serving events while the user decides.
"""

import asyncio

_openDialogs = set()  # keep awaited dialogs alive until they finish


async def asyncExec(dialog) -> int:
    """Show *dialog* non-blockingly and return its result code."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _openDialogs.add(dialog)

    def _finished(result: int):
        _openDialogs.discard(dialog)
        if not future.done():
            future.set_result(result)

    dialog.finished.connect(_finished)
    dialog.open()
    return await future
