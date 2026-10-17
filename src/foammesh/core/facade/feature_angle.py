"""The project feature angle and the five places it is derived into.

Plan 40 UXA2 (#31, F1c). Five controls each held a feature angle of their own
(30 / 60 / 150 / 60 / 30) and nothing tied them together. The project holds one
number, ``mesh/featureAngle`` (degrees of turn from flat, default 30), and the
five are read from it. At the default every place keeps its schema default,
so a case that never touches the angle meshes on the defaults.

| Place                                   | Value        | Default |
|-----------------------------------------|--------------|---------|
| ``castellation/resolveFeatureAngle``    | A            | 30      |
| ``addLayers/featureAngle``              | 2 A + 40     | 100     |
| ``addLayers/slipFeatureAngle``          | A            | 30      |
| every group's ``includedAngle``         | 180 - A      | 150     |
| the import dialog's split angle         | 2 A          | 60      |

``includedAngle`` is surfaceFeatures' own measure: it keeps an edge whose two
faces meet at less than this angle, so the same crease is 180 - A there.

Plan 40 U1 (DEF, 2026-10-11). The layers' angle was 2 A (60 at the default)
and is now 2 A + 40 (100 at the default, OpenFOAM 13's own value). MEASURED
on the footer-only duct, layer stage rerun on the walk's snapped mesh: 60
gave 86.6 % coverage and 100 gave 89.6 % with nothing else changed, because
layers stopped at every rim the walls turn through. The slope is kept, so a
larger project angle still lets layers round sharper corners.
"""
from __future__ import annotations

DEFAULT_FEATURE_ANGLE = 30.0
MIN_FEATURE_ANGLE = 1.0
MAX_FEATURE_ANGLE = 179.0

#: Field ids of the project angle and of the places derived from it.
PROJECT_FIELD = 'mesh.feature_angle'
RESOLVE_FIELD = 'meshing.castellation.resolve_feature_angle'
LAYER_FIELD = 'meshing.layers.feature_angle'
SLIP_FIELD = 'meshing.layers.slip_feature_angle'
INCLUDED_ANGLE_KEY = 'included_angle'
#: Plan 42 SG5 (D5). Gmsh's classification angle, a sixth place: it follows
#: the project angle (A + 10, which is the 40 it always was at the default 30)
#: until the user types a value of their own.
CLASSIFICATION_FIELD = 'gmsh.describe_geometry.classification_angle'
CLASSIFICATION_OFFSET = 10.0
CLASSIFICATION_DEFAULT = DEFAULT_FEATURE_ANGLE + CLASSIFICATION_OFFSET


def normalise(angle) -> float:
    """*angle* as a usable project angle (default when unreadable)."""
    try:
        value = float(angle)
    except (TypeError, ValueError):
        return DEFAULT_FEATURE_ANGLE
    if value != value:
        return DEFAULT_FEATURE_ANGLE
    return min(MAX_FEATURE_ANGLE, max(MIN_FEATURE_ANGLE, value))


def included_angle(angle) -> float:
    """surfaceFeatures' ``includedAngle`` for a project angle."""
    return 180.0 - normalise(angle)


def project_angle_from_included(included) -> float:
    """The project angle that gives surfaceFeatures *included*."""
    return normalise(180.0 - float(included))


def import_split_angle(angle) -> int:
    """The import dialog's starting split angle (whole degrees)."""
    return int(round(min(180.0, 2.0 * normalise(angle))))


#: Plan 40 U1. Degrees the layers' feature angle sits above 2 A.
LAYER_OFFSET = 40.0


def layer_angle(angle) -> float:
    """snappy's ``addLayers/featureAngle`` for a project angle."""
    return min(180.0, 2.0 * normalise(angle) + LAYER_OFFSET)


def derived_fields(angle) -> dict:
    """``{field id: value}`` for the places stored in the configuration.

    The included angle lives on each refinement group and the import dialog's
    starting value is not stored, so neither is in this map; see
    :func:`included_angle` and :func:`import_split_angle`.
    """
    value = normalise(angle)
    return {
        RESOLVE_FIELD: value,
        LAYER_FIELD: layer_angle(value),
        SLIP_FIELD: value,
    }


def project_angle(configuration) -> float:
    """The project angle stored in *configuration* (a facade snapshot)."""
    mesh = (configuration or {}).get('mesh')
    stored = mesh.get('featureAngle') if isinstance(mesh, dict) else None
    return normalise(stored)


def classification_angle_for(angle) -> float:
    """Gmsh's classification angle that follows project angle *angle*."""
    return min(180.0, normalise(angle) + CLASSIFICATION_OFFSET)


def stored_classification_angle(configuration) -> float:
    """The classification angle stored in *configuration* (default 40)."""
    healing = ((configuration or {}).get('gmsh') or {}).get('healing')
    stored = healing.get('classificationAngle') if isinstance(
        healing, dict) else None
    try:
        return float(stored) if stored is not None else CLASSIFICATION_DEFAULT
    except (TypeError, ValueError):
        return CLASSIFICATION_DEFAULT


def classification_follows(configuration) -> bool:
    """True while the stored classification angle is the one derived.

    A value that differs from what the project angle gives was typed, and a
    typed angle is never overwritten (D5).
    """
    derived = classification_angle_for(project_angle(configuration))
    return abs(stored_classification_angle(configuration) - derived) < 1e-9


def project_patch(configuration, angle) -> dict:
    """The ``configuration.patch`` that sets the project angle to *angle*.

    The project field, the places stored beside it, and the Gmsh
    classification angle only while it still follows the project angle.
    """
    target = normalise(angle)
    patch = {PROJECT_FIELD: target}
    patch.update(derived_fields(target))
    if classification_follows(configuration):
        patch[CLASSIFICATION_FIELD] = classification_angle_for(target)
    return patch
