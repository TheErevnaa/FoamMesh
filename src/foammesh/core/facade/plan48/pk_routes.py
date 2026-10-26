"""Plan 48 PK local-API routes: the Cells filter of the Pick tool.

A person picks single mesh elements in the viewport (filter Cells, offered
only while a mesh is on screen, on either route). These routes are the same
pick, driven from outside: they start the Pick tool on Cells through the
selection service, click elements by their tag through the path a mouse click
takes, and accept or abort as Enter and Escape do. A plain remote control for
what the window does, nothing agent-specific.

* ``mesh.cells.pick`` -- ``action`` start, click, drop, accept, abort or set
  (``set`` = start, click each of ``tags``, accept). The answer is the state.
* ``mesh.cells.state`` -- read: whether a mesh is on screen, whether a Cells
  pick is live, the picked and hovered element tags, the last accepted pick.

The picked elements are the Gmsh element tags (the polyMesh cell index on the
snappy route, which carries no Gmsh tags). Without a mesh on screen both
answer ``precondition_failed``.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

ACTIONS = ('start', 'click', 'drop', 'accept', 'abort', 'set')

#: The last accepted Cells pick of the API (the viewport's own accept goes to
#: whatever asked for the pick; this is what an API client reads back).
_LAST: dict = {'elements': []}


def _schema(**properties) -> dict:
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties}


OPERATIONS = (
    OperationDescriptor(
        'mesh.cells.pick', 'Pick mesh elements (Cells filter)', 'case',
        ImpactClass.READ,
        parameters_schema={
            'type': 'object', 'required': ['action'],
            'additionalProperties': False,
            'properties': {
                'action': {'type': 'string', 'enum': list(ACTIONS)},
                'tags': {'type': 'array', 'items': {'type': 'integer'}},
                'extend': {'type': 'boolean'}}},
        summary='The Cells filter of the Pick tool: start a pick of single '
                'mesh elements, click elements by tag (the first replaces '
                'the pending list unless extend), drop some, accept or '
                'abort; set does start, click and accept in one call. Works '
                'on both routes while a mesh is on screen; the answer is '
                'the state, as mesh.cells.state.'),
    OperationDescriptor(
        'mesh.cells.state', 'Cells pick state', 'case', ImpactClass.READ,
        summary='Whether a mesh is on screen, whether a Cells pick is live, '
                'the element tags picked and hovered, the number of '
                'elements, and the last pick the API accepted.'),
)


def _view():
    from foammesh.core.selection import mesh_on_screen

    view = mesh_on_screen.current()
    if view is None:
        raise PreconditionFailedError(
            'no mesh is on screen, and the Cells filter picks elements of '
            'it: generate or load a mesh in the FoamMesh window')
    return view


def _service(view):
    service = view.pick_service()
    if service is None:
        raise PreconditionFailedError(
            'the Pick tool is not running: open the case in the FoamMesh '
            'window')
    return service


def _state(view) -> dict:
    out = view.cells_state()
    out['last_accepted'] = list(_LAST['elements'])
    return out


def _start(view) -> None:
    from foammesh.core.selection.pick import PickRequest

    service = _service(view)

    def accepted(chosen):
        _LAST['elements'] = [int(e[1]) for e in chosen]

    request = PickRequest(('Cells',), requester='local-api',
                          on_accept=accepted)
    if not service.begin_pick(request, engine='gmsh', mesh_on_screen=True):
        raise PreconditionFailedError(service.pick_reason
                                      or 'the Cells pick did not start')
    if not view._cells_live():
        # the Gmsh route loads its geometry first, then starts the pick
        raise PreconditionFailedError(
            'the geometry is still loading for the pick; ask again in a '
            'moment')


def _picker(view):
    picker = view._cells_picker
    if picker is None or not view._cells_live():
        raise ValueError('there is no Cells pick in progress; start one')
    return picker


def run(action: str, tags, extend: bool) -> dict:
    """The GUI-thread body of ``mesh.cells.pick``."""
    view = _view()
    if action in ('start', 'set'):
        _start(view)
    if action in ('click', 'set'):
        if not tags:
            raise ValueError('click needs tags: the element tags to click')
        _picker(view)
        view.click_elements(tags, extend=bool(extend) and action == 'click')
    elif action == 'drop':
        picker = _picker(view)
        for tag in tags or ():
            picker.remove_chip((4, int(tag)))
    if action in ('accept', 'set'):
        from foammesh.core.gmsh import session_entities as entities

        try:
            _picker(view).accept()
        except entities.PickRefusal as error:
            raise ValueError(str(error)) from None
    elif action == 'abort':
        _picker(view).abort()
    return _state(view)


async def _pick(session, command) -> OperationResult:
    from foammesh.view.gmsh_tools.window_actions import on_gui_thread

    p = command.parameters or {}
    action = str(p.get('action') or '')
    if action not in ACTIONS:
        raise ValidationFailedError(
            f'{action!r} is not a Cells pick action; they are '
            + ', '.join(ACTIONS), details={'actions': list(ACTIONS)})
    tags = [int(t) for t in (p.get('tags') or ())]
    try:
        payload = await asyncio.to_thread(
            on_gui_thread, run, action, tags, bool(p.get('extend')))
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


async def _read_state(session, command) -> OperationResult:
    from foammesh.core.selection import mesh_on_screen
    from foammesh.view.gmsh_tools.window_actions import on_gui_thread

    def read():
        view = mesh_on_screen.current()
        if view is None:
            return {'on_screen': False, 'active': False, 'filter': None,
                    'elements': [], 'hovered': None, 'element_count': 0,
                    'last_accepted': list(_LAST['elements'])}
        return _state(view)

    payload = await asyncio.to_thread(on_gui_thread, read)
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def handlers() -> dict:
    return {'mesh.cells.pick': _pick, 'mesh.cells.state': _read_state}
