"""The size field as a facade service (Plan 41 section 12.2 S3).

Reads the inputs from the case, stages the prepared surfaces, composes the
proposals of the active route from the field, and resolves a proposal target
to a stored control. The operations in ``domain_operations`` are thin calls
into this module.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np

from foammesh.core.facade import feature_angle as angle_rules

from . import field_runner, gmsh_mapping, snappy_mapping
from .features import feature_edges
from .inputs import SizeFieldInputs, default_inputs
from .proposals import Proposal, ProposalStore, proposals_path
from .staging import (
    StagedSurface, prepared_sources, read_stl_solids, stage_sources)

_ROW = re.compile(r'^(?P<collection>[^\[\]]+)\[(?P<key>.*)\]\.(?P<element>.+)$')
_SAFE = re.compile(r'[^A-Za-z0-9_.-]+')
#: Row-name fields tried in order when a proposal names a collection row.
_NAME_FIELDS = ('groupName', 'name', 'scopeToken')
#: At most this many rays per surface when walking distance bands.
MAX_RAYS = 1500

NOT_STORED = 'not_stored'
NO_ROW = 'no_row'


def _safe(name) -> str:
    return _SAFE.sub('_', str(name).strip()).strip('_') or 'surface'


# -- inputs ---------------------------------------------------------------- #

def _get(db, path, default=None):
    try:
        value = db.getValue(path)
    except Exception:                                       # noqa: BLE001
        return default
    return default if value is None else getattr(value, 'value', value)


def stored_inputs(db) -> dict:
    """What the case holds for the field's inputs (0 = from the model)."""
    return {
        'hmin': float(_get(db, 'sizeField/minimumSize', 0.0) or 0.0),
        'hmax': float(_get(db, 'sizeField/maximumSize', 0.0) or 0.0),
        'hbulk': float(_get(db, 'sizeField/bulkSize', 0.0) or 0.0),
        'gradation': float(_get(db, 'sizeField/gradation', 1.1) or 1.1),
        'nodes_per_two_pi': int(_get(db, 'sizeField/nodesPerTwoPi', 20) or 20),
        'nodes_per_gap': int(_get(db, 'sizeField/nodesPerGap', 4) or 0),
        'smoothing': bool(_get(db, 'sizeField/smoothing', True)),
        'aniso': bool(_get(db, 'sizeField/anisotropic', False)),
        'feature_angle': angle_rules.normalise(_get(db, 'mesh/featureAngle')),
        'threads': int(_get(db, 'gmsh/parallel/threads', 4) or 4),
    }


def resolve_inputs(db, bounds, overrides=None) -> SizeFieldInputs:
    """Defaults from the model, then what the case holds, then overrides."""
    stored = stored_inputs(db)
    typed = {k: v for k, v in stored.items() if k in ('hmin', 'hmax', 'hbulk')
             and v > 0}
    rest = {k: v for k, v in stored.items()
            if k not in ('hmin', 'hmax', 'hbulk')}
    typed.update({k: v for k, v in (overrides or {}).items()
                  if v not in (None, '')})
    return default_inputs(bounds, typed=typed, **rest)


def stage_case(case_path):
    """``(staged surfaces, prepared fingerprint, bounds)`` of the case."""
    sources, fingerprint = prepared_sources(case_path)
    staged = stage_sources(sources, field_runner.staged_dir(case_path))
    low = np.full(3, np.inf)
    high = np.full(3, -np.inf)
    for item in staged:
        for _, triangles in read_stl_solids(item.path):
            flat = triangles.reshape(-1, 3)
            low = np.minimum(low, flat.min(axis=0))
            high = np.maximum(high, flat.max(axis=0))
    bounds = (low[0], high[0], low[1], high[1], low[2], high[2])
    return staged, fingerprint, tuple(float(x) for x in bounds)


# -- proposals --------------------------------------------------------------- #

def _surface_geometry(staged: list[StagedSurface], limit=MAX_RAYS):
    """``{name: (triangles, origins, normals)}`` for the distance walk."""
    out = {}
    for item in staged:
        triangles = np.concatenate(
            [t for _, t in read_stl_solids(item.path)])
        centroids = triangles.mean(axis=1)
        normals = np.cross(triangles[:, 1] - triangles[:, 0],
                           triangles[:, 2] - triangles[:, 0])
        length = np.linalg.norm(normals, axis=1)
        keep = length > 0
        normals = normals[keep] / length[keep][:, None]
        centroids, triangles = centroids[keep], triangles[keep]
        if len(centroids) > limit:
            pick = np.linspace(0, len(centroids) - 1, limit).astype(int)
            centroids, normals = centroids[pick], normals[pick]
        out[item.name] = (triangles, centroids, normals)
    return out


def _growth_direction(field, origins, normals, reach):
    """The side of each triangle on which the field grows (the fluid side)."""
    plus = field.sample_along(origins, normals, [reach])[:, 0]
    minus = field.sample_along(origins, -normals, [reach])[:, 0]
    sign = np.where(np.nan_to_num(plus) >= np.nan_to_num(minus), 1.0, -1.0)
    return normals * sign[:, None]


def snappy_proposals(field, staged, bounds, settings: dict,
                     locked_cells=None, user_cell=None) -> list[Proposal]:
    rows, base = snappy_mapping.base_grid(field, bounds, locked_cells,
                                          user_cell)
    out = list(rows)
    angle = settings['feature_angle']
    out.append(snappy_mapping.castellation_between_levels(settings['gradation']))
    out.append(snappy_mapping.resolve_feature_angle(angle))
    out += snappy_mapping.cell_limits(field.estimated_hex_cells())
    out += snappy_mapping.surface_levels(field, base)
    geometry = _surface_geometry(staged)
    total_edges = 0
    stats = field.per_surface()
    for name, (triangles, origins, normals) in geometry.items():
        edges = feature_edges(triangles, angle)
        total_edges += len(edges)
        out.append(snappy_mapping.included_angle(angle, name))
        level = snappy_mapping.feature_edge_level(
            field.lookup(edges.midpoints()) if len(edges) else [], base, name)
        if level is not None:
            out.append(level)
        row = stats.get(name)
        if row:
            gap = snappy_mapping.gap_level_increment(
                row, base, settings['nodes_per_gap'], name)
            if gap is not None:
                out.append(gap)
            high = snappy_mapping.level_for(base, row['size_p10'])
            reach = field.length / 4.0
            direction = _growth_direction(field, origins, normals, reach)
            distances = {}
            for level_k in range(high, 0, -1):
                distances[level_k] = snappy_mapping.distance_to_level(
                    field, origins, direction, base, level_k,
                    max_distance=reach)
            out += snappy_mapping.distance_bands(distances, name)
    out += snappy_mapping.snap_features(total_edges)
    out.append(snappy_mapping.layer_feature_angle(angle))
    walls = {name: row['size_p50'] for name, row in stats.items()}
    out += snappy_mapping.boundary_layers(field, walls)
    return out


def gmsh_proposals(field, staged, bounds, settings: dict,
                   user_fields: int = 0, curves=None, volumes=None,
                   structured=()) -> list[Proposal]:
    """The Gmsh rows of Plan 41 4.8.4.

    ``curves`` is ``{scope token: (length, points)}`` and ``volumes``
    ``{scope token: points}``, from ``structure.read_structure``; a case
    whose sources have none passes none and gets no such proposal.
    ``structured`` names the scopes whose curve row builds a transfinite or
    extruded block: they take the Structure count, the others the Curves one.
    """
    out = list(gmsh_mapping.global_sizing(field))
    out.append(gmsh_mapping.native_curvature(settings['nodes_per_two_pi'], True))
    out.append(gmsh_mapping.field_combiner(user_fields))
    out += gmsh_mapping.features(settings['feature_angle'], True)
    curves = dict(curves or {})
    structured = set(structured or ())
    out += gmsh_mapping.curve_controls(
        field, {k: v for k, v in curves.items() if k not in structured})
    out += gmsh_mapping.structure_counts(
        field, {k: v for k, v in curves.items() if k in structured})
    out += gmsh_mapping.volume_controls(field, dict(volumes or {}))
    walls = {name: row['size_p50'] for name, row in field.per_surface().items()}
    out += gmsh_mapping.boundary_layers(walls)
    return out


def structured_scopes(db) -> set:
    """Scope tokens of the curve rows that ask for a structured surface."""
    _registry, collections = _collection_paths()
    base = collections.get(gmsh_mapping.CURVES)
    if base is None:
        return set()
    try:
        keys = db.getKeys(base)
    except Exception:                                       # noqa: BLE001
        return set()
    found = set()
    for key in keys:
        if _get(db, f'{base}/{key}/transfiniteSurface') in (True, 'true',
                                                            'True', 1):
            scope = _get(db, f'{base}/{key}/scopeToken')
            if scope:
                found.add(_safe(scope))
    return found


def gmsh_extras(engine: str, db, case_path, staged, settings) -> dict:
    """What `proposals_for` needs of the geometry's curves and volumes.

    Decided here, beside `proposals_for`, so the facade never compares engine
    names: an engine whose controls are not curves and volumes gets nothing.
    Returns the keyword arguments and ``notes`` (said once on the card).
    """
    if engine != 'gmsh':
        return {}
    from .structure import read_structure
    found = read_structure(case_path, staged,
                           float(settings.get('feature_angle') or 30.0))
    return {'curves': {_safe(k): v for k, v in found.curves.items()},
            'volumes': {_safe(k): v for k, v in found.volumes.items()},
            'structured': structured_scopes(db), 'notes': found.notes}


# -- surfaces to the groups that refine them ------------------------------------ #

#: Which stored geometry field binds a surface to a group, per collection.
_GROUP_BINDINGS = {
    snappy_mapping.SURFACES: 'castellationGroup',
    snappy_mapping.LAYER_GROUPS: 'layerGroup',
}


def _min_value(values):
    return min(values)


def _first_value(values):
    return values[0]


#: How several surfaces of one group combine for each proposed element: the
#: coarsest of the minimums, the finest of the rest.
_COMBINE = {
    'surfaceRefinement.minimumLevel': _min_value,
    'firstLayerThickness': _min_value,
    'includedAngle': _first_value,
}


def group_names_by_surface(db, collection: str, aliases=None) -> dict:
    """``{safe surface name: safe group name}`` for one group collection.

    ``aliases`` maps a staged surface name (the solid a patch was cut from,
    ``face0``) to the patch that carries it (``elbow_1``): a case whose STL was
    split by angle stages ``face<N>`` but binds its groups to the patches.
    """
    _registry, collections = _collection_paths()
    base = collections.get(collection)
    field = _GROUP_BINDINGS.get(collection)
    if base is None or field is None:
        return {}
    try:
        geometry = db.getKeys('geometry')
    except Exception:                                       # noqa: BLE001
        return {}
    found = {}
    for key in geometry:
        group = _get(db, f'geometry/{key}/{field}')
        name = _get(db, f'geometry/{key}/name')
        if group in (None, '', 'None') or name in (None, ''):
            continue
        label = _get(db, f'{base}/{group}/groupName')
        if label in (None, ''):
            continue
        found[_safe(name)] = _safe(label)
    for original, patch in (aliases or {}).items():
        if patch in found and original not in found:
            found[original] = found[patch]
    return found


def regroup(db, proposals, aliases=None) -> tuple[list[Proposal], list[str]]:
    """Re-aim per-surface proposals at the group rows that refine them.

    The field sizes surfaces; the case refines *groups* (``walls`` binds every
    wall). A group takes the finest level of the surfaces bound to it (the
    coarsest for a minimum level). A surface bound to no group keeps its own
    target, which resolves to no row, and is returned as unmatched.
    """
    from dataclasses import replace
    maps = {collection: group_names_by_surface(db, collection, aliases)
            for collection in _GROUP_BINDINGS}
    kept, grouped, unmatched = [], {}, []
    for item in proposals:
        match = _ROW.match(item.target)
        collection = match.group('collection') if match else None
        if collection not in maps:
            kept.append(item)
            continue
        group = maps[collection].get(match.group('key'))
        if group is None:
            kept.append(item)
            if match.group('key') not in unmatched:
                unmatched.append(match.group('key'))
            continue
        target = f"{collection}[{group}].{match.group('element')}"
        grouped.setdefault(target, []).append(item)
    for target, items in grouped.items():
        element = _ROW.match(target).group('element')
        combine = _COMBINE.get(element, max)
        values = [item.value for item in items]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in values):
            combine = _first_value
        value = combine(values)
        chosen = next((i for i in items if i.value == value), items[0])
        badge = chosen.badge
        if len(items) > 1:
            badge = f'{badge} ({len(items)} surfaces)'
        kept.append(replace(chosen, target=target, value=value, badge=badge))
    return kept, unmatched


def proposals_for(engine: str, field, staged, bounds, settings,
                  **extra) -> list[Proposal]:
    if engine == 'gmsh':
        return gmsh_proposals(field, staged, bounds, settings, **{
            k: v for k, v in extra.items()
            if k in ('user_fields', 'curves', 'volumes', 'structured')})
    return snappy_proposals(field, staged, bounds, settings, **{
        k: v for k, v in extra.items() if k in ('locked_cells', 'user_cell')})


# -- the stage that owns a control (FIX-H) ------------------------------------ #

#: The snappy stages upstream of Castellation, where the card sits, and the
#: controls each one reads. Their stage fingerprints hash these values, so a
#: write after the stage ran makes it stale and Castellate refuses until it
#: is rerun ("upstream mesh stages are stale ...: blockMesh, surfaceFeatures").
_UPSTREAM_OWNERS = (
    ('meshing.base_grid.', 'blockMesh'),
)
_UPSTREAM_ELEMENTS = (
    (f'{snappy_mapping.SURFACES}[', '.includedAngle', 'surfaceFeatures'),
)


def owning_stage(target: str) -> str | None:
    """The snappy stage upstream of Castellation that reads *target*."""
    for prefix, stage in _UPSTREAM_OWNERS:
        if target.startswith(prefix):
            return stage
    for prefix, suffix, stage in _UPSTREAM_ELEMENTS:
        if target.startswith(prefix) and target.endswith(suffix):
            return stage
    return None


def stages_run(case_path) -> set:
    """The stages ``stage-runs.json`` records a run of."""
    import json
    path = (Path(case_path) / 'foammesh' / 'dictionaries'
            / 'stage-runs.json')
    try:
        stages = json.loads(path.read_text('utf-8')).get('stages') or {}
    except (OSError, ValueError, AttributeError):
        return set()
    return set(stages) if isinstance(stages, dict) else set()


def base_grid_is_users(db, case_path) -> bool:
    """Whether the base grid is the user's: run already, or typed.

    blockMesh ran on it, a target cell size was typed in target-size mode,
    or a cell count was typed in counts mode. Such a grid is respected
    (Plan 41 4.8.3): Apply leaves it and every level is computed against it.
    """
    if 'blockMesh' in stages_run(case_path):
        return True
    from .proposals import _same
    mode = _get(db, 'baseGrid/sizingMode', 'counts')
    mode = str(getattr(mode, 'value', mode))
    if mode == 'target_size':
        held = _get(db, 'baseGrid/targetCellSize')
        default = default_of(snappy_mapping.BASE_TARGET)
        return held not in (None, '') and (
            default is None or not _same(held, default))
    for target in (snappy_mapping.BASE_X, snappy_mapping.BASE_Y,
                   snappy_mapping.BASE_Z):
        path, _reason = resolve_target(db, target)
        default = default_of(target)
        if path is not None and default is not None                 and not _same(_get(db, path), default):
            return True
    return False


def user_base_cell(db, bounds) -> float | None:
    """The base cell the case's own grid gives around the bodies."""
    from foammesh.core.mesh import background_estimate
    try:
        found = background_estimate.estimate(db, bounds, available_bytes=0)
    except Exception:                                       # noqa: BLE001
        return None
    if found is None:
        return None
    cell = found.near_cell or found.cell
    return float(cell) if cell else None


def grid_extras(engine: str, db, case_path, bounds) -> dict:
    """What `proposals_for` needs to know about the case's own base grid.

    FIX-H. An engine whose levels are counted from a background grid (snappy)
    computes them against the cell the user's grid really gives, once that
    grid is the user's (typed, or blockMesh already ran; Plan 41 4.8.3). An
    engine without one (Gmsh) needs nothing. Decided here, beside
    `proposals_for`, so the facade never compares engine names.
    """
    if engine == 'gmsh' or not base_grid_is_users(db, case_path):
        return {}
    return {'user_cell': user_base_cell(db, bounds)}


def store_for(case_path) -> ProposalStore:
    return ProposalStore.load(proposals_path(case_path))


# -- target resolution -------------------------------------------------------- #

@lru_cache(maxsize=1)
def _collection_paths():
    from foammesh.core.facade.fields import build_field_registry
    registry = build_field_registry()
    return registry, {k: v.storage_path
                      for k, v in registry.collections.items()}


def resolve_target(db, target: str):
    """``(storage path, None)`` for a stored control, else ``(None, reason)``."""
    registry, collections = _collection_paths()
    if target.startswith('option:'):
        return None, NOT_STORED
    match = _ROW.match(target)
    if match is None:
        descriptor = registry.get(target)
        if descriptor is None:
            return None, NOT_STORED
        return descriptor.storage_path, None
    base = collections.get(match.group('collection'))
    if base is None:
        return None, NOT_STORED
    wanted = match.group('key')
    try:
        keys = db.getKeys(base)
    except Exception:                                       # noqa: BLE001
        return None, NO_ROW
    element = match.group('element').replace('.', '/')
    for key in keys:
        for name_field in _NAME_FIELDS:
            name = _get(db, f'{base}/{key}/{name_field}')
            if name is not None and _safe(name) == wanted:
                return f'{base}/{key}/{element}', None
    return None, NO_ROW


def current_values(db, targets) -> tuple[dict, dict]:
    """``(values, unresolved)``: what each stored control holds now."""
    values, unresolved = {}, {}
    for target in targets:
        path, reason = resolve_target(db, target)
        if path is None:
            unresolved[target] = reason
            continue
        values[target] = _get(db, path)
    return values, unresolved


def write_values(data, db, writes: dict) -> tuple[list[str], dict]:
    """Set each write in the checked-out *data*; returns ``(set, failed)``."""
    done, failed = [], {}
    for target, value in writes.items():
        path, reason = resolve_target(db, target)
        if path is None:
            failed[target] = reason
            continue
        held = _get(data, path)
        if isinstance(held, str) and not isinstance(value, str):
            # Numeric controls are stored as text; keep the stored form.
            value = (str(int(value)) if isinstance(value, (int, float))
                     and float(value).is_integer() else str(value))
        try:
            data.setValue(path, value)
        except Exception as error:                          # noqa: BLE001
            failed[target] = str(error)
            continue
        done.append(target)
    return done, failed


def default_of(target: str):
    """The schema default of a stored control (what an untouched one holds)."""
    registry, collections = _collection_paths()
    match = _ROW.match(target)
    if match is None:
        descriptor = registry.get(target)
        return None if descriptor is None else descriptor.default
    collection = registry.collections.get(match.group('collection'))
    if collection is None:
        return None
    node = collection.element_schema
    for part in match.group('element').split('.'):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    default = getattr(node, 'default', None)
    return default() if callable(default) else default


def defaults_for(targets) -> dict:
    return {target: default_of(target) for target in targets}
