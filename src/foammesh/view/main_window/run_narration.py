"""What a mesh run is doing, said without naming a processor directory.

Plan 31 CP-07 item 6, second half. Everything a user needed to answer "is this
running in parallel, on how many workers, where is the log, and what happened
when I cancelled it" was already in the run payload -- ``allocation`` carries
the rank count and the backend, every execution carries its ``log_path``, and
the cancel operation reports how many jobs it stopped. None of it reached the
screen. The status strip said ``Run snappy-2026-09-05 completed.`` whether the
run had used one worker or thirty-two, and a cancelled run said nothing at all
beyond the button greying out.

The only place the split was visible was on disk, as ``processor0/``,
``processor1/`` ... -- which is exactly the internal a user should not have to
learn to know whether their four cores were used.

These functions are deliberately free of Qt so the sentences can be tested as
sentences. They never invent a number: a payload that does not say how many
workers ran produces a sentence that does not claim one.
"""
from __future__ import annotations

from pathlib import Path

#: Backends that mean "more than this machine's own process". A run on one
#: rank is serial however it was launched, so the rank count decides first.
_DISTRIBUTED = frozenset({'openfoam-mpi', 'mpi', 'slurm'})


def worker_count(allocation) -> int:
    """How many workers a run had, or 0 when the payload does not say.

    Zero is not one. A run whose allocation never reached the payload is a
    run we cannot describe, and saying "1 worker" there would be a claim
    about the machine made up by the view.
    """
    if not isinstance(allocation, dict):
        return 0
    for key in ('effective_ranks', 'ranks', 'cpu_ranks'):
        value = allocation.get(key)
        if isinstance(value, bool):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return count
    return 0


def is_parallel(allocation) -> bool:
    """Whether this run really is split across workers."""
    return worker_count(allocation) > 1


def describe_workers(allocation) -> str:
    """The clause that says serial or parallel, and on how many workers.

    Empty when the payload does not say, so a caller can join it to a
    sentence without producing "on 0 workers".
    """
    count = worker_count(allocation)
    if count <= 0:
        return ''
    if count == 1:
        return 'on a single worker'
    backend = str((allocation or {}).get('backend_id') or '')
    where = ' across this machine' if backend in _DISTRIBUTED else ''
    return f'in parallel on {count} workers{where}'


def describe_start(allocation) -> str:
    """What to say while a run is under way."""
    clause = describe_workers(allocation)
    if not clause:
        return 'Meshing.'
    return f'Meshing {clause}.'


def newest_log(payload) -> str:
    """The log of the last execution that has one, as a plain path.

    The last one is the interesting one: a run that failed failed there, and
    a run that finished ended there. Executions without a log are skipped
    rather than reported as an empty path.
    """
    if not isinstance(payload, dict):
        return ''
    for execution in reversed(list(payload.get('executions') or ())):
        if not isinstance(execution, dict):
            continue
        log = str(execution.get('log_path') or '').strip()
        if log:
            return log
    log = str(payload.get('log') or '').strip()
    return log


def describe_result(payload, *, failed: bool = False) -> str:
    """One sentence for a run that has stopped, whichever way it stopped."""
    payload = payload if isinstance(payload, dict) else {}
    allocation = payload.get('allocation')
    clause = describe_workers(allocation)
    run_id = str(payload.get('run_id') or '').strip()
    named = f'Run {run_id}' if run_id else 'The run'
    if failed:
        node = str(payload.get('failed_node') or '').strip()
        at = f' at {node}' if node else ''
        tail = f', {clause}' if clause else ''
        return f'{named} failed{at}{tail}.'
    tail = f' {clause}' if clause else ''
    return f'{named} finished, having meshed{tail}.'


def describe_cancellation(payload, allocation=None) -> str:
    """What a cancelled run left behind, in the terms the user asked in.

    The plan's acceptance for a cancelled parallel run is that no worker of
    that job survives it and no partial reconstruction is accepted. Both are
    true of this product -- the job manager kills the process group, and the
    mesh state is published only after the last node succeeds -- but neither
    was ever said, so a user watching a parallel run stop had no way to know
    whether half a mesh had just been adopted as the case mesh.
    """
    payload = payload if isinstance(payload, dict) else {}
    stopped = payload.get('cancelled')
    try:
        stopped = int(stopped)
    except (TypeError, ValueError):
        stopped = 0
    clause = describe_workers(allocation)
    if stopped <= 0:
        return 'Nothing was running, so nothing was stopped.'
    what = 'job' if stopped == 1 else 'jobs'
    where = f' running {clause}' if clause else ''
    return (f'Cancelled: {stopped} {what}{where} stopped. '
            'The case mesh is unchanged - a run only becomes the case mesh '
            'when it finishes.')


def log_label(log: str) -> str:
    """The button text for a log, naming the file rather than the path."""
    name = Path(log).name if log else ''
    return f'Show log ({name})' if name else 'Show log'
