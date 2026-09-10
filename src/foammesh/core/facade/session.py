"""Persistent case-session owner used by headless and future desktop adapters."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import socket
import sqlite3
import sys
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from foammesh.core.jobs import JobManager
from foammesh.core.project import Event, EventBus, ProjectState

from .errors import (CaseLockedError, EventReplayGapError, IdempotencyConflictError,
                     ReadOnlySessionError, RevisionConflictError)
from .events import build_event
from .field_claims import FieldClaimRegistry
from .instrumentation import OwnerLoopMonitor
from .journal import JOURNAL_FILE_NAME, EventJournal
from .results import RevisionSnapshot
from .scheduler import CommandScheduler


_ACTIVE_WRITERS: set[Path] = set()
LOCK_FILE = 'facade.session.lock'
LOCK_INFO_FILE = 'facade.session.lock.json'
SESSION_METADATA_FILE = 'facade.session.json'
FORCE_LOCK_PROBE_ENV = 'FOAMMESH_FORCE_LOCK_PROBE'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_network_path(path: Path) -> bool:
    text = str(path)
    return text.startswith('\\\\') or text.startswith('//')


def probe_locking(directory: Path) -> tuple[bool, str]:
    """Prove sidecar file-lock and SQLite exclusion semantics on this volume.

    Returns ``(reliable, reason)``.  If exclusion cannot be demonstrated the
    caller must fall back to read-only rather than risking two journals or
    interleaved artifact mutations (§6.1).
    """
    directory = Path(directory)
    try:
        with tempfile.TemporaryDirectory(dir=directory, prefix='.foammesh-lock-probe-') as probe_dir:
            probe_path = Path(probe_dir)
            first = FileLock(probe_path / 'probe.lock')
            first.acquire(timeout=0)
            try:
                second = FileLock(probe_path / 'probe.lock')
                try:
                    second.acquire(timeout=0)
                except Timeout:
                    pass
                else:
                    second.release()
                    return False, 'file lock did not exclude a second holder'
            finally:
                first.release()

            database = probe_path / 'probe.sqlite3'
            writer = sqlite3.connect(database, timeout=0.05)
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE probe (value INTEGER)')
                writer.execute('BEGIN IMMEDIATE')
                contender = sqlite3.connect(database, timeout=0.05)
                try:
                    contender.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError:
                    pass
                else:
                    return False, 'sqlite did not exclude a concurrent immediate transaction'
                finally:
                    contender.close()
                writer.rollback()
            finally:
                writer.close()
    except (OSError, sqlite3.Error) as error:
        return False, f'locking probe failed: {error}'
    return True, 'file lock and sqlite exclusion verified'


@dataclass
class CaseSession:
    case_path: Path
    storage_path: Path
    state: ProjectState
    read_only: bool = False
    session_id: str = ''
    case_id: str = ''

    def __post_init__(self):
        self.case_path = self.case_path.resolve()
        self.storage_path = self.storage_path.resolve()
        self.session_id = self.session_id or str(uuid4())
        self.case_id = self.case_id or str(uuid4())
        self.session_epoch = 1
        self.authored_revision = 0
        self.artifact_sequence = 0
        self.presentation_sequence = 0
        self.event_sequence = 0
        self.monitor = OwnerLoopMonitor()
        self.scheduler = CommandScheduler(monitor=self.monitor)
        self.field_claims = FieldClaimRegistry()
        self.jobs = JobManager(self.state.bus)
        self.journal: EventJournal | None = None
        self.change_sets: list[dict] = []
        self.read_only_reason: str | None = None
        self.presentation = None  # attached PresentationState when a desktop surface exists
        self._configuration_cache: tuple[int, dict] | None = None
        self._events: list[dict] = []
        self._idempotency: dict[str, tuple[str, object]] = {}
        self._closed = False
        self._event_context: ContextVar[tuple[dict | None, str | None, str | None]] = (
            ContextVar(f'foammesh_event_context_{self.session_id}',
                       default=(None, None, None)))
        self._lock: FileLock | None = None
        self._unsubscribers = [self.state.bus.subscribe(event, self._on_project_event) for event in (
            Event.TRANSACTION_APPLIED, Event.UNDONE, Event.REDONE,
            Event.ARTIFACT_GEOMETRY_CHANGED, Event.ARTIFACT_DICTIONARIES_CHANGED,
            Event.ARTIFACT_MESH_CHANGED,
            Event.ARTIFACT_QUALITY_CHANGED, Event.ARTIFACT_RESTORED,
            Event.JOB_STARTED, Event.JOB_OUTPUT, Event.JOB_PROGRESS,
            Event.JOB_CANCEL_REQUESTED, Event.JOB_FINISHED, Event.JOB_FAILED,
            Event.JOB_CANCELLED, Event.OPERATION_STARTED, Event.OPERATION_SUCCEEDED,
            Event.OPERATION_FAILED, Event.OPERATION_RECOVERED,
        )]

    @classmethod
    def open(cls, path: str | Path, *, create: bool = False,
             read_only_on_lock: bool = True) -> 'CaseSession':
        case_path = Path(path).resolve()
        from foammesh.db.configurations import Configurations, FILE_NAME
        sidecar = case_path / 'foammesh'
        if (sidecar / FILE_NAME).is_file():
            storage_path = sidecar
        elif (case_path / FILE_NAME).is_file():
            storage_path = case_path
        else:
            storage_path = sidecar if sidecar.is_dir() else case_path
        if create:
            storage_path.mkdir(parents=True, exist_ok=True)
        # Keep the facade importable in a headless service process. The legacy
        # configuration implementation currently uses Qt primitives, so load it
        # only when a persistent legacy configuration is actually opened.
        from foammesh.db.configurations_schema import schema
        db = Configurations(schema)
        config_path = storage_path / FILE_NAME
        if config_path.exists():
            db.load(storage_path)
        elif create:
            db.create(storage_path)
        else:
            raise FileNotFoundError(f'FoamMesh configuration does not exist: {config_path}')
        bus = EventBus()
        session = cls(case_path, storage_path, ProjectState(db, bus=bus))
        session._probe_volume(read_only_on_lock)
        if not session.read_only:
            session._acquire_writer(read_only_on_lock)
        session._load_or_create_identity()
        if not session.read_only:
            session._start_journal()
        session.configuration()  # warm the DTO cache before the first command
        session._emit('case.opened', path=str(case_path), read_only=session.read_only,
                      read_only_reason=session.read_only_reason)
        return session

    @classmethod
    def from_state(cls, case_path: str | Path, state: ProjectState, *, storage_path: str | Path | None = None,
                   read_only: bool = False, jobs: JobManager | None = None) -> 'CaseSession':
        """Adapt an already-open project without making another state owner."""
        case_path = Path(case_path).resolve()
        session = cls(case_path, Path(storage_path or case_path), state, read_only=read_only)
        if jobs is not None:
            session.jobs = jobs
        session._load_or_create_identity()
        if not read_only:
            session._start_journal()
        session.configuration()  # warm the DTO cache before the first command
        session._emit('case.attached', path=str(case_path), read_only=read_only)
        return session

    @property
    def revisions(self) -> RevisionSnapshot:
        return RevisionSnapshot(self.session_epoch, self.authored_revision,
                                self.artifact_sequence, self.presentation_sequence)

    def configuration(self) -> dict:
        """Configuration DTO cached per authored revision; treat as immutable."""
        cached = self._configuration_cache
        if cached is not None and cached[0] == self.authored_revision:
            return cached[1]
        import yaml
        with self.monitor.measure('snapshot_build'):
            configuration = yaml.safe_load(self.state.db.toYaml())
        self._configuration_cache = (self.authored_revision, configuration)
        return configuration

    def snapshot(self) -> dict:
        return {
            'session_id': self.session_id, 'case_id': self.case_id,
            'case_path': str(self.case_path), 'storage_path': str(self.storage_path),
            'read_only': self.read_only, 'revisions': self.revisions.to_dict(),
            'configuration': self.configuration(),
        }

    async def snapshot_json(self) -> str:
        """Encode the snapshot off the owner loop from the immutable DTO."""
        snapshot = self.snapshot()
        return await asyncio.to_thread(json.dumps, snapshot, sort_keys=True, default=str)

    async def refresh_configuration_cache(self) -> None:
        """Rebuild the immutable configuration DTO off the owner loop.

        Called from the serialized command path after a mutation, so the next
        command slice reads a warm cache instead of parsing the configuration
        on the Qt loop.  Safe because the scheduler still serializes access.
        """
        cached = self._configuration_cache
        if cached is not None and cached[0] == self.authored_revision:
            return
        import yaml
        revision = self.authored_revision
        configuration = await asyncio.to_thread(
            lambda: yaml.safe_load(self.state.db.toYaml()))
        if self.authored_revision == revision:
            self._configuration_cache = (revision, configuration)

    def require_writable(self):
        if self.read_only:
            raise ReadOnlySessionError('case session is read-only', details={
                'case_id': self.case_id, 'reason': self.read_only_reason})

    def latest_change_set(self) -> dict | None:
        return self.change_sets[-1] if self.change_sets else None

    def attach_presentation(self, state) -> None:
        """Attach a desktop rendering surface so presentation ops are available."""
        self.presentation = state

    def bump_presentation(self, event: str, **payload) -> int:
        """Advance the presentation sequence and emit a memory-only event."""
        self.presentation_sequence += 1
        self._emit(event, durable=False, **payload)
        return self.presentation_sequence

    def assert_revision(self, expected_revision: int | None):
        if expected_revision is not None and expected_revision != self.authored_revision:
            raise RevisionConflictError('case configuration changed', details={
                'expected_revision': expected_revision,
                'current_revision': self.authored_revision,
            })

    def get_idempotent(self, key: str | None, fingerprint: str | None = None):
        entry = self._idempotency.get(key) if key else None
        if entry is None:
            return None
        stored_fingerprint, result = entry
        if fingerprint is not None and stored_fingerprint != fingerprint:
            raise IdempotencyConflictError('idempotency key was used for a different command', details={
                'idempotency_key': key,
            })
        return result

    def remember_idempotent(self, key: str | None, fingerprint: str, result) -> None:
        if key:
            self._idempotency[key] = (fingerprint, result)

    def set_event_context(self, command) -> None:
        self._event_context.set((
            {'id': command.actor.id, 'kind': command.actor.kind.value},
            command.source.value, command.correlation_id or command.command_id))

    def clear_event_context(self) -> None:
        self._event_context.set((None, None, None))

    def emit_as(self, event: str, *, actor: dict | None, source: str | None,
                correlation_id: str | None, **payload) -> None:
        """Emit a background fact with its captured initiating identity."""
        token = self._event_context.set((actor, source, correlation_id))
        try:
            self._emit(event, **payload)
        finally:
            self._event_context.reset(token)

    def events_after(self, sequence: int, *, limit: int = 1_000) -> list[dict]:
        if self.journal is None:
            return [event for event in self._events if event['sequence'] > sequence][:limit]
        self.journal.flush()
        earliest = self.journal.earliest_sequence()
        if earliest is not None and sequence < earliest - 1:
            raise EventReplayGapError('requested event cursor is no longer retained', details={
                'after_sequence': sequence, 'earliest_sequence': earliest,
                'latest_sequence': self.event_sequence, 'resync_required': True,
            })
        return self.journal.after(sequence, limit=limit)

    async def flush_events(self) -> int:
        return 0 if self.journal is None else await self.journal.flush_off_loop()

    def close(self, *, save: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        if save and not self.read_only:
            self.state.db.save()
        self._emit('case.closed', path=str(self.case_path))
        if self.journal is not None:
            self.journal.close()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        if self._lock is not None:
            info = self.storage_path / LOCK_INFO_FILE
            if info.exists():
                info.unlink(missing_ok=True)
            self._lock.release()
            _ACTIVE_WRITERS.discard(self.case_path)
            self._lock = None

    async def aclose(self, *, save: bool = True) -> None:
        """Orderly async shutdown for desktop/headless owner loops."""
        await self.scheduler.close()
        await self.flush_events()
        self.close(save=save)

    def _probe_volume(self, read_only_on_lock: bool) -> None:
        """Writable network-share use requires a proven locking probe (§6.1)."""
        if not (_is_network_path(self.case_path) or os.environ.get(FORCE_LOCK_PROBE_ENV)):
            return
        reliable, reason = probe_locking(self.storage_path)
        if not reliable:
            self._locked(read_only_on_lock, f'cross-process locking is unreliable here: {reason}')

    def _acquire_writer(self, read_only_on_lock: bool) -> None:
        if self.case_path in _ACTIVE_WRITERS:
            self._locked(read_only_on_lock, 'a writable session already owns this case in this process',
                         owner=self._read_lock_info())
            return
        lock = FileLock(self.storage_path / LOCK_FILE)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            self._locked(read_only_on_lock, 'another FoamMesh process owns this case',
                         owner=self._read_lock_info())
            return
        self._lock = lock
        _ACTIVE_WRITERS.add(self.case_path)
        info = {
            'pid': os.getpid(), 'host': socket.gethostname(), 'session_id': self.session_id,
            'started_at': _now(), 'case_path': str(self.case_path),
        }
        (self.storage_path / LOCK_INFO_FILE).write_text(
            json.dumps(info, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    def _read_lock_info(self) -> dict | None:
        """Best-effort owner metadata for structured case_locked results."""
        path = self.storage_path / LOCK_INFO_FILE
        try:
            info = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(info, dict):
            pid = info.get('pid')
            if isinstance(pid, int) and info.get('host') == socket.gethostname():
                # The OS releases a crashed writer's lock, so a dead pid here
                # only means the info sidecar is stale, never that the case is
                # safe to force-unlock while the lock itself is still held.
                info['owner_alive'] = _pid_alive(pid)
            return info
        return None

    def _locked(self, read_only_on_lock: bool, reason: str, *, owner: dict | None = None) -> None:
        if not read_only_on_lock:
            details = {'case_path': str(self.case_path)}
            if owner is not None:
                details['owner'] = owner
            raise CaseLockedError(reason, details=details)
        self.read_only = True
        self.read_only_reason = reason

    def _start_journal(self) -> None:
        self.journal = EventJournal(self.storage_path / JOURNAL_FILE_NAME, monitor=self.monitor)
        self.event_sequence = self.journal.latest_sequence()
        if self.event_sequence:
            previous = self.journal.after(self.event_sequence - 1, limit=1)[0]['revisions']
            self.authored_revision = int(previous['authored_revision'])
            self.artifact_sequence = int(previous['artifact_sequence'])
            self.presentation_sequence = int(previous['presentation_sequence'])

    def _load_or_create_identity(self) -> None:
        """Keep the case ID durable while each open receives a new epoch."""
        path = self.storage_path / SESSION_METADATA_FILE
        try:
            stored = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            stored = {}
        if isinstance(stored.get('case_id'), str):
            self.case_id = stored['case_id']
        previous_epoch = int(stored.get('session_epoch', 0) or 0)
        self.session_epoch = previous_epoch + 1 if not self.read_only else max(previous_epoch, 1)
        if self.read_only:
            return
        payload = {'case_id': self.case_id, 'session_epoch': self.session_epoch}
        temporary = path.with_suffix('.json.tmp')
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record_change_set(self, kind: str, payload: dict) -> None:
        actor, source, correlation_id = self._event_context.get()
        transaction = payload.get('transaction')
        self.change_sets.append({
            'change_set_id': correlation_id or str(uuid4()),
            'kind': kind, 'actor': actor, 'source': source,
            'authored_revision': self.authored_revision,
            'transaction_id': getattr(transaction, 'tx_id', None),
        })

    def _on_project_event(self, *, event: str, **payload) -> None:
        if event in (Event.TRANSACTION_APPLIED, Event.UNDONE, Event.REDONE):
            self.authored_revision += 1
            self._configuration_cache = None
            self._record_change_set({Event.TRANSACTION_APPLIED: 'edit', Event.UNDONE: 'undo',
                                     Event.REDONE: 'redo'}[event], payload)
        elif event in (Event.ARTIFACT_GEOMETRY_CHANGED, Event.ARTIFACT_DICTIONARIES_CHANGED,
                       Event.ARTIFACT_MESH_CHANGED,
                       Event.ARTIFACT_QUALITY_CHANGED, Event.ARTIFACT_RESTORED):
            self.artifact_sequence += 1
        self._emit(event, **payload)

    def _emit(self, event: str, *, durable: bool = True, **payload) -> None:
        self.event_sequence += 1
        actor, source, correlation_id = self._event_context.get()
        envelope = build_event(sequence=self.event_sequence, event=event,
                               session_id=self.session_id, case_id=self.case_id,
                               revisions=self.revisions, payload=payload,
                               actor=actor, source=source, correlation_id=correlation_id)
        self._events.append(envelope.to_dict())
        # Presentation events are memory-only; only durable facts reach the
        # reconnect journal (§6.10).
        if durable and self.journal is not None:
            self.journal.append(envelope)
