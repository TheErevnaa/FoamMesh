#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Await a dialog without a nested blocking event loop.

``dialog.exec()`` spins a nested modal loop that blocks the shared qasync
owner loop, stalling facade commands and the desktop API. ``asyncExec`` shows
the dialog window-modally with ``open()`` and resolves on ``finished``, so the
owner loop keeps serving events while the user decides.

Plan 50 IP19 (ST-1, DP-6360). A nested loop entered while an asyncio task is
current loses that loop's task wake-ups for good ("Cannot enter into task
... while another task ... is being executed"); ``foammesh.log`` 2026-11-01
shows the WSL health poll dying that way. Every dialog kind the GUI opens
therefore has a non-blocking form here, in two shapes:

* ``await xxxAsync(...)`` for a coroutine, and
* ``xxx(..., then=callback)`` for a plain slot: the dialog opens and the slot
  returns; ``callback`` gets what the static call would have returned once
  the user answers.

The answer is the same value the matching Qt static returns: ``(path,
filter)`` for a file, ``path`` for a directory, ``(value, ok)`` for an input,
a ``QColor`` (invalid when cancelled) for a colour.

The static call stays the seam a test answers the dialog through, the way
``async_message_box.decide`` did: when ``QFileDialog.getOpenFileName`` (or a
sibling) is not the one Qt ships, it is asked instead and the answer is
delivered at once.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QDialog, QFileDialog, QInputDialog, QLineEdit,
                               QMenu)

logger = logging.getLogger(__name__)

_openDialogs = set()  # keep awaited dialogs alive until they finish

#: ``QDialog.exec`` as Qt ships it (every Qt dialog class shares it).
_SHIPPED_EXEC = QDialog.exec


def _answeredByTest(dialog) -> bool:
    """True when a test answers *dialog* through a patched ``exec``.

    The same seam as the static calls below: an ``exec`` that is not the one
    Qt ships is asked instead, and its answer is delivered at once.
    """
    if not isinstance(dialog, QDialog):
        # A stand-in that is no Qt dialog cannot be shown; it is asked.
        return callable(getattr(dialog, 'exec', None))
    return _execPatched(dialog, _SHIPPED_EXEC)


def _execPatched(widget, shipped) -> bool:
    """True when ``exec`` on *widget* (or its class) is not *shipped*."""
    if 'exec' in getattr(widget, '__dict__', {}):
        return True
    return type(widget).exec is not shipped


def openLater(future, opener) -> None:
    """Run *opener* on the next loop turn, outside the awaiting task.

    The ``await`` forms open their window from a plain loop callback, not
    from inside the task that awaits it -- the same place the window's
    answer comes from. A test's stand-in for the dialog then also runs with
    no task current, as the real non-blocking window does; an opener that
    raises fails the awaited future instead of leaving it pending.
    """
    def run():
        try:
            opener()
        except BaseException as error:      # noqa: BLE001 - handed to the awaiter
            if not future.done():
                future.set_exception(error)

    asyncio.get_running_loop().call_soon(run)


async def asyncExec(dialog) -> int:
    """Show *dialog* non-blockingly and return its result code."""
    if _answeredByTest(dialog):
        future = asyncio.get_running_loop().create_future()

        def answer():
            result = dialog.exec()
            if not future.done():
                future.set_result(result)

        openLater(future, answer)
        return await future
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


def openDialog(dialog, then: Callable[[int], Any] | None = None, *, modality=None):
    """Show *dialog* without a nested loop; ``then(result)`` once it finishes.

    *modality* None opens it window-modal (``open()``); a
    ``Qt.WindowModality`` shows it with that modality instead (an
    application-modal ``show()`` blocks input to every window, as ``exec()``
    did, without a nested loop).
    """
    if _answeredByTest(dialog):
        result = dialog.exec()
        if then is not None:
            _deliver(then, result)
        return dialog
    _openDialogs.add(dialog)

    def _finished(result: int):
        _openDialogs.discard(dialog)
        if then is not None:
            _deliver(then, result)

    dialog.finished.connect(_finished)
    if modality is None:
        dialog.open()
    else:
        dialog.setWindowModality(modality)
        dialog.show()
    return dialog


def popupMenu(menu, position, then: Callable[[Any], Any] | None = None):
    """Show *menu* at *position* without a nested loop.

    ``menu.exec(position)`` spins one. Its actions' own ``triggered`` slots
    still run; ``then(action)`` gets the chosen action, or None when the menu
    closes without one.

    A test that replaced the menu's ``exec`` answers through it at once.
    """
    if _execPatched(menu, QMenu.exec):
        action = menu.exec(position)
        if then is not None:
            _deliver(then, action)
        return menu
    _openDialogs.add(menu)
    chosen = []

    def _triggered(action):
        chosen.append(action)

    def _hidden():
        try:
            menu.triggered.disconnect(_triggered)
            menu.aboutToHide.disconnect(_hidden)
        except (RuntimeError, TypeError):
            pass
        _openDialogs.discard(menu)
        if then is not None:
            # ``aboutToHide`` comes before ``triggered``: answer on the next turn.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: _deliver(then, chosen[0] if chosen else None))

    menu.triggered.connect(_triggered)
    menu.aboutToHide.connect(_hidden)
    menu.popup(position)
    return menu


def _deliver(then, *args):
    try:
        then(*args)
    except Exception:                                       # noqa: BLE001
        logger.exception('a dialog answer handler failed')
        raise


def _awaitable(opener, *args, **kwargs):
    """Run a ``then=`` opener and await its answer."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def answer(*values):
        if not future.done():
            future.set_result(values[0] if len(values) == 1 else values)

    openLater(future, lambda: opener(*args, then=answer, **kwargs))
    return future


# --------------------------------------------------------------------------- #
# File dialogs
# --------------------------------------------------------------------------- #

_FILE_STATICS = {name: getattr(QFileDialog, name) for name in (
    'getOpenFileName', 'getOpenFileNames', 'getSaveFileName', 'getExistingDirectory')}


def _patched(owner, name, shipped):
    current = getattr(owner, name)
    return None if current is shipped.get(name) else current


def _fileDialog(parent, caption, directory, filter, options):
    dialog = QFileDialog(parent, caption, directory or '', filter or '')
    if options is not None:
        dialog.setOptions(options)
    if platform.system() == 'Darwin':
        # ``show()`` for a native file dialog does not work on macOS.
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
    return dialog


def _openFile(name, parent, caption, directory, filter, options, selectedFilter, then,
              mode, accept):
    patched = _patched(QFileDialog, name, _FILE_STATICS)
    if patched is not None:
        args = [parent, caption, directory]
        if name != 'getExistingDirectory':
            args.append(filter)
            answer = patched(*args) if options is None else patched(*args, options=options)
        else:
            answer = patched(*args) if options is None else patched(*args, options)
        if isinstance(answer, tuple):
            _deliver(then, *answer)
        else:
            _deliver(then, answer)
        return None
    if options is None and mode == QFileDialog.FileMode.Directory:
        options = QFileDialog.Option.ShowDirsOnly
    dialog = _fileDialog(parent, caption, directory, filter, options)
    dialog.setFileMode(mode)
    dialog.setAcceptMode(accept)
    if selectedFilter:
        dialog.selectNameFilter(selectedFilter)

    def finished(result):
        files = dialog.selectedFiles() if result else []
        if name == 'getExistingDirectory':
            then(files[0] if files else '')
        elif name == 'getOpenFileNames':
            then(files, dialog.selectedNameFilter() if files else '')
        else:
            then(files[0] if files else '', dialog.selectedNameFilter() if files else '')
        dialog.deleteLater()

    return openDialog(dialog, finished, modality=Qt.WindowModality.ApplicationModal)


def getOpenFileName(parent=None, caption='', dir='', filter='', *, then,
                    options=None, selectedFilter=''):
    """``QFileDialog.getOpenFileName`` without a nested loop; ``then(path, filter)``."""
    return _openFile('getOpenFileName', parent, caption, dir, filter, options,
                     selectedFilter, then, QFileDialog.FileMode.ExistingFile,
                     QFileDialog.AcceptMode.AcceptOpen)


def getOpenFileNames(parent=None, caption='', dir='', filter='', *, then,
                     options=None, selectedFilter=''):
    """``QFileDialog.getOpenFileNames``; ``then(paths, filter)``."""
    return _openFile('getOpenFileNames', parent, caption, dir, filter, options,
                     selectedFilter, then, QFileDialog.FileMode.ExistingFiles,
                     QFileDialog.AcceptMode.AcceptOpen)


def getSaveFileName(parent=None, caption='', dir='', filter='', *, then,
                    options=None, selectedFilter=''):
    """``QFileDialog.getSaveFileName``; ``then(path, filter)``."""
    return _openFile('getSaveFileName', parent, caption, dir, filter, options,
                     selectedFilter, then, QFileDialog.FileMode.AnyFile,
                     QFileDialog.AcceptMode.AcceptSave)


def getExistingDirectory(parent=None, caption='', dir='', options=None, *, then):
    """``QFileDialog.getExistingDirectory``; ``then(path)``."""
    return _openFile('getExistingDirectory', parent, caption, dir, '', options, '',
                     then, QFileDialog.FileMode.Directory,
                     QFileDialog.AcceptMode.AcceptOpen)


def getOpenFileNameAsync(*args, **kwargs):
    return _awaitable(getOpenFileName, *args, **kwargs)


def getOpenFileNamesAsync(*args, **kwargs):
    return _awaitable(getOpenFileNames, *args, **kwargs)


def getSaveFileNameAsync(*args, **kwargs):
    return _awaitable(getSaveFileName, *args, **kwargs)


def getExistingDirectoryAsync(*args, **kwargs):
    return _awaitable(getExistingDirectory, *args, **kwargs)


# --------------------------------------------------------------------------- #
# Input dialogs
# --------------------------------------------------------------------------- #

_INPUT_STATICS = {name: getattr(QInputDialog, name) for name in (
    'getText', 'getInt', 'getDouble', 'getItem')}


def _inputDialog(parent, title, label):
    dialog = QInputDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    return dialog


def _openInput(dialog, then, read):
    def finished(result):
        then(read() if result else _empty(dialog), bool(result))
        dialog.deleteLater()
    return openDialog(dialog, finished, modality=Qt.WindowModality.ApplicationModal)


def _empty(dialog):
    mode = dialog.inputMode()
    if mode == QInputDialog.InputMode.IntInput:
        return dialog.intValue()
    if mode == QInputDialog.InputMode.DoubleInput:
        return dialog.doubleValue()
    return ''


def _askStandIn(standIn, leading, **optional):
    """Call a test's stand-in for a static the way the app used to.

    The app passed the optional arguments by keyword (``text=...``); a
    stand-in written ``lambda *args: ...`` takes them by position instead.
    """
    import inspect
    try:
        parameters = inspect.signature(standIn).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    named = {p.name for p in parameters
             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    takesAny = any(p.kind == p.VAR_KEYWORD for p in parameters)
    if takesAny or named.issuperset(optional):
        return standIn(*leading, **optional)
    keywords = {k: v for k, v in optional.items() if k in named}
    if keywords:
        return standIn(*leading, **keywords)
    return standIn(*leading, *optional.values())


def getText(parent, title, label, echo=QLineEdit.EchoMode.Normal, text='', *, then,
            inputMethodHints=None):
    """``QInputDialog.getText``; ``then(text, ok)``."""
    patched = _patched(QInputDialog, 'getText', _INPUT_STATICS)
    if patched is not None:
        _deliver(then, *_askStandIn(patched, (parent, title, label),
                                    echo=echo, text=text))
        return None
    dialog = _inputDialog(parent, title, label)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    dialog.setTextEchoMode(echo)
    dialog.setTextValue(text)
    if inputMethodHints is not None:
        dialog.setInputMethodHints(inputMethodHints)
    return _openInput(dialog, then, dialog.textValue)


def getInt(parent, title, label, value=0, minValue=-2147483647, maxValue=2147483647,
           step=1, *, then):
    """``QInputDialog.getInt``; ``then(value, ok)``."""
    patched = _patched(QInputDialog, 'getInt', _INPUT_STATICS)
    if patched is not None:
        _deliver(then, *patched(parent, title, label, value, minValue, maxValue, step))
        return None
    dialog = _inputDialog(parent, title, label)
    dialog.setInputMode(QInputDialog.InputMode.IntInput)
    dialog.setIntRange(minValue, maxValue)
    dialog.setIntStep(step)
    dialog.setIntValue(value)
    return _openInput(dialog, then, dialog.intValue)


def getDouble(parent, title, label, value=0.0, minValue=-2147483647.0,
              maxValue=2147483647.0, decimals=1, *, then, step=1.0):
    """``QInputDialog.getDouble``; ``then(value, ok)``."""
    patched = _patched(QInputDialog, 'getDouble', _INPUT_STATICS)
    if patched is not None:
        _deliver(then, *patched(parent, title, label, value, minValue, maxValue, decimals))
        return None
    dialog = _inputDialog(parent, title, label)
    dialog.setInputMode(QInputDialog.InputMode.DoubleInput)
    dialog.setDoubleRange(minValue, maxValue)
    dialog.setDoubleDecimals(decimals)
    dialog.setDoubleStep(step)
    dialog.setDoubleValue(value)
    return _openInput(dialog, then, dialog.doubleValue)


def getItem(parent, title, label, items, current=0, editable=True, *, then):
    """``QInputDialog.getItem``; ``then(item, ok)``."""
    patched = _patched(QInputDialog, 'getItem', _INPUT_STATICS)
    if patched is not None:
        _deliver(then, *patched(parent, title, label, items, current, editable))
        return None
    items = list(items)
    dialog = _inputDialog(parent, title, label)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    dialog.setComboBoxItems(items)
    dialog.setComboBoxEditable(editable)
    if 0 <= current < len(items):
        dialog.setTextValue(items[current])

    def finished(result):
        # Cancelled answers the current item, as Qt's static does.
        fallback = items[current] if 0 <= current < len(items) else ''
        then(dialog.textValue() if result else fallback, bool(result))
        dialog.deleteLater()

    return openDialog(dialog, finished, modality=Qt.WindowModality.ApplicationModal)


def getItemAsync(*args, **kwargs):
    return _awaitable(getItem, *args, **kwargs)


def getTextAsync(*args, **kwargs):
    return _awaitable(getText, *args, **kwargs)


def getIntAsync(*args, **kwargs):
    return _awaitable(getInt, *args, **kwargs)


def getDoubleAsync(*args, **kwargs):
    return _awaitable(getDouble, *args, **kwargs)


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #

_COLOR_STATIC = {'getColor': QColorDialog.getColor}


def getColor(initial=None, parent=None, title='', options=None, *, then):
    """``QColorDialog.getColor``; ``then(colour)``, invalid when cancelled."""
    initial = QColor(Qt.GlobalColor.white) if initial is None else QColor(initial)
    patched = _patched(QColorDialog, 'getColor', _COLOR_STATIC)
    if patched is not None:
        # Positional, as the app called the static: a test's stand-in is
        # often ``lambda *args: colour``.
        args = [initial, parent]
        if title or options is not None:
            args.append(title)
        if options is not None:
            args.append(options)
        _deliver(then, patched(*args))
        return None
    dialog = QColorDialog(initial, parent)
    if title:
        dialog.setWindowTitle(title)
    if options is not None:
        dialog.setOptions(options)

    def finished(result):
        then(dialog.selectedColor() if result else QColor())
        dialog.deleteLater()

    return openDialog(dialog, finished, modality=Qt.WindowModality.ApplicationModal)


def getColorAsync(*args, **kwargs):
    return _awaitable(getColor, *args, **kwargs)
