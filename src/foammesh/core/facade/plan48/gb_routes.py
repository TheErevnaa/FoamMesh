"""Plan 48 GB: the geometry operations added after GM5, on the local API.

Plain remote control of what the window's Geometry build panel does, nothing
agent-specific. ``gmsh.geometry.build`` (GM5) already runs any operation of the
catalogue by id; each operation added here also has its own route whose
parameters are the form's fields, so a client sees the fields in the route's
schema:

``gmsh.geometry.add_<operation id>``
    One operation of the panel: ``add_bspline_filling``,
    ``add_bezier_filling``, ``add_bspline_surface``, ``add_bezier_surface``,
    ``add_trimmed_surface``, ``add_affine_transform``, ``add_copy`` (both
    kernels), and the built-in kernel's ``add_polyline``,
    ``add_compound_spline``, ``add_compound_bspline`` and
    ``add_point_on_geometry``. Picked entities are ``[dim, tag]`` pairs, curve
    loops are the numbers ``Curve loop`` returned (a list, or ``"3, 4"``).
``gmsh.geometry.kernel_change_preview``
    What changing the kernel would build again and what it would refuse by
    name (the question the panel asks before it empties the model).
``gmsh.geometry.reimport_report``
    What the last kernel change built again and what it refused, by name.
    The change itself is ``gmsh.geometry.set_kernel`` (GM5), which answers
    with the same report.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass

_ID = 'gmsh.geometry.'

#: The operations of the catalogue this package added routes for.
NEW_OPERATIONS = (
    'bspline_filling', 'bezier_filling', 'bspline_surface', 'bezier_surface',
    'trimmed_surface', 'affine_transform', 'copy', 'polyline',
    'compound_spline', 'compound_bspline', 'point_on_geometry',
    'parametric_geometry')


def route_id(op_id: str) -> str:
    return _ID + 'add_' + op_id


def _field_schema(spec) -> dict:
    from foammesh.core.gmsh import geometry_build as gb
    if spec.kind == gb.ENTITIES:
        return {'type': 'array', 'description': spec.title,
                'items': {'type': 'array', 'minItems': 2, 'maxItems': 2,
                          'items': {'type': 'integer'}}}
    if spec.kind == gb.BOOLEAN:
        return {'type': 'boolean', 'description': spec.title}
    if spec.kind == gb.CHOICE:
        return {'type': 'string', 'enum': list(spec.choices),
                'description': spec.title}
    if spec.kind == gb.TEXT:
        return {'type': 'string', 'description': spec.title}
    if spec.kind == gb.INTEGER and spec.name in gb.TAG_LISTS:
        return {'description': spec.title,
                'oneOf': [{'type': 'string'}, {'type': 'integer'},
                          {'type': 'array', 'items': {'type': 'integer'}}]}
    if spec.kind == gb.INTEGER:
        return {'description': spec.title,
                'oneOf': [{'type': 'integer'}, {'type': 'string'}]}
    return {'description': spec.title,
            'oneOf': [{'type': 'number'}, {'type': 'string'}]}


def _schema(op) -> dict:
    from foammesh.core.gmsh import geometry_build as gb
    required = [f.name for f in op.fields
                if f.kind == gb.ENTITIES and not f.optional]
    schema = {'type': 'object',
              'properties': {f.name: _field_schema(f) for f in op.fields}}
    if required:
        schema['required'] = required
    return schema


def _descriptors() -> tuple:
    from foammesh.core.gmsh import geometry_build as gb
    rows = []
    for op_id in NEW_OPERATIONS:
        op = gb.operation(op_id)
        kernels = ' and '.join(gb.KERNEL_TITLES[k] for k in op.kernels)
        rows.append(OperationDescriptor(
            route_id(op_id), op.title, 'case', ImpactClass.MESH_MUTATION,
            summary='Run %s on the geometry session (%s kernel) with the '
                    'form of the Geometry build panel; the route ends in '
                    'the state the panel does.' % (op.title, kernels),
            parameters_schema=_schema(op)))
    return tuple(rows)


OPERATIONS = _descriptors() + (
    OperationDescriptor(
        _ID + 'kernel_change_preview',
        'What a geometry kernel change would build again', 'case',
        ImpactClass.READ,
        summary='For the given kernel: the built steps it has an equivalent '
                'for (built again, in order) and the ones it would refuse '
                'by name, with the reason. Changes nothing.',
        parameters_schema={'type': 'object', 'required': ['kernel'],
                           'properties': {'kernel': {
                               'type': 'string', 'enum': ['occ', 'geo']}}}),
    OperationDescriptor(
        _ID + 'reimport_report',
        'What the last geometry kernel change built again', 'case',
        ImpactClass.READ,
        summary='The steps the last kernel change built again on the new '
                'kernel and the ones it refused, by name and reason; empty '
                'when the kernel was not changed with geometry built.'),
)


def _routes():
    from foammesh.core.facade.plan48 import gm5_routes
    return gm5_routes


def _make_builder(op_id):
    async def run(session, command):
        from foammesh.core.gmsh import geometry_build as gb
        routes = _routes()
        op = gb.operation(op_id)
        names = {f.name for f in op.fields}
        unknown = sorted(set(command.parameters) - names)
        if unknown:
            raise ValidationFailedError(
                '%s has no field %s; its fields are %s'
                % (op.title, ', '.join(unknown), ', '.join(sorted(names))))
        values = {k: v for k, v in command.parameters.items()}
        controller = routes._controller(session)
        routes._builder(session)

        def work():
            return controller.build(op_id, values)

        try:
            step = await asyncio.to_thread(work)
        except Exception as error:                       # noqa: BLE001
            raise routes._refusal(error) from error
        return routes._result(session, command, routes._step_row(step))
    return run


def _kernel_change_preview(session, command):
    from foammesh.core.gmsh import checkpoints
    from foammesh.core.gmsh import geometry_build as gb
    routes = _routes()
    kernel = str(command.parameters.get('kernel') or '').strip()
    if kernel not in gb.KERNELS:
        raise ValidationFailedError(
            'kernel must be one of %s' % ', '.join(gb.KERNELS))
    builder = routes._builder(session)
    try:
        preview = builder.reimport_preview(kernel)
        preview['current'] = builder.kernel
        preview['text'] = checkpoints.describe_kernel_change(builder, kernel)
    except Exception as error:                           # noqa: BLE001
        raise routes._refusal(error) from error
    return routes._result(session, command, preview)


def _reimport_report(session, command):
    routes = _routes()
    builder = routes._builder(session)
    report = getattr(builder, 'last_reimport', None)
    if not report:
        report = {'kernel': builder.kernel, 'cleared': False,
                  'reimported': [], 'refused': []}
    return routes._result(session, command, dict(report))


def handlers() -> dict:
    table = {route_id(op_id): _make_builder(op_id)
             for op_id in NEW_OPERATIONS}
    table[_ID + 'kernel_change_preview'] = _kernel_change_preview
    table[_ID + 'reimport_report'] = _reimport_report
    return table
