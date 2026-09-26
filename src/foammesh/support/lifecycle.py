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

This module imports no Qt and no VTK at import time: the facade client, which
imports no Qt, records its operations here.
"""
from __future__ import annotations

import atexit
import collections
import datetime
import faulthandler
import json
import os
import sys
import threading
from pathlib import Path

LOG_DIRECTORY_NAME = 'logs'
FAULT_LOG = 'faulthandler.log'
LIFECYCLE_LOG = 'lifecycle.log'
VTK_LOG = 'vtk.log'
SESSION_PATTERN = 'session-{pid}.json'

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
    """Every earlier session whose record outlived its process."""
    dead = []
    for path in sorted(directory.glob(SESSION_PATTERN.format(pid='*'))):
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {'pid': None, 'unreadable': str(path.name)}
        pid = previous.get('pid')
        if pid == os.getpid() or _process_alive(pid):
            continue
        operations = previous.get('recent_operations') or []
        _write_line(
            'the session pid={0} (started {1}) ended without a clean exit; '
            'last operations: {2}; see {3} for a native fault'.format(
                pid, previous.get('started_at'),
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


def install(settings_path) -> list[dict]:
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
            # Kept open for the life of the process: the fault handler writes
            # to the descriptor from a signal context and cannot open files.
            _state['fault_file'] = open(directory / FAULT_LOG, 'a',
                                        encoding='utf-8')
        fault_file = _state['fault_file']
        fault_file.write(f'--- session pid={os.getpid()} started '
                         f'{_state["started_at"]} ---\n')
        fault_file.flush()
        _state.setdefault('fault_was_enabled', faulthandler.is_enabled())
        faulthandler.enable(file=fault_file, all_threads=True)
        _route_vtk_output(directory)
        _write_line(f'session started: {" ".join(sys.argv)}')
        _write_session()
        if not _state['installed']:
            atexit.register(_at_exit)
            _state['installed'] = True
    return dead


def uninstall() -> None:
    """Undo :func:`install` (tests)."""
    with _lock:
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
