"""Autosave journal and crash recovery (Plan 35 CR9).

A GUI crash must lose only the authored changes that had not reached the disk.
This module is the ordering contract that makes that true:

1. **Sequence numbers.** Every change set the session records (an edit, an
   undo, a redo) is given ``seq``, a per-project counter that only increases,
   on the thread that applied it and at the moment it applied. So ``seq`` order
   is apply order. An undo is a change set of its own, never a deletion.
2. **The journal.** ``<storage>/autosave/journal.bin`` is append-only. A record
   is ``length | seq | crc32 | payload`` (payload = the change set as JSON,
   holding "set to value" operations, so replay is idempotent). A short record
   or a bad crc ends the valid journal; what follows it is dropped and counted.
3. **The durability boundary.** One writer thread appends in ``seq`` order and
   calls ``fsync`` at most once per ``fsync_interval`` (1 s). After each fsync
   it publishes ``durable_seq``. An edit is protected once
   ``seq <= durable_seq``.
4. **The project carries ``saved_seq``.** A save records, inside the saved
   configuration, the ``seq`` of the last change set its content includes. The
   project is the single source of truth for what is saved.
5. **Save order.** The save snapshots ``seq`` = N *before* it serialises the
   state, writes temporaries, fsyncs, replaces, fsyncs the directory, and only
   then asks the writer to "compact through N". Compaction writes a new journal
   holding the records with ``seq > N``, fsyncs it and replaces the old file.
   Truncation is garbage collection only, never a correctness step.
6. **Recovery.** Read ``saved_seq`` = S, read the journal up to its first
   invalid record, replay in order the records with ``seq > S``.

Mesh outputs never enter the journal: only the configuration document and the
imported geometry surfaces (authored input) do.
"""
from __future__ import annotations

import copy
import errno
import json
import logging
import os
import re
import struct
import sys
import threading
import time
import weakref
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

AUTOSAVE_DIR = 'autosave'
JOURNAL_FILE = 'journal.bin'

#: ``length`` (payload bytes), ``seq``, ``crc32`` of seq + payload.
_HEADER = struct.Struct('<IQI')
_SEQ = struct.Struct('<Q')
#: A length beyond this is corruption, not a record.
MAX_RECORD_BYTES = 512 * 1024 * 1024

DEFAULT_FSYNC_INTERVAL = 1.0
DEFAULT_RETRY_INTERVAL = 5.0

FAULT_ENV = 'FOAMMESH_JOURNAL_FAULT'
FAULT_REPORT_ENV = 'FOAMMESH_JOURNAL_FAULT_REPORT'
FAULT_EXIT_CODE = 86

#: Named transitions the fault-point gate kills a process at.
FAULT_POINTS = (
    'after-assign-seq', 'after-append', 'after-fsync',
    'save-temp-written', 'save-temp-fsynced', 'save-replaced', 'save-dir-fsynced',
    'compact-begin', 'compact-temp-written', 'compact-replaced',
    'replay',
)

# --------------------------------------------------------------------------- #
# Fault points (tests only)
# --------------------------------------------------------------------------- #

#: In-process hook for tests: ``hook(name, context)``. May raise to simulate
#: a death at that point. ``None`` in production.
fault_hook: Callable[[str, dict], None] | None = None

_fault_lock = threading.Lock()
_fault_counts: dict[str, int] = {}
_LIVE: 'weakref.WeakSet[Autosave]' = weakref.WeakSet()


def fault_point(name: str, **context) -> None:
    """A named transition. Inert unless a test armed it.

    ``FOAMMESH_JOURNAL_FAULT=<name>`` or ``<name>@<n>`` (the n-th hit) kills
    the process with ``os._exit`` right there, after writing the journal state
    at the moment of death to ``FOAMMESH_JOURNAL_FAULT_REPORT``.
    """
    hook = fault_hook
    if hook is not None:
        hook(name, context)
    spec = os.environ.get(FAULT_ENV)
    if not spec:
        return
    target, _, nth = spec.partition('@')
    if target != name:
        return
    with _fault_lock:
        count = _fault_counts[name] = _fault_counts.get(name, 0) + 1
    if nth and count != int(nth):
        return
    report = os.environ.get(FAULT_REPORT_ENV)
    if report:
        state = {
            'point': name, 'hit': count,
            'context': {key: value for key, value in context.items()
                        if isinstance(value, (str, int, float, bool, type(None)))},
            'journals': [autosave.state_for_report() for autosave in list(_LIVE)],
        }
        try:
            with open(report, 'w', encoding='utf-8') as output:
                json.dump(state, output)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            pass
    os._exit(FAULT_EXIT_CODE)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

def _crc(seq: int, body: bytes) -> int:
    return zlib.crc32(body, zlib.crc32(_SEQ.pack(seq))) & 0xFFFFFFFF


def encode_record(seq: int, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True,
                      default=str).encode('utf-8')
    return _HEADER.pack(len(body), seq, _crc(seq, body)) + body


@dataclass(frozen=True)
class JournalRecord:
    seq: int
    payload: dict
    raw: bytes

    @property
    def kind(self) -> str:
        return str(self.payload.get('kind', 'edit'))

    @property
    def action(self) -> str:
        return str(self.payload.get('action') or self.kind)

    def changed_paths(self) -> list[str]:
        paths = ['/'.join(str(part) for part in op[1]) or '<document>'
                 for op in self.payload.get('ops', ())]
        paths += [f'geometry:{key}' for key in self.payload.get('files', {})]
        return paths


@dataclass
class JournalScan:
    records: list[JournalRecord]
    valid_bytes: int
    total_bytes: int
    dropped_records: int

    @property
    def torn(self) -> bool:
        return self.valid_bytes < self.total_bytes

    @property
    def last_seq(self) -> int:
        return self.records[-1].seq if self.records else 0


def scan_journal(path: str | Path) -> JournalScan:
    """Read *path* up to its first invalid record.

    Everything before a short record, a bad crc, a non-increasing seq or an
    unparseable payload is valid; everything from it on is discarded and
    counted (best effort: the records whose headers still chain).
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b''
    records: list[JournalRecord] = []
    offset = 0
    last_seq = 0
    size = len(data)
    while offset + _HEADER.size <= size:
        length, seq, crc = _HEADER.unpack_from(data, offset)
        end = offset + _HEADER.size + length
        if length > MAX_RECORD_BYTES or end > size:
            break
        body = data[offset + _HEADER.size:end]
        if _crc(seq, body) != crc or seq <= last_seq:
            break
        try:
            payload = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            break
        if not isinstance(payload, dict):
            break
        records.append(JournalRecord(seq, payload, data[offset:end]))
        last_seq = seq
        offset = end
    dropped = 0
    cursor = offset
    while cursor < size:
        dropped += 1
        if cursor + _HEADER.size > size:
            break
        length = _HEADER.unpack_from(data, cursor)[0]
        if length > MAX_RECORD_BYTES:
            break
        cursor += _HEADER.size + length
    return JournalScan(records, offset, size, dropped)


# --------------------------------------------------------------------------- #
# "Set to value" change sets
# --------------------------------------------------------------------------- #

def _same(old, new) -> bool:
    return type(old) is type(new) and old == new


def diff_documents(old, new, path: tuple = ()) -> list[list]:
    """Operations turning *old* into *new*: ``['set', path, value]``/``['del', path]``.

    Applying them is idempotent, so a record replayed over a file that already
    holds it changes nothing.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        ops: list[list] = []
        for key, value in new.items():
            if key not in old:
                ops.append(['set', [*path, key], value])
            else:
                ops.extend(diff_documents(old[key], value, (*path, key)))
        for key in old:
            if key not in new:
                ops.append(['del', [*path, key]])
        return ops
    if _same(old, new):
        return []
    return [['set', list(path), new]]


def apply_ops(document, ops: Iterable) -> object:
    """Apply *ops* to *document* in place where possible; returns the document."""
    for op in ops:
        kind, path = op[0], list(op[1])
        if not path:
            if kind == 'set':
                document = copy.deepcopy(op[2])
            continue
        parent = document
        for key in path[:-1]:
            child = parent.get(key) if isinstance(parent, dict) else None
            if not isinstance(child, dict):
                if kind == 'del':
                    parent = None
                    break
                child = parent[key] = {}
            parent = child
        if parent is None:
            continue
        if kind == 'set':
            parent[path[-1]] = copy.deepcopy(op[2])
        elif kind == 'del':
            parent.pop(path[-1], None)
    return document


# --------------------------------------------------------------------------- #
# Low-level file operations (a test replaces these to simulate a failing disk)
# --------------------------------------------------------------------------- #

class FileOps:
    def open_append(self, path: Path, offset: int):
        handle = open(path, 'r+b' if path.exists() else 'w+b')
        handle.seek(offset)
        handle.truncate()
        return handle

    def write(self, handle, data: bytes) -> None:
        handle.write(data)

    def fsync(self, handle) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    def write_new(self, path: Path, data: bytes) -> None:
        with open(path, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def replace(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    def fsync_dir(self, directory: Path) -> None:
        fsync_directory(directory)


def fsync_directory(directory: str | Path) -> None:
    """Make a rename durable. Windows has no directory fsync; NTFS journals it."""
    if sys.platform == 'win32':
        return
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rewrite_journal(path: Path, raws: Iterable[bytes], ops: FileOps | None = None) -> None:
    """Replace the journal with *raws*: temp, fsync, replace, fsync dir."""
    ops = ops or FileOps()
    temporary = path.with_name(path.name + '.tmp')
    try:
        ops.write_new(temporary, b''.join(raws))
        fault_point('compact-temp-written', path=str(path))
        ops.replace(temporary, path)
        fault_point('compact-replaced', path=str(path))
        ops.fsync_dir(path.parent)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# The writer thread
# --------------------------------------------------------------------------- #

class JournalWriter:
    """Appends records in ``seq`` order, fsyncs at most once per interval.

    ``durable_seq`` only rises after an fsync covered the record. A failed
    write or fsync stops it rising, reports through *on_status*, and is
    retried every *retry_interval*; a retry first truncates the file back to
    the last durable byte so a half-written record never sits in front of the
    records written after it. Compaction waits until the writer is healthy and
    everything queued is durable.
    """

    def __init__(self, path: Path, *, records: Iterable[JournalRecord] = (),
                 durable_seq: int = 0, fsync_interval: float = DEFAULT_FSYNC_INTERVAL,
                 retry_interval: float = DEFAULT_RETRY_INTERVAL,
                 file_ops: FileOps | None = None,
                 on_status: Callable[[bool, str | None], None] | None = None):
        self.path = Path(path)
        self.fsync_interval = fsync_interval
        self.retry_interval = retry_interval
        self.file_ops = file_ops or FileOps()
        self._on_status = on_status
        self._cond = threading.Condition()
        self._retained: list[tuple[int, bytes]] = [(r.seq, r.raw) for r in records]
        size = sum(len(raw) for _seq, raw in self._retained)
        self._written = self._durable = len(self._retained)
        self._written_offset = self._durable_offset = size
        self._durable_seq = max(durable_seq, self._retained[-1][0] if self._retained else 0)
        self._compactions: list[tuple[int, int | None]] = []
        self._force = False
        self._closing = False
        self._handle = None
        self._last_fsync = 0.0
        self._failing: str | None = None
        self._retry_at = 0.0
        self._generation = 0  # bumps when an fsync, compaction or failure completes
        self._thread: threading.Thread | None = None
        self._dead = False  # a test "kill": the thread stops touching the disk

    # -- producer side (GUI thread) --------------------------------------- #

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name='foammesh-autosave-journal', daemon=True)
            self._thread.start()

    def append(self, seq: int, data: bytes) -> None:
        with self._cond:
            if self._retained and seq <= self._retained[-1][0]:
                raise ValueError(f'journal seq {seq} is not after {self._retained[-1][0]}')
            self._retained.append((seq, data))
            self._cond.notify_all()
        self.start()

    def compact(self, low: int, high: int | None = None) -> None:
        """Keep only records with ``low < seq <= high`` (``high=None``: all above)."""
        with self._cond:
            if self._thread is None and not self._retained and not self.path.exists():
                return  # nothing was ever journalled: do not create the file
            self._compactions.append((low, high))
            self._cond.notify_all()
        self.start()

    @property
    def durable_seq(self) -> int:
        return self._durable_seq

    @property
    def written_seq(self) -> int:
        with self._cond:
            return self._retained[self._written - 1][0] if self._written else 0

    @property
    def failure(self) -> str | None:
        return self._failing

    def retained_seqs(self) -> list[int]:
        with self._cond:
            return [seq for seq, _raw in self._retained]

    def wait_durable(self, seq: int, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._durable_seq < seq:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def flush(self, timeout: float | None = 10.0) -> bool:
        """Write, fsync and compact everything queued now. False on failure/timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if self._thread is None and self._idle():
                return True
        self.start()
        with self._cond:
            self._force = True
            self._cond.notify_all()
            while not self._idle():
                if self._failing is not None or self._dead:
                    return False
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining if remaining is None else min(remaining, 0.25))
            return True

    def close(self, timeout: float | None = 10.0) -> bool:
        flushed = self.flush(timeout)
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._close_handle()
        return flushed

    def kill(self) -> None:
        """Test hook: stop exactly where it is, as a process death would."""
        with self._cond:
            self._dead = True
            self._closing = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)
        self._close_handle()

    # -- writer thread ----------------------------------------------------- #

    def _idle(self) -> bool:
        return (self._durable == len(self._retained) and not self._compactions)

    def _next_wait(self, now: float) -> float | None:
        if self._failing is not None:
            return max(0.0, self._retry_at - now)
        if self._written < len(self._retained) or self._compactions or self._force:
            return 0.0
        if self._durable < self._written:
            return max(0.0, self._last_fsync + self.fsync_interval - now)
        return None

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._dead:
                        return
                    wait = self._next_wait(time.monotonic())
                    if wait == 0.0:
                        break
                    if self._closing and wait is None:
                        return
                    self._cond.wait(wait)
            try:
                self._step()
            except Exception:  # noqa: BLE001 - the writer must never die silently
                logger.exception('autosave journal writer step failed')
                self._fail('unexpected journal error')
            except BaseException:  # a simulated death: stop touching the disk
                with self._cond:
                    self._dead = True
                    self._cond.notify_all()
                self._close_handle()
                return

    def _step(self) -> None:
        now = time.monotonic()
        with self._cond:
            if self._dead:
                return
            if self._failing is not None and now < self._retry_at:
                return
            force = self._force or bool(self._compactions)
            pending = self._retained[self._written:]
        try:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.file_ops.open_append(self.path, self._durable_offset)
            for seq, raw in pending:
                if self._dead:
                    return
                self.file_ops.write(self._handle, raw)
                with self._cond:
                    self._written += 1
                    self._written_offset += len(raw)
                fault_point('after-append', seq=seq)
            with self._cond:
                due = self._durable < self._written and (
                    force or now - self._last_fsync >= self.fsync_interval)
                target = self._written
                offset = self._written_offset
            if due:
                self.file_ops.fsync(self._handle)
                with self._cond:
                    self._durable = max(self._durable, target)
                    self._durable_offset = offset
                    self._last_fsync = time.monotonic()
                    if self._durable:
                        self._durable_seq = max(self._durable_seq,
                                                self._retained[self._durable - 1][0])
                    self._generation += 1
                    self._cond.notify_all()
                fault_point('after-fsync', seq=self._durable_seq)
            with self._cond:
                compactions = list(self._compactions) if self._idle_but_compactions() else []
            for low, high in compactions:
                self._compact(low, high)
            recovered = self._failing is not None
            with self._cond:
                self._failing = None
                if self._written == len(self._retained) and self._durable == self._written:
                    self._force = False
                self._cond.notify_all()
            if recovered:
                self._status(True, None)
        except OSError as error:
            self._fail(_describe(error))

    def _idle_but_compactions(self) -> bool:
        return (self._compactions and self._durable == len(self._retained))

    def _compact(self, low: int, high: int | None) -> None:
        fault_point('compact-begin', low=low)
        with self._cond:
            keep = [(seq, raw) for seq, raw in self._retained
                    if seq > low and (high is None or seq <= high)]
            snapshot_len = len(self._retained)
        self._close_handle()
        rewrite_journal(self.path, (raw for _seq, raw in keep), self.file_ops)
        with self._cond:
            # Anything appended while the rewrite ran is still unwritten and
            # sits after the kept records.
            extra = self._retained[snapshot_len:]
            self._retained = keep + extra
            self._written = self._durable = len(keep)
            self._written_offset = self._durable_offset = sum(len(raw) for _s, raw in keep)
            self._compactions.remove((low, high))
            self._generation += 1
            self._cond.notify_all()

    def _fail(self, reason: str) -> None:
        self._close_handle()
        with self._cond:
            first = self._failing != reason
            self._failing = reason
            self._retry_at = time.monotonic() + self.retry_interval
            # A retry rewrites everything after the last durable byte.
            self._written = self._durable
            self._written_offset = self._durable_offset
            self._generation += 1
            self._cond.notify_all()
        if first:
            self._status(False, reason)

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _status(self, ok: bool, reason: str | None) -> None:
        if self._on_status is not None:
            try:
                self._on_status(ok, reason)
            except Exception:  # noqa: BLE001
                logger.exception('autosave status listener failed')


def _describe(error: OSError) -> str:
    if error.errno == errno.ENOSPC:
        return 'the disk is full'
    if error.errno in (errno.EACCES, errno.EPERM):
        return 'FoamMesh is not allowed to write the autosave file'
    if error.errno in (errno.ENOENT, errno.ENODEV, errno.EIO):
        return 'the drive holding the case is not available'
    return error.strerror or str(error)


# --------------------------------------------------------------------------- #
# The document being protected
# --------------------------------------------------------------------------- #

class DocumentSource:
    """What an :class:`Autosave` protects. Subclassed per document kind."""

    def document(self) -> dict:
        raise NotImplementedError

    def files(self) -> dict:
        return {}

    def encode_file(self, value) -> str | None:
        return None

    def decode_file(self, text: str | None):
        return text

    def replace(self, document: dict, files: dict, *, action: str) -> None:
        raise NotImplementedError


class ConfigurationSource(DocumentSource):
    """A :class:`ProjectState` over the ``Configurations`` db.

    The configuration document plus the imported geometry surfaces, which are
    authored input the configuration refers to by key. A restore goes through
    ``ProjectState.replace_document`` so it is one ordinary, undoable,
    journalled change set.
    """

    GEOMETRY = 'geometry'

    def __init__(self, state):
        self.state = state

    @property
    def db(self):
        return self.state.db

    def document(self) -> dict:
        return self.db.data()

    def files(self) -> dict:
        store = getattr(self.db, '_files', None) or {}
        return dict(store.get(self.GEOMETRY, {}))

    def encode_file(self, value) -> str | None:
        if value is None:
            return None
        from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter
        writer = vtkXMLPolyDataWriter()
        writer.SetInputData(value)
        writer.WriteToOutputStringOn()
        writer.Update()
        return writer.GetOutputString()

    def decode_file(self, text: str | None):
        if text is None:
            return None
        from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
        reader = vtkXMLPolyDataReader()
        reader.ReadFromInputStringOn()
        reader.SetInputString(text)
        reader.Update()
        return reader.GetOutput()

    def replace(self, document: dict, files: dict, *, action: str) -> None:
        db = self.db
        if files:
            store = db._files.setdefault(self.GEOMETRY, {})
            for key, value in files.items():
                store[key] = value
                match = re.search(r'(\d+)$', key)
                if match:
                    cls = type(db)
                    cls._geometryNextKey = max(getattr(cls, '_geometryNextKey', 0),
                                               int(match.group(1)))
        document = db.validateData(document, fillWithDefault=True)
        from .transactions import Source
        self.state.replace_document(document, action=action, source=Source.SYSTEM)


# --------------------------------------------------------------------------- #
# The per-project autosave
# --------------------------------------------------------------------------- #

@dataclass
class RecoveryOffer:
    """Unsaved change sets a dead session left behind."""
    saved_seq: int
    records: list[JournalRecord]
    dropped_records: int = 0
    resolved: bool = False

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def last_seq(self) -> int:
        return self.records[-1].seq if self.records else self.saved_seq

    def describe(self) -> list[dict]:
        return [{'seq': record.seq, 'kind': record.kind, 'action': record.action,
                 'timestamp': record.payload.get('timestamp'),
                 'changes': record.changed_paths()} for record in self.records]


@dataclass
class _Shadow:
    document: dict
    files: dict = field(default_factory=dict)


class Autosave:
    """Journals every change set of one project and offers them back after a crash."""

    def __init__(self, directory: str | Path, source: DocumentSource, *,
                 saved_seq: int = 0, fsync_interval: float = DEFAULT_FSYNC_INTERVAL,
                 retry_interval: float = DEFAULT_RETRY_INTERVAL,
                 file_ops: FileOps | None = None):
        self.directory = Path(directory)
        self.path = self.directory / AUTOSAVE_DIR / JOURNAL_FILE
        self.source = source
        self._status_listeners: list[Callable[[bool, str | None], None]] = []
        scan = scan_journal(self.path)
        pending = [record for record in scan.records if record.seq > saved_seq]
        if scan.torn or len(pending) != len(scan.records):
            # Drop the torn tail and the saved records before appending, or
            # new records would sit behind garbage and be unreachable.
            rewrite_journal(self.path, (record.raw for record in pending), file_ops)
        self.saved_seq = saved_seq
        self._seq = max(saved_seq, scan.last_seq)
        # Only a torn tail and nothing whole after saved_seq: nothing to offer.
        self.recovery = (RecoveryOffer(saved_seq, pending, scan.dropped_records)
                         if pending else None)
        self.writer = JournalWriter(
            self.path, records=pending, durable_seq=self._seq,
            fsync_interval=fsync_interval, retry_interval=retry_interval,
            file_ops=file_ops, on_status=self._writer_status)
        self._shadow = self._take_shadow()
        self._closed = False
        # Change sets are applied one at a time already; this keeps seq order
        # and the shadow consistent should two threads ever publish at once.
        self._record_lock = threading.RLock()
        _LIVE.add(self)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def attach(cls, state, storage_path: str | Path, **options) -> 'Autosave | None':
        """The autosave of *state*'s configuration, created once per db."""
        db = state.db
        existing = getattr(db, 'autosave', None)
        if isinstance(existing, Autosave) and not existing._closed:
            if existing.directory == Path(storage_path).resolve():
                return existing
            existing.close(discard=False)
        if not isinstance(getattr(db, 'data', lambda: None)(), dict):
            return None
        autosave = cls(Path(storage_path).resolve(), ConfigurationSource(state),
                       saved_seq=int(getattr(db, 'savedSeq', 0) or 0), **options)
        db.autosave = autosave
        return autosave

    # -- the GUI-thread side ----------------------------------------------- #

    @property
    def applied_seq(self) -> int:
        """``seq`` of the last change set applied to the in-memory state."""
        return self._seq

    @property
    def durable_seq(self) -> int:
        return self.writer.durable_seq

    @property
    def failure(self) -> str | None:
        return self.writer.failure

    def record(self, kind: str, *, action: str = '', tx_id: str | None = None,
               timestamp: str | None = None) -> int:
        """Journal the change set just applied. Returns its ``seq``."""
        with self._record_lock:
            return self._record(kind, action, tx_id, timestamp)

    def _record(self, kind, action, tx_id, timestamp) -> int:
        if self._closed:
            return self._seq
        document = self.source.document()
        ops = diff_documents(self._shadow.document, document)
        self._shadow.document = apply_ops(self._shadow.document, ops)
        files = {}
        for key, value in self.source.files().items():
            if self._shadow.files.get(key, _MISSING) is not value:
                files[key] = self.source.encode_file(value)
                self._shadow.files[key] = value
        self._seq += 1
        seq = self._seq
        fault_point('after-assign-seq', seq=seq)
        payload = {'kind': kind, 'action': action, 'tx_id': tx_id,
                   'timestamp': timestamp, 'ops': ops}
        if files:
            payload['files'] = files
        self.writer.append(seq, encode_record(seq, payload))
        return seq

    def saved(self, seq: int) -> None:
        """The project files holding everything through *seq* are durable."""
        self.saved_seq = max(self.saved_seq, seq)
        if self.recovery is not None and seq >= self.recovery.last_seq:
            self.recovery.resolved = True
        self.writer.compact(seq)

    # -- recovery ---------------------------------------------------------- #

    def pending_recovery(self) -> RecoveryOffer | None:
        offer = self.recovery
        return offer if offer is not None and not offer.resolved else None

    def restore(self) -> int:
        """Replay the offered records over the state. Returns how many."""
        offer = self.pending_recovery()
        if offer is None:
            return 0
        document = copy.deepcopy(self.source.document())
        files = {}
        for record in offer.records:
            fault_point('replay', seq=record.seq)
            document = apply_ops(document, record.payload.get('ops', ()))
            for key, text in record.payload.get('files', {}).items():
                files[key] = text
        decoded = {key: self.source.decode_file(text) for key, text in files.items()}
        self.source.replace(document, decoded,
                            action=f'Restore unsaved changes ({offer.count} edits)')
        offer.resolved = True
        return offer.count

    def discard_recovery(self) -> None:
        """Open last saved: the offered records are dropped from the journal."""
        offer = self.pending_recovery()
        if offer is None:
            return
        offer.resolved = True
        self.writer.compact(offer.last_seq)

    # -- lifetime ---------------------------------------------------------- #

    def wait_durable(self, seq: int | None = None, timeout: float | None = None) -> bool:
        return self.writer.wait_durable(self._seq if seq is None else seq, timeout)

    def flush(self, timeout: float | None = 10.0) -> bool:
        return self.writer.flush(timeout)

    def close(self, *, discard: bool = True, timeout: float | None = 10.0) -> None:
        """An orderly close. *discard* drops this session's unsaved records.

        An unanswered recovery offer survives the close, so a case that was
        closed before the user chose is offered again next time.
        """
        if self._closed:
            return
        self._closed = True
        if discard:
            offer = self.pending_recovery()
            if offer is not None:
                self.writer.compact(offer.saved_seq, offer.last_seq)
            else:
                self.writer.compact(self._seq)
        self.writer.close(timeout)
        _LIVE.discard(self)

    # -- status ------------------------------------------------------------ #

    def add_status_listener(self, callback: Callable[[bool, str | None], None]) -> Callable[[], None]:
        self._status_listeners.append(callback)

        def _remove():
            if callback in self._status_listeners:
                self._status_listeners.remove(callback)
        return _remove

    def _writer_status(self, ok: bool, reason: str | None) -> None:
        for callback in list(self._status_listeners):
            try:
                callback(ok, reason)
            except Exception:  # noqa: BLE001
                logger.exception('autosave status listener failed')

    def state_for_report(self) -> dict:
        return {'path': str(self.path), 'applied_seq': self._seq,
                'durable_seq': self.writer.durable_seq,
                'written_seq': self.writer.written_seq,
                'saved_seq': self.saved_seq}

    def _take_shadow(self) -> _Shadow:
        return _Shadow(copy.deepcopy(self.source.document()), dict(self.source.files()))


_MISSING = object()


def read_saved_seq(*values: int | None) -> int:
    """Recovery uses the lowest ``saved_seq`` of the files one save wrote."""
    present = [int(value) for value in values if value is not None]
    return min(present) if present else 0
