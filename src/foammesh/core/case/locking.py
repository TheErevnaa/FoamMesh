"""Inspectable per-case lock metadata layered over the OS-backed file lock."""
from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


LOCK_INFO_FILE = 'case.lock.info.json'


class CaseLockState(str, Enum):
    UNLOCKED = 'unlocked'
    ACTIVE = 'active'
    STALE = 'stale'
    UNREADABLE = 'unreadable'


@dataclass(frozen=True)
class CaseLockInfo:
    pid: int
    host: str
    started_at: str
    app_version: str

    @classmethod
    def current(cls, app_version: str) -> 'CaseLockInfo':
        return cls(
            pid=os.getpid(), host=socket.gethostname(),
            started_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            app_version=app_version,
        )


@dataclass(frozen=True)
class CaseLockInspection:
    state: CaseLockState
    info: CaseLockInfo | None = None
    reason: str = ''


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        # ``os.kill(pid, 0)`` is not a non-signalling existence probe on
        # Windows.  Use a query-only process handle so inspecting stale case
        # metadata can never interrupt or terminate another process.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied still proves that a protected process exists.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def inspect_case_lock(storage_path: str | Path) -> CaseLockInspection:
    path = Path(storage_path) / LOCK_INFO_FILE
    if not path.exists():
        return CaseLockInspection(CaseLockState.UNLOCKED)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        info = CaseLockInfo(
            pid=int(raw['pid']), host=str(raw['host']),
            started_at=str(raw['started_at']), app_version=str(raw['app_version']))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return CaseLockInspection(CaseLockState.UNREADABLE, reason=str(error))
    local = info.host.casefold() == socket.gethostname().casefold()
    if local and not _pid_alive(info.pid):
        return CaseLockInspection(CaseLockState.STALE, info, 'recorded local process is not running')
    return CaseLockInspection(CaseLockState.ACTIVE, info)


def write_case_lock_info(storage_path: str | Path, info: CaseLockInfo) -> Path:
    destination = Path(storage_path) / LOCK_INFO_FILE
    temporary = destination.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as output:
            json.dump(asdict(info), output, indent=2, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
