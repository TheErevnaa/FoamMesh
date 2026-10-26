"""The default *walls* refinement group and the project feature angle's writes.

Plan 40 UXA2 (#3, #31, F1c, 4.1.2).

A new snappy case used to leave its walls at level 0 and explain why in a
paragraph; the first mesh was poor (F1b). It now holds one surface refinement
group, *walls*. Which surfaces it binds, at which levels, and when it is made
is decided in ``core/meshing/walls_default`` (DP-6063), which the run path
asks too; this module is the pages' write of that decision.

The project feature angle (``mesh/featureAngle``) is written here as well:
:func:`apply_project_angle` is the one write that changes every place the
angle is read (see ``core/facade/feature_angle.py``).
"""
from __future__ import annotations

from foammesh.core.facade import feature_angle as angles
from foammesh.core.meshing import walls_default as model
from foammesh.view.facade_client import query, submit

GROUP_NAME = model.GROUP_NAME
DEFAULTED_FIELD = 'meshing.castellation.defaulted'
REFINEMENT_COLLECTION = 'meshing.castellation.surface_refinements'

_building: set = set()


def _configuration(client) -> dict:
    try:
        return client.configuration() or {}
    except Exception:                                    # noqa: BLE001
        return {}


def case_was_defaulted(client) -> bool:
    return model.was_defaulted(_configuration(client))


def prepared_manifest(client):
    """The prepared geometry's group manifest, or ``None``."""
    try:
        payload = query(client, 'geometry.prepared.current', {}).payload or {}
    except Exception:                                    # noqa: BLE001
        return None
    prepared = payload.get('prepared')
    if not isinstance(prepared, dict):
        return None
    manifest = prepared.get('group_manifest')
    if not isinstance(manifest, dict) or not manifest:
        manifest = prepared if 'groups' in prepared else None
    return manifest


def derived_levels(client) -> tuple:
    """``(min, max, feature)`` level for the case's Mesh size choice."""
    return model.derived_levels(_configuration(client))


def ensure_walls_group(client, then=None) -> bool:
    """Make the case's *walls* group once; ``True`` when a write was sent.

    The decision is ``core/meshing/walls_default.plan_walls_group``, the one
    the run path asks (DP-6063): nothing without a surface to bind, nothing
    after the case has had its say, and only the mark over a group already
    present. The page writes through the facade so the group and its
    bindings arrive as the person's own edits would.
    """
    key = id(client)
    if key in _building:
        return False
    configuration = _configuration(client)
    if model.was_defaulted(configuration):
        return False
    plan = model.plan_walls_group(configuration, prepared_manifest(client))
    if plan is None:
        return False
    if plan.mark_only:
        submit(client, 'configuration.patch',
               {'patch': {DEFAULTED_FIELD: True}})
        return False
    rows = list(plan.rows)
    from .refinement_membership import (
        RefinementMembership, SURFACE, _patch_bindings)

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
        'surface_refinement.minimum_level': plan.minimum_level,
        'surface_refinement.maximum_level': plan.maximum_level,
        'feature_edge_refinement_level': plan.feature_level,
        'included_angle': plan.included_angle}}, then=created)
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
