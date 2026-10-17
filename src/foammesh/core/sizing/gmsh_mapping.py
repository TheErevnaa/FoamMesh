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
    DEFAULT_EXPANSION, FIRST_LAYER_FRACTION, _mm, row_target)

GLOBAL_SURFACE = 'gmsh.global_sizing.surface'
GLOBAL_VOLUME = 'gmsh.global_sizing.volume'
GLOBAL_TARGET = 'gmsh.global_sizing.target_size'
GLOBAL_MINIMUM = 'gmsh.global_sizing.minimum_size'
GLOBAL_MAXIMUM = 'option:Mesh.MeshSizeMax'
FROM_CURVATURE = 'gmsh.global_sizing.from_curvature'
FROM_POINTS = 'gmsh.global_sizing.from_points'
EXTEND_FROM_BOUNDARY = 'gmsh.global_sizing.extend_from_boundary'
COMBINER = 'gmsh.global_sizing.field_combiner'
THREADS = 'gmsh.global_sizing.threads'
CURVES = 'gmsh.curve_controls.controls'
VOLUMES = 'gmsh.volume_controls.controls'
BOUNDARY_LAYERS = 'gmsh.boundary_layers'
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
DEFAULT_COMBINER = 'Min'


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

def global_sizing(field) -> list[Proposal]:
    """Target = hbulk; minimum = hmin; maximum = hmax."""
    settings = field.settings
    hbulk, hmin = field.hbulk, field.hmin
    hmax = float(settings.get('hmax', hbulk))
    badge = f'field: {_mm(hbulk)}'
    return [Proposal(GLOBAL_SURFACE, hbulk, 'hbulk', badge),
            Proposal(GLOBAL_VOLUME, hbulk, 'hbulk', badge),
            Proposal(GLOBAL_TARGET, hbulk, 'hbulk', badge),
            Proposal(GLOBAL_MINIMUM, hmin, 'hmin', f'field: {_mm(hmin)}'),
            Proposal(GLOBAL_MAXIMUM, hmax, 'hmax', f'field: {_mm(hmax)}')]


def native_curvature(nodes_per_two_pi: int, automatic_active: bool) -> Proposal:
    """The native alternative: same number, never active beside the field."""
    value = 0 if automatic_active else int(nodes_per_two_pi)
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
                    ) -> list[Proposal]:
    """First = 0.3 h_wall, count from total thickness and ratio (as snappy)."""
    rows = []
    for patch, wall in patch_sizes.items():
        first = FIRST_LAYER_FRACTION * wall
        ratio = max(float(expansion), 1.0001)
        count = max(1, int(math.ceil(
            math.log(wall / first) / math.log(ratio) - 1e-9))) if wall > first else 1
        total = sum(first * ratio ** i for i in range(count))
        badge = f'field: wall {_mm(wall)} -> {count} layers'
        for element, value, rule in (
                ('first_height', first, '0.3 x h_wall'),
                ('ratio', ratio, 'the ratio the user has'),
                ('layer_count', count, 'ceil(log(final / first) / log(ratio))'),
                ('total_thickness', total, 'sum of the layers')):
            rows.append(Proposal(row_target(BOUNDARY_LAYERS, patch, element),
                                 value, rule, badge))
    return rows


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
