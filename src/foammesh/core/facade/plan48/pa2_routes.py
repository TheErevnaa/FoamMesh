"""Plan 48 PA2 local-API routes: Generate, Entity and Entity controls panels.

A plain remote control for what three panels of the Gmsh route do in the
window. Each panel registers an API object on the case session
(``gmsh_generate_api``, ``gmsh_entity_api``, ``gmsh_entity_controls_api``);
a route calls it, so the window and the API run the same handlers, and the
routes answer ``precondition_failed`` when no window shows the case. The
catalogues and the row checks need no window.

Entity controls
    ``gmsh.entity_controls.catalogue``  the kinds Add control offers, with
    every parameter's kind, default, range and choices;
    ``.add_row`` the rows with a new row of a kind (as Add control builds
    it; keep them with ``configuration.patch``); ``.check`` the reason the
    rows cannot be applied (the red line under the table); ``.state`` the
    panel's pending rows, summary, help and problem; ``.pick`` the Pick
    button of a row.
Entity panel
    ``gmsh.entities.*`` (state, show_model, model, filter, pick, labels,
    coherence), ``gmsh.groups.*`` (list, add, set_members, rename, remove)
    and ``gmsh.selections.*`` (list, save, show, delete).
Generate panel
    ``gmsh.mesh.state`` / ``.timeline`` (the status line and stage timeline),
    ``.key`` (the 1, 2, 3, F2 and F3 keys), ``.pick`` and ``.poor`` (the Pick
    and Poor buttons of the entity and element lists). ``gmsh.mesh.run`` is
    in ``gm7_routes``.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

GENERATE_ATTRIBUTE = 'gmsh_generate_api'
ENTITY_ATTRIBUTE = 'gmsh_entity_api'
ENTITY_CONTROLS_ATTRIBUTE = 'gmsh_entity_controls_api'


# -- the window registers its panels ------------------------------------------- #

def case_session_of(client=None):
    """The case session a window panel belongs to, or ``None``."""
    try:
        if client is None:
            from foammesh.app import app
            client = getattr(app, 'facadeClient', None)
        return client.session() if client is not None else None
    except Exception:                                    # noqa: BLE001
        return None


def register(session, attribute: str, api) -> None:
    """Make *api* the panel the routes call for *session*."""
    setattr(session, attribute, api)


def unregister(session, attribute: str, api) -> None:
    """Forget *api* (only if it is still the registered one)."""
    if getattr(session, attribute, None) is api:
        setattr(session, attribute, None)


def window_api(session, attribute: str):
    """The registered panel API, or ``None`` with no window."""
    return getattr(session, attribute, None)


def begin_session_action(controller, name: str):
    """Start the run entry of a Gmsh session action (Plan 42 SG16).

    The Generate panel and an API run both use it, so the Live run panel,
    the Console, the run listing and ``job.cancel_active`` hear of either.
    """
    from foammesh.core.jobs import session_actions

    if controller is None or not hasattr(controller, 'case_path'):
        return None
    publish = None
    try:
        from foammesh.app import app
        publish = app.events.publish
    except Exception:                                    # noqa: BLE001
        pass
    return session_actions.begin(
        controller.case_path, name, cancel=controller.cancel,
        publish=publish)


# -- schemas --------------------------------------------------------------------- #

def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


_ENTITY = {'type': 'array', 'minItems': 2, 'maxItems': 2,
           'items': {'type': 'integer'}}
_ENTITIES = {'type': 'array', 'items': _ENTITY}
_ROWS = {'type': 'array', 'items': {'type': 'object'}}

OPERATIONS = (
    # -- Entity controls ------------------------------------------------------- #
    OperationDescriptor(
        'gmsh.entity_controls.catalogue', 'Entity control kinds', 'case',
        ImpactClass.READ,
        summary='The kinds Add control offers (embed, recombine, algorithm, '
                'smoothing, size from boundary, reverse, outward '
                'orientation, compound) with their help and every '
                "parameter's kind, default, range and choices."),
    OperationDescriptor(
        'gmsh.entity_controls.add_row', 'Add an entity control', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(
            ('kind',), kind={'type': 'string'}, tags={'type': 'array'},
            dim={'type': 'integer'}, rows=_ROWS),
        summary='The rows with a new row of a kind, named and filled as the '
                'Add control button does; send them back with '
                'configuration.patch on gmsh.entityControls.rows to keep '
                'them.'),
    OperationDescriptor(
        'gmsh.entity_controls.check', 'Check the entity controls', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(rows=_ROWS),
        summary='The reason the stored (or given) rows cannot be applied, '
                'as the panel shows it under the table, or ok.'),
    OperationDescriptor(
        'gmsh.entity_controls.state', 'Entity controls panel', 'case',
        ImpactClass.READ,
        summary='What the Entity controls panel shows: its rows (pending '
                'edits included), the count line, the help of the chosen '
                'kind and the red problem line.'),
    OperationDescriptor(
        'gmsh.entity_controls.pick', 'Pick the entities of a control',
        'case', ImpactClass.READ,
        parameters_schema=_schema(row={'type': 'integer', 'minimum': 0}),
        summary="Start the Pick button of a control row (the chosen row "
                "when none is given). The entities arrive when "
                "gmsh.entities.pick accepts."),
    # -- Entity panel ------------------------------------------------------------ #
    OperationDescriptor(
        'gmsh.entities.state', 'Entity panel', 'case', ImpactClass.READ,
        summary='What the Entity panel shows: the status line, the '
                'selection filter, the pick in progress, the chips, the '
                'physical groups, the saved selections and the labels.'),
    OperationDescriptor(
        'gmsh.entities.show_model', 'Show model for picking', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Open the geometry in the Gmsh session and draw its points, '
                'curves, surfaces and volumes for picking, as the button '
                'does; returns the panel state.'),
    OperationDescriptor(
        'gmsh.entities.model', 'Entities behind picking', 'case',
        ImpactClass.READ,
        summary='The entity table the viewport picks from: dimension, tag, '
                'name, physical groups, centre and point coordinates.'),
    OperationDescriptor(
        'gmsh.entities.filter', 'Selection filter', 'case', ImpactClass.READ,
        parameters_schema=_schema(
            ('name',), name={'type': 'string', 'enum': [
                'Points', 'Curves', 'Surfaces', 'Volumes', 'Cells']}),
        summary='Choose what a pick selects: Points, Curves, Surfaces or '
                'Volumes.'),
    OperationDescriptor(
        'gmsh.entities.pick', 'Pick entities', 'case', ImpactClass.READ,
        parameters_schema=_schema(
            ('action',),
            action={'type': 'string', 'enum': [
                'start', 'click', 'drop', 'accept', 'abort', 'set']},
            entities=_ENTITIES, filter={'type': 'string'},
            extend={'type': 'boolean'}),
        summary='Drive the one Pick tool: start a pick, click entities (a '
                'click replaces, extend adds), drop a chip, accept (Enter), '
                'abort (Escape), or set = start, click each and accept.'),
    OperationDescriptor(
        'gmsh.entities.labels', 'Entity labels', 'case', ImpactClass.READ,
        parameters_schema=_schema(shown={'type': 'boolean'}),
        summary='Show or hide the entity labels in the viewport; with no '
                'value, whether they are shown.'),
    OperationDescriptor(
        'gmsh.entities.coherence', 'Coherence', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Remove duplicate entities (Geometry, Coherence in Gmsh); '
                'returns how many of each kind were removed.'),
    OperationDescriptor(
        'gmsh.groups.list', 'Physical groups', 'case', ImpactClass.READ,
        parameters_schema=_schema(reload={'type': 'boolean'}),
        summary='The physical groups of the open model, as the Entity '
                'panel lists them (kind, tag, name, entities, origin).'),
    OperationDescriptor(
        'gmsh.groups.add', 'Add a physical group', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('name',), name={'type': 'string'},
                                  entities=_ENTITIES),
        summary='Add a group from the picked entities (or the entities '
                'given, picked as a click does) under a name.'),
    OperationDescriptor(
        'gmsh.groups.set_members', 'Set the members of a group', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('dim', 'tag'), dim={'type': 'integer'},
                                  tag={'type': 'integer'},
                                  entities=_ENTITIES),
        summary='Replace the members of a group by the picked entities (or '
                'the entities given).'),
    OperationDescriptor(
        'gmsh.groups.rename', 'Rename a physical group', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('dim', 'tag', 'name'),
                                  dim={'type': 'integer'},
                                  tag={'type': 'integer'},
                                  name={'type': 'string'}),
        summary='Rename a group; a shared surface name is renamed in the '
                'shared surface names, as the button does.'),
    OperationDescriptor(
        'gmsh.groups.remove', 'Remove a physical group', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('dim', 'tag'), dim={'type': 'integer'},
                                  tag={'type': 'integer'}),
        summary='Remove a physical group of Gmsh\'s own; a shared surface '
                'name is refused.'),
    OperationDescriptor(
        'gmsh.selections.list', 'Saved entity selections', 'case',
        ImpactClass.READ,
        summary='The saved entity selections of the case: name, entities.'),
    OperationDescriptor(
        'gmsh.selections.save', 'Save an entity selection', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('name',), name={'type': 'string'},
                                  entities=_ENTITIES),
        summary='Keep the picked entities (or the entities given) under a '
                'name with the case.'),
    OperationDescriptor(
        'gmsh.selections.show', 'Show an entity selection', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(('name',), name={'type': 'string'}),
        summary='Make a saved selection the picked entities, as the Show '
                'selection button does.'),
    OperationDescriptor(
        'gmsh.selections.delete', 'Delete an entity selection', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema=_schema(('name',), name={'type': 'string'}),
        summary='Delete a saved entity selection.'),
    # -- Generate panel ---------------------------------------------------------- #
    OperationDescriptor(
        'gmsh.mesh.state', 'Generate panel', 'case', ImpactClass.READ,
        summary='What the Generate panel shows: the chosen action, whether a '
                'run is going, the status line (running, done, cancelled or '
                'the error), Run and Cancel enabled, and the stage '
                'timeline.'),
    OperationDescriptor(
        'gmsh.mesh.timeline', 'Stage timeline', 'case', ImpactClass.READ,
        summary='The stages run on the resident session: the rows of the '
                'timeline table (stage, time, nodes, elements, result) and '
                'each stage in full.'),
    OperationDescriptor(
        'gmsh.mesh.key', 'Generate keys 1, 2, 3, F2, F3', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(
            ('key',), key={'type': 'string',
                           'enum': ['1', '2', '3', 'F2', 'F3']}),
        summary="Press one of Gmsh's Mesh keys: 1 meshes in 1D, 2 and F2 in "
                '2D, 3 and F3 in 3D, exactly as the Generate panel runs '
                'them.'),
    OperationDescriptor(
        'gmsh.mesh.pick', 'Pick for a Generate action', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(
            ('argument',), argument={'type': 'string',
                                     'enum': ['entities', 'elements']},
            filter={'type': 'string', 'enum': [
                'Points', 'Curves', 'Surfaces', 'Volumes', 'Cells']}),
        summary="Start the Pick button of the chosen action's entity or "
                'element list, with the pick filter chosen beside it. Cells '
                'picks single mesh elements (element tags) while a mesh is '
                'on screen.'),
    OperationDescriptor(
        'gmsh.mesh.poor', 'Poor elements for a Generate action', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(below={'type': 'number'}),
        summary="The Poor button: fill the chosen action's element list "
                'with the tags of the elements whose quality (minSICN) is '
                'below a value; returns the text it shows.'),
)


# -- helpers --------------------------------------------------------------------- #

def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _api(session, attribute: str, what: str):
    api = window_api(session, attribute)
    if api is None:
        raise PreconditionFailedError(
            f'no window is showing this case, and the {what} lives in it: '
            'open the case in the FoamMesh window',
            details={'needs': attribute})
    return api


async def _call(session, attribute, what, method, *args, **kwargs):
    api = _api(session, attribute, what)
    try:
        return await asyncio.to_thread(
            lambda: getattr(api, method)(*args, **kwargs))
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error


def _parameters(command) -> dict:
    return dict(command.parameters or {})


def _rows_param(parameters):
    given = parameters.get('rows')
    if given is None:
        return None
    if not isinstance(given, list):
        raise ValidationFailedError('rows must be a list')
    return [dict(item) for item in given if isinstance(item, dict)]


def _stored_rows(session) -> list:
    from foammesh.core.facade import FIELD_REGISTRY
    from foammesh.core.facade.field_adapters import read_value
    from foammesh.core.gmsh import entity_controls as entity

    try:
        value = read_value(session.configuration(),
                           FIELD_REGISTRY.get(entity.FIELD_ID).storage_path)
    except Exception:                                    # noqa: BLE001
        value = None
    try:
        return entity.rows_from_text(getattr(value, 'value', value))
    except entity.EntityRefusal as error:
        raise ValidationFailedError(str(error)) from error


# -- Entity controls -------------------------------------------------------------- #

async def _ec_catalogue(session, command):
    from foammesh.core.gmsh import entity_controls as entity

    return _result(session, command, {'kinds': entity.catalogue(),
                                      'field_id': entity.FIELD_ID})


async def _ec_add_row(session, command):
    from foammesh.core.gmsh import entity_controls as entity

    p = _parameters(command)
    kind = str(p.get('kind') or '')
    if kind not in entity.KINDS:
        raise ValidationFailedError(
            f'{kind!r} is not a kind of entity control',
            details={'kinds': list(entity.KINDS)})
    rows = _rows_param(p)
    if rows is None:
        rows = _stored_rows(session)
    spec = entity.KINDS[kind]
    dim = p.get('dim')
    if dim is not None and int(dim) not in spec.dims:
        raise ValidationFailedError(
            f'{spec.title} takes entities of dimension '
            + ', '.join(str(d) for d in spec.dims),
            details={'dims': list(spec.dims)})
    row = entity.new_row(kind, entity.next_row_name(kind, rows), dim)
    if p.get('tags'):
        try:
            row['tags'] = entity.parse_tags(p['tags'])
        except entity.EntityRefusal as error:
            raise ValidationFailedError(str(error)) from error
    rows = [*rows, row]
    return _result(session, command, {
        'row': row, 'rows': rows, 'rows_text': entity.rows_to_text(rows),
        'field_id': entity.FIELD_ID})


async def _ec_check(session, command):
    from foammesh.core.gmsh import entity_controls as entity

    p = _parameters(command)
    rows = _rows_param(p)
    if rows is None:
        api = window_api(session, ENTITY_CONTROLS_ATTRIBUTE)
        if api is not None:
            answer = await asyncio.to_thread(api.check)
            return _result(session, command, answer)
        rows = _stored_rows(session)
    try:
        built = entity.validate(rows)
    except entity.EntityRefusal as error:
        return _result(session, command, {'ok': False, 'reason': str(error)})
    return _result(session, command, {
        'ok': True, 'reason': '', 'controls': built.to_list(),
        'warnings': list(built.warnings)})


async def _ec_state(session, command):
    return _result(session, command, await _call(
        session, ENTITY_CONTROLS_ATTRIBUTE, 'Entity controls panel', 'state'))


async def _ec_pick(session, command):
    p = _parameters(command)
    return _result(session, command, await _call(
        session, ENTITY_CONTROLS_ATTRIBUTE, 'Entity controls panel', 'pick',
        p.get('row')))


# -- Entity panel ------------------------------------------------------------------ #

def _entity_call(session, method, *args, **kwargs):
    return _call(session, ENTITY_ATTRIBUTE, 'Entity panel', method, *args,
                 **kwargs)


def _entities_param(p, key='entities'):
    given = p.get(key)
    if given is None:
        return None
    try:
        return [(int(d), int(t)) for d, t in given]
    except (TypeError, ValueError):
        raise ValidationFailedError(
            f'{key} must be a list of [dimension, tag] pairs') from None


async def _e_state(session, command):
    return _result(session, command, await _entity_call(session, 'state'))


async def _e_show_model(session, command):
    return _result(session, command,
                   await _entity_call(session, 'show_model'))


async def _e_model(session, command):
    rows = await _entity_call(session, 'entities')
    return _result(session, command, {'entities': rows, 'count': len(rows)})


async def _e_filter(session, command):
    return _result(session, command, await _entity_call(
        session, 'set_filter', _parameters(command).get('name')))


async def _e_pick(session, command):
    p = _parameters(command)
    return _result(session, command, await _entity_call(
        session, 'pick', p.get('action'), _entities_param(p),
        p.get('filter'), bool(p.get('extend'))))


async def _e_labels(session, command):
    p = _parameters(command)
    if 'shown' in p:
        answer = await _entity_call(session, 'set_labels', p['shown'])
    else:
        answer = await _entity_call(session, 'state')
    return _result(session, command, {
        'shown': answer['labels'], 'drawn': answer['labels_drawn'],
        'status': answer['status']})


async def _e_coherence(session, command):
    return _result(session, command, await _entity_call(session, 'coherence'))


async def _g_list(session, command):
    if _parameters(command).get('reload'):
        answer = await _entity_call(session, 'reload_groups')
    else:
        answer = await _entity_call(session, 'state')
    return _result(session, command, {'groups': answer['groups'],
                                      'status': answer['status']})


async def _g_add(session, command):
    p = _parameters(command)
    return _result(session, command, await _entity_call(
        session, 'add_group', p.get('name'), _entities_param(p)))


async def _g_set_members(session, command):
    p = _parameters(command)
    return _result(session, command, await _entity_call(
        session, 'set_members', int(p['dim']), int(p['tag']),
        _entities_param(p)))


async def _g_rename(session, command):
    p = _parameters(command)
    return _result(session, command, await _entity_call(
        session, 'rename_group', int(p['dim']), int(p['tag']), p['name']))


async def _g_remove(session, command):
    p = _parameters(command)
    return _result(session, command, await _entity_call(
        session, 'remove_group', int(p['dim']), int(p['tag'])))


async def _s_list(session, command):
    rows = await _entity_call(session, 'selection_records')
    return _result(session, command, {'selections': rows})


async def _s_save(session, command):
    p = _parameters(command)
    return _result(session, command, await _entity_call(
        session, 'save_selection', p.get('name'), _entities_param(p)))


async def _s_show(session, command):
    return _result(session, command, await _entity_call(
        session, 'show_selection', _parameters(command).get('name')))


async def _s_delete(session, command):
    return _result(session, command, await _entity_call(
        session, 'delete_selection', _parameters(command).get('name')))


# -- Generate panel ------------------------------------------------------------------ #

def _cells(stage) -> list:
    """The timeline row of a stage, as the panel's table spells it."""
    elements = sum(int(v) for v in (stage.after.get('elements') or
                                    {}).values())
    return [stage.title, f'{stage.seconds:.2f}',
            str(stage.after.get('nodes', '')),
            str(elements) if stage.after else '',
            'ok' if stage.ok else stage.error]


def _headless_timeline(session) -> dict:
    from foammesh.core.gmsh.session_generate import registered_controller
    from foammesh.core.jobs import session_actions

    controller = registered_controller(session)
    stages = list(controller.timeline.stages) if controller else []
    running = bool(controller and session_actions.active(
        controller.case_path))
    return {'window': False, 'running': running,
            'timeline': [_cells(stage) for stage in stages],
            'stages': [stage.as_dict() for stage in stages]}


async def _m_timeline(session, command):
    api = window_api(session, GENERATE_ATTRIBUTE)
    if api is None:
        return _result(session, command, _headless_timeline(session))
    state = await asyncio.to_thread(api.state)
    headless = _headless_timeline(session)
    return _result(session, command, {
        'window': True, 'running': state['running'],
        'status': state['status'], 'timeline': state['timeline'],
        'stages': headless['stages']})


async def _m_state(session, command):
    state = await _call(session, GENERATE_ATTRIBUTE, 'Generate panel',
                        'state')
    return _result(session, command, state)


async def _m_key(session, command):
    from foammesh.core.gmsh import generate_actions as ga
    from foammesh.core.facade.plan48 import gm7_routes

    key = str(_parameters(command).get('key') or '')
    action = ga.SHORTCUTS.get(key)
    if action is None:
        raise ValidationFailedError(
            f'{key!r} is not a Generate key; they are '
            + ', '.join(ga.SHORTCUTS), details={'keys': list(ga.SHORTCUTS)})
    return await gm7_routes.run_action(session, command, action, {})


async def _m_pick(session, command):
    p = _parameters(command)
    return _result(session, command, await _call(
        session, GENERATE_ATTRIBUTE, 'Generate panel', 'pick',
        p.get('argument'), p.get('filter')))


async def _m_poor(session, command):
    p = _parameters(command)
    return _result(session, command, await _call(
        session, GENERATE_ATTRIBUTE, 'Generate panel', 'poor',
        p.get('below')))


def handlers() -> dict:
    return {
        'gmsh.entity_controls.catalogue': _ec_catalogue,
        'gmsh.entity_controls.add_row': _ec_add_row,
        'gmsh.entity_controls.check': _ec_check,
        'gmsh.entity_controls.state': _ec_state,
        'gmsh.entity_controls.pick': _ec_pick,
        'gmsh.entities.state': _e_state,
        'gmsh.entities.show_model': _e_show_model,
        'gmsh.entities.model': _e_model,
        'gmsh.entities.filter': _e_filter,
        'gmsh.entities.pick': _e_pick,
        'gmsh.entities.labels': _e_labels,
        'gmsh.entities.coherence': _e_coherence,
        'gmsh.groups.list': _g_list,
        'gmsh.groups.add': _g_add,
        'gmsh.groups.set_members': _g_set_members,
        'gmsh.groups.rename': _g_rename,
        'gmsh.groups.remove': _g_remove,
        'gmsh.selections.list': _s_list,
        'gmsh.selections.save': _s_save,
        'gmsh.selections.show': _s_show,
        'gmsh.selections.delete': _s_delete,
        'gmsh.mesh.state': _m_state,
        'gmsh.mesh.timeline': _m_timeline,
        'gmsh.mesh.key': _m_key,
        'gmsh.mesh.pick': _m_pick,
        'gmsh.mesh.poor': _m_poor,
    }
