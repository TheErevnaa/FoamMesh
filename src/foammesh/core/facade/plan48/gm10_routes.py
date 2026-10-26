"""Plan 48 GM10 local-API routes: readers, watch pattern, picture formats,
movie, SU2 viewport reader.

Plain remote control for what a person has in the window: the file types the
Open and Merge dialogs accept, what Open would do with a path, the Watch
pattern scan, the picture formats the capture dialog offers (with the reason
one is unavailable), whether a movie can be encoded, and the SU2 file the
viewport reads. All are reads; nothing here changes a case.
"""
from __future__ import annotations

from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

OPERATIONS = (
    OperationDescriptor(
        'gmsh.reader.formats', 'File types Open and Merge read', 'case',
        ImpactClass.READ,
        summary='Every extension the Gmsh Open and Merge dialogs accept, '
                'the readers among them, and the dialog file filter.'),
    OperationDescriptor(
        'gmsh.reader.probe', 'What Open would do with a file', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {
                               'path': {'type': 'string'},
                               'mode': {'enum': ['open', 'merge']}}},
        summary='Classify a file as Open or Merge would: its kind, the '
                'session operation that reads it, or the refusal reason.'),
    OperationDescriptor(
        'gmsh.watch.scan', 'Scan a Watch pattern', 'case', ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['pattern'],
                           'properties': {
                               'pattern': {'type': 'string'},
                               'seen': {'type': 'object'}}},
        summary='Files matching a pattern that are new or changed since the '
                'last scan; pass the returned table back as seen.'),
    OperationDescriptor(
        'gmsh.image.formats', 'Picture formats', 'case', ImpactClass.READ,
        summary='Every picture format the capture dialog saves, its backend, '
                'and why one is unavailable.'),
    OperationDescriptor(
        'gmsh.movie.status', 'Movie encoder status', 'case', ImpactClass.READ,
        summary='Whether MP4 can be encoded (ffmpeg on the host or in the '
                'distro) and the reason when it cannot.'),
    OperationDescriptor(
        'gmsh.su2.read', 'Read an SU2 mesh for the viewport', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': {'type': 'string'}}},
        summary='Parse an SU2 mesh and report its dimension, counts, cell '
                'types, bounds and boundary markers.'),
)


def _result(session, command, payload: dict) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _path(command) -> str:
    raw = (command.parameters or {}).get('path')
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationFailedError('path is required')
    return raw


def _formats(session, command):
    from foammesh.core.gmsh import import_formats as fmt

    return _result(session, command, {
        'extensions': sorted(fmt.BY_EXTENSION),
        'readers': sorted(fmt.READER_EXTENSIONS),
        'refused': sorted(fmt.REFUSED),
        'filter': fmt.file_filter('merge'),
        'open_filter': fmt.file_filter('open'),
    })


def _probe(session, command):
    from foammesh.core.gmsh import import_formats as fmt

    path = _path(command)
    mode = (command.parameters or {}).get('mode', 'open')
    try:
        request = fmt.plan_open(path, mode)
    except fmt.ImportRefusal as error:
        return _result(session, command, {
            'path': path, 'accepted': False, 'reason': str(error)})
    return _result(session, command, {
        'path': path, 'accepted': True, 'reason': '', 'mode': request.mode,
        'kind': request.kind, 'reader': request.reader,
        'session_operation': request.operation})


def _watch(session, command):
    from foammesh.core.gmsh import import_formats as fmt

    params = command.parameters or {}
    seen = {k: tuple(v) for k, v in (params.get('seen') or {}).items()}
    try:
        changed, table = fmt.watch_scan(params.get('pattern', ''), seen)
    except fmt.ImportRefusal as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {
        'changed': [{'path': p, 'refusal': r} for p, r in changed],
        'seen': {k: list(v) for k, v in table.items()}})


def _images(session, command):
    from foammesh.rendering import image_formats as imf

    return _result(session, command, {'formats': imf.listing()})


def _movie(session, command):
    from foammesh.rendering import image_formats as imf

    reason = imf.movie_refusal()
    return _result(session, command, {'available': not reason,
                                      'reason': reason})


def _su2(session, command):
    from foammesh.rendering import su2_reader

    path = _path(command)
    try:
        mesh = su2_reader.read_su2(path)
    except su2_reader.Su2Error as error:
        raise ValidationFailedError(str(error)) from error
    summary = mesh.summary()
    summary['path'] = path
    summary['patches'] = [{'name': n, 'elements': c} for n, c in
                          su2_reader.boundary_patches(mesh)]
    return _result(session, command, summary)


def handlers() -> dict:
    return {
        'gmsh.reader.formats': _formats,
        'gmsh.reader.probe': _probe,
        'gmsh.watch.scan': _watch,
        'gmsh.image.formats': _images,
        'gmsh.movie.status': _movie,
        'gmsh.su2.read': _su2,
    }
