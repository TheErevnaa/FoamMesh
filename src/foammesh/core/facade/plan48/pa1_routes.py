"""Plan 48 PA1 local-API routes: the Gmsh Display panel and the camera pose.

A plain remote control for what the Display panel of the Gmsh route does in
the window: choose its tab, read its status line, filter and show or hide
entities, set the "visibility affects meshing" switch, and type, apply and
read the camera pose.  (The Drawing tab's 69 options and Reset are
``gmsh.display.set``; the Elements group, the section, the deviation panel and
the worst-cell tools are ``presentation.*`` operations.)

The window's presentation renderer hands the panel over
(``gmsh_display_panel()``); without a window these routes answer
``precondition_failed``.  Every route does what the control does, on the GUI
thread, and a refusal carries the panel's own text.
"""
from __future__ import annotations

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_DIMS = {'type': 'array', 'items': {'type': 'integer', 'minimum': 0,
                                    'maximum': 3}}
_ENTITY = {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 2,
           'maxItems': 2}
_POSE = {'type': 'object', 'additionalProperties': False, 'properties': {
    name: {'type': 'number'}
    for name in ('rx', 'ry', 'rz', 'tx', 'ty', 'tz', 'scale')}}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.display.state', 'Gmsh Display panel state', 'case',
        ImpactClass.READ,
        summary='What the Display panel of the Gmsh route shows: the tab on '
                'show, its status line, the Visibility tab (filter, count, '
                'hidden entities, the affects-meshing switch), the camera '
                'pose fields and the drawing options that differ from '
                'Gmsh\'s defaults. With tab, chooses the Visibility, Camera '
                'or Drawing tab first (Shift+Ctrl+M chooses Camera).',
        parameters_schema={'type': 'object', 'additionalProperties': False,
                           'properties': {'tab': {
                               'type': 'string',
                               'enum': ['visibility', 'camera', 'drawing']}}}),
    OperationDescriptor(
        'gmsh.display.visibility', 'Show and hide Gmsh entities', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        summary='The Visibility tab: fill the filter (dimensions, tag ranges '
                'such as 1-5, 8, physical group name or tag), then Show, '
                'Hide, Show only or Show all; set Visibility affects '
                'meshing (Mesh.MeshOnlyVisible); or do what the eye and the '
                'solo button of one entity row do (entities with visible, '
                'solo). Answers the count the tab shows and the hidden '
                'entities.',
        parameters_schema={'type': 'object', 'additionalProperties': False,
                           'properties': {
                               'dims': _DIMS,
                               'tags': {'type': 'string'},
                               'physical': {'type': ['string', 'integer']},
                               'action': {'type': 'string', 'enum': [
                                   'show', 'hide', 'only', 'all']},
                               'affects_meshing': {'type': 'boolean'},
                               'entities': {'type': 'array', 'items': _ENTITY},
                               'visible': {'type': 'boolean'},
                               'solo': _ENTITY}}),
    OperationDescriptor(
        'gmsh.display.pose', 'Gmsh camera pose', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        summary='The Camera tab: type the pose (rotation about X, Y and Z in '
                'degrees, translation in model units, scale), Apply it to '
                'the camera, or Read from view (the fields take the '
                'camera\'s pose). Answers the seven fields.',
        parameters_schema={'type': 'object', 'additionalProperties': False,
                           'properties': {
                               'pose': _POSE,
                               'apply': {'type': 'boolean'},
                               'read': {'type': 'boolean'}}}),
)


# -- reaching the window ---------------------------------------------------- #

def display_panel_or_none(session):
    """The Gmsh Display panel of the window showing this case, or ``None``."""
    renderer = getattr(session, 'presentation_renderer', None)
    getter = getattr(renderer, 'gmsh_display_panel', None)
    if not callable(getter):
        return None
    try:
        return getter()
    except RuntimeError:                     # the panel was deleted
        return None


def display_panel(session):
    panel = display_panel_or_none(session)
    if panel is None:
        raise PreconditionFailedError(
            'no window is showing the Gmsh Display panel for this case: open '
            'the case in the FoamMesh window and choose the Gmsh route',
            details={'needs': 'gmsh_display_panel'})
    return panel


def on_gui(function):
    """Run ``function()`` on the GUI thread; ``ValueError`` -> validation."""
    from foammesh.view.gmsh_tools.window_actions import on_gui_thread

    try:
        return on_gui_thread(function)
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error


def _done(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


# -- the payloads ----------------------------------------------------------- #

def display_state(panel) -> dict:
    return {
        'tab': panel.currentTab(), 'tabs': panel.tabKeys(),
        'status': panel.status(), 'busy': panel.busy(),
        'visibility': panel.visibilityState(),
        'pose': panel.poseDict(),
        'drawing_changed': {
            name: (list(value) if isinstance(value, tuple) else value)
            for name, value in panel.drawingSettings().changed().items()},
        'viewport_follows': panel.drawingFollowedBy()}


def _visibility(panel, parameters) -> dict:
    steps = []
    asked = False
    if any(key in parameters for key in ('dims', 'tags', 'physical')):
        dims = parameters.get('dims')
        physical = parameters.get('physical')
        panel.setVisibilityFilter(
            None if dims is None else [int(d) for d in dims],
            parameters.get('tags'),
            None if physical is None else str(physical))
        asked = True
    if 'affects_meshing' in parameters:
        panel.setAffectsMeshing(bool(parameters['affects_meshing']))
        asked = True
    if parameters.get('action'):
        steps = panel.applyVisibility(parameters['action'])
        if not steps and panel.lastRefusal():
            raise ValueError(panel.lastRefusal())
        asked = True
    for dim, tag in parameters.get('entities') or ():
        if 'visible' not in parameters:
            raise ValueError('say visible: true or false for the entities')
        if (int(dim), int(tag)) not in {r.key for r in panel._records}:
            raise ValueError('the model has no entity %d %d' % (dim, tag))
        steps += panel.setEntityVisible(dim, tag, parameters['visible'])
        asked = True
    if parameters.get('solo'):
        dim, tag = parameters['solo']
        if (int(dim), int(tag)) not in {r.key for r in panel._records}:
            raise ValueError('the model has no entity %d %d' % (dim, tag))
        steps += panel.soloEntity(dim, tag)
        asked = True
    if not asked:
        raise ValueError('give a filter (dims, tags, physical), an action, '
                         'affects_meshing, entities with visible, or solo')
    panel.waitIdle()
    return {'steps': len(steps), 'status': panel.status(),
            **panel.visibilityState()}


def _pose(panel, parameters) -> dict:
    if not any(parameters.get(key) for key in ('pose', 'apply', 'read')):
        raise ValueError('give pose, apply or read')
    if parameters.get('apply') and parameters.get('read'):
        raise ValueError('apply and read are sent apart: Apply turns the '
                         'camera by the fields, Read from view fills them')
    applied = read = False
    if parameters.get('pose'):
        panel.setPoseValues(dict(parameters['pose']))
    if parameters.get('apply'):
        if not panel.applyPose():
            raise ValueError(panel.status() or 'the pose was not applied')
        applied = True
    if parameters.get('read'):
        if panel.readPose() is None:
            raise ValueError('there is no view to read the pose from')
        read = True
    return {'pose': panel.poseDict(), 'applied': applied, 'read': read,
            'status': panel.status()}


def handlers():
    async def state(session, command):
        parameters = dict(command.parameters or {})

        def run():
            panel = display_panel(session)
            if parameters.get('tab'):
                try:
                    panel.showTab(parameters['tab'])
                except ValueError as error:
                    raise ValidationFailedError(str(error)) from error
            return display_state(panel)
        return _done(session, command, on_gui(run))

    async def visibility(session, command):
        parameters = dict(command.parameters or {})
        return _done(session, command, on_gui(
            lambda: _visibility(display_panel(session), parameters)))

    async def pose(session, command):
        parameters = dict(command.parameters or {})
        return _done(session, command, on_gui(
            lambda: _pose(display_panel(session), parameters)))

    return {'gmsh.display.state': state,
            'gmsh.display.visibility': visibility,
            'gmsh.display.pose': pose}
