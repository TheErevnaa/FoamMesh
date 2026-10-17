"""The automatic size field as the Gmsh run's background field (Plan 41 GN25f).

``sizeField/useAsBackground`` is the Gmsh route's "Automatic" tier (Plan 41
4.8.4 first row). While it is on, the run builds the saved field as a
``PostView`` background (``sizefield_consumer_v1``), combines it with the user's
own fields as the user chose, and holds ``Mesh.MeshSizeExtendFromBoundary``,
``Mesh.MeshSizeFromPoints`` and ``Mesh.MeshSizeFromCurvature`` at 0 (they would
outvote it). Both ways a Gmsh mesh is made use this one block: the batch job
(``write_job`` -> ``runner_v1``) and the resident session
(``GenerateController``).

A field that is switched on but cannot be used is a refusal that says why, never
a mesh made without it: that was the fault (the run ignored the field).
"""
from __future__ import annotations

from pathlib import Path

from . import field_runner

FIELD_PATH = 'sizeField/useAsBackground'
#: Fields with more leaves than this are read through Gmsh's Octree field.
OCTREE_LEAVES = 20000
CLIENT_FIELD = 'sizing.field.use_as_background'
ARRAYS = ('nodes_xyz', 'nodes_size', 'hexes')


class AutomaticFieldRefusal(ValueError):
    """The automatic field is on and cannot be used; the text says why."""


def _get(db, path, default=None):
    try:
        value = db.getValue(path)
    except Exception:                                       # noqa: BLE001
        return default
    return default if value is None else getattr(value, 'value', value)


def is_on(db) -> bool:
    return bool(_get(db, FIELD_PATH, False))


def state_for(db, case_path) -> str:
    """``none`` / ``ready`` / ``stale``: what ``sizing.field.status`` says."""
    key = _current_key(db, case_path)
    return field_runner.status(case_path, key).get('state', 'none')


def block(case_path, on: bool, state: str, translate=None) -> dict:
    """``{}`` while the tier is off, else the block the run is given.

    ``state`` is the field's state (``state_for`` or the status query);
    ``translate`` turns the field folder into the runtime's own path (the
    profile's ``translate_host_path``). A switched-on tier that cannot be
    used raises ``AutomaticFieldRefusal``.
    """
    if not on:
        return {}
    folder = field_runner.field_dir(case_path)
    record = field_runner.saved_run(case_path)
    missing = [name for name in ARRAYS if not (folder / f'{name}.npy').is_file()]
    if state == 'none' or record is None or missing:
        raise AutomaticFieldRefusal(
            'the automatic size field is switched on and no field has been '
            'computed for this project: compute it on the Sizing step, or '
            'switch "Use the field as the background size" off.')
    if state == 'stale':
        raise AutomaticFieldRefusal(
            'the automatic size field was computed for other geometry or '
            'other inputs: compute it again on the Sizing step, or switch '
            '"Use the field as the background size" off.')
    directory = translate(folder) if translate is not None else str(folder)
    leaves = record.get('leaves') or 0
    # GN25f: a PostView lookup in a large field does not finish (measured:
    # 202,819 leaves ran past 150 s without a tetrahedron); Gmsh's Octree
    # wrapper reads it in 13 s. Small fields stay exact.
    return {'directory': directory, 'octree': leaves > OCTREE_LEAVES,
            'key': record.get('key'), 'leaves': record.get('leaves'),
            'toolVersion': record.get('tool_version')}


def _current_key(db, case_path):
    """The Reuse key the saved field would need now, or None when the
    prepared geometry cannot be staged (the run reports that itself)."""
    from . import service
    from .staging import StagingError, geometry_digest
    try:
        staged, _fingerprint, bounds = service.stage_case(Path(case_path))
        inputs = service.resolve_inputs(db, bounds)
    except (StagingError, ValueError, OSError):
        return None
    return field_runner.reuse_key(geometry_digest(staged), inputs)
