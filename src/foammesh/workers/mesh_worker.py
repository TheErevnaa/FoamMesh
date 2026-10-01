"""The mesh worker: parses a polyMesh and runs one check, out of the window.

Plan 35 CR2. ``quality.fidelity``, ``quality.resolution``, ``quality.summary``
and ``quality.cell_fields`` read the mesh -- gigabytes of it on a large case --
and used to do it on the GUI thread. They now run here, in a process that has
its own memory, is capped by a Job Object, and can die without taking the
window with it.

Protocol, all of it on the file system and the exit code:

* ``argv`` is ``[operation, args.json]``. The args name the case, carry the
  project's configuration as YAML, the command parameters, and
  ``result_path`` -- where the result goes.
* When ``wait_for_go`` is set the worker reads one line from stdin before it
  does anything that allocates: the parent assigns the Job Object between
  spawn and that line, so no allocation escapes the cap. EOF there means the
  parent is gone, and the worker leaves.
* The result is JSON written atomically to ``result_path``, at most
  ``max_result_bytes`` large. Per-cell arrays go to an ``.npz`` beside it.

Exit codes: 0 done, 2 failed with a reason in the result, 12 ran out of
memory (``over_budget``), 14 the parent went away. Anything else is a crash
-- 3 included, which is what ``abort()`` leaves on Windows.

The measurements themselves are ``DomainOperations``' own method bodies, run
against a session built from the args: one implementation of each verdict, so
a worker result is the in-process result.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_OVER_BUDGET = 12
EXIT_PARENT_GONE = 14

DEFAULT_MAX_RESULT_BYTES = 64 * 1024 * 1024

#: Operation -> the ``DomainOperations`` method that measures it.
OPERATIONS = {
    'quality.fidelity': '_quality_fidelity',
    'quality.resolution': '_quality_resolution',
    'quality.summary': '_quality_summary',
    'quality.cell_fields': '_quality_cell_fields',
}
#: Plan 35 CR3: the viewport's mesh preview (``core.mesh.mesh_preview``).
PREVIEW_OPERATIONS = ('mesh.preview', 'mesh.volume')
#: Plan 37 UF10: an exact section (``core.section.worker_section``).
SECTION_OPERATIONS = ('mesh.section', 'mesh.section_batch')
#: Plan 37 UF18: checkMesh's written sets, parsed for the viewport
#: (``core.quality.check_artifacts``) -- never in the window's process.
CHECK_HIGHLIGHT_OPERATIONS = ('quality.check_highlights',)
#: Test and support operations: they exercise the transport, never a case.
DIAGNOSTIC_OPERATIONS = ('diag.allocate', 'diag.crash', 'diag.sleep',
                         'diag.echo')


class _NullBus:
    def publish(self, *_args, **_kwargs):
        return None


def _json_default(value):
    try:
        import numpy as np
    except ImportError:                                     # pragma: no cover
        np = None
    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    raise TypeError(f'{type(value).__name__} is not JSON serialisable')


def write_result(path: Path, document: dict, *, max_bytes: int) -> int:
    """Write ``document`` atomically, refusing one larger than ``max_bytes``."""
    data = json.dumps(document, default=_json_default).encode('utf-8')
    if len(data) > max_bytes:
        data = json.dumps({
            'status': 'failed', 'reason': 'result_too_large',
            'message': (f'the result is {len(data)} bytes; the limit is '
                        f'{max_bytes}'),
            'operation': document.get('operation')}).encode('utf-8')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return len(data)


def _read_line(fd: int) -> bytes:
    """One line from ``fd``, unbuffered: no Python stream lock is held."""
    line = b''
    while not line.endswith(b'\n'):
        chunk = os.read(fd, 1)
        if not chunk:
            break
        line += chunk
    return line


def _peek_until_closed(fd: int, interval: float = 0.5) -> None:
    """Return when the parent's end of ``fd`` (an anonymous pipe) closes."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return
    available = wintypes.DWORD(0)
    while kernel32.PeekNamedPipe(handle, None, 0, None,
                                 ctypes.byref(available), None):
        time.sleep(interval)


def _wait_for_go() -> bool:
    try:
        fd = sys.stdin.fileno() if sys.stdin is not None else 0
        line = _read_line(fd)
    except (OSError, ValueError):
        return False
    if not line:
        return False

    # The pipe stays open as the parent's lifetime: when it closes, the
    # parent is gone and nobody will read the result. (On Windows the Job
    # Object already kills this process with its handle; this covers the
    # platforms and the builds without one.) ``os.read`` rather than
    # ``sys.stdin``: a daemon thread parked in the buffered reader holds its
    # lock, and interpreter shutdown then deadlocks closing stdin.
    #
    # Windows: never park a read on the pipe. A synchronous file object
    # serialises every call on it, so a pending ``ReadFile`` here made the
    # next DLL whose runtime asks about its standard handles at load -- numpy's
    # -- wait for the parent to write again, which it never does. The pipe is
    # peeked instead, which ends at once.
    def watch():
        if sys.platform == 'win32':
            _peek_until_closed(fd)
        else:
            try:
                while os.read(fd, 4096):
                    pass
            except OSError:
                pass
        os._exit(EXIT_PARENT_GONE)

    threading.Thread(target=watch, name='parent-watch', daemon=True).start()
    return True


def _session(args: dict):
    from foammesh.core.facade.results import RevisionSnapshot

    db = None
    if args.get('db_yaml') is not None:
        from foammesh.db.configurations import Configurations
        from foammesh.db.configurations_schema import schema

        db = Configurations(schema)
        db.loadYaml(args['db_yaml'], fillWithDefault=True)
    return SimpleNamespace(
        case_path=Path(args['case_path']), case_id=str(args.get('case_id', '')),
        state=SimpleNamespace(db=db, bus=_NullBus()),
        revisions=RevisionSnapshot(0, 0, 0, 0))


def _diagnostic(operation: str, args: dict) -> dict:
    parameters = dict(args.get('parameters') or {})
    if operation == 'diag.allocate':
        size = int(parameters.get('bytes', 0))
        block = bytearray(size)
        for index in range(0, size, 4096):       # commit it, page by page
            block[index] = 1
        return {'allocated': size}
    if operation == 'diag.crash':
        import faulthandler
        faulthandler._read_null()                # a real access violation
        return {}                                # pragma: no cover
    if operation == 'diag.sleep':
        time.sleep(float(parameters.get('seconds', 60)))
        return {'slept': parameters.get('seconds', 60)}
    if parameters.get('import'):
        __import__(str(parameters['import']))
    return {'echo': parameters, 'pid': os.getpid()}


def run_operation(operation: str, args: dict) -> dict:
    """Measure; returns the payload. Raises what the in-process body raised."""
    if operation in DIAGNOSTIC_OPERATIONS:
        return _diagnostic(operation, args)
    # Plan 35 CR7: OCCT and the export writers (foammesh.workers.cad_ops).
    if operation.startswith(('cad.', 'export.')):
        from foammesh.workers import cad_ops
        return cad_ops.run(operation, args)
    if operation in PREVIEW_OPERATIONS:
        from foammesh.core.mesh.mesh_preview import build_preview

        return build_preview(operation, args)
    if operation in SECTION_OPERATIONS:
        from foammesh.core.section import worker_section

        if operation == 'mesh.section_batch':        # UF11 Compare
            return worker_section.run_batch(args)
        return worker_section.run(args)
    if operation in CHECK_HIGHLIGHT_OPERATIONS:
        from foammesh.core.quality import check_artifacts

        return check_artifacts.run(args)
    method = OPERATIONS.get(operation)
    if method is None:
        raise KeyError(f'the mesh worker does not run {operation!r}')
    from foammesh.core.facade.commands import Command
    from foammesh.core.facade.domain_operations import DomainOperations

    operations = DomainOperations()
    budget = args.get('fidelity_budget_seconds')
    # The parent read the setting; the worker must not read a different one.
    operations._fidelity_budget_seconds = lambda _session: budget
    session = _session(args)
    command = Command(operation=operation, case_id=session.case_id,
                      parameters=dict(args.get('parameters') or {}))
    result = getattr(operations, method)(session, command)
    return dict(result.payload or {})


def _externalise_values(payload: dict, result_path: Path) -> dict:
    """Per-cell arrays go to an ``.npz``: JSON would multiply them by ten."""
    values = payload.pop('values', None)
    if not values:
        return payload
    import numpy as np

    target = Path(result_path).with_suffix('.npz')
    temporary = target.with_name(target.stem + '.tmp.npz')
    np.savez(temporary, **{name: np.asarray(array, dtype=np.float64)
                           for name, array in values.items()})
    os.replace(temporary, target)
    payload['values_path'] = str(target)
    return payload


def _failure(operation: str, error: BaseException) -> dict:
    from foammesh.core.facade.errors import FacadeError
    from foammesh.core.mesh.poly_mesh_boundary import PolyMeshReadError

    document = {'status': 'failed', 'operation': operation,
                'message': str(error) or type(error).__name__,
                'error_class': type(error).__name__}
    if isinstance(error, FacadeError):
        document.update(reason=error.code, code=error.code,
                        details=dict(error.details or {}))
    elif isinstance(error, PolyMeshReadError):
        document.update(reason=error.reason)
    elif type(error).__name__ in ('PreviewRefused', 'SectionRefused'):
        document.update(reason=error.reason, details=dict(error.details))
    else:
        document.update(reason='worker_error')
    return document


def _quiet_native_faults() -> None:
    """A native fault ends the worker at once instead of opening an error
    report dialog nobody sees -- which would hold it until its deadline."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
    except Exception:                                       # noqa: BLE001
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _quiet_native_faults()
    if len(argv) < 2:
        print('usage: mesh_worker <operation> <args.json>', file=sys.stderr)
        return EXIT_FAILED
    operation, args_path = argv[0], Path(argv[1])
    try:
        args = json.loads(args_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        print(f'mesh_worker: cannot read {args_path}: {error}', file=sys.stderr)
        return EXIT_FAILED
    if not isinstance(args, dict) or not args.get('result_path'):
        print(f'mesh_worker: {args_path} names no result_path', file=sys.stderr)
        return EXIT_FAILED
    if args.get('wait_for_go') and not _wait_for_go():
        return EXIT_PARENT_GONE
    result_path = Path(args['result_path'])
    max_bytes = int(args.get('max_result_bytes') or DEFAULT_MAX_RESULT_BYTES)
    started = time.monotonic()
    try:
        payload = run_operation(operation, args)
        payload = _externalise_values(payload, result_path)
        document = {'status': 'ok', 'operation': operation,
                    'payload': payload,
                    'seconds': round(time.monotonic() - started, 3)}
        code = EXIT_OK
    except MemoryError:
        document = {'status': 'failed', 'operation': operation,
                    'reason': 'over_budget',
                    'message': 'the worker ran out of the memory it was '
                               'granted'}
        code = EXIT_OVER_BUDGET
    except Exception as error:                              # noqa: BLE001
        import traceback

        traceback.print_exc()
        document = _failure(operation, error)
        code = EXIT_FAILED
    try:
        write_result(result_path, document, max_bytes=max_bytes)
    except MemoryError:
        return EXIT_OVER_BUDGET
    except OSError as error:
        print(f'mesh_worker: cannot write {result_path}: {error}',
              file=sys.stderr)
        return EXIT_FAILED
    return code


if __name__ == '__main__':
    raise SystemExit(main())
