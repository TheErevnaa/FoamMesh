"""What the Automatic Sizing card shows from a computed field (Plan 41 4.8.2).

Plan 41 GN25c (section 12.2 S4 and S6). Everything here is plain data: the
size histogram, the estimate with its RAM, the thin-region warnings, the
"split where levels differ" offers, and the arrays the viewport overlay draws
(the field as a colour on the prepared surfaces, the feature edges, the thin
faces). No Qt, no VTK.
"""
from __future__ import annotations

import math

import numpy as np

from foammesh.support import resource_budget

from . import snappy_mapping
from .features import feature_edges
from .staging import read_stl_solids

#: Triangles sampled per surface for the histogram and the thin-face mask.
MAX_SAMPLES = 20000
BINS = 24


def _triangles(staged_surface) -> np.ndarray:
    parts = [t for _, t in read_stl_solids(staged_surface.path)]
    return np.concatenate(parts) if parts else np.zeros((0, 3, 3))


def histogram(field, staged, bins: int = BINS) -> dict:
    """O10. Field sizes at the surface triangle centres, on a log axis."""
    sizes = []
    for item in staged:
        triangles = _triangles(item)
        if not len(triangles):
            continue
        pick = (np.linspace(0, len(triangles) - 1, MAX_SAMPLES).astype(int)
                if len(triangles) > MAX_SAMPLES else slice(None))
        sizes.append(field.lookup(triangles[pick].mean(axis=1)))
    values = np.concatenate(sizes) if sizes else np.zeros(0)
    values = values[np.isfinite(values) & (values > 0)]
    if not len(values):
        return {'edges': [], 'counts': [], 'samples': 0}
    logs = np.log10(values)
    low, high = float(logs.min()), float(logs.max())
    if high - low < 1e-9:
        high = low + 1e-6
    counts, edges = np.histogram(logs, bins=bins, range=(low, high))
    return {'edges': [float(10 ** e) for e in edges],
            'counts': [int(c) for c in counts], 'samples': int(len(values)),
            'min': float(values.min()), 'max': float(values.max()),
            'median': float(np.median(values))}


def estimate(field, engine: str, available_bytes=None) -> dict:
    """O7 with the RAM the mesher would need. No cap, only the RAM."""
    gmsh = engine == 'gmsh'
    cells = field.estimated_tets() if gmsh else field.estimated_hex_cells()
    need = resource_budget.mesher_memory_needed(
        'gmsh' if gmsh else 'snappy', cells)
    if available_bytes is None:
        try:
            available_bytes = int(resource_budget.physical_memory()[1])
        except Exception:                                    # noqa: BLE001
            available_bytes = 0
    return {'cells': int(cells), 'noun': 'tetrahedra' if gmsh else 'cells',
            'ram_bytes': int(need), 'available_bytes': int(available_bytes),
            'over_ram': bool(available_bytes and need > available_bytes)}


def describe_estimate(found: dict) -> str:
    if not found or not found.get('cells'):
        return ''
    text = '≈ {0:,} {1}, about {2} of RAM'.format(
        found['cells'], found['noun'],
        resource_budget.format_bytes(found['ram_bytes']))
    if found.get('over_ram'):
        text += ' (more than the {0} free)'.format(
            resource_budget.format_bytes(found['available_bytes']))
    return text


def layer_totals(field, expansion=snappy_mapping.DEFAULT_EXPANSION) -> dict:
    """Total layer thickness per patch, from the wall size the field gives."""
    return {name: snappy_mapping.layer_thicknesses(
        row['size_p50'], expansion)['total']
        for name, row in field.per_surface().items()
        if row.get('size_p50')}


def thin_regions(field, totals=None) -> list[dict]:
    """Patches whose feature size is under twice their layer thickness."""
    totals = layer_totals(field) if totals is None else totals
    return snappy_mapping.thin_region_warnings(field, totals)


def describe_thin(warning: dict, unit_scale=1000.0, unit='mm') -> str:
    return ('{0}: feature size {1:.3g} {3} against {2:.3g} {3} of layers '
            '(ratio {4:.2f})').format(
        warning['patch'], warning['feature_size'] * unit_scale,
        warning['layer_thickness'] * unit_scale, unit, warning['ratio'])


def split_offers(field, bounds, hbulk=None) -> list[dict]:
    """The "Split where levels differ" proposals for the base cell the field
    would give."""
    _rows, base = snappy_mapping.base_grid(field, bounds)
    return snappy_mapping.split_where_levels_differ(field, base)


def summary(field, staged, bounds, engine: str, *, available_bytes=None
            ) -> dict:
    """The one dictionary the facade hands the card."""
    out = {'histogram': histogram(field, staged),
           'estimate': estimate(field, engine, available_bytes),
           'per_surface': field.per_surface(),
           'leaves': field.leaf_count, 'engine': engine}
    out['estimate_text'] = describe_estimate(out['estimate'])
    if engine != 'gmsh':
        out['thin'] = thin_regions(field)
        out['thin_text'] = [describe_thin(w) for w in out['thin']]
        out['split'] = split_offers(field, bounds)
    else:
        out['thin'], out['thin_text'], out['split'] = [], [], []
    return out


# -- viewport overlay arrays ------------------------------------------------- #

def overlay_arrays(field, staged, feature_angle: float, thin_names=()) -> dict:
    """Arrays the overlay draws, in the model's own coordinates.

    ``surface``: points (n, 3), triangles (m, 3) and ``log_size`` (n,), the
    log10 of the field at each node, so a log colour scale is the straight
    line; ``thin`` is a 0/1 per triangle where the surface is a thin region;
    ``features`` is (k, 2, 3) end points of the O5 edges.
    """
    points, triangles, thin, segments = [], [], [], []
    base = 0
    for item in staged:
        tris = _triangles(item)
        if not len(tris):
            continue
        flat = tris.reshape(-1, 3)
        points.append(flat)
        triangles.append(np.arange(len(flat)).reshape(-1, 3) + base)
        thin.append(np.full(len(tris), 1 if item.name in thin_names else 0))
        base += len(flat)
        edges = feature_edges(tris, feature_angle)
        if len(edges):
            segments.append(edges.segments)
    if not points:
        return {'points': np.zeros((0, 3)), 'triangles': np.zeros((0, 3), int),
                'log_size': np.zeros(0), 'thin': np.zeros(0, int),
                'features': np.zeros((0, 2, 3))}
    allpoints = np.concatenate(points)
    sizes = field.lookup(allpoints)
    sizes = np.where(np.isfinite(sizes) & (sizes > 0), sizes, field.hbulk)
    return {'points': allpoints, 'triangles': np.concatenate(triangles),
            'log_size': np.log10(sizes), 'thin': np.concatenate(thin),
            'features': (np.concatenate(segments) if segments
                         else np.zeros((0, 2, 3)))}
