"""Plan 48 GM5: the geometry builder on the local API.

Plain remote control of what the window's geometry build panel does, nothing
agent-specific:

``gmsh.geometry.operations``
    The build catalogue: every operation with its fields, the kernels it
    exists on, and, for a kernel, why an operation is not offered.
``gmsh.geometry.kernel`` / ``gmsh.geometry.set_kernel``
    The project's geometry kernel (OpenCASCADE or Built-in) and its change.
``gmsh.geometry.build``
    One operation of the panel (picked entities as ``[dim, tag]`` pairs).
``gmsh.geometry.steps``
    What has been built, in order.
``gmsh.geometry.remove_last``
    "Remove last script command".
``gmsh.geometry.checkpoint_save`` / ``checkpoint_restore`` / ``checkpoints``
    Named positions in the session's history and the way back to them.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_ID = 'gmsh.geometry.'

OPERATIONS = (
    OperationDescriptor(
        _ID + 'operations', 'The geometry build catalogue', 'case',
        ImpactClass.READ,
        summary='Every Geometry → Elementary entities operation with its '
                'fields, the kernels it exists on and, for the given kernel '
                '(default: the project kernel), why one is not offered.',
        parameters_schema={'type': 'object', 'properties': {
            'kernel': {'type': 'string', 'enum': ['occ', 'geo']}}}),
    OperationDescriptor(
        _ID + 'kernel', 'The project geometry kernel', 'case',
        ImpactClass.READ,
        summary='OpenCASCADE (occ) or Built-in (geo), as chosen per project.'),
    OperationDescriptor(
        _ID + 'set_kernel', 'Choose the project geometry kernel', 'case',
        ImpactClass.MESH_MUTATION,
        summary='With geometry built, the change empties the model and '
                'needs confirm=true; the built steps are kept as a script.',
        parameters_schema={'type': 'object', 'required': ['kernel'],
                           'properties': {
                               'kernel': {'type': 'string',
                                          'enum': ['occ', 'geo']},
                               'confirm': {'type': 'boolean'}}}),
    OperationDescriptor(
        _ID + 'build', 'Build one geometry operation', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Run one operation of the geometry build panel on the '
                'session. values holds the form: numbers or expressions, '
                'and [dim, tag] pairs for entity fields.',
        parameters_schema={'type': 'object', 'required': ['operation_id'],
                           'properties': {
                               'operation_id': {'type': 'string'},
                               'values': {'type': 'object'}}}),
    OperationDescriptor(
        _ID + 'steps', 'The geometry steps built so far', 'case',
        ImpactClass.READ),
    OperationDescriptor(
        _ID + 'remove_last', 'Remove the last geometry command', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Take back the last built command; the session rebuilds '
                'the model from the ones before it.',
        parameters_schema={'type': 'object', 'properties': {
            'count': {'type': 'integer', 'minimum': 1}}}),
    OperationDescriptor(
        _ID + 'checkpoint_save', 'Save a geometry checkpoint', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema={'type': 'object', 'required': ['name'],
                           'properties': {'name': {'type': 'string'}}}),
    OperationDescriptor(
        _ID + 'checkpoint_restore', 'Go back to a geometry checkpoint',
        'case', ImpactClass.MESH_MUTATION,
        summary='Rebuilds the model at the checkpoint and verifies it '
                'against the saved inventory.',
        parameters_schema={'type': 'object', 'required': ['name'],
                           'properties': {'name': {'type': 'string'}}}),
    OperationDescriptor(
        _ID + 'checkpoints', 'The saved geometry checkpoints', 'case',
        ImpactClass.READ),
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
    if isinstance(error, BuildRefusal):
        return ValidationFailedError(str(error))
    return PreconditionFailedError(str(error))


def _field_row(spec) -> dict:
    return {'name': spec.name, 'title': spec.title, 'kind': spec.kind,
            'default': spec.default if not isinstance(spec.default, tuple)
            else list(spec.default),
            'dim': spec.dim, 'choices': list(spec.choices),
            'optional': spec.optional, 'length': spec.length,
            'help': spec.help}


def _operations(session, command) -> OperationResult:
    from foammesh.core.gmsh import geometry_build as gb

    kernel = str(command.parameters.get('kernel') or '').strip()
    if not kernel:
        kernel = _builder(session).kernel
    if kernel not in gb.KERNELS:
        raise ValidationFailedError(
            'kernel must be one of %s' % ', '.join(gb.KERNELS))
    rows = []
    for op in gb.OPERATIONS:
        rows.append({'id': op.id, 'title': op.title, 'group': op.group,
                     'kind': op.kind, 'row': op.row,
                     'kernels': list(op.kernels),
                     'unavailable': gb.unavailable(op.id, kernel),
                     'fields': [_field_row(f) for f in op.fields]})
    return _result(session, command, {
        'kernel': kernel, 'kernels': {k: gb.KERNEL_TITLES[k]
                                      for k in gb.KERNELS},
        'groups': list(gb.GROUPS), 'operations': rows})


def _kernel(session, command) -> OperationResult:
    from foammesh.core.gmsh import geometry_build as gb
    kernel = _builder(session).kernel
    return _result(session, command, {'kernel': kernel,
                                      'title': gb.KERNEL_TITLES[kernel]})


async def _set_kernel(session, command) -> OperationResult:
    kernel = str(command.parameters.get('kernel') or '').strip()
    if not kernel:
        raise ValidationFailedError('kernel is required')
    confirm = bool(command.parameters.get('confirm'))
    builder = _builder(session)

    def work():
        return builder.set_kernel(kernel, confirm=confirm)

    try:
        done = await asyncio.to_thread(work)
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, done)


def _step_row(step) -> dict:
    return {'operation_id': step.op_id, 'title': step.title,
            'created': [list(c) for c in step.created],
            'entities': dict(step.entities), 'loop': step.loop,
            'kernel': step.kernel}


async def _build(session, command) -> OperationResult:
    op_id = str(command.parameters.get('operation_id') or '').strip()
    if not op_id:
        raise ValidationFailedError('operation_id is required')
    values = command.parameters.get('values')
    if values is None:
        values = {}
    # an empty list is not an object either (``or {}`` let it through)
    if not isinstance(values, dict):
        raise ValidationFailedError('values must be an object')
    controller = _controller(session)
    _builder(session)

    def work():
        return controller.build(op_id, values)

    try:
        step = await asyncio.to_thread(work)
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, _step_row(step))


def _steps(session, command) -> OperationResult:
    builder = _builder(session)
    return _result(session, command, {
        'kernel': builder.kernel,
        'steps': [_step_row(s) for s in builder.steps],
        'parameters': [{'name': n, 'expression': e, 'value': v}
                       for n, e, v in builder.parameters.rows()]})


async def _remove_last(session, command) -> OperationResult:
    from foammesh.core.gmsh import checkpoints
    try:
        count = int(command.parameters.get('count') or 1)
    except (TypeError, ValueError):
        raise ValidationFailedError('count must be a whole number') from None
    builder = _builder(session)

    def work():
        return checkpoints.remove_last(builder, count)

    try:
        done = await asyncio.to_thread(work)
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    done.pop('session', None)
    return _result(session, command, done)


def _named(command) -> str:
    name = str(command.parameters.get('name') or '').strip()
    if not name:
        raise ValidationFailedError('name is required')
    return name


def _session_call(session, operation, **kwargs):
    builder = _builder(session)
    return builder._session_provider().call(operation, **kwargs)


async def _checkpoint_save(session, command) -> OperationResult:
    name = _named(command)
    try:
        done = await asyncio.to_thread(
            _session_call, session, 'session.checkpoint_save', name=name)
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, done)


async def _checkpoint_restore(session, command) -> OperationResult:
    name = _named(command)
    builder = _builder(session)

    def work():
        done = builder._session_provider().call(
            'session.checkpoint_restore', name=name)
        position = builder._session_provider().call('session.script_position')
        kept = max(0, int(position.get('removable', 0)))
        del builder.steps[kept:]
        return done

    try:
        done = await asyncio.to_thread(work)
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, done)


async def _checkpoints(session, command) -> OperationResult:
    try:
        done = await asyncio.to_thread(
            _session_call, session, 'session.checkpoint_list')
    except Exception as error:                           # noqa: BLE001
        raise _refusal(error) from error
    return _result(session, command, done)


def handlers() -> dict:
    return {
        _ID + 'operations': _operations,
        _ID + 'kernel': _kernel,
        _ID + 'set_kernel': _set_kernel,
        _ID + 'build': _build,
        _ID + 'steps': _steps,
        _ID + 'remove_last': _remove_last,
        _ID + 'checkpoint_save': _checkpoint_save,
        _ID + 'checkpoint_restore': _checkpoint_restore,
        _ID + 'checkpoints': _checkpoints,
    }
