"""Plan 48 GM11b local-API routes: the Gmsh tool dock.

A plain remote control for what the dock in the FoamMesh window controls:
bounding boxes, point placement, partition colouring and ghost cells, the
previews, the console's Save and Clear, the recent-file list and the
Geometry table of the statistics. The window registers the dock on the case
session; without a window these answer ``precondition_failed``.
(Pictures, movies and the watch pattern are the routes of GM13, and the
Gmsh build is the ``gmsh.build.*`` routes of GM16a.)
"""
from __future__ import annotations

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

ATTRIBUTE = 'gmsh_tool_dock'


def _schema(**properties) -> dict:
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties}


OPERATIONS = (
    OperationDescriptor(
        'gmsh.tools.state', 'Gmsh tool dock state', 'case', ImpactClass.READ,
        summary='What the Gmsh tool dock shows: bounding boxes, point '
                'placement, partition colour, ghost cells and the previews.'),
    OperationDescriptor(
        'gmsh.tools.bounding_boxes', 'Bounding boxes', 'case',
        ImpactClass.READ,
        summary='Show or hide the bounding boxes of the model entities '
                '(Alt+B); with fit=true reset the view to the visible '
                'entities (Alt+Shift+1).',
        parameters_schema=_schema(shown={'type': 'boolean'},
                                  fit={'type': 'boolean'})),
    OperationDescriptor(
        'gmsh.tools.placement', 'Point placement', 'case',
        ImpactClass.READ,
        summary='Start or stop the click-to-place point tool and set its '
                'rules: frozen axes (x, y, z), move-only axis (Shift+X/Y/Z), '
                'work plane XY, XZ or YZ with its offset, snap step and the '
                'mesh size at each new point.',
        parameters_schema=_schema(
            active={'type': 'boolean'},
            freeze={'type': 'object', 'additionalProperties': {
                'type': 'boolean'}},
            move_only={'type': 'string', 'enum': ['', 'x', 'y', 'z']},
            plane={'type': 'string', 'enum': ['XY', 'XZ', 'YZ']},
            offset={'type': 'number'}, grid={'type': 'number'},
            size={'type': 'number'})),
    OperationDescriptor(
        'gmsh.tools.place_point', 'Place a point', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Make a geometry point at x, y, z through the placement '
                'rules (snap, frozen axes, size), as a click in the viewport '
                'does.',
        parameters_schema={
            'type': 'object', 'additionalProperties': False,
            'required': ['x', 'y', 'z'],
            'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'},
                           'z': {'type': 'number'}}}),
    OperationDescriptor(
        'gmsh.tools.partition', 'Partition colour and ghost cells', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Colour the mesh by partition and set '
                'Mesh.PartitionCreateGhostCells.',
        parameters_schema=_schema(colour={'type': 'boolean'},
                                  ghost_cells={'type': 'boolean'})),
    OperationDescriptor(
        'gmsh.tools.previews', 'Viewport previews', 'case', ImpactClass.READ,
        summary='How many sweep, transfinite and reclassify segments the '
                'viewport draws; clear=true removes them.',
        parameters_schema=_schema(clear={'type': 'boolean'})),
    OperationDescriptor(
        'gmsh.tools.console', 'Message console', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Read the message console, save it to a file or clear it.',
        parameters_schema={
            'type': 'object', 'additionalProperties': False,
            'required': ['action'],
            'properties': {'action': {'type': 'string',
                                      'enum': ['read', 'save', 'clear']},
                           'path': {'type': 'string'}}}),
    OperationDescriptor(
        'gmsh.tools.recent', 'Recent Gmsh files', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='The recently opened model files; open_index opens one.',
        parameters_schema=_schema(
            open_index={'type': 'integer', 'minimum': 0})),
    OperationDescriptor(
        'gmsh.tools.geometry_counts', 'Statistics geometry', 'case',
        ImpactClass.READ,
        summary='The Points, Curves, Surfaces and Volumes the Geometry tab '
                'of the statistics counts; visible_only counts only what is '
                'not hidden.',
        parameters_schema=_schema(visible_only={'type': 'boolean'})),
)


def _api(session):
    api = getattr(session, ATTRIBUTE, None)
    if api is None:
        raise PreconditionFailedError(
            'no window is showing this case, and the Gmsh tool dock lives '
            'in it: open the case in the FoamMesh window',
            details={'needs': ATTRIBUTE})
    return api


def _call(session, method, *args, **kwargs) -> dict:
    api = _api(session)
    try:
        return dict(getattr(api, method)(*args, **kwargs) or {})
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error


def _done(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def handlers():
    async def state(session, command):
        return _done(session, command, _call(session, 'state'))

    async def boxes(session, command):
        p = dict(command.parameters or {})
        payload = {}
        if 'shown' in p:
            payload.update(_call(session, 'bounding_boxes', p['shown']))
        if p.get('fit'):
            payload.update(_call(session, 'fit_visible'))
        if not payload:
            payload = dict(_call(session, 'state').get('bounding_boxes', {}))
        return _done(session, command, payload)

    async def placement(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(session, 'placement', **{
            k: p[k] for k in ('active', 'freeze', 'move_only', 'plane',
                              'offset', 'grid', 'size') if k in p}))

    async def place_point(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(
            session, 'place_point', p['x'], p['y'], p['z']))

    async def partition(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(session, 'partition', **{
            'colour': p.get('colour'), 'ghost_cells': p.get('ghost_cells')}))

    async def previews(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(
            session, 'previews', clear=bool(p.get('clear'))))

    async def console(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(
            session, 'console', p['action'], p.get('path')))

    async def recent(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(
            session, 'recent', open_index=p.get('open_index')))

    async def geometry_counts(session, command):
        p = dict(command.parameters or {})
        return _done(session, command, _call(
            session, 'geometry_counts', visible_only=p.get('visible_only')))

    return {
        'gmsh.tools.state': state,
        'gmsh.tools.bounding_boxes': boxes,
        'gmsh.tools.placement': placement,
        'gmsh.tools.place_point': place_point,
        'gmsh.tools.partition': partition,
        'gmsh.tools.previews': previews,
        'gmsh.tools.console': console,
        'gmsh.tools.recent': recent,
        'gmsh.tools.geometry_counts': geometry_counts,
    }


# -- file helpers for the dock (views do not touch the file system) ---------- #

def ensure_folder(path) -> None:
    """Make the folder that will hold *path* (the file itself is not made)."""
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def save_text(path, text: str) -> None:
    """Write *text* to *path* as UTF-8, making its folder first."""
    from pathlib import Path
    ensure_folder(path)
    Path(path).write_text(text, encoding='utf-8')
