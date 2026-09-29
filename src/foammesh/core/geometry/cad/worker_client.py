"""OCCT runs in the mesh worker, never in the window (Plan 35 CR7).

pythonocc is a native kernel, and a malformed STEP can take it down with an
access violation that no ``except`` sees. Everything that reads, heals,
measures or re-facets a B-Rep therefore runs in a worker process
(:mod:`foammesh.workers.cad_ops`), and this module is the window's side of
that: it starts the worker, waits for it off the owner loop, and turns every
way it can end into the exception the in-process body would have raised --
or, when the worker died, into a message that says the CAD reader crashed.

Inside a worker (``FOAMMESH_WORKER=1``) the same entry points run the bodies
in-process: that *is* the worker.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

#: What a crashed reader tells the user. The window stays; the import fails.
CRASH_MESSAGE = 'import failed (the CAD reader crashed)'

#: No CAD read is allowed to hold a worker for longer than this.
DEFAULT_TIMEOUT_SECONDS = 1800.0


class CadWorkerError(ValueError):
    """The CAD worker did not produce an answer. ``reason`` says why."""

    def __init__(self, message: str, *, reason: str = 'worker_error',
                 details: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = dict(details or {})


class CadReaderCrashed(CadWorkerError):
    """The OCCT reader took its process down; the window did not go with it."""


class CadWorkerCancelled(CadWorkerError):
    """The user stopped the CAD worker."""


def in_worker() -> bool:
    """True inside a mesh worker, where OCCT is allowed to load."""
    return os.environ.get('FOAMMESH_WORKER') == '1'


def _source_bytes(path) -> int:
    try:
        return int(Path(str(path)).stat().st_size) if path else 0
    except OSError:
        return 0


def _snapshot(budget):
    """The memory budget, without walking every process for the WSL VM.

    :func:`resource_budget.snapshot` names every process to find ``vmmem``,
    and MEASURED on the development machine (785 processes) that is 4.8 s --
    longer than most CAD reads. A CAD worker takes the VM's size from the
    last measurement if one is fresh and does not wait for a new one.
    """
    import os

    if os.environ.get(budget.ENV_BUDGET):
        return budget.snapshot()
    total, available = budget.physical_memory()
    cached = budget._vm_cache
    vm = int(cached[1]) if cached else 0
    return budget.MemorySnapshot(
        total, available, vm, budget.GUI_RESERVE_BYTES,
        max(0, available - vm - budget.GUI_RESERVE_BYTES))


def _cap(operation: str, source) -> int:
    """The Job Object limit for one CAD worker, or refuse with the numbers."""
    from foammesh.support import resource_budget as budget

    estimate = budget.estimate_peak_bytes(
        operation, {'file_bytes': {'source': _source_bytes(source)}})
    measured = _snapshot(budget)
    if estimate.peak_bytes > measured.budget:
        refusal = budget.OverBudget(estimate, measured)
        raise CadWorkerError(f'{operation} was not started: {refusal}',
                             reason='over_budget',
                             details=refusal.to_dict())
    return budget.cap_for(estimate, measured)


_RAISES = {
    'FileNotFoundError': FileNotFoundError,
    'PermissionError': PermissionError,
    'OSError': OSError,
    'RuntimeError': RuntimeError,
    'KeyError': KeyError,
}


def _raise_for(operation: str, outcome, label: str):
    """The exception the in-process body raised, or one that says why not."""
    from foammesh.core.jobs import local_worker

    status = outcome.status
    if status == local_worker.FAILED:
        name = outcome.error_class or ''
        if name == 'TessellationTooFine':
            from .tessellate import TessellationTooFine
            raise TessellationTooFine(outcome.message)
        if name == 'CadSchemaError':
            from .model import CadSchemaError
            raise CadSchemaError(outcome.message)
        exception = _RAISES.get(name)
        if exception is KeyError:
            raise KeyError(outcome.message.strip("'"))
        if exception is not None:
            raise exception(outcome.message)
        raise ValueError(outcome.message)
    details = outcome.to_dict()
    if status == local_worker.CANCELLED:
        raise CadWorkerCancelled(f'{label} was cancelled',
                                 reason='cancelled', details=details)
    if status == local_worker.CRASHED:
        message = (CRASH_MESSAGE if operation == 'cad.import'
                   else f'{label} failed (the CAD kernel crashed)')
        raise CadReaderCrashed(message, reason='worker_crashed',
                               details=details)
    if status == local_worker.OVER_BUDGET:
        raise CadWorkerError(f'{label} stopped: {outcome.message}',
                             reason='over_budget', details=details)
    if status == local_worker.TIMED_OUT:
        raise CadWorkerError(f'{label} stopped: {outcome.message}',
                             reason='timed_out', details=details)
    raise CadWorkerError(f'{label} did not run: {outcome.message}',
                         reason=outcome.reason or 'worker_unavailable',
                         details=details)


@contextmanager
def call(operation: str, parameters: dict, *, label: str = '',
         source=None, cancelled=None,
         timeout: float | None = DEFAULT_TIMEOUT_SECONDS):
    """Run a CAD ``operation`` in a worker; yields its payload.

    The files the worker wrote (a ``.brep``, a ``.vtp``) live in a directory
    that exists for the ``with`` block and is removed after it, so a caller
    copies what it keeps before leaving.
    """
    from foammesh.core.jobs import local_worker

    label = label or operation
    output = Path(tempfile.mkdtemp(prefix='foammesh-cad-'))
    try:
        outcome = local_worker.run_worker_sync(
            operation,
            {'parameters': dict(parameters, output_dir=str(output))},
            cap_bytes=_cap(operation, source), timeout=timeout,
            cancelled=cancelled)
        if not outcome.ok:
            _raise_for(operation, outcome, label)
        yield dict(outcome.payload)
    finally:
        shutil.rmtree(output, ignore_errors=True)


def file_digest(path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return f'sha256:{digest.hexdigest()}'


def read_polydata(path, expected_digest: str | None = None):
    """The ``.vtp`` a worker wrote, after checking it is the one it hashed."""
    if expected_digest and file_digest(path) != expected_digest:
        raise CadWorkerError(
            f'the CAD worker wrote {Path(path).name} and it changed before it '
            'was read', reason='digest_mismatch')
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader

    reader = vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    output = vtkPolyData()
    output.DeepCopy(reader.GetOutput())
    return output


def write_polydata(polydata, path) -> str:
    """Write ``polydata`` losslessly as ``.vtp``; returns its digest."""
    from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = vtkXMLPolyDataWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(polydata)
    writer.SetDataModeToBinary()
    writer.SetCompressorTypeToNone()
    if not writer.Write():
        raise OSError(f'could not write {path}')
    return file_digest(path)
