"""Async, streaming job execution with one mutating operation per case."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from .job import JobStatus
from .process_control import kill_process_tree, new_process_group_kwargs
from .wsl_wrapper import (
    EXIT_PREFIX, HEARTBEAT_PREFIX, RUN_DIR_VARIABLE, RUN_ID_VARIABLE, RUN_PREFIX,
)
from foammesh.core.project.events import Event
from foammesh.core.quantities import agreeing, count_text

logger = logging.getLogger(__name__)

#: CR4 (F4). Output is published in batches, not line by line: a batch goes
#: out when it is this old, or when it holds OUTPUT_BATCH_LINES lines.
OUTPUT_BATCH_SECONDS = 0.1
OUTPUT_BATCH_LINES = 500
#: CR4. A finished job's result keeps at most this much of its output in
#: memory (the tail); the log file keeps all of it.
RETAINED_RESULT_BYTES = 256 * 1024
#: CR4. How many finished results the manager keeps for ``result()``. They
#: are also recorded in ``foammesh/logs/jobs.jsonl``.
RETAINED_RESULTS = 64
#: Plan 35 CR5 step 2. One line of output may be this long; the rest of an
#: overlong line is dropped and the line says so, instead of the reader dying.
LINE_LIMIT_BYTES = 1024 * 1024
TRUNCATED_LINE_MARK = b' [... line truncated by FoamMesh]'
#: How long the process may take to be reaped once its output has ended.
WAIT_TIMEOUT_SECONDS = 30
#: Lines kept for the job record and the decoded reason.
TAIL_LINES = 20


@dataclass(frozen=True)
class JobRequest:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    mutation: bool = False
    environment: dict[str, str] | None = None
    log_path: Path | None = None
    timeout: float | None = None
    max_output_bytes: int = 1024 * 1024
    expected_outputs: tuple[Path, ...] = ()
    cleanup_argv: tuple[str, ...] = ()
    #: Plan 35 CR5 step 1. Seconds without mesher output before the user is
    #: asked [Keep waiting] / [Stop]. ``None`` picks the job kind's default
    #: (:func:`default_idle_timeout`); ``0`` turns the prompt off. Never a kill.
    idle_timeout: float | None = None
    #: Plan 35 CR5 step 4. The case whose ``foammesh/run/`` gets this job's
    #: durable run record, written before the process is spawned.
    record_case: Path | None = None
    #: Extra facts for that record (operation, recovery id, ranks).
    record_fields: dict | None = None


class JobErrorCategory(str, Enum):
    LAUNCH = 'launch'
    PROCESS = 'process'
    TIMEOUT = 'timeout'
    CANCELLATION = 'cancellation'
    VALIDATION = 'validation'
    PARSE = 'parse'
    RECOVERY = 'recovery'
    TRANSPORT = 'transport'


#: Idle limits by job kind, seconds (plan 35 CR5 step 1).
IDLE_SNAPPY_SECONDS = 30 * 60
IDLE_CHECKMESH_SECONDS = 10 * 60
IDLE_DEFAULT_SECONDS = 30 * 60
#: Gmsh reports its phase; a surface pass that goes quiet for long is stuck
#: sooner than a 3D pass or an optimisation is.
GMSH_PHASE_IDLE_SECONDS = (
    ('Meshing 1D', 5 * 60),
    ('Meshing 2D', 15 * 60),
    ('Meshing 3D', 30 * 60),
    ('Optimizing', 30 * 60),
    ('Optimising', 30 * 60),
)


def job_kind(argv) -> str:
    text = ' '.join(str(item) for item in argv)
    if 'snappyHexMesh' in text:
        return 'snappy'
    if 'checkMesh' in text:
        return 'checkMesh'
    if 'gmsh' in text.lower():
        return 'gmsh'
    return 'other'


def default_idle_timeout(argv) -> float:
    return {'snappy': IDLE_SNAPPY_SECONDS, 'checkMesh': IDLE_CHECKMESH_SECONDS,
            'gmsh': GMSH_PHASE_IDLE_SECONDS[2][1]}.get(job_kind(argv), IDLE_DEFAULT_SECONDS)


def is_wrapped(argv) -> bool:
    """The command runs inside the CR5 WSL wrapper (it names the run id)."""
    return any(RUN_ID_VARIABLE in str(item) for item in argv)


class _LineReader:
    """``readline`` with a hard per-line cap that truncates instead of raising."""

    def __init__(self, stream, limit: int = LINE_LIMIT_BYTES):
        self.stream = stream
        self.limit = limit
        self.truncated_lines = 0

    async def readline(self) -> bytes:
        kept = bytearray()
        truncated = False
        while True:
            done = True
            try:
                chunk = await self.stream.readuntil(b'\n')
            except asyncio.IncompleteReadError as error:
                chunk = error.partial
            except asyncio.LimitOverrunError as error:
                chunk = await self.stream.readexactly(max(1, error.consumed))
                done = False
            room = self.limit - len(kept)
            if room > 0:
                kept += chunk[:room]
            if len(chunk) > max(room, 0):
                truncated = True
            if done:
                break
        if truncated:
            self.truncated_lines += 1
            kept = kept.rstrip(b'\r\n') + TRUNCATED_LINE_MARK + b'\n'
        return bytes(kept)


_HEARTBEAT_FRAGMENT = re.compile(r'FOAMMESH_HB \S+\r?\n?')


class _Transport:
    """What the wrapper's control lines, and the silence between lines, say."""

    def __init__(self, loop, argv, idle_timeout):
        self.loop = loop
        self.kind = job_kind(argv)
        self.wrapped = is_wrapped(argv)
        self.base_idle = (default_idle_timeout(argv) if idle_timeout is None
                          else float(idle_timeout))
        self.idle_limit = self.base_idle
        self.last_output = loop.time()
        self.last_heartbeat = None
        #: CR6. When anything at all -- heartbeat, control line or output --
        #: last came through; `_watch_transport` measures silence from here.
        self.last_seen = loop.time()
        self.idle_announced = False
        self.idle_prompts = 0
        self.kept_waiting = 0
        self.run_line: dict | None = None
        self.exit_rc: int | None = None
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.run_recorded = False

    def observe(self, text: str) -> tuple[str, str]:
        """``('heartbeat'|'control'|'output', cleaned line)``."""
        self.last_seen = self.loop.time()
        if HEARTBEAT_PREFIX in text:
            self.last_heartbeat = self.loop.time()
            text = _HEARTBEAT_FRAGMENT.sub('', text)
            if not text.strip():
                return 'heartbeat', ''
        stripped = text.strip()
        if stripped.startswith(RUN_PREFIX):
            fields = dict(item.split('=', 1) for item in stripped.split()[2:] if '=' in item)
            self.run_line = {'run_id': stripped.split()[1] if len(stripped.split()) > 1 else '',
                             **{key: _int_or_none(value) for key, value in fields.items()}}
            return 'control', text
        if stripped.startswith(EXIT_PREFIX):
            for item in stripped.split()[2:]:
                if item.startswith('rc='):
                    self.exit_rc = _int_or_none(item[3:])
            return 'control', text
        self.last_output = self.loop.time()
        if stripped:
            self.tail.append(stripped)
        if self.kind == 'gmsh' and self.base_idle:
            for phase, seconds in GMSH_PHASE_IDLE_SECONDS:
                if phase in stripped:
                    self.idle_limit = float(seconds)
                    break
        return 'output', text

    def idle_seconds(self) -> float:
        return self.loop.time() - self.last_output


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class JobResult:
    job_id: str
    name: str
    status: JobStatus
    returncode: int | None
    output: str
    log_path: Path | None
    error: str | None = None
    error_category: JobErrorCategory | None = None
    output_truncated: bool = False
    artifacts: tuple[str, ...] = ()
    started_at: str | None = None
    finished_at: str | None = None
    warnings: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    environment_fingerprint: str | None = None
    mutation: bool = False
    #: Plan 35 CR5 step 8: how the job ended, in words (see
    #: :func:`foammesh.core.run_result.decode_exit`), and what was seen of it.
    exit: dict | None = None
    transport: str | None = None
    idle: dict | None = None
    tail: tuple[str, ...] = ()
    run_id: str | None = None
    run_state: str | None = None
    linux_rc: int | None = None

    @property
    def reason(self) -> str:
        return str((self.exit or {}).get('reason') or '')

    def to_dict(self) -> dict:
        value = {'job_id': self.job_id, 'name': self.name, 'status': self.status.value,
                 'returncode': self.returncode,
                 'log_path': str(self.log_path) if self.log_path else None,
                 'error': self.error,
                 'error_category': self.error_category.value if self.error_category else None,
                 'output_truncated': self.output_truncated,
                 'artifacts': list(self.artifacts),
                 'started_at': self.started_at, 'finished_at': self.finished_at,
                 'warnings': list(self.warnings)}
        value.update({
            'command': {'argv': list(self.argv), 'cwd': self.cwd,
                        'environment_fingerprint': self.environment_fingerprint},
            'mutation': self.mutation,
        })
        if self.exit is not None or self.transport is not None or self.run_id:
            value.update({
                'reason': self.reason or None,
                'signal': (self.exit or {}).get('signal') or None,
                'exit': self.exit, 'transport': self.transport,
                'idle': self.idle, 'tail': list(self.tail),
                'run_id': self.run_id, 'run_state': self.run_state,
                'linux_rc': self.linux_rc,
            })
        return value


def decode_output(raw: bytes) -> str:
    """One line of a tool's output, decoded in whatever width it speaks.

    DP-13. Windows host tools -- ``wsl.exe`` above all -- write their own
    failures in UTF-16LE. ``bytes.decode()`` defaults to UTF-8, under which
    those bytes decode to NUL-interleaved ASCII, survive the re-encode
    unharmed, and land in the log as ``T?h?e? ...``. So a WSL container that
    failed to start wrote ``HCS_E_CONNECTION_TIMEOUT`` into ``runner.log`` in
    a form nothing downstream could read, and eight runs of one sweep were
    filed as per-model mesh refusals.

    The NUL is the signal: no tool writes one into a line of text on purpose.
    ``readline`` splits on the single byte 0x0a, which in UTF-16LE leaves the
    trailing 0x00 at the head of the *next* line, so alternate lines arrive
    byte-shifted and look big-endian. Both are decoded, by asking which half
    of the line holds the NULs.
    """
    ending = ''
    if raw.endswith(b'\n'):
        # Taken off before the width test and put back after: unpaired, it
        # would decode to a replacement character on every single line.
        raw, ending = raw[:-1], '\n'
    if raw and raw.count(0) * 3 >= len(raw):
        if len(raw) % 2 and raw.startswith(b'\x00'):
            # The other half of the previous line's terminator: 0x0a paired
            # with 0x00 is where `readline` cut, so this byte belongs there
            # and dropping it re-pairs the whole line.
            raw = raw[1:]
        codec = ('utf-16-le' if raw[1::2].count(0) >= raw[0::2].count(0)
                 else 'utf-16-be')
        text = raw.decode(codec, errors='replace').lstrip('\ufeff')
    else:
        text = raw.decode(errors='replace')
    text += ending
    # A NUL reaching here is a decode residue, never content.
    return text.replace('\x00', '')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def environment_fingerprint(environment: dict[str, str]) -> str:
    """Stable environment identity without persisting secret-bearing values."""
    encoded = json.dumps(sorted((str(key), str(value)) for key, value in environment.items()),
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


_SECRET_ARGUMENT_MARKERS = ('token', 'secret', 'password', 'credential', 'api-key', 'apikey')


def redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Redact secret option values before commands enter results or events."""
    redacted = []
    hide_next = False
    for raw in argv:
        item = str(raw)
        if hide_next:
            redacted.append('***redacted***')
            hide_next = False
            continue
        lowered = item.lower()
        marker = next((value for value in _SECRET_ARGUMENT_MARKERS if value in lowered), None)
        if marker and '=' in item:
            redacted.append(item.split('=', 1)[0] + '=***redacted***')
        else:
            redacted.append(item)
            hide_next = bool(marker and item.startswith('-'))
    return tuple(redacted)


class JobManager:
    """Run argv-based jobs without shell construction or pipe backpressure."""

    def __init__(self, event_bus=None, *, health=None):
        #: Plan 35 CR6. ``health(argv)`` -> the WSL health monitor for a job's
        #: command line, or ``None``; see ``wsl_health.WslHealth.for_argv``.
        self._health = health
        self._jobs: dict[str, asyncio.subprocess.Process] = {}
        self._requests: dict[str, JobRequest] = {}
        self._results: OrderedDict[str, JobResult] = OrderedDict()
        self._mutating_job: str | None = None
        self._cancelled: set[str] = set()
        self._cancelling: set[str] = set()
        self._state_callbacks = set()
        self._read_barriers = set()
        self._starting_mutations = 0
        self._events = event_bus
        self._transports: dict[str, _Transport] = {}

    def _publish(self, event, **payload):
        if self._events is not None:
            self._events.publish(event, **payload)

    @property
    def has_mutating_job(self) -> bool:
        return self._mutating_job is not None

    @property
    def mesh_write_pending(self) -> bool:
        """A mutating job is running, or waiting out reads to start (DP-485)."""
        return self._mutating_job is not None or self._starting_mutations > 0

    @property
    def active_job_ids(self) -> tuple[str, ...]:
        return tuple(self._jobs)

    @property
    def has_cancelling_job(self) -> bool:
        return bool(self._cancelling)

    def result(self, job_id: str) -> JobResult | None:
        """Return a completed result; active jobs deliberately have no result yet."""
        return self._results.get(job_id)

    def record_result(self, result: JobResult) -> None:
        """Let operation-level parsing/validation refine the terminal result."""
        if result.job_id not in self._results:
            raise KeyError(f'job result is not managed here: {result.job_id}')
        self._retain(result)
        if result.cwd:
            self._persist_result(Path(result.cwd), result, revision=True)

    def _retain(self, result: JobResult) -> None:
        """Keep a finished result with a bounded tail of its output (CR4).

        The caller of :meth:`run` gets the output it asked for through
        ``max_output_bytes``; what stays behind in the manager is the last
        256 KiB of it plus the log path, for at most the last 64 jobs.
        """
        output = result.output
        if len(output) > RETAINED_RESULT_BYTES // 4:
            encoded = output.encode('utf-8', errors='replace')
            if len(encoded) > RETAINED_RESULT_BYTES:
                result = replace(
                    result, output=encoded[-RETAINED_RESULT_BYTES:].decode(
                        'utf-8', errors='ignore'),
                    output_truncated=True)
        self._results[result.job_id] = result
        self._results.move_to_end(result.job_id)
        while len(self._results) > RETAINED_RESULTS:
            self._results.popitem(last=False)

    @staticmethod
    def persisted_results(case_path: str | Path, limit: int = 200) -> tuple[dict, ...]:
        path = Path(case_path) / 'foammesh' / 'logs' / 'jobs.jsonl'
        if not path.is_file():
            return ()
        records = []
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        bounded_limit = max(0, int(limit))
        return tuple(records[-bounded_limit:]) if bounded_limit else ()

    def subscribe_state(self, callback):
        """Notify shell adapters only when aggregate job state changes."""
        self._state_callbacks.add(callback)

        def unsubscribe():
            self._state_callbacks.discard(callback)
        return unsubscribe

    def add_read_barrier(self, barrier):
        """Hold every mutating job until ``barrier()`` has been awaited.

        DP-485. The viewport reads the case's polyMesh -- twelve processor
        directories of it on a parallel run -- and a mutating job rewrites
        exactly those files. MEASURED on `centrifugal_impeller`: the layers
        button commits its settings and starts snappy in one press; the
        commit schedules a viewport reload of the snap mesh, the reader was
        still inside `processor7..11/constant/polyMesh/faces` when addLayers
        rewrote them, VTK hit "Unexpected EOF" and the process died rc=139.
        A reader registers here so a writer waits for the read in flight.
        """
        self._read_barriers.add(barrier)

        def remove():
            self._read_barriers.discard(barrier)
        return remove

    async def _wait_for_readers(self):
        for barrier in tuple(self._read_barriers):
            try:
                await barrier()
            except Exception:       # noqa: BLE001 - a failed read holds nothing
                logger.exception('read barrier failed')

    def _notify_state(self):
        for callback in tuple(self._state_callbacks):
            callback()

    async def run(self, request: JobRequest, *, on_line=None) -> JobResult:
        if not request.argv:
            raise ValueError('job argv must not be empty')
        if request.mutation and self.mesh_write_pending:
            raise RuntimeError('a mutating job is already active for this case')
        if request.timeout is not None and request.timeout <= 0:
            raise ValueError('job timeout must be positive')
        if request.max_output_bytes < 0:
            raise ValueError('max_output_bytes must not be negative')
        monitor = self._health_monitor(request.argv)
        if monitor is not None:
            # Plan 35 CR6 step 1: probe before every run, off the GUI thread,
            # unless the runtime answered a moment ago.
            from .wsl_health import UNREACHABLE
            if await monitor.before_run() == UNREACHABLE:
                reason = (f'the WSL runtime {monitor.distribution} is not reachable'
                          + (f': {monitor.reason}' if monitor.reason else ''))
                return self.record_refusal(
                    request, f'transport-error: {reason}',
                    category=JobErrorCategory.TRANSPORT,
                    exit={'kind': 'transport', 'reason': reason, 'signal': '',
                          'actions': ['retry'], 'hint': ''})

        # DP-485. Counted as mutating from here, so no viewport read can
        # start while this job waits out the reads already in flight.
        starting = bool(request.mutation)
        if starting:
            self._starting_mutations += 1
        records = None
        try:
            if starting:
                await self._wait_for_readers()
            job_id = uuid4().hex
            started_at = _now()
            environment = os.environ.copy()
            if request.environment:
                environment.update(request.environment)
            environment_id = environment_fingerprint(environment)
            safe_argv = redact_argv(request.argv)
            wrapped = is_wrapped(request.argv)
            if wrapped:
                # Plan 35 CR5 step 5. The run id -- and where the Linux mirror
                # of the run record goes -- crosses into WSL through WSLENV;
                # its presence is what arms the wrapper's lifetime watcher.
                environment[RUN_ID_VARIABLE] = job_id
                shared = [RUN_ID_VARIABLE + '/u']
                if request.record_case is not None:
                    from .run_records import run_directory
                    environment[RUN_DIR_VARIABLE] = str(run_directory(request.record_case))
                    shared.append(RUN_DIR_VARIABLE + '/pu')
                kept = [item for item in environment.get('WSLENV', '').split(':')
                        if item and item.split('/')[0] not in (RUN_ID_VARIABLE, RUN_DIR_VARIABLE)]
                environment['WSLENV'] = ':'.join(kept + shared)
            else:
                # Only a wrapped run may carry a run id: the orphan sweep
                # finds a run's processes by it.
                environment.pop(RUN_ID_VARIABLE, None)
            if request.log_path:
                request.log_path.parent.mkdir(parents=True, exist_ok=True)
            launch_error = None
            if request.record_case is not None:
                # Written and fsynced before anything is spawned (step 4).
                from .run_records import RunRecordStore, wsl_target
                try:
                    records = RunRecordStore(request.record_case)
                    records.create(
                        job_id, name=request.name, job_kind=job_kind(request.argv),
                        wrapped=wrapped, wsl=wsl_target(request.argv),
                        cwd=str(request.cwd),
                        log_path=str(request.log_path) if request.log_path else None,
                        **dict(request.record_fields or {}))
                except OSError as error:
                    records = None
                    launch_error = f'the run record could not be written: {error}'
            process = None
            if launch_error is None:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *request.argv, cwd=request.cwd, env=environment,
                        stdin=asyncio.subprocess.PIPE if wrapped else None,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        limit=LINE_LIMIT_BYTES,
                        **new_process_group_kwargs())
                except OSError as error:
                    launch_error = str(error)
            if process is None:
                run_state = None
                if records is not None:
                    self._record(records, job_id, 'closed', resolution='never-launched',
                                 reason=launch_error)
                    run_state = 'closed'
                result = JobResult(
                    job_id, request.name, JobStatus.FAILED, None, '', request.log_path,
                    launch_error, JobErrorCategory.LAUNCH, started_at=started_at,
                    finished_at=_now(), argv=safe_argv, cwd=str(request.cwd),
                    environment_fingerprint=environment_id, mutation=request.mutation,
                    **({'exit': {'kind': 'launch',
                                 'reason': f'could not be started: {launch_error}',
                                 'signal': '', 'actions': ['retry'], 'hint': ''},
                        'transport': 'not-started', 'run_id': job_id,
                        'run_state': run_state}
                       if (wrapped or records is not None) else {}))
                self._retain(result)
                self._persist_result(request.cwd, result)
                self._publish(Event.JOB_FAILED, job_id=job_id, result=result.to_dict())
                return result

        finally:
            if starting:
                self._starting_mutations -= 1
        if records is not None:
            host = {'host_pid': process.pid}
            try:
                import psutil
                host['host_create_time'] = psutil.Process(process.pid).create_time()
            except Exception:  # noqa: BLE001 - the pid alone still helps
                pass
            # A wrapped run becomes `running` on the wrapper's FOAMMESH_RUN line.
            self._record(records, job_id, None if wrapped else 'running', **host)
        self._jobs[job_id] = process
        self._requests[job_id] = request
        if request.mutation:
            self._mutating_job = job_id
        self._notify_state()
        self._publish(
            Event.JOB_STARTED, job_id=job_id, name=request.name,
            argv=list(safe_argv), cwd=str(request.cwd), mutation=request.mutation,
            environment_fingerprint=environment_id,
            # CR4. The log is the record of the output; say where it is.
            log_path=str(request.log_path) if request.log_path else None)
        retained = bytearray()
        output_truncated = False
        transport = _Transport(asyncio.get_running_loop(), request.argv, request.idle_timeout)
        self._transports[job_id] = transport
        log_file = request.log_path.open('w', encoding='utf-8', newline='\n') if request.log_path else None
        watchdog = None
        transport_watch = None
        if monitor is not None:
            monitor.job_started()
        try:
            assert process.stdout is not None
            reader = _LineReader(process.stdout)
            # CR4 (F4). Lines are published in batches -- every 100 ms or
            # 500 lines -- so a chatty stage costs the GUI thread one event,
            # one console append and one progress parse per batch rather than
            # per line. `on_line` still sees every line.
            batch: list[str] = []
            batch_started = 0.0
            clock = asyncio.get_running_loop().time

            def publish_batch():
                nonlocal batch
                if not batch:
                    return
                lines, batch = batch, []
                self._publish(Event.JOB_OUTPUT, job_id=job_id,
                              line='\n'.join(lines), lines=lines)
                for text in reversed(lines):
                    progress = _parse_progress(text) if '%' in text else None
                    if progress is not None:
                        self._publish(
                            Event.JOB_PROGRESS, job_id=job_id, progress=progress)
                        break

            async def publish_on_time():
                # A stage that prints a line and then goes quiet is shown that
                # line within a batch period, not at its next line.
                while True:
                    await asyncio.sleep(OUTPUT_BATCH_SECONDS)
                    publish_batch()

            async def read_output():
                ticker = asyncio.ensure_future(publish_on_time())
                try:
                    await read_lines()
                finally:
                    ticker.cancel()
                    publish_batch()

            async def read_lines():
                nonlocal output_truncated, batch_started
                while line := await reader.readline():
                    decoded = decode_output(line).replace('\r\n', '\n')
                    kind, decoded = transport.observe(decoded)
                    if monitor is not None:
                        monitor.heartbeat()
                    if kind != 'output':
                        # CR5: the wrapper's control lines go to the log only,
                        # its heartbeat not even there.
                        if kind == 'control':
                            if log_file:
                                log_file.write(decoded)
                            self._on_control(records, job_id, transport)
                        continue
                    if transport.idle_announced:
                        self._clear_idle(job_id, transport, 'resumed')
                    encoded = decoded.encode('utf-8', errors='replace')
                    if request.max_output_bytes:
                        retained.extend(encoded)
                        overflow = len(retained) - request.max_output_bytes
                        if overflow > 0:
                            del retained[:overflow]
                            output_truncated = True
                    elif encoded:
                        output_truncated = True
                    if log_file:
                        log_file.write(decoded)
                    text = decoded.rstrip('\r\n')
                    if on_line:
                        callback_result = on_line(text)
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    if not batch:
                        batch_started = clock()
                    batch.append(text)
                    if (len(batch) >= OUTPUT_BATCH_LINES
                            or clock() - batch_started >= OUTPUT_BATCH_SECONDS):
                        publish_batch()

            watchdog = asyncio.create_task(self._watch_idle(job_id, request, transport))
            if monitor is not None and wrapped:
                transport_watch = asyncio.create_task(
                    self._watch_transport(job_id, request, transport, monitor))
            timed_out = False
            transport_error = None
            try:
                if request.timeout is None:
                    await read_output()
                else:
                    await asyncio.wait_for(read_output(), request.timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await self._kill_tree(process)
                await self._run_cleanup(request)
            except asyncio.CancelledError:
                await self._kill_tree(process)
                await self._run_cleanup(request)
                raise
            except Exception as error:  # noqa: BLE001 - plan 35 CR5 step 2
                # Whatever broke the stream, the job can no longer be watched:
                # kill it rather than leave a writer nobody is reading.
                transport_error = type(error).__name__
                logger.warning('job %s output stream failed: %r', job_id, error)
                await self._kill_tree(process)
                await self._run_cleanup(request)
            if log_file:
                log_file.flush()
                os.fsync(log_file.fileno())
            wait_timed_out = False
            try:
                returncode = await asyncio.wait_for(process.wait(), WAIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                wait_timed_out = True
                await self._kill_tree(process)
                await self._run_cleanup(request)
                try:
                    returncode = await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    returncode = process.returncode
            output = retained.decode('utf-8', errors='replace')
            cancelled = job_id in self._cancelled
            acknowledged = transport.exit_rc is not None
            if transport_error:
                transport_state = f'error:{transport_error}'
            elif timed_out or cancelled or wait_timed_out:
                transport_state = 'killed'
            elif wrapped and not acknowledged and returncode not in (0, None):
                transport_state = 'lost'
            else:
                transport_state = 'ok'
            if monitor is not None:
                self._after_transport(monitor, transport_state)
            from foammesh.core.run_result import decode_exit
            decoded_exit = decode_exit(
                transport.exit_rc if acknowledged else returncode,
                transport_lost=transport_state == 'lost',
                log_tail='\n'.join(transport.tail)).to_dict()
            if timed_out:
                status = JobStatus.TIMED_OUT
                # Plan 37 #7. Say which setting stopped it and how to lift it.
                error = (f'operation exceeded its {request.timeout:g} second '
                         'timeout; raise the limit, or set 0 for no limit, in '
                         'Preferences > OpenFOAM runtime > Stage time limit')
                category = JobErrorCategory.TIMEOUT
            elif cancelled:
                status = JobStatus.CANCELLED
                error = 'operation was cancelled'
                category = JobErrorCategory.CANCELLATION
            elif transport_error:
                status = JobStatus.FAILED
                error = f'transport-error: {transport_error}'
                category = JobErrorCategory.TRANSPORT
                decoded_exit = {'kind': 'transport', 'reason': error, 'signal': '',
                                'actions': ['retry'], 'hint': ''}
            elif wait_timed_out:
                status = JobStatus.FAILED
                error = (f'transport-error: the process did not exit within '
                         f'{WAIT_TIMEOUT_SECONDS} s of its output ending')
                category = JobErrorCategory.TRANSPORT
                decoded_exit = {'kind': 'transport', 'reason': error, 'signal': '',
                                'actions': ['retry'], 'hint': ''}
            elif returncode != 0:
                status = JobStatus.FAILED
                error = f'process exited with code {returncode}'
                category = JobErrorCategory.PROCESS
            else:
                status = JobStatus.DONE
                error = None
                category = None

            artifacts = []
            missing = []
            for expected in request.expected_outputs:
                path = expected if expected.is_absolute() else request.cwd / expected
                if path.exists():
                    artifacts.append(str(path))
                else:
                    missing.append(str(path))
            warnings = ()
            if status is JobStatus.DONE and missing:
                status = JobStatus.FAILED
                category = JobErrorCategory.VALIDATION
                error = 'expected output was not produced: ' + ', '.join(missing)
            if output_truncated:
                warnings = ('in-memory output was truncated; the full stream remains in the log',)
            if reader.truncated_lines:
                cut = reader.truncated_lines
                warnings = (*warnings,
                            f"{count_text(cut, 'overlong output line')} "
                            f"{agreeing(cut, 'was', 'were')} cut at "
                            f'{LINE_LIMIT_BYTES // 1024} KiB')
            run_state = None
            if records is not None:
                # `exited` needs the wrapper's acknowledgement, or a native
                # process's own exit; anything else may have left a writer.
                confirmed = (acknowledged if wrapped else True) or (
                    returncode == 0 and transport_state == 'ok')
                run_state = 'exited' if confirmed else 'recovery_pending'
                self._record(
                    records, job_id, run_state,
                    outcome='ok' if status is JobStatus.DONE else 'failed',
                    rc=returncode, linux_rc=transport.exit_rc,
                    transport=transport_state, reason=decoded_exit.get('reason'),
                    signal=decoded_exit.get('signal'), tail=list(transport.tail))
            idle = {'limit_seconds': transport.idle_limit,
                    'prompts': transport.idle_prompts,
                    'kept_waiting': transport.kept_waiting,
                    'idle_at_end': transport.idle_announced}
            identified = wrapped or records is not None
            result = JobResult(
                job_id, request.name, status, returncode, output, request.log_path,
                error, category, output_truncated, tuple(artifacts), started_at,
                _now(), warnings, safe_argv, str(request.cwd), environment_id,
                request.mutation, exit=decoded_exit, transport=transport_state,
                idle=idle, tail=tuple(transport.tail),
                run_id=job_id if identified else None,
                run_state=run_state, linux_rc=transport.exit_rc)
            self._retain(result)
            self._persist_result(request.cwd, result)
            event = {
                JobStatus.DONE: Event.JOB_FINISHED,
                JobStatus.CANCELLED: Event.JOB_CANCELLED,
                JobStatus.TIMED_OUT: Event.JOB_FAILED,
                JobStatus.FAILED: Event.JOB_FAILED,
            }[status]
            self._publish(event, job_id=job_id, result=result.to_dict())
            return result
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if transport_watch is not None:
                transport_watch.cancel()
            if monitor is not None:
                monitor.job_ended()
            if log_file:
                log_file.close()
            if getattr(process, 'stdin', None) is not None:
                # The lifetime pipe is let go only once the job has ended.
                try:
                    process.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            self._transports.pop(job_id, None)
            self._jobs.pop(job_id, None)
            self._requests.pop(job_id, None)
            self._cancelled.discard(job_id)
            self._cancelling.discard(job_id)
            if self._mutating_job == job_id:
                self._mutating_job = None
            self._notify_state()

    # -- plan 35 CR5 --------------------------------------------------------

    def record_refusal(self, request: JobRequest, error: str, *,
                       category: JobErrorCategory = JobErrorCategory.RECOVERY,
                       exit: dict | None = None) -> JobResult:
        """A job that was refused before launch, recorded like one that ran.

        Plan 35 CR5 step 6: the recovery gate refuses a run while an earlier
        one is unresolved; the refusal is a terminal job result, in
        ``jobs.jsonl`` and on the bus, so every surface reports it the same way.
        """
        job_id = uuid4().hex
        now = _now()
        result = JobResult(
            job_id, request.name, JobStatus.FAILED, None, '', None, error, category,
            started_at=now, finished_at=now, argv=redact_argv(request.argv),
            cwd=str(request.cwd), mutation=request.mutation,
            exit=exit or {'kind': 'refused', 'reason': error, 'signal': '',
                          'actions': [], 'hint': ''},
            transport='not-started')
        self._retain(result)
        self._persist_result(request.cwd, result)
        self._publish(Event.JOB_FAILED, job_id=job_id, result=result.to_dict())
        return result

    @staticmethod
    def _record(records, job_id: str, state, **fields) -> None:
        """Move a run record; a disk error leaves it where it was, never ends the job."""
        try:
            if state is None:
                records.update(job_id, **fields)
            else:
                records.transition(job_id, state, **fields)
        except Exception:  # noqa: BLE001 - the sweep treats a stale record as open
            logger.exception('run record %s could not move to %s', job_id, state)

    def _on_control(self, records, job_id: str, transport: '_Transport') -> None:
        if records is None or transport.run_line is None or transport.run_recorded:
            return
        transport.run_recorded = True
        self._record(records, job_id, 'running',
                     pgid=transport.run_line.get('pgid'), sid=transport.run_line.get('sid'))

    @staticmethod
    async def _kill_tree(process) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(kill_process_tree, process.pid), 30)
        except Exception:  # noqa: BLE001 - the cleanup argv is the second line
            logger.exception('could not kill job process %s', process.pid)

    async def _watch_idle(self, job_id: str, request: JobRequest, transport: '_Transport'):
        """Ask, never kill: publish ``JOB_IDLE`` once per silence (step 1)."""
        while True:
            limit = transport.idle_limit
            if not limit or limit <= 0:
                return
            await asyncio.sleep(max(0.05, min(5.0, limit / 4)))
            if transport.idle_announced:
                continue
            silent = transport.idle_seconds()
            if silent < transport.idle_limit:
                continue
            transport.idle_announced = True
            transport.idle_prompts += 1
            heartbeat = (transport.last_heartbeat is not None
                         and transport.loop.time() - transport.last_heartbeat < 10)
            self._publish(
                Event.JOB_IDLE, job_id=job_id, name=request.name, state='idle',
                idle_seconds=silent, limit_seconds=transport.idle_limit,
                kind=transport.kind, heartbeat=heartbeat,
                message=f'no output for {_minutes(silent)}',
                actions=['keep_waiting', 'stop'])

    # -- plan 35 CR6 --------------------------------------------------------

    def _health_monitor(self, argv):
        if self._health is None:
            return None
        try:
            return self._health(argv)
        except Exception:  # noqa: BLE001 - health is advice, never a reason not to run
            logger.exception('WSL health lookup failed')
            return None

    async def _watch_transport(self, job_id: str, request: JobRequest,
                               transport: '_Transport', monitor) -> None:
        """No heartbeat and no output for 10 s: one probe decides (CR6 step 1a).

        A silent mesher whose wrapper still heartbeats never gets here -- its
        lines keep ``last_seen`` fresh -- and meets the CR5 idle prompt
        instead. The job is never killed from here: Stop is the user's.
        """
        probed_for = None
        while True:
            armed = transport.last_heartbeat is not None or transport.run_line is not None
            limit = monitor.silence_seconds if armed else monitor.launch_grace_seconds
            silent = transport.loop.time() - transport.last_seen
            if silent < limit:
                # Armed, it wakes exactly at the deadline (re-measured, since a
                # line may have come meanwhile); unarmed, it watches for the
                # first control line so the shorter limit takes over at once.
                wait = limit - silent if armed else min(limit - silent, 0.5)
                await asyncio.sleep(max(0.01, wait))
                continue
            if probed_for == transport.last_seen:
                # Already decided for this silence; ask again one period on.
                await asyncio.sleep(monitor.silence_seconds)
            probed_for = transport.last_seen
            silent = transport.loop.time() - transport.last_seen
            state = await monitor.transport_silent(silent)
            if transport.last_seen != probed_for:
                continue  # it spoke while we asked: the heartbeat already said so
            self._publish(
                Event.JOB_TRANSPORT, job_id=job_id, name=request.name, state=state,
                silent_seconds=transport.loop.time() - transport.last_seen,
                distribution=monitor.distribution, reason=monitor.reason)

    @staticmethod
    def _after_transport(monitor, transport_state: str) -> None:
        """A job ended: a lost relay is a lost runtime until a probe says not."""
        if transport_state == 'lost':
            monitor.transport_lost('the connection to WSL was lost during a run')
        elif transport_state.startswith('error:'):
            try:
                asyncio.get_running_loop().create_task(monitor.probe('run'))
            except RuntimeError:
                pass

    def _clear_idle(self, job_id: str, transport: '_Transport', state: str) -> None:
        transport.idle_announced = False
        transport.last_output = transport.loop.time()
        self._publish(Event.JOB_IDLE, job_id=job_id, state=state, idle_seconds=0.0,
                      limit_seconds=transport.idle_limit)

    def keep_waiting(self, job_id: str) -> bool:
        """[Keep waiting]: restart the silence clock for one more interval."""
        transport = self._transports.get(job_id)
        if transport is None:
            return False
        transport.kept_waiting += 1
        self._clear_idle(job_id, transport, 'kept_waiting')
        return True

    def idle_state(self, job_id: str) -> dict | None:
        transport = self._transports.get(job_id)
        if transport is None:
            return None
        return {'idle': transport.idle_announced,
                'idle_seconds': transport.idle_seconds(),
                'limit_seconds': transport.idle_limit}

    async def cancel(self, job_id: str) -> bool:
        process = self._jobs.get(job_id)
        if process is None or process.returncode is not None:
            return False
        self._cancelled.add(job_id)
        self._cancelling.add(job_id)
        self._notify_state()
        self._publish(Event.JOB_CANCEL_REQUESTED, job_id=job_id)
        try:
            await asyncio.to_thread(kill_process_tree, process.pid)
            request = self._requests.get(job_id)
            if request is not None:
                await self._run_cleanup(request)
        except Exception:
            self._cancelled.discard(job_id)
            self._cancelling.discard(job_id)
            self._notify_state()
            raise
        return True

    async def cancel_all(self) -> int:
        results = await asyncio.gather(*(self.cancel(job_id) for job_id in tuple(self._jobs)))
        return sum(results)

    @staticmethod
    async def _run_cleanup(request: JobRequest) -> None:
        if not request.cleanup_argv:
            return
        try:
            process = await asyncio.create_subprocess_exec(
                *request.cleanup_argv,
                cwd=request.cwd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **new_process_group_kwargs(),
            )
            await asyncio.wait_for(process.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            # Cleanup is best-effort here. Operation-level recovery and the
            # orphan audit retain the terminal failure evidence.
            return

    @staticmethod
    def _persist_result(cwd: Path, result: JobResult, *,
                        revision: bool = False) -> None:
        """Append a durable, secret-redacted terminal record beside case logs."""
        try:
            path = Path(cwd) / 'foammesh' / 'logs' / 'jobs.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            document = {
                'schema_version': 1,
                'record_kind': 'refinement' if revision else 'terminal',
                **result.to_dict(),
            }
            with path.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(
                    document, sort_keys=True, separators=(',', ':')) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            # A read-only or disappearing cwd must not replace the real job result.
            return


_PERCENT_PROGRESS = re.compile(
    r'(?<![\d.])(?P<value>100(?:\.0+)?|[0-9]?\d(?:\.\d+)?)\s*%')


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f'{minutes} min' if minutes >= 1 else f'{int(seconds)} s'


def _parse_progress(line: str) -> dict | None:
    match = _PERCENT_PROGRESS.search(line)
    if match:
        value = float(match.group('value'))
        return {'fraction': value / 100.0, 'percent': value, 'line': line.strip()}
    return None
