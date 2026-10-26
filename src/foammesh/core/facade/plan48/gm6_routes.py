"""Plan 48 GM6: sweeps and structured meshing on the local API.

Plain remote control of what the window's build and Structure panels do,
nothing agent-specific.  The sweep operations themselves (an extrusion with a
layer table, a picked direction or axis, QuadTri / ScaleLast, extrusion along
the normals, the 2D-case and wedge presets) are operations of
``gmsh.geometry.build``; this file adds what surrounds them:

``gmsh.geometry.sweep_preview``
    Where the layers of a sweep will fall, before it is built: the arrow or
    axis, the point of every layer boundary, and the line segments the
    viewport draws.
``gmsh.geometry.layer_table``
    The layer-table editor: a grading preset (uniform, geometric ratio, first
    height and ratio) or a typed table, as the counts and cumulative heights
    ``gmsh.geometry.build`` takes, with the thickness of every layer.
``gmsh.structure.precheck``
    Can these surfaces or volumes be transfinite with these corners, and the
    reason when not.
``gmsh.structure.qualify`` / ``gmsh.structure.make_structured``
    "Make structured where possible": which volumes qualify (the coloured
    preview, no change), and the same with the constraints set.
``gmsh.structure.hex_dominant``
    The "Hex-dominant (experimental)" action with its element-count readout.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_GEOMETRY = 'gmsh.geometry.'
_STRUCTURE = 'gmsh.structure.'

_DIM = {'type': 'integer', 'enum': [2, 3]}
_TAGS = {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}}

OPERATIONS = (
    OperationDescriptor(
        _GEOMETRY + 'sweep_preview', 'Preview the layers of a sweep', 'case',
        ImpactClass.READ,
        summary='Where each layer of an extrusion, revolve, twist, '
                'normal extrusion, 2D case or wedge falls, before it is '
                'built. Same operation_id and values as '
                'gmsh.geometry.build; the model is not changed. With a '
                'window open the viewport draws the layers as the Preview '
                'layers button does (draw=false only returns them; clear '
                'them with gmsh.tools.previews).',
        parameters_schema={'type': 'object', 'required': ['operation_id'],
                           'properties': {
                               'operation_id': {'type': 'string'},
                               'values': {'type': 'object'},
                               'draw': {'type': 'boolean'}}}),
    OperationDescriptor(
        _GEOMETRY + 'layer_table', 'Make or check a layer table', 'case',
        ImpactClass.READ,
        summary='A grading preset (uniform, geometric, first_height) or a '
                'typed table ("2@0.4, 3@1") as the layer counts and '
                'cumulative heights of an extrusion, with the thickness of '
                'every layer for the given total length.',
        parameters_schema={'type': 'object', 'properties': {
            'preset': {'type': 'string',
                       'enum': ['uniform', 'geometric', 'first_height']},
            'layers': {'type': 'integer', 'minimum': 1},
            'ratio': {'type': 'number', 'exclusiveMinimum': 0},
            'first': {'type': 'number', 'exclusiveMinimum': 0},
            'total': {'type': 'number', 'exclusiveMinimum': 0},
            'table': {'type': 'string'}}}),
    OperationDescriptor(
        _STRUCTURE + 'precheck', 'Can these be transfinite?', 'case',
        ImpactClass.READ,
        summary='For each surface or volume, whether it can be transfinite '
                'with the named corner points, the reason when it cannot, '
                'and the points that may be named as corners.',
        parameters_schema={'type': 'object', 'required': ['dim', 'tags'],
                           'properties': {
                               'dim': _DIM, 'tags': _TAGS,
                               'corners': _TAGS}}),
    OperationDescriptor(
        _STRUCTURE + 'qualify', 'Which volumes can be structured?', 'case',
        ImpactClass.READ,
        summary='The coloured preview of "Make structured where possible": '
                'the tags that qualify, those that do not with the reason, '
                'and an RGB per tag. Nothing is set. With a window open the '
                'viewport paints the colours (green: can be structured, '
                'red: cannot) as the Which can be structured? button does; '
                'paint=false takes them down instead.',
        parameters_schema={'type': 'object', 'properties': {
            'dim': _DIM, 'tags': _TAGS, 'paint': {'type': 'boolean'}}}),
    OperationDescriptor(
        _STRUCTURE + 'make_structured', 'Make structured where possible',
        'case', ImpactClass.MESH_MUTATION,
        summary='Set setTransfiniteAutomatic on the volumes (dim 3) or '
                'surfaces (dim 2) that can be structured; the others are '
                'named with the reason. Blank tags: every entity. As the '
                'button, it is kept as an Automatic transfinite row: the '
                'open Structure panel adds it, and the reply carries row '
                'and rows_text to send back with configuration.patch on '
                'gmsh.structure.rows.',
        parameters_schema={'type': 'object', 'properties': {
            'dim': _DIM, 'tags': _TAGS,
            'corner_angle': {'type': 'number', 'minimum': 0,
                             'maximum': 3.141592653589793},
            'recombine': {'type': 'boolean'},
            'paint': {'type': 'boolean'}}}),
    OperationDescriptor(
        _STRUCTURE + 'hex_dominant', 'Hex-dominant meshing (experimental)',
        'case', ImpactClass.MESH_MUTATION,
        summary='Switch on 3D recombination, mesh in 3D and count the '
                'tetrahedra, hexahedra, prisms and pyramids made.',
        parameters_schema={'type': 'object', 'properties': {
            'level': {'type': 'integer', 'minimum': 0, 'maximum': 2},
            'conformity': {'type': 'integer', 'minimum': 0, 'maximum': 4}}}),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _builder(session):
    from foammesh.core.gmsh import checkpoints
    controller = _controller(session)
    builder = controller.builder()
    case_path = getattr(controller, 'case_path', None)
    if case_path is not None:
        checkpoints.bind(builder, case_path)
    return builder


def _refusal(error):
    from foammesh.core.gmsh.geometry_build import BuildRefusal
    from foammesh.core.gmsh.layer_preview import LayerTableRefusal
    from foammesh.core.gmsh.structure import StructureRefusal
    if isinstance(error, (BuildRefusal, LayerTableRefusal,
                          StructureRefusal)):
        return ValidationFailedError(str(error))
    return PreconditionFailedError(str(error))


def _with_session(session, work):
    """``work(call)`` on the project's Gmsh session, under the controller's
    lock like every panel action."""
    controller = _controller(session)
    with controller._lock:                              # noqa: SLF001
        gmsh = controller._build_session()              # noqa: SLF001
        return work(gmsh.call)


def _tags(command, name, required=False) -> list:
    raw = command.parameters.get(name)
    if raw in (None, ''):
        if required:
            raise ValidationFailedError('%s is required' % name)
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValidationFailedError('%s must be a list of tags' % name)
    try:
        tags = [int(t) for t in raw]
    except (TypeError, ValueError):
        raise ValidationFailedError(
            '%s must be whole numbers' % name) from None
    if any(t < 1 for t in tags):
        raise ValidationFailedError('%s are entity tags, from 1' % name)
    return tags


def _dim(command, default=None) -> int:
    raw = command.parameters.get('dim', default)
    try:
        dim = int(raw)
    except (TypeError, ValueError):
        raise ValidationFailedError('dim is required (2 or 3)') from None
    if dim not in (2, 3):
        raise ValidationFailedError('dim must be 2 (surfaces) or 3 '
                                    '(volumes)')
    return dim


async def _sweep_preview(session, command) -> OperationResult:
    op_id = str(command.parameters.get('operation_id') or '').strip()
    if not op_id:
        raise ValidationFailedError('operation_id is required')
    values = command.parameters.get('values')
    if values is None:
        values = {}
    # an empty list is not an object either (``or {}`` let it through)
    if not isinstance(values, dict):
        raise ValidationFailedError('values must be an object')
    builder = _builder(session)
    try:
        done = await asyncio.to_thread(builder.preview, op_id, values)
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    # Plan 48 PA3. The open Geometry build panel draws the layers (the
    # arrow or axis and the layer boundaries) and says so, as its Preview
    # layers button does; draw=false only returns them.
    from foammesh.core.facade.plan48 import pa3_routes
    panel = pa3_routes.window_panel(session, 'build')
    if panel is not None and command.parameters.get('draw', True) is not False:
        done = dict(done)
        done['status'] = await asyncio.to_thread(panel.show_sweep_preview,
                                                 done)
    return _result(session, command, done)


def _layer_table(session, command) -> OperationResult:
    from foammesh.core.gmsh import layer_preview as lp
    parameters = command.parameters
    try:
        total = float(parameters.get('total') or 1.0)
        if parameters.get('table'):
            rows = lp.parse_table(parameters['table'])
        elif parameters.get('preset'):
            first = parameters.get('first')
            rows = lp.grade(str(parameters['preset']),
                            int(parameters.get('layers') or 0),
                            float(parameters.get('ratio') or 1.0),
                            float(first) if first is not None else None,
                            total)
        else:
            raise ValidationFailedError('give a preset or a table')
        counts, heights = lp.counts_heights(rows)
    except ValidationFailedError:
        raise
    except (lp.LayerTableRefusal, TypeError, ValueError) as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {
        'table': lp.table_text(rows), 'rows': [list(r) for r in rows],
        'layer_counts': counts, 'layer_heights': heights,
        'fractions': lp.layer_fractions(rows),
        'thicknesses': lp.thicknesses(rows, total),
        'summary': lp.summary(rows, total), 'total': total})


async def _precheck(session, command) -> OperationResult:
    from foammesh.core.gmsh import structure
    dim = _dim(command)
    tags = _tags(command, 'tags', required=True)
    corners = _tags(command, 'corners')
    try:
        done = await asyncio.to_thread(
            _with_session, session,
            lambda call: structure.precheck_on_session(call, dim, tags,
                                                       corners))
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, done)


def _structured(apply):
    async def run(session, command) -> OperationResult:
        from foammesh.core.gmsh import structure
        dim = _dim(command, 3)
        tags = _tags(command, 'tags')
        try:
            angle = float(command.parameters.get(
                'corner_angle', structure.DEFAULT_CORNER_ANGLE))
        except (TypeError, ValueError):
            raise ValidationFailedError(
                'corner_angle must be a number') from None
        recombine = bool(command.parameters.get('recombine', True))
        try:
            done = await asyncio.to_thread(
                _with_session, session,
                lambda call: structure.make_structured_on_session(
                    call, dim, tags, angle, recombine, apply))
        except Exception as error:                       # noqa: BLE001
            raise _refusal(error) from error
        # Plan 48 PA3. The open Structure panel colours the volumes and says
        # what it found, as after its own button (paint=false: clears).
        from foammesh.core.facade.plan48 import pa3_routes
        panel = pa3_routes.window_panel(session, 'structure')
        done = dict(done)
        if apply:
            # DP-5738 (DP-4216): the press is kept as the row it stands for
            if panel is not None:
                done['row'] = panel.keep_structured(dim, tags, angle,
                                                    recombine)
                rows = panel.rows()
            else:
                stored = pa3_routes._structure_rows(session, command)
                rows, done['row'], _added = structure.with_automatic_row(
                    stored, dim, tags, angle, recombine,
                    name=pa3_routes.next_name(stored, 'transfiniteAutomatic'))
            done['rows_text'] = structure.rows_to_text(rows)
        if panel is not None:
            if command.parameters.get('paint', True) is False:
                panel.clear_qualification()
            else:
                done['status'] = panel.show_result(
                    'structured' if apply else 'qualify', done)
        return _result(session, command, done)
    return run


async def _hex_dominant(session, command) -> OperationResult:
    from foammesh.core.gmsh import structure
    try:
        level = int(command.parameters.get('level', 1))
        conformity = int(command.parameters.get('conformity', 0))
    except (TypeError, ValueError):
        raise ValidationFailedError(
            'level and conformity must be whole numbers') from None
    try:
        done = await asyncio.to_thread(
            _with_session, session,
            lambda call: structure.hex_dominant_on_session(
                call, level, conformity))
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, done)


def handlers() -> dict:
    return {
        _GEOMETRY + 'sweep_preview': _sweep_preview,
        _GEOMETRY + 'layer_table': _layer_table,
        _STRUCTURE + 'precheck': _precheck,
        _STRUCTURE + 'qualify': _structured(False),
        _STRUCTURE + 'make_structured': _structured(True),
        _STRUCTURE + 'hex_dominant': _hex_dominant,
    }
