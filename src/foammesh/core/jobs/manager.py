"""Async, streaming job execution with one mutating operation per case."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from .job import JobStatus
from .process_control import kill_process_tree, new_process_group_kwargs
from foammesh.core.project.events import Event


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


class JobErrorCategory(str, Enum):
    LAUNCH = 'launch'
    PROCESS = 'process'
    TIMEOUT = 'timeout'
    CANCELLATION = 'cancellation'
    VALIDATION = 'validation'
    PARSE = 'parse'
    RECOVERY = 'recovery'


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

    def __init__(self, event_bus=None):
        self._jobs: dict[str, asyncio.subprocess.Process] = {}
        self._requests: dict[str, JobRequest] = {}
        self._results: dict[str, JobResult] = {}
        self._mutating_job: str | None = None
        self._cancelled: set[str] = set()
        self._cancelling: set[str] = set()
        self._state_callbacks = set()
        self._events = event_bus

    def _publish(self, event, **payload):
        if self._events is not None:
            self._events.publish(event, **payload)

    @property
    def has_mutating_job(self) -> bool:
        return self._mutating_job is not None

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
        self._results[result.job_id] = result
        if result.cwd:
            self._persist_result(Path(result.cwd), result, revision=True)

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

    def _notify_state(self):
        for callback in tuple(self._state_callbacks):
            callback()

    async def run(self, request: JobRequest, *, on_line=None) -> JobResult:
        if not request.argv:
            raise ValueError('job argv must not be empty')
        if request.mutation and self._mutating_job is not None:
            raise RuntimeError('a mutating job is already active for this case')
        if request.timeout is not None and request.timeout <= 0:
            raise ValueError('job timeout must be positive')
        if request.max_output_bytes < 0:
            raise ValueError('max_output_bytes must not be negative')

        job_id = uuid4().hex
        started_at = _now()
        environment = os.environ.copy()
        if request.environment:
            environment.update(request.environment)
        environment_id = environment_fingerprint(environment)
        safe_argv = redact_argv(request.argv)
        if request.log_path:
            request.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv, cwd=request.cwd, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                **new_process_group_kwargs())
        except OSError as error:
            result = JobResult(
                job_id, request.name, JobStatus.FAILED, None, '', request.log_path,
                str(error), JobErrorCategory.LAUNCH, started_at=started_at,
                finished_at=_now(), argv=safe_argv, cwd=str(request.cwd),
                environment_fingerprint=environment_id, mutation=request.mutation)
            self._results[job_id] = result
            self._persist_result(request.cwd, result)
            self._publish(Event.JOB_FAILED, job_id=job_id, result=result.to_dict())
            return result

        self._jobs[job_id] = process
        self._requests[job_id] = request
        if request.mutation:
            self._mutating_job = job_id
        self._notify_state()
        self._publish(
            Event.JOB_STARTED, job_id=job_id, name=request.name,
            argv=list(safe_argv), cwd=str(request.cwd), mutation=request.mutation,
            environment_fingerprint=environment_id)
        retained = bytearray()
        output_truncated = False
        log_file = request.log_path.open('w', encoding='utf-8', newline='\n') if request.log_path else None
        try:
            assert process.stdout is not None
            async def read_output():
                nonlocal output_truncated
                while line := await process.stdout.readline():
                    decoded = decode_output(line).replace('\r\n', '\n')
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
                    if on_line:
                        callback_result = on_line(decoded.rstrip('\r\n'))
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    self._publish(Event.JOB_OUTPUT, job_id=job_id, line=decoded.rstrip('\r\n'))
                    progress = _parse_progress(decoded)
                    if progress is not None:
                        self._publish(
                            Event.JOB_PROGRESS, job_id=job_id, progress=progress)

            timed_out = False
            try:
                if request.timeout is None:
                    await read_output()
                else:
                    await asyncio.wait_for(read_output(), request.timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await asyncio.to_thread(kill_process_tree, process.pid)
                await self._run_cleanup(request)
            if log_file:
                log_file.flush()
                os.fsync(log_file.fileno())
            returncode = await process.wait()
            output = retained.decode('utf-8', errors='replace')
            if timed_out:
                status = JobStatus.TIMED_OUT
                error = f'operation exceeded its {request.timeout:g} second timeout'
                category = JobErrorCategory.TIMEOUT
            elif job_id in self._cancelled:
                status = JobStatus.CANCELLED
                error = 'operation was cancelled'
                category = JobErrorCategory.CANCELLATION
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
            result = JobResult(
                job_id, request.name, status, returncode, output, request.log_path,
                error, category, output_truncated, tuple(artifacts), started_at,
                _now(), warnings, safe_argv, str(request.cwd), environment_id,
                request.mutation)
            self._results[job_id] = result
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
            if log_file:
                log_file.close()
            self._jobs.pop(job_id, None)
            self._requests.pop(job_id, None)
            self._cancelled.discard(job_id)
            self._cancelling.discard(job_id)
            if self._mutating_job == job_id:
                self._mutating_job = None
            self._notify_state()

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


def _parse_progress(line: str) -> dict | None:
    match = _PERCENT_PROGRESS.search(line)
    if match:
        value = float(match.group('value'))
        return {'fraction': value / 100.0, 'percent': value, 'line': line.strip()}
    return None
