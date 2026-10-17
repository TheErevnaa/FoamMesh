"""The default *walls* refinement group and the project feature angle's writes.

Plan 40 UXA2 (#3, #31, F1c, 4.1.2).

A new snappy case used to leave its walls at level 0 and explain why in a
paragraph; the first mesh was poor (F1b). It now holds one surface refinement
group, *walls*, binding every wall-typed surface at the levels the shared
derivation proposes for the chosen Mesh size, with feature refinement at the
same level. Like the layers page's *Walls* group it is made once per case
(``castellation/defaulted``), so a group the user removes stays removed, and a
case saved before the flag counts as having had its say (schema migration).

The project feature angle (``mesh/featureAngle``) is written here as well:
:func:`apply_project_angle` is the one write that changes every place the
angle is read (see ``core/facade/feature_angle.py``).
"""
from __future__ import annotations

from foammesh.core.facade import feature_angle as angles
from foammesh.core.geometry.boundary_roles import (
    boundaries_from_manifest, default_layer_targets)
from foammesh.view.facade_client import query, submit

GROUP_NAME = 'walls'
DEFAULTED_FIELD = 'meshing.castellation.defaulted'
REFINEMENT_COLLECTION = 'meshing.castellation.surface_refinements'

_building: set = set()


def _configuration(client) -> dict:
    try:
        return client.configuration() or {}
    except Exception:                                    # noqa: BLE001
        return {}


def case_was_defaulted(client) -> bool:
    castellation = _configuration(client).get('castellation')
    return bool(isinstance(castellation, dict)
                and castellation.get('defaulted'))


def wall_names(client) -> tuple:
    """The boundaries the prepared geometry gives the wall role."""
    try:
        payload = query(client, 'geometry.prepared.current', {}).payload or {}
    except Exception:                                    # noqa: BLE001
        return ()
    prepared = payload.get('prepared')
    if not isinstance(prepared, dict):
        return ()
    manifest = prepared.get('group_manifest')
    if not isinstance(manifest, dict) or not manifest:
        manifest = prepared if 'groups' in prepared else {}
    return default_layer_targets(boundaries_from_manifest(manifest))


def derived_levels(client) -> tuple:
    """``(min, max, feature)`` level for the case's Mesh size choice."""
    from foammesh.core.sizing import derive_sizes

    preset = ((_configuration(client).get('mesh') or {}).get('sizePreset'))
    proposal = derive_sizes(preset, diagonal=1.0)
    return (proposal.surface_min_level, proposal.surface_max_level,
            proposal.feature_level)


def wall_rows(configuration, names) -> list:
    """Geometry row ids that refine as walls, given the wall boundary *names*.

    The rows a group may refine are the picker's (``RefinementMembership``);
    of those, the ones named like a wall boundary. When the prepared geometry
    names no wall among them, every eligible surface is a wall.
    """
    from .refinement_membership import (
        RefinementMembership, SURFACE, geometry_rows)

    class _Config:
        def configuration(self):
            return configuration

    picker = RefinementMembership(_Config(), SURFACE)
    entries = picker._eligible(configuration, None)       # noqa: SLF001
    rows = geometry_rows(configuration)
    chosen = [gId for gId, _label, _checked in entries
              if str(rows.get(gId, {}).get('name') or '') in set(names)]
    return chosen or [gId for gId, _label, _checked in entries]


def ensure_walls_group(client, then=None) -> bool:
    """Make the case's *walls* group once; ``True`` when a write was sent.

    Nothing happens without a surface to bind (the geometry may not be in yet,
    and the case is not marked, so the group is made when it is), with a group
    already present (the case is marked and left alone), or after the case has
    had its say.
    """
    key = id(client)
    if key in _building or case_was_defaulted(client):
        return False
    configuration = _configuration(client)
    existing = (configuration.get('castellation') or {}).get(
        'refinementSurfaces') or {}
    if existing:
        submit(client, 'configuration.patch',
               {'patch': {DEFAULTED_FIELD: True}})
        return False
    rows = wall_rows(configuration, wall_names(client))
    if not rows:
        return False
    from .refinement_membership import (
        RefinementMembership, SURFACE, _patch_bindings)

    low, high, feature = derived_levels(client)
    included = angles.included_angle(angles.project_angle(configuration))
    _building.add(key)

    def finish(*_args) -> None:
        _building.discard(key)
        if then is not None:
            then()

    def created(result) -> None:
        if getattr(result, 'status', 'accepted') != 'accepted':
            finish()
            return
        group = str((getattr(result, 'payload', None) or {}).get('entity_id'))
        picker = RefinementMembership(client, SURFACE)
        picker.load(None)
        picker.setChecked(rows)
        changes = picker.pending(group)

        def bound() -> None:
            submit(client, 'configuration.patch',
                   {'patch': {DEFAULTED_FIELD: True}}, then=finish)

        _patch_bindings(client, changes, bound)

    submit(client, REFINEMENT_COLLECTION + '.create', {'fields': {
        'group_name': GROUP_NAME,
        'surface_refinement.minimum_level': low,
        'surface_refinement.maximum_level': high,
        'feature_edge_refinement_level': feature,
        'included_angle': included}}, then=created)
    return True


def refinement_groups(client) -> dict:
    groups = (_configuration(client).get('castellation') or {}).get(
        'refinementSurfaces') or {}
    return {str(key): dict(value) for key, value in groups.items()
            if isinstance(value, dict)}


def apply_project_angle(client, included: float, then=None) -> None:
    """Set the project feature angle from an included angle and fan it out.

    One configuration write for the project angle and the places stored in
    the configuration, then one write per refinement group for its included
    angle (a group that already holds it is not written). The project angle is
    left alone when it is unchanged, so re-entering the included angle to fix
    a disagreement between groups does not overwrite a resolve angle the user
    typed.
    """
    configuration = _configuration(client)
    target = angles.project_angle_from_included(included)
    current = angles.project_angle(configuration)
    pending = [
        entity_id for entity_id, group in refinement_groups(client).items()
        if _angle_of(group) is None
        or abs(_angle_of(group) - angles.included_angle(target)) >= 1e-9]
    value = angles.included_angle(target)

    def write_groups(index: int = 0) -> None:
        if index >= len(pending):
            if then is not None:
                then(True)
            return

        def written(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                if then is not None:
                    then(result)
                return
            write_groups(index + 1)

        submit(client, REFINEMENT_COLLECTION + '.patch',
               {'entity_id': pending[index],
                'fields': {angles.INCLUDED_ANGLE_KEY: value}}, then=written)

    if abs(target - current) < 1e-9 and (
            (_configuration(client).get('mesh') or {}).get('featureAngle')
            is not None):
        write_groups()
        return
    patch = angles.project_patch(configuration, target)

    def project_written(result) -> None:
        if getattr(result, 'status', 'accepted') != 'accepted':
            if then is not None:
                then(result)
            return
        write_groups()

    submit(client, 'configuration.patch', {'patch': patch},
           then=project_written)


def _angle_of(group: dict):
    try:
        return float(group.get('includedAngle'))
    except (TypeError, ValueError):
        return None
