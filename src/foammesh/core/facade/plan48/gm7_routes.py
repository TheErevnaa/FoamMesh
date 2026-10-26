"""Plan 48 GM7 local-API routes: the Mesh actions beyond Generate.

Plain remote control of what the Generate panel and the Mesh menu do:

``gmsh.mesh.actions``
    The actions (High-order tools, Reverse, Delete, Reclassify, Experimental)
    with the controls each takes, the High-order tools' algorithms, ranges and
    the fields of Gmsh's window that have no option behind them.
``gmsh.mesh.plan``
    What an action would do, or why it cannot, without running it.
``gmsh.mesh.run``
    Run any Generate action on the resident session (1D, 2D, 3D, All,
    Optimize, Refine, Partition, Delete, ... as well as the GM7 ones). With a
    window showing the case the run is the Generate panel's own (the form
    filled, the Run button's code, the status line and a timeline row); with
    none it is the same session action (a run entry, ``job.cancel_active``
    reaches it, a cancel rebuilds the session from its log).
``gmsh.mesh.elements``
    Element tags of entities, optionally only those below a quality, for the
    pickers of Reverse, Delete and Reclassify.
``gmsh.mesh.order``
    The highest element order the model holds.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult
from foammesh.core.gmsh import generate_actions as _ga

#: The actions GM7 added to the Generate panel.
GM7_ACTION_IDS = (
    'set_order', 'regularize', 'reverse_elements', 'reverse_entities',
    'delete_elements', 'delete_entity_mesh', 'reclassify_subset',
    'reclassify_preview', 'quad_layout', 'optimize_quad_topology',
    'untangle_geometry',
)

#: Plan 48 PA2. Every action of the Generate panel: ``gmsh.mesh.run`` takes
#: all of them, as the window's Run button does.
ACTION_IDS = tuple(_ga.ACTIONS)

_VALUES = {'type': 'object'}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.mesh.actions', 'Mesh actions of the Generate panel', 'case',
        ImpactClass.READ,
        summary='Every action of the Generate panel (1D, 2D, 3D, Optimize, '
                'Set order, Refine, Partition, Delete, Reclassify, '
                'High-order tools, Reverse, Experimental, ...): its help, '
                'keys and the form of each control (kind, default, range, '
                'choices) and Gmsh option (kind, default, choices), the '
                'algorithms and ranges of the High-order tools, and the '
                'fields of Gmsh\'s window that have no option behind '
                'them.'),
    OperationDescriptor(
        'gmsh.mesh.plan', 'Plan a mesh action', 'case', ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['action'],
                           'properties': {'action': {'type': 'string'},
                                          'values': _VALUES}},
        summary='The session operations an action expands to, or the reason '
                'it is refused. Nothing runs.'),
    OperationDescriptor(
        'gmsh.mesh.run', 'Run a mesh action', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema={'type': 'object', 'required': ['action'],
                           'properties': {'action': {'enum': list(
                               ACTION_IDS)}, 'values': _VALUES}},
        summary='Run any Generate action on the resident session, as the '
                'panel\'s Run button does: the run is a session action '
                '(job.cancel_active stops it), is listed in the stage '
                'timeline and the run listing. Returns the stage with the '
                'mesh counts before and after.'),
    OperationDescriptor(
        'gmsh.mesh.elements', 'Element tags for picking', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'entities': {'type': 'array'}, 'dim': {'type': 'integer'},
            'below': {'type': 'number'}, 'measure': {'type': 'string'},
            'limit': {'type': 'integer'}}},
        summary='Element tags of the entities (or of one dimension), '
                'optionally only those whose quality is below a value.'),
    OperationDescriptor(
        'gmsh.mesh.order', 'Highest element order', 'case', ImpactClass.READ,
        summary='The highest element order in the model (1 with no mesh).'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _values(command) -> dict:
    values = (command.parameters or {}).get('values') or {}
    if not isinstance(values, dict):
        raise ValidationFailedError('values must be an object')
    return values


def _action_id(command, *, only_ours=False) -> str:
    action = (command.parameters or {}).get('action')
    if not isinstance(action, str) or not action:
        raise ValidationFailedError('action is required')
    if only_ours and action not in ACTION_IDS:
        raise ValidationFailedError(
            f'"{action}" is not a Generate action; they are: '
            + ', '.join(ACTION_IDS), details={'action': action})
    return action


def _actions(session, command) -> OperationResult:
    from foammesh.core.gmsh import generate_actions as ga

    rows = []
    for action_id in ACTION_IDS:
        chosen = ga.ACTIONS[action_id]
        form = ga.form(action_id)
        rows.append({'id': chosen.id, 'title': chosen.title,
                     'group': chosen.group, 'help': chosen.help,
                     'needs_dimension': chosen.needs,
                     'options': list(chosen.options),
                     'arguments': list(chosen.arguments),
                     'keys': form['shortcuts'],
                     'argument_forms': form['arguments'],
                     'option_forms': form['options']})
    return _result(session, command, {
        'actions': rows, 'order_range': list(ga.ORDER_RANGE),
        'regularize': {
            'algorithms': [
                {'id': i, 'title': t, 'method': m or '',
                 'available': m is not None, 'options': list(o)}
                for i, t, m, o in ga.REGULARIZE_ALGORITHMS],
            'ranges': {k: list(v) for k, v in ga.REGULARIZE_RANGES.items()},
            'unreachable': {k: {'label': v[0], 'gmsh_value': v[1]}
                            for k, v in ga.REGULARIZE_UNREACHABLE.items()},
        },
        'menu': [{'action': a, 'menu': m, 'order': o}
                 for a, m, o in ga.MENU_ENTRIES],
        'keys': dict(ga.SHORTCUTS),
        'pick_filters': list(ga.PICK_FILTERS),
        'element_pick_filters': list(ga.ELEMENT_PICK_FILTERS)})


def _plan(session, command) -> OperationResult:
    from foammesh.core.gmsh import generate_actions as ga

    try:
        planned = ga.plan(_action_id(command), _values(command), None)
    except ga.ActionRefusal as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {
        'action': planned.action, 'notes': list(planned.notes),
        'ops': [op.as_request() for op in planned.ops]})


def _stage_payload(info) -> dict:
    stage = info.get('stage')
    return {'stage': stage.as_dict() if stage is not None else None,
            'outcome': info['outcome'], 'status': info['message'],
            'job_id': info.get('job_id', '')}


async def _run_in_window(session, api, command, action, values):
    """The Generate panel's own run (form, status line, timeline row)."""
    try:
        info = await asyncio.to_thread(api.run, action, values)
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    payload = _stage_payload(info)
    stage = info.get('stage')
    if info['outcome'] != 'done':
        raise PreconditionFailedError(
            (stage.error if stage is not None and stage.error
             and info['outcome'] == 'failed' else info['message']),
            details=payload)
    return _result(session, command, payload)


async def _run_headless(session, command, action, values):
    """The same run with no window: a session action, a recovery on cancel."""
    from foammesh.core.facade.plan48.pa2_routes import begin_session_action

    controller = _controller(session)
    title = _ga.action(action).title
    handle = begin_session_action(controller, title)
    job_id = getattr(handle, 'job_id', '')
    try:
        stage = await asyncio.to_thread(controller.run, action, values)
    except Exception as error:                           # noqa: BLE001
        message = str(error)
        outcome = (handle.finish(False, message)
                   if handle is not None else 'failed')
        if outcome == 'cancelled':
            message = ('Cancelled; the session was rebuilt from its log.')
        message = _recover(controller, message)
        raise PreconditionFailedError(message, details={
            'outcome': outcome, 'job_id': job_id}) from error
    outcome = (handle.finish(bool(stage.ok), stage.error or '')
               if handle is not None else ('done' if stage.ok else 'failed'))
    if outcome == 'cancelled':
        message = _recover(controller, (
            f'{stage.title}: cancelled; the session was rebuilt from its '
            'log.'))
    elif stage.ok:
        message = f'{stage.title}: done in {stage.seconds:.2f} s'
    else:
        message = stage.error
    payload = {'stage': stage.as_dict(), 'outcome': outcome,
               'status': message, 'job_id': job_id}
    if outcome != 'done':
        raise PreconditionFailedError(message, details=payload)
    return _result(session, command, payload)


def _recover(controller, message: str) -> str:
    """After a cancel: a fresh process plus the logged operations."""
    try:
        controller.recover()
    except Exception as error:                           # noqa: BLE001
        return f'{message} ({error})'
    return message


async def run_action(session, command, action, values) -> OperationResult:
    """Run *action* as a click on the Generate panel's Run does.

    One entry for ``gmsh.mesh.run`` and the digit keys (``gmsh.mesh.key``):
    the refusal before any wait is the panel's (``plan``), then the window's
    panel runs it when a window shows the case, else the controller does.
    """
    try:
        _ga.plan(action, values, None)
    except _ga.ActionRefusal as error:
        raise ValidationFailedError(str(error)) from error
    from foammesh.core.facade.plan48 import pa2_routes

    api = pa2_routes.window_api(session, pa2_routes.GENERATE_ATTRIBUTE)
    if api is not None:
        return await _run_in_window(session, api, command, action, values)
    return await _run_headless(session, command, action, values)


async def _run(session, command) -> OperationResult:
    action = _action_id(command, only_ours=True)
    return await run_action(session, command, action, _values(command))


def _live(session):
    controller = _controller(session)
    live = controller.session
    if live is None or not live.alive:
        raise PreconditionFailedError(
            'there is no mesh yet; generate one first')
    return live


async def _elements(session, command) -> OperationResult:
    from foammesh.core.gmsh import generate_actions as ga

    parameters = command.parameters or {}
    try:
        entities = ga.dim_tags(parameters.get('entities'))
    except ga.ActionRefusal as error:
        raise ValidationFailedError(str(error)) from error
    live = _live(session)
    kwargs = {'entities': entities,
              'dim': parameters.get('dim'),
              'below': parameters.get('below'),
              'measure': str(parameters.get('measure') or 'minSICN'),
              'limit': int(parameters.get('limit') or 100000)}
    answer = await asyncio.to_thread(
        live.call, 'session.list_mesh_elements', **kwargs)
    # The Poor and Pick buttons fill the list with the shortest text.
    answer = dict(answer or {})
    text = ga.compress_tags(answer.get('elements') or [])
    answer['text'] = text + (' ...' if answer.get('truncated') else '')
    return _result(session, command, answer)


async def _order(session, command) -> OperationResult:
    live = _live(session)
    answer = await asyncio.to_thread(live.call, 'session.get_mesh_order')
    return _result(session, command, answer)


def handlers() -> dict:
    return {
        'gmsh.mesh.actions': _actions,
        'gmsh.mesh.plan': _plan,
        'gmsh.mesh.run': _run,
        'gmsh.mesh.elements': _elements,
        'gmsh.mesh.order': _order,
    }
