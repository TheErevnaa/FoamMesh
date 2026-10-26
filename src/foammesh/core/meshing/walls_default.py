"""The default *walls* surface refinement group of a snappy case.

Plan 40 UXA2 (#3, #31, F1c). A new snappy case holds one surface refinement
group, *walls*, binding the surfaces a region is walled by at the levels the
shared derivation proposes for the chosen Mesh size, with feature refinement
at the same level. It is made once per case (``castellation/defaulted``), so
a group the person removes stays removed, and a case saved before the flag
counts as having had its say (schema migration).

DP-6063. MEASURED on run sr9h (packaged build of 610e6bc3, the snappy CHT
tube driven over the local API): every surface was refined at level (0 0)
and the steel zone came out ragged, because the group was made only by the
Castellation and Surface features pages when they opened
(``view/snappy_workflow/walls_default.py``). A run that never opened them
meshed a different case than the window would. The decision now lives here,
with no Qt and no facade client, so the run path (``workflow.generate_
dictionaries``, ``workflow.run_stage``, ``workflow.run_pipeline``) and the
pages make the same group from the same rule.

Which surfaces: every surface row a surface group may refine (the picker's
rules) whose role makes it the wall of a region -- a wall boundary, or a
surface inside the domain (an internal face, a baffle, a region interface,
which every region it touches is walled by). Inlets, outlets, symmetry and
far-field boundaries keep the background size. When no row is classified
that way, every eligible surface is refined, as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

GROUP_NAME = 'walls'
REFINEMENTS = 'castellation/refinementSurfaces'
DEFAULTED = 'castellation/defaulted'

#: The categories the default group refines (``boundary_roles.role_category``
#: makes every surface inside the domain an ``interface``).
REFINED_CATEGORIES = ('wall', 'interface')

#: Rows a bounding Hex6 publishes for its faces (``Shape.PLATES``).
_PLATE_SHAPES = ('xMin', 'xMax', 'yMin', 'yMax', 'zMin', 'zMax')
#: DP-578 / DP-668. The surface rows a surface refinement group can refine.
_SURFACE_GROUP_SHAPES = ('triSurfaceMesh', '', None, 'hex', 'sphere',
                         'cylinder')


@dataclass(frozen=True)
class WallsPlan:
    """What the default needs written.

    ``rows`` empty means a refinement group already exists: the case is only
    marked as having had its say.
    """
    rows: tuple
    minimum_level: int = 0
    maximum_level: int = 0
    feature_level: int = 0
    included_angle: float = 150.0

    @property
    def mark_only(self) -> bool:
        return not self.rows


def _text(value) -> str:
    value = getattr(value, 'value', value)
    return '' if value is None else str(value).strip()


def _key(value):
    text = _text(value)
    return None if text in ('', 'None') else text


def _order(key):
    return (not str(key).isdigit(),
            int(key) if str(key).isdigit() else 0, str(key))


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('true', 'yes', 'on', '1')
    return bool(value)


def was_defaulted(configuration) -> bool:
    castellation = (configuration or {}).get('castellation')
    return bool(isinstance(castellation, dict)
                and _flag(castellation.get('defaulted')))


def existing_groups(configuration) -> dict:
    castellation = (configuration or {}).get('castellation') or {}
    groups = castellation.get('refinementSurfaces') or {}
    return groups if isinstance(groups, dict) else {}


def _rows(configuration) -> dict:
    geometry = (configuration or {}).get('geometry') or {}
    return {str(key): value for key, value in geometry.items()
            if isinstance(value, dict)}


def eligible_surface_rows(configuration) -> list:
    """Surface rows a new surface group may refine, in row order.

    The rules of the Castellation page's picker
    (``RefinementMembership._eligible`` for a group not made yet): surface
    rows, not the faces of the bounding Hex6, not bound to a group, and of a
    shape a surface group writes.
    """
    hex6 = _key(((configuration or {}).get('baseGrid') or {})
                .get('boundingHex6'))
    chosen = []
    for gId, row in sorted(_rows(configuration).items(),
                           key=lambda item: _order(item[0])):
        if _text(row.get('gType')) != 'surface':
            continue
        shape = row.get('shape')
        shape = None if shape is None else _text(shape)
        if (hex6 is not None and shape in _PLATE_SHAPES
                and _key(row.get('volume')) == hex6):
            continue
        if _key(row.get('castellationGroup')) is not None:
            continue
        if shape not in _SURFACE_GROUP_SHAPES:
            continue
        chosen.append(gId)
    return chosen


def manifest_categories(manifest) -> dict:
    """``{boundary name: category}`` out of a prepared group manifest."""
    from foammesh.core.geometry.boundary_roles import boundaries_from_manifest
    if not isinstance(manifest, Mapping):
        return {}
    return dict(boundaries_from_manifest(manifest))


def row_category(configuration, gId, fallback: Mapping | None = None) -> str:
    """The category a surface row's current intent gives it.

    The row's own authored intent first (``surface_intent``: the role, the
    purpose and the mesh type, as the case builder lowers them); a row with
    none takes the prepared group's category by name.
    """
    from foammesh.core.geometry.boundary_roles import (
        boundary_role, role_category)
    from foammesh.db import surface_intent as si
    row = _rows(configuration).get(str(gId)) or {}
    name = _text(row.get('name'))
    intent = si.surface_intent(configuration, gId)
    category = ''
    if intent is not None:
        category = role_category(intent.get('boundary_category') or '',
                                 intent.get('role') or '',
                                 intent.get('mesh_patch_type') or '')
    if not category:
        category = _text((fallback or {}).get(name))
    return boundary_role(name, category)


def wall_rows(configuration, manifest=None) -> list:
    """The rows the default group binds (see the module docstring)."""
    eligible = eligible_surface_rows(configuration)
    fallback = manifest_categories(manifest)
    chosen = [gId for gId in eligible
              if row_category(configuration, gId, fallback)
              in REFINED_CATEGORIES]
    return chosen or eligible


def derived_levels(configuration) -> tuple:
    """``(min, max, feature)`` level for the case's Mesh size choice."""
    from foammesh.core.sizing.derive import WALL_LEVELS, derive_sizes
    preset = ((configuration or {}).get('mesh') or {}).get('sizePreset')
    proposal = derive_sizes(getattr(preset, 'value', preset), diagonal=1.0)
    if proposal is None:
        return WALL_LEVELS, WALL_LEVELS, WALL_LEVELS
    return (int(proposal.surface_min_level), int(proposal.surface_max_level),
            int(proposal.feature_level))


def plan_walls_group(configuration, manifest=None) -> WallsPlan | None:
    """What to write for the default group, or ``None`` for nothing.

    Nothing after the case has had its say, and nothing without a surface to
    bind (the geometry may not be in yet; the case is not marked, so the
    group is made once it is). A group already present only marks the case.
    """
    if was_defaulted(configuration):
        return None
    if existing_groups(configuration):
        return WallsPlan(rows=())
    rows = wall_rows(configuration, manifest)
    if not rows:
        return None
    from foammesh.core.facade import feature_angle as angles
    low, high, feature = derived_levels(configuration)
    return WallsPlan(
        rows=tuple(rows), minimum_level=low, maximum_level=high,
        feature_level=feature,
        included_angle=float(angles.included_angle(
            angles.project_angle(configuration))))


def apply_walls_group(data, plan: WallsPlan) -> str | None:
    """Write *plan* into a project checkout; the new group's key, if any."""
    key = None
    if not plan.mark_only:
        key, element = data.addNewElement(REFINEMENTS)
        element.setValue('groupName', GROUP_NAME)
        element.setValue('surfaceRefinement/minimumLevel',
                         str(int(plan.minimum_level)))
        element.setValue('surfaceRefinement/maximumLevel',
                         str(int(plan.maximum_level)))
        element.setValue('featureEdgeRefinementLevel',
                         str(int(plan.feature_level)))
        element.setValue('includedAngle', str(float(plan.included_angle)))
        for gId in plan.rows:
            data.setValue(f'geometry/{gId}/castellationGroup', str(key))
    data.setValue(DEFAULTED, True)
    return None if key is None else str(key)
