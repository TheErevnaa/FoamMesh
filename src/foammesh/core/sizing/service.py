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
#: Plan 47 DP-2865. A body's whole distance ramp, ``volume_bands[<group>]``.
_RAMP = re.compile(r'^' + re.escape(snappy_mapping.VOLUME_BANDS)
                   + r'\[(?P<key>[^\]]*)\]$')
#: The only geometry rows a field group is created for.
_TRI_SURFACE = 'triSurfaceMesh'
RAMP_PREFIX = 'ramp:'
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


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(value)


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
        'use_as_background': _truthy(_get(db, 'sizeField/useAsBackground',
                                          False)),
        'feature_angle': angle_rules.normalise(_get(db, 'mesh/featureAngle')),
        'threads': int(_get(db, 'gmsh/parallel/threads', 4) or 4),
    }


def resolve_inputs(db, bounds, overrides=None) -> SizeFieldInputs:
    """Defaults from the model, then what the case holds, then overrides."""
    stored = stored_inputs(db)
    typed = {k: v for k, v in stored.items() if k in ('hmin', 'hmax', 'hbulk')
             and v > 0}
    # Whether the field is the Gmsh background changes what is proposed, not
    # the field: it is no input of the tool and no part of the reuse key.
    rest = {k: v for k, v in stored.items()
            if k not in ('hmin', 'hmax', 'hbulk', 'use_as_background')}
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
            high = snappy_mapping.max_level(base, row)
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
    # DP-2877. The stack is sized from the cell snappy puts on the wall.
    walls = {name: snappy_mapping.wall_cell(base, row)
             for name, row in stats.items()}
    out += snappy_mapping.boundary_layers(
        field, walls, gaps=snappy_mapping.wall_gaps(
            field, settings.get('nodes_per_gap', 0)))
    return out


def gmsh_proposals(field, staged, bounds, settings: dict,
                   user_fields: int = 0, curves=None, volumes=None,
                   structured=(), walls=None) -> list[Proposal]:
    """The Gmsh rows of Plan 41 4.8.4.

    ``curves`` is ``{scope token: (length, points)}`` and ``volumes``
    ``{scope token: points}``, from ``structure.read_structure``; a case
    whose sources have none passes none and gets no such proposal.
    ``structured`` names the scopes whose curve row builds a transfinite or
    extruded block: they take the Structure count, the others the Curves one.
    """
    background = bool(settings.get('use_as_background', False))
    out = list(gmsh_mapping.global_sizing(field, background))
    # DP-2862. Held at 0 only while the field is the background.
    out.append(gmsh_mapping.native_curvature(
        settings['nodes_per_two_pi'], background))
    out.append(gmsh_mapping.field_combiner(user_fields))
    out += gmsh_mapping.features(settings['feature_angle'], True)
    curves = dict(curves or {})
    structured = set(structured or ())
    out += gmsh_mapping.curve_controls(
        field, {k: v for k, v in curves.items() if k not in structured})
    out += gmsh_mapping.structure_counts(
        field, {k: v for k, v in curves.items() if k in structured})
    out += gmsh_mapping.volume_controls(field, dict(volumes or {}))
    sizes = {name: row['size_p50'] for name, row in field.per_surface().items()
             if walls is None or name in walls}
    gaps = snappy_mapping.wall_gaps(field, settings.get('nodes_per_gap', 0))
    out += gmsh_mapping.boundary_layers(
        sizes, gaps={k: v for k, v in gaps.items() if k in sizes})
    return out


#: Plan 47 SZ9 (decision Q1): the field is the Gmsh background once it exists.
BACKGROUND = 'sizing.field.use_as_background'


def background_proposal() -> Proposal:
    return Proposal(BACKGROUND, True, 'a fresh field exists',
                    'field: the background size', engine='gmsh')


def background_wanted(settings: dict, store) -> bool:
    """Whether the Gmsh proposals are made for the field as the background.

    On once a field exists, unless the user changed the box after an Apply
    put it on (DP-2871): then the stored value is theirs.
    """
    stored = _truthy(settings.get('use_as_background', False))
    entry = store.entries.get(BACKGROUND) if store is not None else None
    if entry is not None and (entry.locked or (
            entry.applied is not None and stored != bool(entry.applied))):
        return stored
    if entry is not None and entry.source == 'user':
        return stored
    return True


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
    try:
        found = read_structure(case_path, staged,
                               float(settings.get('feature_angle') or 30.0))
    except Exception as error:                              # noqa: BLE001
        # Plan 47 SZ1. Both engines are proposed on every run; a structure
        # the reader cannot take costs the Gmsh curve rows, not the run.
        return {'structured': structured_scopes(db),
                'notes': [f'curves and volumes were not read: {error}']}
    # Plan 47 SZ12 (live, elbow). Only a curve or volume that has a control
    # row is sized: proposing for every curve of a fresh case named rows no
    # store has (no_row). Rows marked "from field" are SZ6.
    curves = _control_tokens(db, gmsh_mapping.CURVES)
    volumes = _control_tokens(db, gmsh_mapping.VOLUMES)
    return {'curves': {_safe(k): v for k, v in found.curves.items()
                       if _safe(k) in curves},
            'volumes': {_safe(k): v for k, v in found.volumes.items()
                        if _safe(k) in volumes},
            'structured': structured_scopes(db), 'notes': found.notes}


def _control_tokens(db, collection: str) -> set:
    """The safe names of the control rows *collection* holds."""
    _registry, collections = _collection_paths()
    base = collections.get(collection)
    if base is None:
        return set()
    try:
        keys = list(db.getKeys(base))
    except Exception:                                       # noqa: BLE001
        return set()
    found = set()
    for key in keys:
        for name_field in _NAME_FIELDS:
            name = _get(db, f'{base}/{key}/{name_field}')
            if name not in (None, ''):
                found.add(_safe(name))
    return found


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
    'minThickness': _min_value,
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
        # Plan 47 SZ12. A volume row's castellationGroup names a refinement
        # *volume*; read against the surface groups it matched by key alone.
        if _get(db, f'geometry/{key}/gType') == 'volume':
            continue
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
    bodies = body_names_by_surface(db, aliases)
    kept, grouped, unmatched = [], {}, []
    ramps: dict = {}
    for item in proposals:
        ramp = _RAMP.match(item.target)
        if ramp is not None:
            # DP-2865. A ramp is measured per surface and written per body.
            body = bodies.get(ramp.group('key'))
            if body is None:
                kept.append(item)
                if ramp.group('key') not in unmatched:
                    unmatched.append(ramp.group('key'))
            else:
                ramps.setdefault(body, []).append(item)
            continue
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
    for body, items in ramps.items():
        kept.append(replace(items[0], target=snappy_mapping.ramp_target(body),
                            value=merge_ramps([i.value for i in items])))
    return kept, unmatched


def merge_ramps(ramps) -> list:
    """One ramp that refines at least as far as each of *ramps* asks.

    Per level the farthest distance any ramp reaches, then the order snappy
    demands (distance up, level down): a band a finer level already reaches
    past is dropped.
    """
    farthest: dict = {}
    for ramp in ramps:
        for distance, level in ramp or ():
            level, distance = int(level), float(distance)
            farthest[level] = max(distance, farthest.get(level, 0.0))
    merged, reach = [], 0.0
    for level in sorted(farthest, reverse=True):
        if farthest[level] > reach:
            merged.append([farthest[level], level])
            reach = farthest[level]
    return merged


def body_names_by_surface(db, aliases=None) -> dict:
    """``{safe surface name: safe ramp group}`` through each surface's body.

    A distance ramp is a ``refinementRegions`` entry on the body's staged
    surface: the body's volume row bound to a distance-mode volume group whose
    ``volumeBands`` rows carry the group's name (``CaseBuilder``
    ``_registered_volume_name``). A body already bound names its group; an
    unbound one is named after itself, the group Apply creates for it.
    """
    rows = _geometry_rows(db)
    volumes = {}
    for key, row in rows.items():
        if row.value('gType') != 'volume' or str(
                row.value('shape') or '') != _TRI_SURFACE:
            continue
        group = row.value('castellationGroup')
        name = None
        if group not in (None, '', 'None'):
            name = _get(db, f'castellation/refinementVolumes/{group}/groupName')
        volumes[key] = _safe(name or row.value('name') or key)
    found = {}
    for row in rows.values():
        if row.value('gType') != 'surface' or not row.value('name'):
            continue
        body = volumes.get(str(row.value('volume')))
        if body is not None:
            found[_safe(row.value('name'))] = body
    for original, patch in (aliases or {}).items():
        if patch in found and original not in found:
            found[original] = found[patch]
    return found


# -- groups the field creates (Plan 47 SZ4) ---------------------------------- #

def _geometry_rows(db) -> dict:
    try:
        return {str(k): v for k, v in dict(
            db.getElements('geometry') or {}).items()}
    except Exception:                                       # noqa: BLE001
        return {}


def _unbound(value) -> bool:
    return value is None or str(value).strip() in ('', 'None')


def _surface_row(rows: dict, key: str, aliases=None):
    """The surface row whose safe name is *key* (or the patch it aliases)."""
    wanted = {key, (aliases or {}).get(key, key)}
    for gid, row in rows.items():
        if row.value('gType') == 'surface' and _safe(
                row.value('name') or '') in wanted:
            return gid, row
    return None, None


def _new_group(data, path: str, name: str, values: dict) -> str:
    element = data.newElement(path)
    element.setValue('groupName', name)
    for field, value in values.items():
        element.setValue(field, value)
    return str(data.addElement(path, element))


def publishes_as_wall(boundary) -> bool:
    """Whether a boundary publishes as a wall, so layers may grow on it.

    Plan 47 SZ12 (live, elbow). ``elbow_surface`` carries no category and its
    name says none, so ``layer_wall`` left it out and the field's layers named
    no row. A boundary with no category publishes as a wall (DP-867,
    ``gmsh.execution.surface_categories``); one whose category or name says
    inlet, outlet or interface still does not.
    """
    name, category = tuple(boundary)
    if layer_wall((name, category)):
        return True
    from foammesh.core.gmsh.layer_targets import layer_targets_module
    return (not str(category or '').strip()
            and layer_targets_module().boundary_category(name, '')
            == 'unclassified')


def ensure_groups(data, targets, aliases=None, categories=None) -> list[str]:
    """Create and bind the groups the field's targets need, in *data*.

    DP-2863/2864/2865. On a fresh case no group refines the surfaces the field
    sized, so every level, layer and band resolved to no row. For a target
    that names no row, a group named after its surface is created and the
    surface (for a ramp, the body) is bound to it, so the target names it. A
    surface or body the user already bound keeps its group. Layer groups go
    on walls only (``layer_wall``; ``categories`` maps a patch name to its
    category). Returns the targets the call made resolvable.
    """
    _registry, collections = _collection_paths()
    rows = _geometry_rows(data)
    made = []
    for target in targets:
        if resolve_target(data, target)[0] is not None:
            continue
        ramp = _RAMP.match(target)
        if ramp is not None:
            for gid, row in rows.items():
                if (row.value('gType') == 'volume'
                        and str(row.value('shape') or '') == _TRI_SURFACE
                        and _safe(row.value('name') or '') == ramp.group('key')
                        and _unbound(row.value('castellationGroup'))):
                    group = _new_group(
                        data, 'castellation/refinementVolumes',
                        str(row.value('name')), {'mode': 'distance'})
                    data.setValue(f'geometry/{gid}/castellationGroup', group)
                    made.append(target)
                    break
            continue
        match = _ROW.match(target)
        if match is None or match.group('collection') not in _GROUP_BINDINGS:
            continue
        collection = match.group('collection')
        gid, row = _surface_row(rows, match.group('key'), aliases)
        if row is None or str(row.value('shape') or _TRI_SURFACE) \
                != _TRI_SURFACE:
            continue
        binding = _GROUP_BINDINGS[collection]
        if not _unbound(row.value(binding)):
            continue
        name = str(row.value('name'))
        if collection == snappy_mapping.LAYER_GROUPS and not publishes_as_wall((name, (categories or {}).get(name, ''))):
            continue
        # Named as the target names it (the staged surface, which may be the
        # body the row belongs to), so the target resolves to it.
        group = _new_group(data, collections[collection],
                           match.group('key') or name, {})
        data.setValue(f'geometry/{gid}/{binding}', group)
        made.append(target)
    return made


def body_aliases(db) -> dict:
    """``{body: its one surface}`` where the field's name is the body's.

    Plan 47 SZ12 (live, elbow). An STL of one solid is staged under the
    solid's name (``elbow``) while its one boundary row is ``elbow_surface``,
    so every per-surface proposal named no row and no group. A body with
    exactly one surface row stands for that row; a name a surface row has
    itself is never aliased.
    """
    rows = _geometry_rows(db)
    surfaces = {_safe(r.value('name') or '') for r in rows.values()
                if r.value('gType') == 'surface'}
    children: dict = {}
    for row in rows.values():
        if row.value('gType') == 'surface' and not _unbound(row.value('volume')):
            children.setdefault(str(row.value('volume')), []).append(
                _safe(row.value('name') or ''))
    found = {}
    for gid, row in rows.items():
        if row.value('gType') != 'volume':
            continue
        body = _safe(row.value('name') or '')
        kids = children.get(str(gid), [])
        if body and len(kids) == 1 and body not in surfaces:
            found[body] = kids[0]
    return found


def _ramp_group(db, key: str):
    """The refinementVolumes row named *key* that a volume row is bound to."""
    try:
        groups = list(db.getKeys('castellation/refinementVolumes'))
    except Exception:                                       # noqa: BLE001
        return None
    bound = {str(row.value('castellationGroup'))
             for row in _geometry_rows(db).values()
             if row.value('gType') == 'volume'}
    for group in groups:
        name = _get(db, f'castellation/refinementVolumes/{group}/groupName')
        if name is not None and _safe(name) == key and str(group) in bound:
            return group
    return None


def ramp_is_users(db, target: str) -> bool:
    """A ramp lands only on a distance-mode group; another mode is theirs."""
    match = _RAMP.match(target)
    group = None if match is None else _ramp_group(db, match.group('key'))
    if group is None:
        return False
    mode = _get(db, f'castellation/refinementVolumes/{group}/mode')
    return 'distance' not in str(getattr(mode, 'value', mode)).lower()


#: Plan 47 SZ1. Every field run proposes for both engines, so switching the
#: engine finds its numbers already filled.
ENGINES = ('snappy', 'gmsh')


def proposals_for(engine: str, field, staged, bounds, settings,
                  **extra) -> list[Proposal]:
    if engine == 'gmsh':
        rows = gmsh_proposals(field, staged, bounds, settings, **{
            k: v for k, v in extra.items()
            if k in ('user_fields', 'curves', 'volumes', 'structured',
                     'walls')})
    else:
        engine = 'snappy'
        rows = snappy_proposals(field, staged, bounds, settings, **{
            k: v for k, v in extra.items()
            if k in ('locked_cells', 'user_cell')})
    for row in rows:
        row.engine = engine
    return rows


def proposals_all(field, staged, bounds, settings, extras: dict
                  ) -> list[Proposal]:
    """Both engines' proposals; ``extras`` maps engine -> its keywords."""
    rows = []
    for engine in ENGINES:
        rows += proposals_for(engine, field, staged, bounds, settings,
                              **dict(extras.get(engine) or {}))
    return rows


# -- walls -------------------------------------------------------------------- #

def layer_wall(boundary) -> bool:
    """Whether a ``(name, category)`` boundary is a wall layers may grow on.

    The rule Gmsh's "All eligible walls" uses (``layer_targets``), so both
    engines put layers on the same surfaces.
    """
    from foammesh.core.gmsh.layer_targets import eligible_wall_names
    return bool(eligible_wall_names([tuple(boundary)]))


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
    ramp = _RAMP.match(target)
    if ramp is not None:
        group = _ramp_group(db, ramp.group('key'))
        if group is None:
            return None, NO_ROW
        name = _get(db, f'castellation/refinementVolumes/{group}/groupName')
        return RAMP_PREFIX + str(name), None
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
    # Plan 47 SZ12 (live, elbow): a surface already bound to a group of
    # another name (the import's `walls`) is sized through that group.
    group = _bound_group(db, match.group('collection'), wanted, keys)
    if group is None and match.group('collection') ==             snappy_mapping.LAYER_GROUPS:
        group = _pattern_layer_group(db, wanted, keys)
    if group is not None:
        return f'{base}/{group}/{element}', None
    return None, NO_ROW


def _pattern_layer_group(db, surface: str, keys):
    """The layer group whose patch pattern already names *surface*.

    Plan 47 SZ12 (live, elbow, round 3). The case's `Walls` group layers the
    patch `elbow` by the pattern `elbow`. Apply made a second group for the
    same patch with absolute sizes, and the dictionary refused: "Foundation
    13 applies relativeSizes globally". The field sizes the group that is
    already there.
    """
    from foammesh.core.layer_patterns import PatternError, matching_patches
    names = {surface}
    _gid, row = _surface_row(_geometry_rows(db), surface)
    if row is not None:
        names.add(_safe(row.value('name') or ''))
    for key in keys:
        if str(_get(db, f'addLayers/layers/{key}/patchSelector')) != 'pattern':
            continue
        pattern = str(_get(db, f'addLayers/layers/{key}/patchPattern') or '')
        try:
            if pattern and matching_patches(pattern, sorted(names)):
                return str(key)
        except PatternError:
            continue
    return None


def _bound_group(db, collection: str, surface: str, keys):
    """The group key *surface*'s geometry row is bound to, if it exists."""
    binding = _GROUP_BINDINGS.get(collection)
    if binding is None:
        return None
    present = {str(key) for key in keys}
    _gid, row = _surface_row(_geometry_rows(db), surface)
    if row is None:
        return None
    group = row.value(binding)
    if _unbound(group) or str(group) not in present:
        return None
    return str(group)


def merge_shared(db, writes: dict, current: dict) -> tuple[dict, dict]:
    """``(writes, merged)`` with one value per stored path.

    Plan 47 SZ12. Two surfaces bound to one group resolve to the same row;
    written one after the other, the last surface won, and the next Apply
    found the first surface's control "typed". The paths are grouped, the
    value `regroup` would give is kept for every target sharing the path (``merged``:
    target -> the value that path now holds), and a path that already holds
    it is not rewritten.
    """
    by_path: dict = {}
    for target in sorted(writes):
        path, _reason = resolve_target(db, target)
        by_path.setdefault(path or target, []).append(target)
    out, merged = {}, {}
    for path, targets in by_path.items():
        values = [writes[t] for t in targets]
        value = values[0]
        if len(targets) > 1:
            match = _ROW.match(targets[0])
            element = match.group('element') if match else ''
            if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in values):
                value = _COMBINE.get(element, max)(values)
        for target in targets:
            merged[target] = value
        held = current.get(targets[0])
        if held is not None and _same_value(held, value):
            continue
        out[targets[0]] = value
    return out, merged


def _same_value(left, right) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def current_values(db, targets) -> tuple[dict, dict]:
    """``(values, unresolved)``: what each stored control holds now."""
    values, unresolved = {}, {}
    for target in targets:
        path, reason = resolve_target(db, target)
        if path is None:
            unresolved[target] = reason
            continue
        if path.startswith(RAMP_PREFIX):
            from foammesh.db import volume_bands
            values[target] = [[d, level] for d, level in
                              volume_bands.group_rows(
                                  db, path[len(RAMP_PREFIX):])]
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
        if path.startswith(RAMP_PREFIX):
            from foammesh.db import volume_bands
            group = path[len(RAMP_PREFIX):]
            try:
                volume_bands.write_group_rows(
                    data, (group,), group,
                    [(float(d), int(level)) for d, level in value or ()])
            except Exception as error:                      # noqa: BLE001
                failed[target] = str(error)
                continue
            done.append(target)
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
    if _RAMP.match(target):
        return None
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


def defaults_for(targets, db=None) -> dict:
    defaults = {target: default_of(target) for target in targets}
    if db is not None:
        from .proposals import AnyOf
        for target, value in app_defaults(db, targets).items():
            defaults[target] = AnyOf((defaults.get(target), value))
    return defaults


#: The layer count the snappy Layers page gives its default *Walls* group
#: (``SnappyLayersPage.DEFAULT_SURFACE_LAYERS``; a test keeps the two equal).
APP_LAYER_COUNT = 3
#: The name both default groups carry (Plan 40 UXA2 ``walls``, the Layers
#: page's ``Walls``).
_APP_GROUP = 'walls'


def app_defaults(db, targets) -> dict:
    """What the app's own default groups were made holding, per target.

    Plan 47 SZ12 (live, elbow and jacketed pipe, round 4). A new snappy case
    gets a *walls* refinement group at the levels its Mesh size choice
    derives and a *Walls* layer group of three layers. Nobody typed those,
    yet they are not schema defaults, so every Apply skipped them as typed
    and the field never set a wall level or a layer count. A control still
    holding the value the app made it with is untouched, like one at its
    schema default.
    """
    from .derive import derive_sizes
    _registry, collections = _collection_paths()
    levels = None
    found = {}
    for target in targets:
        match = _ROW.match(target)
        if match is None:
            continue
        collection = match.group('collection')
        if collection not in _GROUP_BINDINGS:
            continue
        path, _reason = resolve_target(db, target)
        base = collections.get(collection)
        if path is None or base is None or not path.startswith(base + '/'):
            continue
        key = path[len(base) + 1:].split('/', 1)[0]
        name = _safe(_get(db, f'{base}/{key}/groupName') or '')
        if name.lower() != _APP_GROUP:
            continue
        element = match.group('element')
        if collection == snappy_mapping.LAYER_GROUPS:
            if element == 'nSurfaceLayers':
                found[target] = APP_LAYER_COUNT
            continue
        if levels is None:
            preset = _get(db, 'mesh/sizePreset')
            proposal = derive_sizes(getattr(preset, 'value', preset),
                                    diagonal=1.0)
            levels = {} if proposal is None else {
                'surfaceRefinement.minimumLevel': proposal.surface_min_level,
                'surfaceRefinement.maximumLevel': proposal.surface_max_level,
                'featureEdgeRefinementLevel': proposal.feature_level}
        if element in levels:
            found[target] = levels[element]
    return found
