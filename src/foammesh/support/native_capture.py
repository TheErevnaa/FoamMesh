#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR0 step 2: native output lands in a file, not on a NUL handle.

The packaged build is ``console=False`` (``packaging/foammesh.spec``), so
``sys.stderr`` is ``None`` and everything written to the standard streams is
lost -- Python's and, more importantly, native code's. Reassigning
``sys.stderr`` catches Python writes only: a DLL writes through a C runtime
file descriptor, or a Win32 handle it looks up with ``GetStdHandle``.

:func:`install` therefore redirects the *operating-system* streams:

* it opens ``<log_dir>/native.<pid>.log``, unbuffered, never rotated while
  open (older files are pruned at the next start, keeping
  :data:`KEEP_NATIVE_LOGS`);
* ``os.dup2``s that descriptor onto fds 1 and 2 of Python's C runtime
  (``ucrtbase``), so ``printf``/``fprintf(stderr)`` from any DLL on the same
  runtime lands in the file;
* calls ``SetStdHandle(STD_OUTPUT_HANDLE / STD_ERROR_HANDLE)`` with the file's
  OS handle, so code that resolves the handle at write time lands there too;
* points ``sys.stdout``/``sys.stderr`` at a write-through text wrapper over
  the same descriptor.

The known gap, written down rather than hidden: a DLL linked against a
different C runtime has its own descriptor table, and a library that cached
the handle before this ran still writes to NUL. VTK's own output goes through
``vtkOutputWindow`` (``vtk.log``, see ``support.lifecycle``) and Qt's through
the message handler, so the gap is third-party C code only.

This module imports no Qt and no VTK. The PyInstaller runtime hook (CR10) and
``foammesh.main`` both call :func:`install`; the second call is a no-op.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

NATIVE_PATTERN = 'native.{pid}.log'
KEEP_NATIVE_LOGS = 10

STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12

_state: dict = {'path': None, 'fd': None}


def default_log_directory() -> Path:
    """``~/.FoamMesh/logs``, the directory ``support.lifecycle`` also uses.

    Computed without the settings store so it is usable before anything else
    has been imported (the runtime hook runs before ``foammesh.app``).
    """
    return Path.home() / '.FoamMesh' / 'logs'


def installed_path() -> Path | None:
    """The native log this process writes to, once installed."""
    return _state['path']


def _prune(directory: Path, keep: int = KEEP_NATIVE_LOGS) -> None:
    """Keep the newest ``keep`` native logs; a file still open is skipped."""
    try:
        logs = sorted(directory.glob(NATIVE_PATTERN.format(pid='*')),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in logs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _set_std_handles(fd: int) -> None:
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.SetStdHandle.argtypes = (ctypes.c_uint32, ctypes.c_void_p)
        for which in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
            kernel32.SetStdHandle(ctypes.c_uint32(which & 0xFFFFFFFF), handle)
    except Exception:                                      # noqa: BLE001
        pass


def _redirect_legacy_crt(fd: int) -> None:
    """Give ``msvcrt.dll``'s own fds 1/2 the file too.

    ``msvcrt.dll`` (the pre-UCRT runtime some third-party DLLs still link) has
    its own descriptor table, initialised from the standard handles when it
    loaded -- usually before this ran -- so ``dup2`` on Python's runtime never
    reaches it. Its ``_open_osfhandle``/``_dup2`` do. Other runtimes (a static
    CRT inside a DLL) remain the documented gap.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.DuplicateHandle.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_int,
            ctypes.c_uint32)
        legacy = ctypes.CDLL('msvcrt')
        legacy._open_osfhandle.argtypes = (ctypes.c_void_p, ctypes.c_int)
        process = kernel32.GetCurrentProcess()
        for target in (1, 2):
            duplicate = ctypes.c_void_p()
            if not kernel32.DuplicateHandle(
                    process, msvcrt.get_osfhandle(fd), process,
                    ctypes.byref(duplicate), 0, False, 2):  # SAME_ACCESS
                continue
            legacy_fd = legacy._open_osfhandle(duplicate, 0)
            if legacy_fd >= 0 and legacy_fd != target:
                legacy._dup2(legacy_fd, target)
                legacy._close(legacy_fd)
    except Exception:                                      # noqa: BLE001
        pass


def install(log_dir: Path | None = None) -> None:
    """Send fds 1/2, the Win32 standard handles and ``sys.std*`` to a file.

    Idempotent, safe before Qt or VTK is imported, and never raises: a failure
    costs evidence, never a launch.
    """
    if _state['fd'] is not None:
        return
    try:
        directory = Path(log_dir) if log_dir else default_log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        _prune(directory, KEEP_NATIVE_LOGS - 1)
        path = directory / NATIVE_PATTERN.format(pid=os.getpid())
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(
            os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)
        fd = os.open(path, flags, 0o644)
    except OSError:
        return
    os.write(fd, f'--- native output of pid={os.getpid()} ---\n'.encode())
    for target in (1, 2):
        try:
            os.dup2(fd, target)
        except OSError:
            pass
    _set_std_handles(fd)
    _redirect_legacy_crt(fd)
    _state['path'] = path
    _state['fd'] = fd
    for name, target in (('stdout', 1), ('stderr', 2)):
        try:
            stream = io.TextIOWrapper(
                io.FileIO(target, 'w', closefd=False), encoding='utf-8',
                errors='backslashreplace', write_through=True)
            setattr(sys, name, stream)
        except (OSError, ValueError):
            pass
