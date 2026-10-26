"""Plan 48 WG: the front, back and side patches a 2D-case or wedge preset
declares, on the local API.

Plain remote control of the Geometry build panel's *Patch roles* table
(DP-4215), nothing agent-specific:

``gmsh.geometry.patch_roles``
    The declared patches: for each, its name (as the physical-group table has
    it now), role (front, back, front_and_back, sides), the category it
    publishes with and the categories it may be given. ``declared`` is what
    the publisher is told is the front and back of a one-cell-thick mesh
    (``empty`` or ``wedge``); ``categories`` is every declared patch.
``gmsh.geometry.set_patch_role``
    The *Publishes as* combo of one row.

The wedge's *Symmetric about the picked plane* checkbox is the ``symmetric``
value of ``gmsh.geometry.build`` for the ``axisymmetric_wedge`` operation (it
is a field of the catalogue ``gmsh.geometry.operations`` returns).
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.plan48 import gm5_routes, pa3_routes
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_ID = 'gmsh.geometry.'

OPERATIONS = (
    OperationDescriptor(
        _ID + 'patch_roles', 'The declared front, back and side patches',
        'case', ImpactClass.READ,
        summary='The patches the 2D-case and axisymmetric-wedge presets '
                'declared, as the Patch roles table of the Geometry build '
                'panel lists them: name, role, the category each publishes '
                'with and the categories it may be given. The names follow '
                'a rename made in the physical groups table.'),
    OperationDescriptor(
        _ID + 'set_patch_role', 'Choose what a declared patch publishes as',
        'case', ImpactClass.REVERSIBLE_EDIT,
        parameters_schema={'type': 'object',
                           'required': ['name', 'category'],
                           'properties': {'name': {'type': 'string'},
                                          'category': {'type': 'string'}}},
        summary='The Publishes as combo of one row of the Patch roles table: '
                'a front or back patch may publish as empty (2D case), wedge '
                '(axisymmetric), symmetry or wall; sides as wall, inlet, '
                'outlet, symmetry, far_field or patch. The window shows the '
                'change when it has the case open.'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _row(row: dict) -> dict:
    from foammesh.core.gmsh import geometry_build as gb

    return {'name': row['name'], 'role': row['role'], 'tag': row['tag'],
            'source': row.get('source', ''), 'category': row['category'],
            'default': row['default'],
            'choices': list(gb.ROLE_CATEGORIES.get(row['default'],
                                                   (row['default'],)))}


def _snapshot(session) -> dict:
    """The rows after following the physical-group table (best effort)."""
    from foammesh.core.gmsh import geometry_build as gb

    builder = gm5_routes._builder(session)
    rows = builder.roles()
    if rows:
        try:
            controller = gm5_routes._controller(session)
            groups = [(g.dim, g.tag, g.name, g.entities)
                      for g in controller.physical_editor().groups()]
            rows = builder.follow_names(groups)
        except Exception:                                  # noqa: BLE001
            pass
    return {'roles': [_row(r) for r in rows],
            'categories': gb.role_categories(rows),
            'declared': gb.constrained_roles(rows)}


async def _patch_roles(session, command) -> OperationResult:
    return _result(session, command,
                   await asyncio.to_thread(_snapshot, session))


async def _set_patch_role(session, command) -> OperationResult:
    name = str(command.parameters.get('name') or '').strip()
    category = str(command.parameters.get('category') or '').strip()
    if not name or not category:
        raise ValidationFailedError('name and category are required')
    builder = gm5_routes._builder(session)
    try:
        await asyncio.to_thread(builder.set_role_category, name, category)
    except Exception as error:                             # noqa: BLE001
        raise gm5_routes._refusal(error) from error
    api = pa3_routes.window_panel(session, 'build')
    if api is not None and hasattr(api, 'show_roles'):
        await asyncio.to_thread(api.show_roles)
    return _result(session, command,
                   await asyncio.to_thread(_snapshot, session))


def handlers() -> dict:
    return {_ID + 'patch_roles': _patch_roles,
            _ID + 'set_patch_role': _set_patch_role}
