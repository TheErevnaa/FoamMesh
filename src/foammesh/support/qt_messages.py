#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR0 step 4: Qt's own warnings reach the log.

Qt reports through ``qDebug``/``qWarning``/``qCritical``/``qFatal``, which by
default go to ``stderr`` -- ``NUL`` in the packaged build. :func:`install`
routes them to the ``qt`` logger (and so to ``foammesh.log``). A ``qFatal``
is about to ``abort()`` the process: before it does, the Python stacks of
every thread are written to ``faulthandler.log`` and the log handlers are
flushed, so the one message that explains the abort is not the one lost.
"""
from __future__ import annotations

import faulthandler
import logging

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from foammesh.support import lifecycle

logger = logging.getLogger('qt')

_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}

_state: dict = {'installed': False, 'previous': None}


def _where(context) -> str:
    try:
        if context is not None and context.file:
            return f' ({context.file}:{context.line}, {context.function})'
    except Exception:                                      # noqa: BLE001
        pass
    return ''


def _flush_handlers() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:                                  # noqa: BLE001
            pass


def handle(mode, context, message) -> None:
    """The installed handler; never raises (Qt calls it from any thread)."""
    try:
        if mode == QtMsgType.QtFatalMsg:
            fault_file = lifecycle.fault_file()
            if fault_file is not None:
                fault_file.write(f'--- qFatal: {message} ---\n')
                fault_file.flush()
                faulthandler.dump_traceback(file=fault_file, all_threads=True)
            logger.critical('qFatal: %s%s', message, _where(context))
            _flush_handlers()
            return
        logger.log(_LEVELS.get(mode, logging.WARNING), '%s%s', message,
                   _where(context))
    except Exception:                                      # noqa: BLE001
        pass


def install() -> None:
    """Route Qt messages to the log (idempotent)."""
    if _state['installed']:
        return
    _state['previous'] = qInstallMessageHandler(handle)
    _state['installed'] = True


def uninstall() -> None:
    if not _state['installed']:
        return
    qInstallMessageHandler(_state['previous'])
    _state['installed'] = False
    _state['previous'] = None
