"""What the size field proposes for the snappy route (Plan 41 section 4.8.3).

One function per row of the table, each returning ``Proposal`` records: the
target control, the value, the rule that gave it and the badge text. Targets
are the registry field ids; a row in a collection is
``<collection id>[<key>].<element field>``. The two groups the GUI hides
because OpenFOAM 13 does not read them (the curvature group and the gap-region
group) get no proposal: the field reaches the mesh through keys v13 reads.
"""
from __future__ import annotations

import math

import numpy as np

from foammesh.core.facade import feature_angle as angle_rules

from .proposals import Proposal

FIELD = 'field'

BASE_X = 'meshing.base_grid.cells.x'
BASE_Y = 'meshing.base_grid.cells.y'
BASE_Z = 'meshing.base_grid.cells.z'
BASE_MODE = 'meshing.base_grid.sizing_mode'
BASE_TARGET = 'meshing.base_grid.target_cell_size'
CELLS_BETWEEN = 'meshing.castellation.cells_between_levels'
RESOLVE_ANGLE = angle_rules.RESOLVE_FIELD
MAX_LOCAL = 'meshing.castellation.max_local_cells'
MAX_GLOBAL = 'meshing.castellation.max_global_cells'
SURFACES = 'meshing.castellation.surface_refinements'
VOLUMES = 'meshing.castellation.volume_refinements'
FEATURE_BANDS = 'meshing.castellation.feature_bands'
VOLUME_BANDS = 'meshing.castellation.volume_bands'
EXPLICIT_SNAP = 'meshing.snap.explicit_feature_snap'
IMPLICIT_SNAP = 'meshing.snap.implicit_feature_snap'
LAYER_ANGLE = angle_rules.LAYER_FIELD
LAYER_GROUPS = 'meshing.layers.groups'

#: Never ask for a gradation below this; ln 2 / ln g explodes towards 1.
MIN_GRADATION = 1.05
#: snappy's limits are 2 x the estimate so it never truncates (Plan 41 4.8.3).
LIMIT_FACTOR = 2.0
#: Plan 41 4.8.3 boundary layers: first = 0.3 h_wall, final = h_wall.
FIRST_LAYER_FRACTION = 0.3
DEFAULT_EXPANSION = 1.2


def row_target(collection: str, key: str, element_field: str) -> str:
    return f'{collection}[{key}].{element_field}'


def level_for(base_cell: float, size: float) -> int:
    """Nearest level whose cell is ``size``: ``round(log2(base / size))``.

    Nearest, not ceiling: the ceiling rule overshot the estimate 3x on the
    jacketed pipe (Plan 41 section 12.3). Clamped at zero.
    """
    if size <= 0 or base_cell <= 0:
        return 0
    return max(0, int(round(math.log2(base_cell / size))))


def cell_size_at(base_cell: float, level: int) -> float:
    return base_cell / float(2 ** level)


def _mm(value: float) -> str:
    if value >= 1.0:
        return f'{value:.3g} m'
    if value >= 1e-3:
        return f'{value * 1e3:.3g} mm'
    return f'{value * 1e6:.3g} um'


# -- base grid --------------------------------------------------------------- #

def base_grid_cells(bounds, hbulk: float) -> tuple[int, int, int]:
    """Whole cells per axis so the bounding box divides by about ``hbulk``."""
    values = [float(item) for item in bounds]
    spans = (values[1] - values[0], values[3] - values[2],
             values[5] - values[4])
    return tuple(max(1, int(math.ceil(span / hbulk - 1e-9))) for span in spans)


def base_cell_of(bounds, cells) -> float:
    """The single base cell size of a grid: the mean of its three edges."""
    values = [float(item) for item in bounds]
    spans = (values[1] - values[0], values[3] - values[2],
             values[5] - values[4])
    edges = [span / max(1, int(count)) for span, count in zip(spans, cells)]
    return float(np.mean(edges))


def base_grid(field, bounds, locked_cells=None, user_cell=None):
    """Row: Base grid cells. Returns ``(proposals, base_cell)``.

    ``locked_cells`` is the user's grid; it is respected, and every level
    below is computed against its cell instead. ``user_cell`` is the base
    cell of a grid the user typed or already ran (FIX-H): the proposals stay,
    as the badge the Base grid page offers, but Apply does not write them,
    so every level below is computed against the cell the mesh really has.
    """
    if locked_cells is not None:
        cells = tuple(int(item) for item in locked_cells)
        return [], base_cell_of(bounds, cells)
    cells = base_grid_cells(bounds, field.hbulk)
    cell = base_cell_of(bounds, cells)
    if user_cell is not None and float(user_cell) > 0:
        cell = float(user_cell)
    badge = (f'field: {_mm(field.hbulk)} -> '
             f'{cells[0]} × {cells[1]} × {cells[2]}')
    rule = 'hbulk rounded so the bounding box divides into whole cells'
    # GN25f. Cell counts are read only in counts mode: without this row the
    # applied 14 x 14 x 5 sat beside a target size that still made the grid.
    return ([Proposal(BASE_MODE, 'counts', 'the field gives cell counts', badge),
             Proposal(BASE_X, cells[0], rule, badge),
             Proposal(BASE_Y, cells[1], rule, badge),
             Proposal(BASE_Z, cells[2], rule, badge)], cell)


# -- castellation ------------------------------------------------------------ #

def cells_between_levels(gradation: float) -> int:
    """``n = ceil(ln 2 / ln g)``: g = 1.3 -> 3, g = 1.1 -> 8."""
    g = max(float(gradation), MIN_GRADATION)
    return int(math.ceil(math.log(2.0) / math.log(g) - 1e-9))


def castellation_between_levels(gradation: float) -> Proposal:
    n = cells_between_levels(gradation)
    return Proposal(CELLS_BETWEEN, n, 'ceil(ln 2 / ln gradation)',
                    f'field: gradation {gradation:g} -> {n}')


def resolve_feature_angle(feature_angle: float) -> Proposal:
    value = angle_rules.normalise(feature_angle)
    return Proposal(RESOLVE_ANGLE, value,
                    'the feature angle of the features run',
                    f'field: {value:g} deg')


def cell_limits(estimated_cells: int) -> list[Proposal]:
    """snappy's own counters: 2 x the estimate, never a FoamMesh cap."""
    limit = int(math.ceil(max(estimated_cells, 1) * LIMIT_FACTOR))
    badge = f'estimate {estimated_cells:,}, snappy limit {limit:,}'
    return [Proposal(MAX_LOCAL, limit, '2 x the estimate', badge),
            Proposal(MAX_GLOBAL, limit, '2 x the estimate', badge)]


# -- surface refinement ------------------------------------------------------ #

def surface_levels(field, base_cell: float) -> list[Proposal]:
    """Per surface ``level (min max)`` from the p90 and p10 sizes (O10)."""
    rows = []
    for name, stats in field.per_surface().items():
        low = level_for(base_cell, stats['size_p90'])
        high = max(low, level_for(base_cell, stats['size_p10']))
        got = cell_size_at(base_cell, high)
        badge = (f'field: {_mm(stats["size_p10"])} -> level {high} '
                 f'({_mm(got)})')
        rule = 'round(log2(baseCell / h)) at p90 (min) and p10 (max)'
        rows.append(Proposal(row_target(SURFACES, name,
                                        'surfaceRefinement.minimumLevel'),
                             low, rule, badge))
        rows.append(Proposal(row_target(SURFACES, name,
                                        'surfaceRefinement.maximumLevel'),
                             high, rule, badge))
    return rows


def feature_edge_level(edge_sizes, base_cell: float, surface: str) -> Proposal | None:
    """``features { level }`` from the p10 of the field along O5 edges."""
    sizes = np.asarray(edge_sizes, dtype=float)
    sizes = sizes[np.isfinite(sizes)]
    if not len(sizes):
        return None
    h = float(np.percentile(sizes, 10))
    level = level_for(base_cell, h)
    return Proposal(row_target(SURFACES, surface, 'featureEdgeRefinementLevel'),
                    level, 'round(log2(baseCell / h_edge_p10))',
                    f'field: {_mm(h)} -> level {level}')


def included_angle(feature_angle: float, surface: str) -> Proposal:
    value = angle_rules.included_angle(feature_angle)
    return Proposal(row_target(SURFACES, surface, 'includedAngle'), value,
                    'the angle the field used, as surfaceFeatures reads it',
                    f'field: {value:g} deg')


def gap_level_increment(stats: dict, base_cell: float, nodes_per_gap: int,
                        surface: str) -> Proposal | None:
    """``max(0, round(log2(h_max_level / max(feature_size_p10, size_min))))``.

    Only where O4 is finite on that surface and the thin-region rule is on
    (``nodes_per_gap`` > 0). GN17/GN25f (measured 2026-10-12): the tool's O4
    (``feature_size``) is the size the thin-region rule asks for, that is the
    gap already divided by the nodes per gap and clamped to [hmin, hmax]
    (Gmsh ``featureSize()``: ``h = e.length() / nLayersPerGap``). Measured on
    the jacketed pipe's 12 mm annulus with a surface sampled finely enough for
    the rule to fire: ``feature_size_p10`` = 2.99 mm = 12 / 4. The plan's
    ``feature_size / nPointsPerGap`` therefore divided twice and asked for a
    cell four times finer than the gap rule did. The increment never asks for
    more than the field holds: ``size_min`` is the finest size the field itself
    has on the surface, so a gap the field did not refine for (the elbow, the
    pipe with its feature size clamped to hMax) adds nothing. The nearest level
    is used, as on the other level rows.
    """
    feature = stats.get('feature_size_p10')
    if not nodes_per_gap or feature is None or not math.isfinite(feature)             or feature <= 0 or feature >= 1e29:
        return None
    high = level_for(base_cell, stats['size_p10'])
    h_level = cell_size_at(base_cell, high)
    needed = float(feature)
    finest = stats.get('size_min')
    if finest is not None and math.isfinite(finest) and finest > 0:
        needed = max(needed, float(finest))
    value = max(0, int(round(math.log2(h_level / needed))))
    return Proposal(row_target(SURFACES, surface, 'gapLevelIncrement'), value,
                    'round(log2(h at max level / max(feature size, finest '
                    'size of the field))); the feature size is already the '
                    'gap over the nodes per gap',
                    f'field: gap {_mm(feature * nodes_per_gap)} -> +{value}')


def split_where_levels_differ(field, base_cell: float) -> list[dict]:
    """New row: surfaces whose p10 and p90 levels differ by two or more.

    These are offered a split (per-STL-solid ``regions`` or a feature-angle
    split); the user picks. Returns plain records, not proposals.
    """
    offers = []
    for name, stats in field.per_surface().items():
        low = level_for(base_cell, stats['size_p90'])
        high = level_for(base_cell, stats['size_p10'])
        if high - low >= 2:
            offers.append({'surface': name, 'minimum_level': low,
                           'maximum_level': high,
                           'options': ['regions', 'feature_angle_split']})
    return offers


# -- volume refinement ------------------------------------------------------- #

def distance_bands(distances_by_level: dict, surface: str) -> list[Proposal]:
    """``refinementRegions.<s> { mode distance; levels }``.

    ``distances_by_level`` maps level k -> the distance at which the field
    first reaches ``baseCell / 2**k`` (see ``distance_to_level``). Writes
    strictly ordered pairs: larger distance, lower level.
    """
    rows = []
    # strictly ordered: distance increases as level decreases
    usable = [(level, d) for level, d in distances_by_level.items()
              if d is not None and math.isfinite(d) and d > 0]
    usable.sort(key=lambda item: -item[0])
    ordered, previous = [], 0.0
    for level, distance in usable:
        if distance > previous:
            ordered.append((level, distance))
            previous = distance
    for index, (level, distance) in enumerate(ordered):
        key = f'{surface}.{index + 1}'
        rows.append(Proposal(row_target(VOLUME_BANDS, key, 'distance'),
                             distance, 'distance where the field reaches '
                             'baseCell / 2^level',
                             f'field: level {level} to {_mm(distance)}'))
        rows.append(Proposal(row_target(VOLUME_BANDS, key, 'level'), level,
                             'band level', f'field: level {level}'))
    return rows


def distance_to_level(field, origins, directions, base_cell: float,
                      level: int, *, max_distance: float | None = None,
                      steps: int = 400) -> float | None:
    """Median over rays of the distance at which the field reaches level's size.

    Rays start on the surface and walk along *directions* (the side the field
    grows towards).
    """
    target = cell_size_at(base_cell, level)
    top = max_distance or field.length / 2.0
    distances = np.linspace(0.0, top, steps)
    sizes = field.sample_along(origins, directions, distances)
    reached = sizes >= target
    first = np.where(reached.any(axis=1), reached.argmax(axis=1), -1)
    # GN25f. A ray that starts already at (or beyond) the target size needs no
    # band: counting its distance of zero dragged the median to zero and
    # dropped the whole band (the elbow, where half the wall is coarser than
    # level 2). Only the rays that start finer than the level vote.
    found = (first >= 0) & ~(sizes[:, 0] >= target)
    if not found.any():
        return None
    return float(np.median(distances[first[found]]))


# -- snap --------------------------------------------------------------------- #

def snap_features(feature_edge_count: int) -> list[Proposal]:
    """Explicit feature snap when O5 is non-empty, implicit otherwise."""
    explicit = feature_edge_count > 0
    badge = (f'field: {feature_edge_count} feature edges' if explicit
             else 'field: no feature edges')
    rule = 'explicit when O5 is non-empty, implicit otherwise'
    return [Proposal(EXPLICIT_SNAP, explicit, rule, badge),
            Proposal(IMPLICIT_SNAP, not explicit, rule, badge)]


# -- boundary layers ---------------------------------------------------------- #

def layer_feature_angle(feature_angle: float) -> Proposal:
    value = angle_rules.layer_angle(feature_angle) \
        if hasattr(angle_rules, 'layer_angle') else 2.0 * float(feature_angle)
    return Proposal(LAYER_ANGLE, value, 'the field feature angle (UXA2 rule)',
                    f'field: {value:g} deg')


def layer_thicknesses(wall_size: float, expansion: float = DEFAULT_EXPANSION,
                      ) -> dict:
    """first = 0.3 h_wall, final = h_wall, layers from the user's ratio."""
    first = FIRST_LAYER_FRACTION * wall_size
    ratio = max(float(expansion), 1.0001)
    layers = max(1, int(math.ceil(
        math.log(wall_size / first) / math.log(ratio) - 1e-9))) \
        if wall_size > first else 1
    total = sum(first * ratio ** i for i in range(layers))
    return {'first': first, 'final': wall_size, 'layers': layers,
            'expansion': ratio, 'total': total}


def boundary_layers(field, patch_sizes: dict, expansion=DEFAULT_EXPANSION,
                    ) -> list[Proposal]:
    """Per patch: first/final thickness and layer count (relative mode)."""
    rows = []
    for patch, wall in patch_sizes.items():
        layer = layer_thicknesses(wall, expansion)
        badge = (f'field: wall {_mm(wall)} -> {layer["layers"]} layers, '
                 f'first {_mm(layer["first"])}')
        for element, value, rule in (
                ('firstLayerThickness', layer['first'], '0.3 x h_wall'),
                ('finalLayerThickness', layer['final'], 'h_wall'),
                ('nSurfaceLayers', layer['layers'],
                 'ceil(log(final / first) / log(ratio))')):
            rows.append(Proposal(row_target(LAYER_GROUPS, patch, element),
                                 value, rule, badge))
    return rows


def thin_region_warnings(field, layer_totals: dict) -> list[dict]:
    """Patches whose ``feature_size < 2 x total layer thickness``, with ratio."""
    warnings = []
    stats = field.per_surface()
    for patch, total in layer_totals.items():
        feature = (stats.get(patch) or {}).get('feature_size_p10')
        if feature is None or not math.isfinite(feature) or total <= 0 \
                or feature >= 1e29:
            continue
        if feature < 2.0 * total:
            warnings.append({'patch': patch, 'feature_size': feature,
                             'layer_thickness': total,
                             'ratio': feature / total})
    return warnings


# -- after castellation ------------------------------------------------------- #

REFINE_FACTOR = 1.5


def refine_to_field_cells(cell_sizes, field_sizes) -> np.ndarray:
    """Mask of cells for the ``refineMesh`` cell set: ``cellSize > 1.5 h``."""
    cell_sizes = np.asarray(cell_sizes, dtype=float)
    field_sizes = np.asarray(field_sizes, dtype=float)
    with np.errstate(invalid='ignore'):
        return cell_sizes > REFINE_FACTOR * field_sizes
