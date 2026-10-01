"""The case mutation lease (Plan 37 UF5, DP-1039).

Plan 35 left one durable record per *run* (:mod:`.run_records`) and a job
manager, but nothing case-wide: a check could read ``constant/polyMesh``
while a mesher rewrote it, and nothing stopped an unlock from moving files
under a running worker. This module is that case-wide lock.

It is a readers/writer lease, one per case directory:

* ``exclusive`` -- a mesher run, a mesh-writing operation, unlock and undo.
  Nothing else may hold the case at the same time.
* ``shared`` -- checks and worker jobs that read the mesh. Any number may
  hold the case together; none may while an exclusive holder does.

Holders are kept in memory (a registry keyed by the resolved case path) and
mirrored to ``<case>/foammesh/lease.json`` so another FoamMesh process (the
CLI, a second GUI) sees them, and so a crash leaves evidence behind. A holder
whose process is dead is reclaimed on the next acquisition and reported;
whatever it half wrote is Plan 35's run records' business (the recovery gate
restores it before any new run starts).

The lease is re-entrant inside one logical flow: an asyncio task (or thread
started through ``asyncio.to_thread``) that already holds the case may take it
again -- a pipeline holding it exclusively runs its stages, and a shared
holder may upgrade to exclusive when it is the only holder.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .process_control import is_running

logger = logging.getLogger(__name__)

EXCLUSIVE = 'exclusive'
SHARED = 'shared'
MODES = (EXCLUSIVE, SHARED)

LEASE_FILE = ('foammesh', 'lease.json')
#: A sidecar ``.lock`` older than this belongs to a process that died while
#: rewriting ``lease.json`` (the rewrite itself takes milliseconds).
STALE_GUARD_SECONDS = 10.0
GUARD_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.05
#: How long a mutation waits for readers (a running check) before it is
#: refused with :class:`CaseBusyError`.
DEFAULT_WAIT_SECONDS = 120.0


class CaseBusyError(RuntimeError):
    """The case is held by another operation that conflicts with this one."""

    code = 'case_busy'

    def __init__(self, message: str, *, holders=(), case_path=None):
        super().__init__(message)
        self.holders = [dict(item) for item in holders]
        self.case_path = str(case_path) if case_path is not None else None

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': str(self),
                'holders': list(self.holders), 'case_path': self.case_path}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _key(case_path) -> str:
    return os.path.normcase(str(Path(case_path).resolve()))


def lease_path(case_path) -> Path:
    return Path(case_path).joinpath(*LEASE_FILE)


@dataclass(frozen=True)
class LeaseHolder:
    token: str
    mode: str
    operation: str
    pid: int
    host: str
    acquired_at: str
    run_id: str | None = None

    def to_dict(self) -> dict:
        return {'token': self.token, 'mode': self.mode, 'operation': self.operation,
                'pid': self.pid, 'host': self.host, 'acquired_at': self.acquired_at,
                'run_id': self.run_id}

    @classmethod
    def from_dict(cls, data: dict) -> 'LeaseHolder | None':
        try:
            mode = str(data['mode'])
            if mode not in MODES:
                return None
            return cls(str(data['token']), mode, str(data.get('operation') or ''),
                       int(data['pid']), str(data.get('host') or ''),
                       str(data.get('acquired_at') or ''), data.get('run_id'))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class LeaseGrant:
    """What one acquisition obtained. ``reentrant`` grants release nothing."""

    case_path: str
    mode: str
    operation: str
    token: str
    reentrant: bool = False
    reclaimed: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {'case_path': self.case_path, 'mode': self.mode,
                'operation': self.operation, 'token': self.token,
                'reentrant': self.reentrant, 'reclaimed': list(self.reclaimed)}


# The in-process registry: case key -> {token: LeaseHolder}.
_REGISTRY: dict[str, dict[str, LeaseHolder]] = {}
_REGISTRY_LOCK = threading.Lock()
# Tokens this logical flow holds: tuple of (case key, token, mode).
_HELD: contextvars.ContextVar[tuple] = contextvars.ContextVar('foammesh_case_lease', default=())


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ''


def _alive(holder: LeaseHolder) -> bool:
    if holder.host and holder.host != _host():
        # A holder on another machine (a shared drive) cannot be probed:
        # it is assumed alive rather than reclaimed from under it.
        return True
    return is_running(holder.pid)


@contextlib.contextmanager
def _file_guard(path: Path):
    """Serialise rewrites of ``lease.json`` between processes."""
    guard = path.with_name(path.name + '.lock')
    guard.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + GUARD_WAIT_SECONDS
    handle = None
    while handle is None:
        try:
            handle = os.open(str(guard), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - guard.stat().st_mtime > STALE_GUARD_SECONDS:
                    guard.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise CaseBusyError(
                    f'the case lease file is being rewritten by another process ({guard})')
            time.sleep(0.01)
    try:
        os.write(handle, str(os.getpid()).encode('ascii'))
        yield
    finally:
        os.close(handle)
        with contextlib.suppress(OSError):
            guard.unlink()


def _read_file(path: Path) -> list[LeaseHolder]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning('case lease file %s is unreadable; treating it as empty', path)
        return []
    holders = []
    for item in data.get('holders') or ():
        holder = LeaseHolder.from_dict(item) if isinstance(item, dict) else None
        if holder is not None:
            holders.append(holder)
    return holders


def _write_file(path: Path, holders: list[LeaseHolder]) -> None:
    from .run_records import _write_durably
    _write_durably(path, {'version': 1, 'updated_at': _now(),
                          'holders': [holder.to_dict() for holder in holders]})


def _conflicts(mode: str, others: list[LeaseHolder]) -> list[LeaseHolder]:
    if mode == EXCLUSIVE:
        return list(others)
    return [holder for holder in others if holder.mode == EXCLUSIVE]


def _held_in_flow(key: str) -> list[tuple[str, str]]:
    """This flow's still-registered tokens on *key*: [(token, mode)]."""
    live = _REGISTRY.get(key, {})
    return [(token, mode) for held_key, token, mode in _HELD.get()
            if held_key == key and token in live]


def _describe(holders: list[LeaseHolder]) -> str:
    return ', '.join(f'{holder.operation or "an operation"} ({holder.mode}, pid {holder.pid})'
                     for holder in holders)


def _try_acquire(case_path, mode: str, operation: str, run_id: str | None):
    """One attempt. Returns ``(grant, None)`` or ``(None, conflicting holders)``."""
    if mode not in MODES:
        raise ValueError(f'unknown lease mode {mode!r}')
    key = _key(case_path)
    path = lease_path(case_path)
    with _REGISTRY_LOCK:
        mine = _held_in_flow(key)
        if any(held_mode == EXCLUSIVE for _, held_mode in mine) or (
                mine and mode == SHARED):
            return LeaseGrant(str(case_path), mode, operation, mine[0][0], reentrant=True), None
        own_tokens = {token for token, _ in mine}
        local = _REGISTRY.setdefault(key, {})
        reclaimed: list[dict] = []
        durable = True
        try:
            with _file_guard(path):
                stored = _read_file(path)
                foreign = []
                for holder in stored:
                    if holder.pid == os.getpid() and holder.host in ('', _host()):
                        continue  # this process's holders are the registry's
                    if _alive(holder):
                        foreign.append(holder)
                    else:
                        reclaimed.append(holder.to_dict())
                others = [holder for token, holder in local.items() if token not in own_tokens]
                conflicts = _conflicts(mode, others + foreign)
                if conflicts:
                    if reclaimed:
                        _write_file(path, list(local.values()) + foreign)
                    return None, conflicts
                holder = LeaseHolder(uuid4().hex, mode, operation, os.getpid(), _host(),
                                     _now(), run_id)
                local[holder.token] = holder
                _write_file(path, list(local.values()) + foreign)
        except CaseBusyError:
            raise
        except OSError as error:
            # A read-only or vanished metadata root: the lease still holds
            # inside this process, it just leaves no crash evidence.
            durable = False
            logger.warning('case lease for %s is not durable: %s', case_path, error)
            others = [holder for token, holder in local.items() if token not in own_tokens]
            conflicts = _conflicts(mode, others)
            if conflicts:
                return None, conflicts
            holder = LeaseHolder(uuid4().hex, mode, operation, os.getpid(), _host(),
                                 _now(), run_id)
            local[holder.token] = holder
        if reclaimed:
            logger.warning('reclaimed the case lease from dead holders: %s',
                           _describe([LeaseHolder.from_dict(item) for item in reclaimed]))
        grant = LeaseGrant(str(case_path), mode, operation, holder.token, reclaimed=reclaimed)
        grant.durable = durable  # type: ignore[attr-defined]
        return grant, None


def _release(case_path, grant: LeaseGrant) -> None:
    if grant.reentrant:
        return
    key = _key(case_path)
    path = lease_path(case_path)
    with _REGISTRY_LOCK:
        local = _REGISTRY.get(key, {})
        local.pop(grant.token, None)
        if not local:
            _REGISTRY.pop(key, None)
        try:
            with _file_guard(path):
                stored = [holder for holder in _read_file(path) if holder.token != grant.token]
                if stored or path.exists():
                    _write_file(path, stored)
        except (OSError, CaseBusyError) as error:
            logger.warning('could not release the durable case lease for %s: %s',
                           case_path, error)


def _busy(case_path, mode: str, operation: str, conflicts) -> CaseBusyError:
    return CaseBusyError(
        f'{operation or "This operation"} cannot {"change" if mode == EXCLUSIVE else "read"} '
        f'the case while {_describe(conflicts)} holds it. Wait for it to finish or stop it, '
        'then try again.',
        holders=[holder.to_dict() for holder in conflicts], case_path=case_path)


@contextlib.contextmanager
def hold(case_path, mode: str, operation: str, *, run_id: str | None = None):
    """Acquire now or raise :class:`CaseBusyError`; release on exit."""
    grant, conflicts = _try_acquire(case_path, mode, operation, run_id)
    if grant is None:
        raise _busy(case_path, mode, operation, conflicts)
    token = _HELD.set(_HELD.get() + ((_key(case_path), grant.token, mode),)) \
        if not grant.reentrant else None
    try:
        yield grant
    finally:
        if token is not None:
            _HELD.reset(token)
        _release(case_path, grant)


@contextlib.asynccontextmanager
async def acquire(case_path, mode: str, operation: str, *, run_id: str | None = None,
                  timeout: float | None = DEFAULT_WAIT_SECONDS):
    """Wait (without blocking the loop) for the lease; release on exit.

    ``timeout=0`` refuses at once; ``None`` waits indefinitely.
    """
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        grant, conflicts = await asyncio.to_thread(
            _try_acquire, case_path, mode, operation, run_id)
        if grant is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise _busy(case_path, mode, operation, conflicts)
        await asyncio.sleep(POLL_SECONDS)
    token = _HELD.set(_HELD.get() + ((_key(case_path), grant.token, mode),)) \
        if not grant.reentrant else None
    try:
        yield grant
    finally:
        if token is not None:
            _HELD.reset(token)
        await asyncio.to_thread(_release, case_path, grant)


def holders(case_path, *, include_own_flow: bool = True) -> list[dict]:
    """Every live holder of *case_path*, in this process and in others."""
    key = _key(case_path)
    with _REGISTRY_LOCK:
        own = {token for token, _ in _held_in_flow(key)}
        local = [holder for token, holder in _REGISTRY.get(key, {}).items()
                 if include_own_flow or token not in own]
    foreign = [holder for holder in _read_file(lease_path(case_path))
               if not (holder.pid == os.getpid() and holder.host in ('', _host()))
               and _alive(holder)]
    return [holder.to_dict() for holder in local + foreign]


def reclaim(case_path) -> list[dict]:
    """Drop the durable holders whose process is dead; return them."""
    path = lease_path(case_path)
    if not path.exists():
        return []
    key = _key(case_path)
    with _REGISTRY_LOCK:
        local = _REGISTRY.get(key, {})
        with _file_guard(path):
            kept, dropped = [], []
            for holder in _read_file(path):
                own = holder.pid == os.getpid() and holder.host in ('', _host())
                if own and holder.token not in local:
                    dropped.append(holder.to_dict())
                elif own or _alive(holder):
                    kept.append(holder)
                else:
                    dropped.append(holder.to_dict())
            if dropped:
                _write_file(path, kept)
    return dropped


def held_by_current_flow(case_path, mode: str | None = None) -> bool:
    key = _key(case_path)
    with _REGISTRY_LOCK:
        return any(mode is None or held_mode == mode for _, held_mode in _held_in_flow(key))
