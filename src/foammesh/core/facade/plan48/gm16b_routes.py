"""Plan 48 GM16b local-API routes: Remesh surface (CVT).

Plain remote control of the "Remesh surface (CVT)" panel; every control the
panel has is a parameter here and nothing else is added.

``gmsh.cvt.catalogue``  the six options with Gmsh's defaults, and the formulas.
``gmsh.cvt.convert``    Sites <-> target edge length (needs the area) and the
                        normal radius <-> its length (needs the scale radius).
``gmsh.cvt.measure``    area, counts, scale radius and mean edge of surfaces.
``gmsh.cvt.run``        remesh the surfaces in the isolated worker and, unless
                        ``apply`` is false, put the result in the model.
``gmsh.cvt.preview``    the before and after ``.vtu`` files (gamma, deviation).
``gmsh.cvt.apply``      keep a finished run, or replace the originals by it.
``gmsh.cvt.undo``       restore the checkpoint taken before the last remesh.
``gmsh.cvt.status``     the runs, the undo stack and whether the build is in.
``gmsh.cvt.export``     write the result of a run as an ASCII STL.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_FACES = {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1}
_RUN = {'type': 'string'}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.cvt.catalogue', 'Remesh surface (CVT) options', 'case',
        ImpactClass.READ,
        summary='The six options of Plugin(CVTRemesh) with Gmsh\'s defaults '
                'and what each means, and the two formulas the panel uses '
                '(points from edge length, normal radius to length).'),
    OperationDescriptor(
        'gmsh.cvt.convert', 'Convert the paired CVT fields', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'additionalProperties': False,
                           'properties': {
                               'area': {'type': 'number'},
                               'scale': {'type': 'number'},
                               'sites': {'type': 'number'},
                               'target_edge_length': {'type': 'number'},
                               'normal_radius': {'type': 'number'},
                               'normal_radius_length': {'type': 'number'}}},
        summary='Sites <-> target edge length (area / (sqrt(3)/2 h^2)) and '
                'the normalised normal radius <-> its length equivalent '
                '(radius x scale). Whatever pair can be worked out from what '
                'was given is returned.'),
    OperationDescriptor(
        'gmsh.cvt.measure', 'Measure surfaces for a CVT remesh', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['faces'],
                           'properties': {'faces': _FACES}},
        summary='Area, triangle and point counts, mean edge, gamma, and the '
                'scale radius Revoropt normalises by, for the picked '
                'surfaces of the session mesh.'),
    OperationDescriptor(
        'gmsh.cvt.run', 'Remesh surfaces (CVT)', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema={
            'type': 'object', 'required': ['faces'],
            'properties': {
                'faces': _FACES,
                'options': {'type': 'object'},
                'target_edge_length': {'type': ['number', 'null']},
                'replace': {'type': 'boolean'},
                'tolerance': {'type': ['number', 'null']},
                'apply': {'type': 'boolean'}}},
        summary='Remesh only the picked surfaces with CVTRemesh in an '
                'isolated Gmsh process. With replace the originals are '
                'swapped for one new surface per original face, but only if '
                're-split, conforming interfaces and closed volumes all '
                'pass; otherwise the failed checks are named and the result '
                'is kept beside the originals. A checkpoint is taken before '
                'the model changes.'),
    OperationDescriptor(
        'gmsh.cvt.preview', 'Before and after of a CVT remesh', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['run_id'],
                           'properties': {'run_id': _RUN}},
        summary='The picked surfaces before and after as .vtu files '
                'coloured by gamma, with the distance to the other surface '
                'per triangle, and the before/after summary.'),
    OperationDescriptor(
        'gmsh.cvt.apply', 'Apply a finished CVT run', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema={'type': 'object', 'required': ['run_id'],
                           'properties': {
                               'run_id': _RUN,
                               'mode': {'enum': ['keep', 'replace']},
                               'name': {'type': 'string'}}},
        summary='Keep the result beside the originals or replace them, '
                'after a checkpoint. Replace is refused with the failed '
                'checks named unless the run passed them all.'),
    OperationDescriptor(
        'gmsh.cvt.undo', 'Undo the last CVT remesh', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Restore the checkpoint taken before the last remesh and '
                'verify the model matches it.'),
    OperationDescriptor(
        'gmsh.cvt.status', 'CVT remesh runs and undo', 'case',
        ImpactClass.READ,
        summary='The runs of this session, whether each was applied, the '
                'undo stack, and the notice shown while FoamMesh\'s Gmsh '
                'build is not in use.'),
    OperationDescriptor(
        'gmsh.cvt.export', 'Export a CVT result as STL', 'case',
        ImpactClass.FILE_PRODUCING,
        parameters_schema={'type': 'object', 'required': ['run_id', 'path'],
                           'properties': {'run_id': _RUN,
                                          'path': {'type': 'string'}}},
        summary='Write the remeshed surface of a run as an ASCII STL, as a '
                'new surface, without changing the model.'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _window(session):
    """Plan 48 PA3. The open CVT panel, or ``None``: it shows what a run, a
    keep or an undo made through the API did, as after its own buttons."""
    from foammesh.core.facade.plan48 import pa3_routes
    return pa3_routes.window_panel(session, 'cvt')


def _faces(parameters) -> list:
    faces = parameters.get('faces')
    if not isinstance(faces, list) or not faces:
        raise ValidationFailedError('faces must be a non-empty list')
    try:
        return [int(f) for f in faces]
    except (TypeError, ValueError):
        raise ValidationFailedError('faces must be surface tags') from None


def _guard(call):
    """Run *call* off the loop, turning a refusal into a precondition error."""
    from foammesh.core.gmsh.cvt_remesh import CvtRefusal

    async def run(*args):
        try:
            return await asyncio.to_thread(call, *args)
        except CvtRefusal as error:
            raise PreconditionFailedError(str(error)) from error
    return run


def _need(parameters, name):
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailedError('%s is required' % name)
    return value


def handlers() -> dict:
    from foammesh.core.gmsh import cvt_remesh as cvt

    async def catalogue(session, command):
        w = cvt.worker()
        return _result(session, command, {
            'plugin': cvt.PLUGIN_NAME, 'title': cvt.PANEL_TITLE,
            'options': [{'name': o.name, 'kind': o.kind,
                         'default': o.default, 'help': o.help}
                        for o in cvt.options()],
            'formulas': {
                'sites': 'sites = area / (sqrt(3)/2 * h^2)',
                'normal_radius_length': 'length = normal_radius * scale, '
                                        'scale = max |2c − x| over the '
                                        'vertices (Revoropt normalize_mesh)',
            },
            'point_area_factor': w.POINT_AREA_FACTOR,
            'checks': ['boundary_loops', 'face_kept', 'face_connected',
                       'interfaces_unpicked', 'interfaces_picked',
                       'no_overlap', 'orientation', 'stitch_quality',
                       'deviation',
                       'closed_volumes']})

    async def convert(session, command):
        p = command.parameters
        try:
            out = cvt.convert(
                area=p.get('area'), scale=p.get('scale'),
                sites=p.get('sites'), edge=p.get('target_edge_length'),
                radius=p.get('normal_radius'),
                radius_length=p.get('normal_radius_length'))
        except cvt.CvtRefusal as error:
            raise ValidationFailedError(str(error)) from error
        return _result(session, command, out)

    async def measure(session, command):
        found = await _guard(cvt.measure)(
            _controller(session), _faces(command.parameters))
        return _result(session, command, found)

    async def run(session, command):
        p = command.parameters
        controller = _controller(session)

        def work():
            return cvt.run(
                controller, _faces(p), p.get('options') or {},
                target_edge_length=p.get('target_edge_length'),
                replace=bool(p.get('replace')), tolerance=p.get('tolerance'),
                apply=p.get('apply', True))
        try:
            report = await asyncio.to_thread(work)
        except cvt.CvtRefusal as error:
            raise PreconditionFailedError(str(error)) from error
        panel = _window(session)
        if panel is not None:
            await asyncio.to_thread(panel.adopt, report)
        return _result(session, command, report)

    async def preview(session, command):
        found = await _guard(cvt.preview)(
            _controller(session), _need(command.parameters, 'run_id'))
        return _result(session, command, found)

    async def apply(session, command):
        p = command.parameters
        found = await _guard(cvt.apply_run)(
            _controller(session), _need(p, 'run_id'),
            p.get('mode') or 'keep', p.get('name') or '')
        panel = _window(session)
        if (panel is not None and (p.get('mode') or 'keep') == 'keep'
                and found.get('entity')):
            await asyncio.to_thread(panel.kept, found)
        return _result(session, command, found)

    async def undo(session, command):
        found = await _guard(cvt.undo)(_controller(session))
        panel = _window(session)
        if panel is not None:
            await asyncio.to_thread(
                panel.undone, found,
                cvt.status(_controller(session))['can_undo'])
        return _result(session, command, found)

    async def status(session, command):
        return _result(session, command, cvt.status(_controller(session)))

    async def export(session, command):
        p = command.parameters
        found = await _guard(cvt.export_surface)(
            _controller(session), _need(p, 'run_id'), _need(p, 'path'))
        return _result(session, command, found)

    return {'gmsh.cvt.catalogue': catalogue, 'gmsh.cvt.convert': convert,
            'gmsh.cvt.measure': measure, 'gmsh.cvt.run': run,
            'gmsh.cvt.preview': preview, 'gmsh.cvt.apply': apply,
            'gmsh.cvt.undo': undo, 'gmsh.cvt.status': status,
            'gmsh.cvt.export': export}
