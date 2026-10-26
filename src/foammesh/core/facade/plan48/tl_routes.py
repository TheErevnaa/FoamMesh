"""Plan 48 TL local-API routes: the time limits of the Gmsh calls that can hang.

Gmsh 4.15.2 leaves some calls hanging (a plugin such as CurvedBndDist,
``computeCrossField``, ``createTopology``, ``Recombine3D``). FoamMesh runs
each of them under a time limit; one that passes it is refused with
"Gmsh 4.15.2 limit: <what> did not return in <n> s" and the session is
restarted from its log, so it is alive afterwards. A plain remote control for
the same limits the Import panel's "Time limit" box sets:

``gmsh.time_limits``
    The limit of every kind of call (seconds, 0 = none), its default and what
    it guards.
``gmsh.time_limits.set``
    Set the limit of one kind for the project (``seconds`` null restores the
    default). ``gmsh.model.reclassify`` and ``gmsh.model.create_topology`` use
    the limits of the kinds ``reclassify`` and ``topology``.
``gmsh.model.reclassify`` / ``gmsh.model.create_topology``
    The Import panel's "Reclassify" and "Create topology" buttons, with the
    time limit of the call (``time_limit``) when you want a different one.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_LIMIT = {'type': ['number', 'null'], 'minimum': 0}


def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


OPERATIONS = (
    OperationDescriptor(
        'gmsh.time_limits', 'Time limits of the Gmsh calls that can hang',
        'case', ImpactClass.READ,
        parameters_schema=_schema(),
        summary='The limit in seconds (0 = none) of each kind of Gmsh call '
                'that can hang on Gmsh 4.15.2: plugin, cross_field (Compute '
                'quad layout), topology (createTopology), reclassify '
                '(Reclassify 2D), recombine3d (hex-dominant meshing); with '
                'the default of each and what it guards. A call that passes '
                'its limit is refused with a message that starts "Gmsh '
                '4.15.2 limit" and the session is restarted from its log.'),
    OperationDescriptor(
        'gmsh.time_limits.set', 'Set the time limit of a Gmsh call',
        'case', ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('kind',), kind={'type': 'string'},
                                  seconds=_LIMIT),
        summary='Set the limit, in seconds, of one kind of call for this '
                'project (0 = wait without a limit, null = the default). '
                'Returns every limit as gmsh.time_limits does.'),
    OperationDescriptor(
        'gmsh.model.reclassify', 'Reclassify 2D in the Gmsh session',
        'case', ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(
            angle={'type': 'number'}, boundary={'type': 'boolean'},
            reparametrize={'type': 'boolean'},
            curve_angle={'type': 'number'},
            export_discrete={'type': 'boolean'},
            create_geometry={'type': 'boolean'}, time_limit=_LIMIT),
        summary='The Import panel\'s Reclassify button: classifySurfaces '
                'with every argument it has. time_limit (seconds, 0 = none) '
                'replaces the project limit of kind reclassify for this '
                'call.'),
    OperationDescriptor(
        'gmsh.model.create_topology', 'Create topology in the Gmsh session',
        'case', ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(
            make_simply_connected={'type': 'boolean'},
            export_discrete={'type': 'boolean'}, time_limit=_LIMIT),
        summary='The Import panel\'s Create topology button: createTopology '
                'on the session model. Gmsh 4.15.2 can leave it hanging: '
                'it stops at time_limit (seconds, 0 = none; else the '
                'project limit of kind topology) with a "Gmsh 4.15.2 limit" '
                'refusal and the session survives.'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _payload(controller) -> dict:
    from foammesh.core.gmsh import generate_actions as ga

    limits = controller.time_limits()
    return {
        'tag': ga.LIMIT_TAG,
        'limits': limits,
        'defaults': dict(ga.DEFAULT_LIMITS),
        'kinds': [{'kind': kind, 'seconds': limits[kind],
                   'default': ga.DEFAULT_LIMITS[kind],
                   'guards': ga.LIMIT_WHAT[kind],
                   'set': kind in controller.limits}
                  for kind in ga.DEFAULT_LIMITS]}


def _time_limits(session, command) -> OperationResult:
    return _result(session, command, _payload(_controller(session)))


def _per_call(command):
    value = (command.parameters or {}).get('time_limit')
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value < 0:
        raise ValidationFailedError(
            'time_limit is a number of seconds, 0 for none',
            details={'time_limit': value})
    return float(value)


def _set(session, command) -> OperationResult:
    from foammesh.core.gmsh import generate_actions as ga

    parameters = command.parameters or {}
    kind = parameters.get('kind')
    controller = _controller(session)
    seconds = parameters.get('seconds')
    if isinstance(seconds, bool):
        raise ValidationFailedError('seconds is a number, 0 for none')
    try:
        controller.set_time_limit(kind, seconds)
    except ga.ActionRefusal as error:
        raise ValidationFailedError(
            str(error), details={'kind': kind}) from error
    return _result(session, command, _payload(controller))


def _refused(error, kind, seconds=None):
    from foammesh.core.gmsh import generate_actions as ga

    details = {}
    if isinstance(error, ga.TimeLimitRefusal):
        details['limit'] = {'kind': kind, 'seconds': error.seconds,
                            'what': error.what, 'tag': ga.LIMIT_TAG}
    return PreconditionFailedError(str(error), details=details)


async def _create_topology(session, command) -> OperationResult:
    from foammesh.core.gmsh import generate_actions as ga

    parameters = command.parameters or {}
    limit = _per_call(command)
    controller = _controller(session)

    def work():
        return controller.create_topology(
            parameters.get('make_simply_connected', True),
            parameters.get('export_discrete', True), time_limit=limit)

    try:
        answer = await asyncio.to_thread(work)
    except ga.ActionRefusal as error:
        raise _refused(error, 'topology') from error
    except RuntimeError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, {'result': answer})


async def _reclassify(session, command) -> OperationResult:
    from foammesh.core.gmsh import generate_actions as ga
    from foammesh.core.gmsh import import_formats as formats

    parameters = dict(command.parameters or {})
    limit = _per_call(command)
    parameters.pop('time_limit', None)
    try:
        formats.reclassify_arguments(**parameters)
    except formats.ImportRefusal as error:
        raise ValidationFailedError(str(error)) from error
    controller = _controller(session)

    def work():
        return controller.reclassify(time_limit=limit, **parameters)

    try:
        answer = await asyncio.to_thread(work)
    except ga.ActionRefusal as error:
        raise _refused(error, 'reclassify') from error
    except RuntimeError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, {'result': answer})


def handlers() -> dict:
    return {
        'gmsh.time_limits': _time_limits,
        'gmsh.time_limits.set': _set,
        'gmsh.model.reclassify': _reclassify,
        'gmsh.model.create_topology': _create_topology,
    }
