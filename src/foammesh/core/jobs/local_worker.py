"""Run one mesh-worker process and say how it ended (Plan 35 CR2).

This is deliberately *not* :class:`foammesh.core.jobs.manager.JobManager`.
A worker job reads the case and writes only its own reports: it creates no
run record, is not held back by the recovery gate, and is not a mutating run
anybody needs to recover after a crash. What it shares with the job manager
is the process hygiene -- its own process group, a whole-tree kill on cancel
-- and on Windows it adds a Job Object that caps the worker's memory and
kills it with the window.

Every way a worker can end is an outcome, never an exception into the GUI:

``ok``           the result, as the in-process handler would have returned it
``failed``       the check ran and refused, with the handler's reason code
``over_budget``  the worker hit its memory cap (a ``MemoryError``, or killed
                 by the Job Object), reported as a budget refusal, not a crash
``crashed``      the process died without a result -- the exit code and the
                 tail of its log are the reason
``unavailable``  the worker could not be started at all; the check can be
                 retried and the mesh stays usable
``cancelled``    the user pressed Cancel
``timed_out``    the worker outlived its deadline and was stopped
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from foammesh.core.jobs.process_control import (
    kill_process_tree, new_process_group_kwargs,
)

logger = logging.getLogger(__name__)

OK = 'ok'
FAILED = 'failed'
OVER_BUDGET = 'over_budget'
CRASHED = 'crashed'
UNAVAILABLE = 'unavailable'
CANCELLED = 'cancelled'
TIMED_OUT = 'timed_out'

#: Windows STATUS_NO_MEMORY, as an unsigned and as a signed exit code.
_NO_MEMORY_CODES = {0xC0000017, 0xC0000017 - (1 << 32)}
#: A worker that died having committed this share of its cap was killed by
#: the cap -- a native allocation failure rarely leaves a Python MemoryError.
_CAP_KILL_SHARE = 0.8
#: The environment variable that makes a worker spawn fail (K7 "cannot
#: start"); a test override, like the budget ones in ``resource_budget``.
ENV_WORKER_EXECUTABLE = 'FOAMMESH_WORKER_EXECUTABLE'
LOG_TAIL_BYTES = 4096
#: How often a capped worker is looked at, and how long it may sit at its cap
#: without using the processor before it is stopped as over budget.
WATCH_INTERVAL = 0.5
WEDGED_SECONDS = 5.0


@dataclass
class WorkerOutcome:
    status: str
    operation: str
    payload: dict = field(default_factory=dict)
    reason: str = ''
    message: str = ''
    exit_code: int | None = None
    peak_bytes: int = 0
    cap_bytes: int = 0
    seconds: float = 0.0
    details: dict = field(default_factory=dict)
    error_class: str = ''

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_dict(self) -> dict:
        return {'status': self.status, 'operation': self.operation,
                'reason': self.reason, 'message': self.message,
                'exit_code': self.exit_code, 'peak_bytes': self.peak_bytes,
                'cap_bytes': self.cap_bytes, 'seconds': self.seconds,
                'details': dict(self.details)}


@dataclass
class _Active:
    token: int
    operation: str
    group: str
    process: object
    cancelled: bool = False


_active: dict[int, _Active] = {}
_tokens = itertools.count(1)


def active_workers(group: str | None = None) -> list[dict]:
    return [{'operation': item.operation, 'group': item.group,
             'pid': getattr(item.process, 'pid', None)}
            for item in list(_active.values())
            if group is None or item.group == group]


def cancel_all(group: str | None = None) -> int:
    """Kill every running worker (of ``group``); returns how many."""
    killed = 0
    for item in list(_active.values()):
        if group is not None and item.group != group:
            continue
        item.cancelled = True
        pid = getattr(item.process, 'pid', None)
        if pid:
            try:
                kill_process_tree(pid, timeout=2.0)
            except Exception:                               # noqa: BLE001
                logger.debug('could not kill worker %s', pid, exc_info=True)
        killed += 1
    return killed


def worker_command(operation: str, args_path: Path) -> list[str]:
    """How this build starts a worker."""
    override = os.environ.get(ENV_WORKER_EXECUTABLE)
    if override:
        return [override, operation, str(args_path)]
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--worker', operation, str(args_path)]
    return [sys.executable, '-m', 'foammesh.workers.mesh_worker',
            operation, str(args_path)]


def worker_environment() -> dict:
    """The parent's environment, with the source tree importable."""
    env = dict(os.environ)
    if not getattr(sys, 'frozen', False):
        import foammesh

        src = Path(foammesh.__file__).resolve().parent.parent
        paths = [str(src)]
        vendor = src.parent / 'vendor'
        if vendor.is_dir():
            paths.append(str(vendor))
        if env.get('PYTHONPATH'):
            paths.append(env['PYTHONPATH'])
        env['PYTHONPATH'] = os.pathsep.join(paths)
    env['FOAMMESH_WORKER'] = '1'
    # OpenBLAS commits a buffer per thread when numpy loads -- ~750 MB on a
    # many-core machine, before the check reads a byte -- so a worker under
    # its cap failed inside the loader and sat there. The checks do no BLAS
    # work worth threading.
    for name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                 'OMP_NUM_THREADS'):
        env[name] = '1'
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    return env


def _spawn_kwargs() -> dict:
    kwargs = new_process_group_kwargs()
    if sys.platform == 'win32':
        # A console-less GUI must not flash a console per check.
        kwargs['creationflags'] = (kwargs.get('creationflags', 0)
                                   | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return kwargs


def _log_tail(path: Path) -> str:
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            return handle.read().decode('utf-8', 'replace').strip()
    except OSError:
        return ''


def _read_document(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _load_values(payload: dict) -> dict:
    values_path = payload.pop('values_path', None)
    if not values_path:
        return payload
    import numpy as np

    with np.load(values_path) as archive:
        payload['values'] = {name: np.array(archive[name])
                             for name in archive.files}
    return payload


def _classify(operation: str, code: int | None, document: dict | None, *,
              cancelled: bool, peak: int, cap: int, log_path: Path,
              seconds: float) -> WorkerOutcome:
    common = dict(operation=operation, exit_code=code, peak_bytes=peak,
                  cap_bytes=cap, seconds=seconds)
    if cancelled:
        return WorkerOutcome(CANCELLED, reason='cancelled',
                             message='the check was cancelled', **common)
    if document is not None:
        status = document.get('status')
        if status == 'ok':
            return WorkerOutcome(OK, payload=dict(document.get('payload') or {}),
                                 **common)
        reason = str(document.get('reason') or 'worker_error')
        return WorkerOutcome(
            OVER_BUDGET if reason == 'over_budget' else FAILED,
            reason=reason, message=str(document.get('message') or reason),
            details=dict(document.get('details') or {}),
            error_class=str(document.get('error_class') or ''), **common)
    from foammesh.workers.mesh_worker import EXIT_OVER_BUDGET

    capped = cap and peak >= _CAP_KILL_SHARE * cap
    if code == EXIT_OVER_BUDGET or code in _NO_MEMORY_CODES or capped:
        from foammesh.support.resource_budget import format_bytes

        return WorkerOutcome(
            OVER_BUDGET, reason='over_budget',
            message=('the worker was stopped at its memory cap of {0} '
                     '(peak {1})'.format(format_bytes(cap), format_bytes(peak))),
            **common)
    tail = _log_tail(log_path)
    message = f'the worker ended with exit code {code} and no result'
    if tail:
        message += ': ' + tail.splitlines()[-1][:300]
    return WorkerOutcome(CRASHED, reason='worker_crashed', message=message,
                         details={'log_tail': tail}, **common)


async def run_worker(operation: str, args: dict, *, cap_bytes: int = 0,
                     timeout: float | None = None, group: str = '',
                     keep_files: bool = False) -> WorkerOutcome:
    """Run ``operation`` in a worker process and return how it ended."""
    started = time.monotonic()
    work = Path(await asyncio.to_thread(
        tempfile.mkdtemp, prefix='foammesh-worker-'))
    args_path, result_path, log_path = (
        work / 'args.json', work / 'result.json', work / 'worker.log')
    job = None
    process = None
    token = next(_tokens)
    try:
        body = dict(args, result_path=str(result_path), wait_for_go=True)
        await asyncio.to_thread(
            args_path.write_text, json.dumps(body), encoding='utf-8')
        log = open(log_path, 'wb')
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *worker_command(operation, args_path),
                    stdin=asyncio.subprocess.PIPE, stdout=log,
                    stderr=subprocess.STDOUT, env=worker_environment(),
                    cwd=str(work), **_spawn_kwargs())
            except (OSError, ValueError, NotImplementedError) as error:
                return WorkerOutcome(
                    UNAVAILABLE, operation, reason='worker_unavailable',
                    message=f'the check could not start its worker: {error}',
                    seconds=time.monotonic() - started)
        finally:
            log.close()
        entry = _Active(token, operation, group, process)
        _active[token] = entry
        job = _attach_job(process.pid, cap_bytes)
        try:
            process.stdin.write(b'go\n')
            await process.stdin.drain()
        except (OSError, ConnectionError):
            pass                  # it died already; the exit code says how
        code, ended = await _wait(process, job, cap_bytes, timeout)
        if ended == TIMED_OUT:
            return WorkerOutcome(
                TIMED_OUT, operation, reason='timed_out',
                message=f'the check did not finish within {timeout:.0f} s',
                exit_code=code, cap_bytes=cap_bytes,
                seconds=time.monotonic() - started)
        if ended == OVER_BUDGET:
            from foammesh.support.resource_budget import format_bytes

            return WorkerOutcome(
                OVER_BUDGET, operation, reason='over_budget',
                message=('the worker reached its memory cap of {0} and '
                         'stopped making progress'.format(
                             format_bytes(cap_bytes))),
                exit_code=code, cap_bytes=cap_bytes,
                peak_bytes=job.peak_process_memory() if job else 0,
                seconds=time.monotonic() - started)
        peak = job.peak_process_memory() if job is not None else 0
        document = await asyncio.to_thread(_read_document, result_path)
        outcome = _classify(
            operation, code, document, cancelled=entry.cancelled, peak=peak,
            cap=cap_bytes if job is not None else 0, log_path=log_path,
            seconds=round(time.monotonic() - started, 3))
        if outcome.ok and outcome.payload.get('values_path'):
            try:
                outcome.payload = await asyncio.to_thread(
                    _load_values, outcome.payload)
            except (OSError, ValueError, KeyError) as error:
                return WorkerOutcome(
                    FAILED, operation, reason='worker_error',
                    message=f'the worker wrote unreadable arrays: {error}',
                    exit_code=code)
        if outcome.status in (CRASHED, FAILED):
            logger.warning('mesh worker %s: %s (%s)', operation,
                           outcome.status, outcome.message)
        return outcome
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            await asyncio.shield(asyncio.to_thread(
                kill_process_tree, process.pid, 2.0))
        raise
    finally:
        _active.pop(token, None)
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:                               # noqa: BLE001
                pass
        if job is not None:
            job.close()
        if not keep_files:
            await asyncio.to_thread(shutil.rmtree, work, True)


async def _wait(process, job, cap_bytes: int, timeout: float | None):
    """Wait for the worker; stop it when it times out or wedges at its cap.

    Returns ``(exit code, None | TIMED_OUT | OVER_BUDGET)``. A worker whose
    committed memory reached its cap and whose processor time then stood
    still for ``WEDGED_SECONDS`` is stopped as over budget: a failed
    allocation inside the interpreter (a thread start, an import) can leave
    it waiting forever instead of raising ``MemoryError``.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + float(timeout)
    watching = job is not None and cap_bytes > 0
    last_cpu, still_since = None, None
    while True:
        wait = WATCH_INTERVAL if watching else None
        if deadline is not None:
            left = max(0.0, deadline - loop.time())
            wait = left if wait is None else min(wait, left)
        try:
            return await asyncio.wait_for(process.wait(), wait), None
        except asyncio.TimeoutError:
            pass
        if deadline is not None and loop.time() >= deadline:
            await asyncio.to_thread(kill_process_tree, process.pid, 2.0)
            return await process.wait(), TIMED_OUT
        if job.peak_process_memory() < _CAP_KILL_SHARE * cap_bytes:
            last_cpu, still_since = None, None
            continue
        cpu = job.cpu_seconds()
        now = loop.time()
        if last_cpu is None or cpu < 0 or cpu - last_cpu > 0.05:
            last_cpu, still_since = cpu, now
            continue
        if now - still_since >= WEDGED_SECONDS:
            await asyncio.to_thread(kill_process_tree, process.pid, 2.0)
            return await process.wait(), OVER_BUDGET


def _attach_job(pid: int, cap_bytes: int):
    """Cap the worker's memory, or run it uncapped and say so."""
    if not cap_bytes:
        return None
    from foammesh.support import resource_budget

    if not resource_budget.job_objects_supported():
        return None
    try:
        job = resource_budget.JobObject(cap_bytes)
    except resource_budget.JobObjectError as error:
        logger.warning('mesh worker runs without a memory cap: %s', error)
        return None
    try:
        job.assign(pid)
    except resource_budget.JobObjectError as error:
        logger.warning('mesh worker runs without a memory cap: %s', error)
        job.close()
        return None
    return job


# --------------------------------------------------------------------------- #
# Plan 35 CR7: a worker called from code that is not a coroutine
# --------------------------------------------------------------------------- #

def run_worker_sync(operation: str, args: dict, *, cap_bytes: int = 0,
                    timeout: float | None = None, group: str = '',
                    cancelled=None, poll: float = 0.2) -> WorkerOutcome:
    """:func:`run_worker`, for a caller that is a plain function.

    The store's CAD and export bodies are synchronous and already run off the
    owner loop (``asyncio.to_thread``); this gives them a worker without
    making every caller a coroutine. The worker runs on an event loop of its
    own, on a thread of its own, so a caller that happens to sit on a running
    loop is not handed a nested one. ``cancelled`` is polled; when it answers
    true the worker's tree is killed and the outcome is ``cancelled``.
    """
    import threading

    group = group or f'sync-{next(_tokens)}'
    box: dict = {}

    def target():
        try:
            box['outcome'] = asyncio.run(run_worker(
                operation, args, cap_bytes=cap_bytes, timeout=timeout,
                group=group))
        except BaseException as error:                      # noqa: BLE001
            box['error'] = error

    thread = threading.Thread(target=target, name=f'worker:{operation}',
                              daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(poll)
        if thread.is_alive() and cancelled is not None:
            try:
                stop = bool(cancelled())
            except Exception:                               # noqa: BLE001
                stop = False
            if stop:
                cancel_all(group)
    if 'error' in box:
        raise box['error']
    return box['outcome']
