"""DP-2762/2763. What the window said: the console, the status line, a job's log.

A person at the window reads the Console panel, the status bar, the notices
over the viewport and the run status strip, and opens a job's log. The local
API reads the same text. The console and the status line belong to a window,
so the headless host answers ``attached: false`` with the reason; a job's log
is a file in the case, so both hosts answer it the same way.

Qt-free: the window's renderer does the reading.
"""
from __future__ import annotations

import re
from pathlib import Path

from .errors import FacadeError, SecurityPolicyError, ValidationFailedError

#: The console's level filter, as its level box offers it.
CONSOLE_LEVELS = ('info', 'warning', 'error')
#: The most lines one console page or log tail hands back.
MAX_LINES = 5000
#: The most bytes of a log one call reads.
MAX_LOG_BYTES = 1_000_000

NO_WINDOW = 'no desktop window is attached; the console and the status line ' \
            'belong to the window'


class JobNotFoundError(FacadeError):
    code = 'job_not_found'


def _int(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValidationFailedError(f'{name} must be a whole number')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationFailedError(f'{name} must be a whole number',
                                    details={name: value}) from None
    if not low <= number <= high:
        raise ValidationFailedError(f'{name} is out of range',
                                    details={name: number, 'range': [low, high]})
    return number


def console(session, *, after_line=0, limit=500, min_level='info') -> dict:
    """The Console panel's lines after ``after_line`` (1-based line numbers)."""
    after_line = _int(after_line, 'after_line', 0, 1 << 62)
    limit = _int(limit, 'limit', 1, MAX_LINES)
    if min_level not in CONSOLE_LEVELS:
        raise ValidationFailedError('unknown console level', details={
            'min_level': min_level, 'allowed': list(CONSOLE_LEVELS)})
    reader = getattr(session.presentation_renderer, 'console', None)
    if session.presentation is None or not callable(reader):
        return {'attached': False, 'lines': [], 'next_line': after_line,
                'reason': NO_WINDOW}
    return reader(after_line=after_line, limit=limit, min_level=min_level)


def status(session, *, after=0) -> dict:
    """The status line now, the messages it showed after ``after``, the
    notices over the viewport and the run status strip."""
    after = _int(after, 'after', 0, 1 << 62)
    reader = getattr(session.presentation_renderer, 'status', None)
    if session.presentation is None or not callable(reader):
        return {'attached': False, 'status_line': '', 'messages': [],
                'next_sequence': after, 'notices': [], 'run_status': None,
                'reason': NO_WINDOW}
    return reader(after=after)


def _job_log_path(session, job_id: str):
    jobs = session.jobs
    result = jobs.result(job_id)
    if result is not None:
        return getattr(result, 'log_path', None), True
    request = getattr(jobs, '_requests', {}).get(job_id)
    if request is not None:
        return getattr(request, 'log_path', None), True
    for record in reversed(jobs.persisted_results(session.case_path)):
        if record.get('job_id') == job_id:
            return record.get('log_path'), True
    # DP-2856. A mesh run's id reads the run's own log: a snappy run's
    # ``run.log`` (every stage in it), a Gmsh run's ``runner.log``.
    run = run_record(session, job_id)
    if run is not None:
        return run.get('log_path'), True
    return None, False


#: What a run id may look like; anything else is never a directory name.
_RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


def run_record(session, run_id: str) -> dict | None:
    """DP-2855/2856. A mesh run, by its run id, as the run listing names it.

    ``None`` when the case has no run of that id.
    """
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id) or '..' in run_id:
        return None
    from foammesh.core.jobs.run_listing import run_row
    return run_row(session.case_path, run_id)


def job_record(session, job_id: str) -> dict | None:
    """DP-2856. A job the case knows, by job id or by a mesh run's id.

    A job the job manager ran (finished, or on its record), else a mesh run
    (``kind: run``) with its log. ``None`` when there is no such job.
    """
    jobs = session.jobs
    if job_id in tuple(getattr(jobs, 'active_job_ids', ()) or ()):
        return {'job_id': job_id, 'status': 'running'}
    result = jobs.result(job_id)
    if result is not None:
        return result.to_dict()
    for record in reversed(jobs.persisted_results(session.case_path)):
        if record.get('job_id') == job_id:
            return dict(record)
    return run_record(session, job_id)


def job_log(session, job_id: str, *, tail_lines=200, offset=None) -> dict:
    """The end of a job's log, or the bytes after ``offset`` (to follow it)."""
    tail_lines = _int(tail_lines, 'tail_lines', 0, MAX_LINES)
    if offset is not None:
        offset = _int(offset, 'offset', 0, 1 << 62)
    path, known = _job_log_path(session, job_id)
    if not known:
        raise JobNotFoundError('no such job in this case', details={'job_id': job_id})
    if not path:
        return {'job_id': job_id, 'path': None, 'lines': [], 'size': 0,
                'next_offset': 0, 'reason': 'the job writes no log'}
    path = Path(path)
    if not path.is_absolute():
        path = Path(session.case_path) / path
    resolved, case = path.resolve(), Path(session.case_path).resolve()
    if case not in resolved.parents:
        raise SecurityPolicyError('the job log is outside the case',
                                  details={'job_id': job_id})
    if not resolved.is_file():
        return {'job_id': job_id, 'path': str(resolved), 'lines': [], 'size': 0,
                'next_offset': 0, 'reason': 'the log has not been written yet'}
    return {'job_id': job_id, **read_log_tail(
        resolved, tail_lines=tail_lines, offset=offset)}


def read_log_tail(path, *, tail_lines: int = 200, offset: int | None = None,
                  max_bytes: int = MAX_LOG_BYTES) -> dict:
    """The last ``tail_lines`` lines of ``path``, or its bytes after ``offset``."""
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as handle:
        if offset is not None:
            start = min(offset, size)
            handle.seek(start)
            data = handle.read(max_bytes)
            return {'path': str(path), 'size': size, 'offset': start,
                    'next_offset': start + len(data),
                    'text': data.decode('utf-8', errors='replace'),
                    'more': start + len(data) < size}
        start = max(size - max_bytes, 0)
        handle.seek(start)
        text = handle.read().decode('utf-8', errors='replace')
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]          # the byte window cut the first line
    lines = lines[-tail_lines:] if tail_lines else []
    return {'path': str(path), 'size': size, 'next_offset': size,
            'lines': lines, 'truncated': start > 0}
