#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Process lifecycle diagnostics: how the last session ended, and what it did.

DP-551. MEASURED in the 24 September four-case audit, case G5: the export
completed, its modal was on screen, and then the whole window was gone -- PID
87920 exited with nothing in ``gui_stderr.log`` but superqt warnings and
nothing in the case's ``log.txt`` after the step transition that preceded the
export. The case said a little more than the logs did: ``case.lock.info.json``
still named the dead PID and the event journal ended two minutes before the
export, so the session was never closed and never flushed. Whatever ended the
process -- a native fault, a stopped event loop, an outside kill -- left no
trace anywhere, and a crash that leaves no trace cannot be fixed.

So every GUI start now leaves one in the application's own log directory
(``~/.foammesh/logs``), which exists before any case is open and survives
the process:

* ``faulthandler.log`` -- Python's fault handler writes the stacks of every
  thread there on an access violation, abort or other fatal signal. On
  Windows that line names the exception code (``access violation`` is
  0xC0000005), which is the exit code the audit could not capture.
* ``lifecycle.log`` -- one line when a session starts, one for each Qt
  shutdown signal (``lastWindowClosed``, ``aboutToQuit``), one when the event
  loop returns and one from ``atexit``. A session with a start line and no
  exit line did not leave through Python.
* ``session-<pid>.json`` -- the running session's last operations,
  rewritten as they happen. It is removed on a clean exit, so a later start
  that finds one whose process is gone knows that session died, and says so
  in ``lifecycle.log`` with the operations it was last seen doing. One file
  per process, because two windows may be open at once.

DP-550. VTK is told to write its warnings to ``vtk.log`` in the same
directory. Left alone, VTK on Windows opens a ``vtkOutputWindow`` for the
first warning it prints -- MEASURED in G6 image 11, over the Generate step --
and when that warning comes from the one VTK worker thread
(``support.vtk_threads``) the window belongs to a thread that never pumps
messages, which is why Windows painted it ``(Not Responding)``.

Plan 35 CR0 adds, in the same directory:

* ``faulthandler.log`` is the *current* session's only: at start the previous
  one is renamed ``faulthandler.<pid>.log`` after the session its last header
  names (the newest :data:`KEEP_FAULT_LOGS` are kept), and each header names
  the version, the pid and the start time. A second window that still holds
  the file makes the rename fail; that start appends instead.
* ``foammesh.log`` -- the root logger, rotating (:data:`ROOT_LOG_BYTES` x
  :data:`ROOT_LOG_FILES`), attached before the ``QApplication`` exists
  (:func:`attach_root_log`); ``threading.excepthook`` and
  ``sys.unraisablehook`` log there too (:func:`install_hooks`), so an
  exception in a worker thread or a ``__del__`` is no longer lost.
* ``watchdog.log`` -- the GUI-thread stall stacks (``support.watchdog``).
* a dead session's record now carries how it ended, when the crash helper
  (``support.crash_helper``) saw it end: the exit code, the last operation,
  the hang and the dumps. The next start's banner reads it.

This module imports no Qt and no VTK at import time: the facade client, which
imports no Qt, records its operations here.
"""
from __future__ import annotations

import atexit
import collections
import datetime
import faulthandler
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from pathlib import Path

LOG_DIRECTORY_NAME = 'logs'
FAULT_LOG = 'faulthandler.log'
LIFECYCLE_LOG = 'lifecycle.log'
VTK_LOG = 'vtk.log'
SESSION_PATTERN = 'session-{pid}.json'
WATCHDOG_LOG = 'watchdog.log'
ROOT_LOG = 'foammesh.log'
#: The root-log handler's name, shared with the packaged build's runtime hook.
ROOT_LOG_HANDLER = 'foammesh.rootlog'
ROTATED_FAULT_PATTERN = 'faulthandler.{pid}.log'

#: Rotated fault logs kept beside the current one.
KEEP_FAULT_LOGS = 10
#: The root log: this many files of this many bytes.
ROOT_LOG_BYTES = 5 * 1024 * 1024
ROOT_LOG_FILES = 5
ROOT_LOG_FORMAT = ('[%(asctime)s][%(levelname)s][%(name)s][%(threadName)s] '
                   '%(message)s')

_HEADER = re.compile(r'--- session pid=(\d+) ')

#: How many operations the session file keeps. Enough to see what led up to
#: an exit; few enough that rewriting the file on every command costs nothing.
RECENT_OPERATIONS = 20

_lock = threading.Lock()
_state: dict = {
    'directory': None,
    'fault_file': None,
    'recent': collections.deque(maxlen=RECENT_OPERATIONS),
    'installed': False,
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec='milliseconds')


def log_directory() -> Path | None:
    """Where this session's diagnostics are written, once installed."""
    return _state['directory']


def _write_line(text: str) -> None:
    directory = _state['directory']
    if directory is None:
        return
    try:
        with open(directory / LIFECYCLE_LOG, 'a', encoding='utf-8') as log:
            log.write(f'[{_now()}] pid={os.getpid()} {text}\n')
    except OSError:
        pass


def _session_path(directory: Path, pid: int | None = None) -> Path:
    return directory / SESSION_PATTERN.format(pid=pid or os.getpid())


def _write_session() -> None:
    directory = _state['directory']
    if directory is None:
        return
    record = {
        'pid': os.getpid(),
        'started_at': _state.get('started_at'),
        'recent_operations': list(_state['recent']),
    }
    try:
        path = _session_path(directory)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, indent=1), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass


def record(event: str) -> None:
    """A lifecycle event worth a line of its own (window closed, quit, ...)."""
    with _lock:
        _write_line(event)


def note_operation(operation: str) -> None:
    """Remember that ``operation`` was started, for the post-mortem."""
    with _lock:
        _state['recent'].append(f'{_now()} {operation}')
        _write_session()


def recent_operations() -> list[str]:
    with _lock:
        return list(_state['recent'])


def _process_alive(pid) -> bool:
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(int(pid))
    except Exception:                                      # noqa: BLE001
        # Never `os.kill(pid, 0)`: on Windows that terminates the process.
        return False


def _report_dead_sessions(directory: Path) -> list[dict]:
    """Every earlier session whose record outlived its process.

    A record the crash helper updated says how the session ended (``ended``,
    ``exit_code``, ``last_op``, ``hangs``, ``dumps``); every record gains
    ``ended_at`` -- the helper's time, else when the record was last written.
    """
    dead = []
    for path in sorted(directory.glob(SESSION_PATTERN.format(pid='*'))):
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {'pid': None, 'unreadable': str(path.name)}
        pid = previous.get('pid')
        if pid == os.getpid() or _process_alive(pid):
            continue
        if not previous.get('ended_at'):
            try:
                previous['ended_at'] = datetime.datetime.fromtimestamp(
                    path.stat().st_mtime).isoformat(timespec='seconds')
            except OSError:
                previous['ended_at'] = None
        operations = previous.get('recent_operations') or []
        _write_line(
            'the session pid={0} (started {1}) ended without a clean exit{2}; '
            'last operations: {3}; see {4} for a native fault'.format(
                pid, previous.get('started_at'),
                f' ({previous["ended"]})' if previous.get('ended') else '',
                ' | '.join(operations[-5:]) or 'none recorded', FAULT_LOG))
        dead.append(previous)
        try:
            path.unlink()
        except OSError:
            pass
    return dead


def _route_vtk_output(directory: Path) -> None:
    """DP-550. Warnings go to ``vtk.log``, never to a native window."""
    try:
        from vtkmodules.vtkCommonCore import (
            vtkFileOutputWindow, vtkOutputWindow)
    except ImportError:                                    # pragma: no cover
        return
    window = vtkFileOutputWindow()
    window.SetFileName(str(directory / VTK_LOG))
    window.SetAppend(True)
    window.SetFlush(True)
    vtkOutputWindow.SetInstance(window)
    _state['vtk_window'] = window


def _at_exit() -> None:
    with _lock:
        _write_line('process exiting through Python (atexit); last '
                    f'operation: {(_state["recent"] or ["none"])[-1]}')
        directory = _state['directory']
        if directory is not None:
            try:
                _session_path(directory).unlink(missing_ok=True)
            except OSError:
                pass


def _previous_fault_pid(path: Path) -> str | None:
    """The pid in the last session header of an old ``faulthandler.log``."""
    try:
        with open(path, 'rb') as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 256 * 1024))
            tail = log.read().decode('utf-8', 'replace')
    except OSError:
        return None
    found = _HEADER.findall(tail)
    return found[-1] if found else None


def _prune_fault_logs(directory: Path, keep: int = KEEP_FAULT_LOGS) -> None:
    try:
        rotated = sorted(
            directory.glob(ROTATED_FAULT_PATTERN.format(pid='*')),
            key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in rotated[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _rotate_fault_log(directory: Path) -> Path | None:
    """Move the last session's ``faulthandler.log`` aside; the new path.

    ``None`` when there was nothing to rotate or another running window still
    holds the file (Windows refuses the rename); that start appends.
    """
    current = directory / FAULT_LOG
    try:
        if current.stat().st_size == 0:
            return None
    except OSError:
        return None
    pid = _previous_fault_pid(current) or 'unknown'
    target = directory / ROTATED_FAULT_PATTERN.format(pid=pid)
    if target.exists():
        target = directory / ROTATED_FAULT_PATTERN.format(
            pid=f'{pid}-{int(time.time())}')
    try:
        os.replace(current, target)
    except OSError:
        return None
    _prune_fault_logs(directory)
    return target


def fault_file():
    """The open ``faulthandler.log`` (for a stack dump before a qFatal)."""
    return _state['fault_file']


def watchdog_log_path() -> Path | None:
    directory = _state['directory']
    return None if directory is None else directory / WATCHDOG_LOG


def install(settings_path, version: str | None = None) -> list[dict]:
    """Start recording for this process; return the sessions that died.

    Called once at GUI start, before the first window. Returns the records
    of earlier sessions that never exited cleanly (empty when none did).
    """
    directory = Path(settings_path) / LOG_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    with _lock:
        _state['directory'] = directory
        dead = _report_dead_sessions(directory)
        _state['started_at'] = _now()
        if _state['fault_file'] is None:
            _rotate_fault_log(directory)
            # Kept open for the life of the process: the fault handler writes
            # to the descriptor from a signal context and cannot open files.
            _state['fault_file'] = open(directory / FAULT_LOG, 'a',
                                        encoding='utf-8')
        fault_file = _state['fault_file']
        fault_file.write(f'--- session pid={os.getpid()} started '
                         f'{_state["started_at"]} version={version or "unknown"} '
                         f'python={sys.version.split()[0]} ---\n')
        fault_file.flush()
        _state.setdefault('fault_was_enabled', faulthandler.is_enabled())
        # DP-926: the current thread only. On Windows faulthandler also fires
        # for a *handled* first-chance exception with the error bit set (COM's
        # 0x80010012 in an asyncio.to_thread worker, crash 69336), and an
        # all-thread dump then walks the GUI thread's live frames without the
        # GIL and can itself fault. The crash helper's minidump has every
        # thread; qFatal and the watchdog still dump all threads on purpose.
        faulthandler.enable(file=fault_file, all_threads=False)
        _route_vtk_output(directory)
        _write_line(f'session started: {" ".join(sys.argv)}')
        _write_session()
        if not _state['installed']:
            atexit.register(_at_exit)
            _state['installed'] = True
    return dead


class _RootLogHandler(logging.handlers.RotatingFileHandler):
    """The rotating root log, tolerant of a second window.

    Two FoamMesh windows append to the same ``foammesh.log``; on Windows the
    one that reaches the size limit cannot rename a file the other holds. It
    then keeps appending and tries again a minute later, rather than failing
    every record.
    """

    RETRY_SECONDS = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._retry_at = 0.0

    def shouldRollover(self, record):
        if time.monotonic() < self._retry_at:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + self.RETRY_SECONDS
            if self.stream is None:
                self.stream = self._open()


def attach_root_log(directory=None, level: int = logging.INFO):
    """Send the root logger to ``foammesh.log`` as well; the handler.

    Idempotent. ``directory`` defaults to the installed log directory. The
    packaged build's runtime hook (``packaging/pyi_rth_foammesh_diagnostics``)
    attaches the same file before ``main.py`` is imported; a root handler named
    :data:`ROOT_LOG_HANDLER` is that one, and is reused, never doubled.
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if existing.get_name() == ROOT_LOG_HANDLER:
            return existing
    directory = Path(directory) if directory else _state['directory']
    if directory is None:
        return None
    handler = _state.get('root_handler')
    if handler is not None:
        return handler
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = _RootLogHandler(
            directory / ROOT_LOG, maxBytes=ROOT_LOG_BYTES,
            backupCount=ROOT_LOG_FILES - 1, encoding='utf-8')
    except OSError:
        return None
    handler.set_name(ROOT_LOG_HANDLER)
    handler.setFormatter(logging.Formatter(ROOT_LOG_FORMAT))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    _state['root_handler'] = handler
    return handler


def _thread_exception(arguments) -> None:
    if arguments.exc_type is not SystemExit:
        name = arguments.thread.name if arguments.thread is not None else '?'
        logging.getLogger('foammesh.thread').critical(
            'Uncaught exception in thread %s', name,
            exc_info=(arguments.exc_type, arguments.exc_value,
                      arguments.exc_traceback))
    previous = _state.get('previous_threading_hook')
    if previous is not None:
        previous(arguments)


def _unraisable(unraisable) -> None:
    exception = unraisable.exc_value
    logging.getLogger('foammesh.unraisable').error(
        '%s: %r', unraisable.err_msg or 'Exception ignored in',
        unraisable.object,
        exc_info=(unraisable.exc_type, exception, unraisable.exc_traceback)
        if exception is not None else None)
    previous = _state.get('previous_unraisable_hook')
    if previous is not None:
        previous(unraisable)


def install_hooks() -> None:
    """Log uncaught thread exceptions and unraisable exceptions (idempotent).

    Each hook then hands over to the one it replaced.
    """
    if 'previous_threading_hook' in _state:
        return
    _state['previous_threading_hook'] = threading.excepthook
    _state['previous_unraisable_hook'] = sys.unraisablehook
    threading.excepthook = _thread_exception
    sys.unraisablehook = _unraisable


def uninstall_hooks() -> None:
    if 'previous_threading_hook' not in _state:
        return
    threading.excepthook = _state.pop('previous_threading_hook')
    sys.unraisablehook = _state.pop('previous_unraisable_hook')


def uninstall() -> None:
    """Undo :func:`install` (tests)."""
    uninstall_hooks()
    with _lock:
        handler = _state.pop('root_handler', None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
        if _state['installed']:
            atexit.unregister(_at_exit)
            _state['installed'] = False
        faulthandler.disable()
        if _state.pop('fault_was_enabled', False) and sys.__stderr__:
            faulthandler.enable(file=sys.__stderr__, all_threads=True)
        if _state['fault_file'] is not None:
            _state['fault_file'].close()
            _state['fault_file'] = None
        window = _state.pop('vtk_window', None)
        if window is not None:
            from vtkmodules.vtkCommonCore import vtkOutputWindow
            vtkOutputWindow.SetInstance(None)
        _state['directory'] = None
        _state['recent'].clear()
