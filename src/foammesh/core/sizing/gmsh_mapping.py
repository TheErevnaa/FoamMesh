"""What the size field proposes for the Gmsh route (Plan 41 section 4.8.4).

One function per row of the table. Targets are registry field ids (a row in a
collection is ``<collection id>[<key>].<element field>``). The route is
independent of OpenFOAM: nothing here reads a snappy key.
"""
from __future__ import annotations

import math

import numpy as np

from foammesh.core.facade import feature_angle as angle_rules

from .proposals import Proposal
from .snappy_mapping import (
    DEFAULT_EXPANSION, _mm, layer_thicknesses, row_target)

GLOBAL_TARGET = 'gmsh.global_sizing.target_size'
GLOBAL_MINIMUM = 'gmsh.global_sizing.minimum_size'
FROM_CURVATURE = 'gmsh.global_sizing.from_curvature'
FROM_POINTS = 'gmsh.global_sizing.from_points'
EXTEND_FROM_BOUNDARY = 'gmsh.global_sizing.extend_from_boundary'
COMBINER = 'gmsh.global_sizing.field_combiner'
THREADS = 'gmsh.global_sizing.threads'
CURVES = 'gmsh.curve_controls.controls'
VOLUMES = 'gmsh.volume_controls.controls'
BOUNDARY_LAYERS = 'gmsh.boundary_layers'
LAYER_FIRST = 'gmsh.boundary_layers.first_height'
LAYER_RATIO = 'gmsh.boundary_layers.ratio'
LAYER_COUNT = 'gmsh.boundary_layers.layer_count'
LAYER_TOTAL = 'gmsh.boundary_layers.total_thickness'
#: The range ``gmsh/globalSizing/fromCurvature`` stores (points per 2 pi).
CURVATURE_RANGE = (0, 50)
CLASSIFICATION = angle_rules.CLASSIFICATION_FIELD

#: The three native size sources held at zero while the automatic field is on.
HELD_OPTIONS = {
    'Mesh.MeshSizeExtendFromBoundary': EXTEND_FROM_BOUNDARY,
    'Mesh.MeshSizeFromPoints': FROM_POINTS,
    'Mesh.MeshSizeFromCurvature': FROM_CURVATURE,
}
#: Gmsh's Algorithm3D value for MMG3D, the only one that takes the metric.
ALGORITHM3D_MMG3D = 7
#: An estimate, not a measurement: bytes per tetrahedron Gmsh holds.
BYTES_PER_TET = 1500.0
#: DP-2860. The enum spells it in lower case; ``Min`` was refused.
DEFAULT_COMBINER = 'min'


# -- Sizing > Automatic ------------------------------------------------------ #

def automatic_hold(active: bool, saved: dict | None = None) -> dict:
    """Option values while the automatic tier is on, or the restore on off.

    On: the three native sources are held at 0. Off: the saved values come
    back (``saved`` is what the session read before it held them).
    """
    if active:
        return {name: 0 for name in HELD_OPTIONS}
    return dict(saved or {})


def held_labels(active: bool) -> dict:
    """The Options dock shows the held options as "held by the automatic field"."""
    return {name: 'held by the automatic field' for name in HELD_OPTIONS
            } if active else {}


# -- Sizing > Global / Curvature / Fields ------------------------------------ #

def global_sizing(field, background: bool = False) -> list[Proposal]:
    """Target = hbulk (hmax while the field is the background); minimum = hmin.

    DP-2859. The target size is ``Mesh.MeshSizeMax`` (the runner writes it
    there), so it is the one maximum: the ``option:Mesh.MeshSizeMax`` row
    beside it was a second copy nothing stored. The surface and volume
    algorithm enums took a size and were dropped.

    DP-2867. While the field is the background, ``MeshSizeMax`` below the
    field's own maximum clamps it: the far field the field coarsened came out
    at hbulk. The target is then the field's maximum, so nothing clamps.
    """
    hbulk, hmin = field.hbulk, field.hmin
    target, rule = hbulk, 'hbulk'
    if background:
        hmax = float(field.settings.get('hmax') or hbulk)
        target, rule = max(hbulk, hmax), 'hmax: the field is the background'
    return [Proposal(GLOBAL_TARGET, target, rule, f'field: {_mm(target)}'),
            Proposal(GLOBAL_MINIMUM, hmin, 'hmin', f'field: {_mm(hmin)}')]


def native_curvature(nodes_per_two_pi: int, automatic_active: bool) -> Proposal:
    """The native alternative: same number, never active beside the field.

    DP-2862. ``automatic_active`` is whether the field is the background
    (``sizing.field.use_as_background``); it was always passed as true.
    """
    low, high = CURVATURE_RANGE
    value = 0 if automatic_active else min(high, max(low, int(
        nodes_per_two_pi)))
    rule = ('off while the automatic field is on' if automatic_active
            else 'nodes per 2 pi of the field')
    return Proposal(FROM_CURVATURE, value, rule,
                    f'field: {int(nodes_per_two_pi)} points per circle')


def field_combiner(user_field_count: int) -> Proposal:
    """``Min(automatic, user fields)`` by default; Max / Mean stay selectable."""
    return Proposal(COMBINER, DEFAULT_COMBINER, 'Min(automatic, user fields)',
                    f'field: Min of automatic and {user_field_count} user')


def threads(gmsh_threads: int) -> int:
    """The tool's ``--threads`` follows ``gmsh.global_sizing.threads``."""
    return max(1, int(gmsh_threads or 1))


# -- Sizing > Curves / Volumes / Structure ----------------------------------- #

def curve_segments(length: float, sizes) -> int:
    """``N = ceil(L / h_p50)`` over the field sampled along the curve."""
    h = float(np.nanmedian(np.asarray(sizes, dtype=float)))
    return max(1, int(math.ceil(length / h - 1e-9)))


def curve_controls(field, curves) -> list[Proposal]:
    """``curves``: ``{key: (length, sample points)}``, marked "from field" only."""
    rows = []
    for key, (length, points) in curves.items():
        n = curve_segments(length, field.lookup(points))
        rows.append(Proposal(row_target(CURVES, key, 'segments'), n,
                             'ceil(L / h_p50)', f'field: {n} segments'))
    return rows


def volume_target_size(field, points) -> float:
    """Median field size inside a volume (sample points inside it)."""
    return float(np.nanmedian(field.lookup(points)))


def volume_controls(field, volumes) -> list[Proposal]:
    rows = []
    for key, points in volumes.items():
        size = volume_target_size(field, points)
        rows.append(Proposal(row_target(VOLUMES, key, 'targetSize'), size,
                             'median of the field inside the volume',
                             f'field: {_mm(size)}'))
    return rows


def structure_counts(field, curves) -> list[Proposal]:
    """Transfinite curve counts and extrusion layer counts: the same N."""
    return [Proposal(row_target(CURVES, row.target.split('[')[1].split(']')[0],
                                'segments'), row.value,
                     'ceil(L / h_p50) on a transfinite or extruded block',
                     row.badge)
            for row in curve_controls(field, curves)]


# -- Sizing > Features ------------------------------------------------------- #

def features(feature_angle: float, input_is_stl: bool) -> list[Proposal]:
    """On STL input the field's angle drives ``classifySurfaces``; on CAD the
    CAD edges are the features and O5 is preview-only (no proposal)."""
    if not input_is_stl:
        return []
    value = angle_rules.classification_angle_for(feature_angle)
    return [Proposal(CLASSIFICATION, value, 'feature angle + 10 deg',
                     f'field: {value:g} deg')]


# -- Layers ------------------------------------------------------------------- #

def boundary_layers(patch_sizes: dict, expansion=DEFAULT_EXPANSION,
                    gaps=None) -> list[Proposal]:
    """First = 0.3 h_wall, count from total thickness and ratio (as snappy).

    DP-2861. ``gmsh/boundaryLayers`` is one block of scalars, not a row per
    patch, so the proposal is one stack sized for the finest wall: the layer
    that fits there fits on every coarser one.
    """
    walls = [float(size) for size in patch_sizes.values()
             if size is not None and math.isfinite(size) and size > 0]
    if not walls:
        return []
    wall = min(walls)
    # Plan 47 S16 (DP-2869): the one stack also fits the thinnest gap.
    narrow = [float(g) for g in (gaps or {}).values()
              if g is not None and math.isfinite(g) and g > 0]
    layer = layer_thicknesses(wall, expansion, min(narrow) if narrow else None)
    first, ratio = layer['first'], layer['expansion']
    count, total = layer['layers'], layer['total']
    badge = f'field: wall {_mm(wall)} -> {count} layers'
    return [Proposal(LAYER_FIRST, first, '0.3 x h_wall of the finest wall',
                     badge),
            Proposal(LAYER_RATIO, ratio, 'the expansion ratio', badge),
            Proposal(LAYER_COUNT, count,
                     'ceil(log(h_wall / first) / log(ratio))', badge),
            Proposal(LAYER_TOTAL, total, 'sum of the layers', badge)]


# -- Generate / Quality / Reuse ---------------------------------------------- #

def element_estimate(field) -> dict:
    """"~N tetrahedra at this field" beside Generate, with the RAM."""
    tets = field.estimated_tets()
    ram = tets * BYTES_PER_TET
    return {'tetrahedra': tets, 'ram_bytes': ram,
            'text': f'~{tets:,} tetrahedra at this field, about '
                    f'{ram / 2 ** 30:.1f} GiB'}


def anisotropic_offered(algorithm_3d: int) -> bool:
    """The metric is offered only while MMG3D is selected (Plan 41 Q3)."""
    return int(algorithm_3d) == ALGORITHM3D_MMG3D


def reuse_key(scope_token: str, inputs: dict) -> str:
    """"Reuse last field" is keyed on ``geometry.prepared.current``."""
    from .field_runner import reuse_key as key
    return key(scope_token, inputs)
