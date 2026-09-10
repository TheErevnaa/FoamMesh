"""Did the mesh keep the features the geometry declared?

Plan 23 §6.3. A section can score well on area-weighted surface distance and
still have lost the thing that mattered: a blade's leading edge rounded by a
millimetre moves almost no area, so a distance statistic over the whole patch
barely registers it. Features are measured on their own, and §6.3 is explicit
that a critical feature below its threshold **fails its section** whatever the
surface score says.

Both directions again, for the same reason as §6.1: reference-to-mesh finds a
feature that was rounded away, and mesh-to-reference finds a crease the mesher
invented where the geometry is smooth.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .distance import SurfaceLocator


class FeatureMatchError(ValueError):
    pass


def polyline_points(feature) -> np.ndarray:
    """The ordered points of a feature's geometry."""
    geometry = getattr(feature, 'geometry', None) or {}
    points = geometry.get('points') or geometry.get('point')
    if points is None:
        return np.empty((0, 3))
    array = np.asarray(points, dtype=np.float64)
    return array.reshape(1, 3) if array.ndim == 1 else array


def resample(points: np.ndarray, spacing: float) -> np.ndarray:
    """Points along a polyline at most ``spacing`` apart.

    A feature is judged along its length, not at its endpoints: an edge whose
    ends are exact and whose middle has been smoothed away is precisely the
    defect this measures, and endpoint-only sampling would miss it.
    """
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 2:
        return points
    spacing = max(float(spacing), 1e-12)
    out = [points[0]]
    for start, end in zip(points[:-1], points[1:]):
        length = float(np.linalg.norm(end - start))
        steps = max(int(np.ceil(length / spacing)), 1)
        for step in range(1, steps + 1):
            out.append(start + (end - start) * (step / steps))
    return np.asarray(out)


def polyline_length(points: np.ndarray) -> float:
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 2:
        return 0.0
    return float(np.linalg.norm(points[1:] - points[:-1], axis=1).sum())


def tangents(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 2:
        return np.empty((0, 3))
    deltas = points[1:] - points[:-1]
    lengths = np.linalg.norm(deltas, axis=1, keepdims=True)
    return np.divide(deltas, lengths, out=np.zeros_like(deltas),
                     where=lengths > 0)


@dataclass(frozen=True)
class FeatureResult:
    """How well one declared feature survived into the mesh."""

    feature_uuid: str
    origin: str
    critical: bool
    owner_patch_uuids: tuple[str, ...] = ()
    #: Reference-to-mesh: how far the mesh is from the feature that should
    #: exist. This is the direction that sees a rounded-away edge.
    max_distance: float = 0.0
    mean_distance: float = 0.0
    #: Fraction of the feature's length within tolerance.
    length_coverage: float = 0.0
    #: Surviving length over reference length (§6.3).
    length_ratio: float = 0.0
    #: Worst endpoint/corner displacement — corners round first.
    endpoint_distance: float = 0.0
    max_tangent_deg: float = 0.0
    reference_length: float = 0.0
    samples: int = 0
    verdict: str = 'unrated'
    reason: str = ''

    def to_dict(self) -> dict:
        return {
            'feature_uuid': self.feature_uuid, 'origin': self.origin,
            'critical': self.critical,
            'owner_patch_uuids': list(self.owner_patch_uuids),
            'max_distance': self.max_distance,
            'mean_distance': self.mean_distance,
            'length_coverage': self.length_coverage,
            'length_ratio': self.length_ratio,
            'endpoint_distance': self.endpoint_distance,
            'max_tangent_deg': self.max_tangent_deg,
            'reference_length': self.reference_length,
            'samples': self.samples, 'verdict': self.verdict,
            'reason': self.reason,
        }


def measure_feature(feature, subject, *, tolerance: float,
                    spacing: float | None = None,
                    covering_radius: float = 0.0,
                    reference_uncertainty: float = 0.0,
                    min_length_coverage: float | None = None) -> FeatureResult:
    """Compare one declared feature against the surface the mesher produced.

    ``subject`` is a ``(vertices, triangles)`` pair — the mesh section that
    should carry this feature. Distance is measured from the feature to the
    mesh, because a feature the mesh no longer has is the defect being looked
    for, and only that direction can see it.
    """
    from .sampling import upper_bound, verdict as verdict_of

    points = polyline_points(feature)
    policy = getattr(feature, 'policy', None)
    tolerance = float(
        getattr(policy, 'max_distance_m', None) or tolerance)
    required_coverage = (
        min_length_coverage
        if min_length_coverage is not None
        else getattr(policy, 'min_length_coverage', None))

    common = {
        'feature_uuid': getattr(feature, 'feature_uuid', ''),
        'origin': getattr(feature, 'origin', ''),
        'critical': bool(getattr(feature, 'critical', False)),
        'owner_patch_uuids': tuple(getattr(feature, 'owner_patch_uuids', ())),
    }
    if len(points) == 0:
        return FeatureResult(**common, verdict='unrated',
                             reason='the feature carries no geometry')

    reference_length = polyline_length(points)
    walk = resample(points, spacing or max(tolerance, 1e-9))
    locator = SurfaceLocator(*subject)
    distances, cells = locator.closest(walk)

    inside = distances <= tolerance
    # Length-weighted, not sample-weighted: a densely resampled short segment
    # must not outvote a long one.
    if len(walk) > 1:
        segment = np.linalg.norm(walk[1:] - walk[:-1], axis=1)
        weight = np.concatenate([[segment[0] / 2],
                                 (segment[:-1] + segment[1:]) / 2,
                                 [segment[-1] / 2]])
    else:
        weight = np.ones(len(walk))
    total = float(weight.sum()) or 1.0
    coverage = float(weight[inside].sum()) / total

    endpoints = distances[[0, -1]] if len(distances) > 1 else distances
    reference_tangents = tangents(walk)
    surface_normals = locator.normals_at(cells[:len(reference_tangents)])
    # A tangent lying in the surface has zero component along its normal; the
    # deviation from that is the angle the feature turned by.
    alignment = np.abs(np.einsum('ij,ij->i', reference_tangents,
                                 surface_normals))
    max_tangent = float(np.degrees(np.arcsin(np.clip(alignment, 0, 1))).max()) \
        if alignment.size else 0.0

    observed = float(distances.max())
    result_verdict = verdict_of(
        observed, tolerance, covering_radius=covering_radius,
        reference_uncertainty=reference_uncertainty)
    reason = ''
    if (required_coverage is not None and coverage < float(required_coverage)
            and result_verdict == 'pass'):
        # Coverage can fail while the maximum passes: a feature can be within
        # tolerance almost everywhere and still have a stretch that is not.
        result_verdict = 'fail'
        reason = (f'length coverage {coverage:.3f} is below the required '
                  f'{float(required_coverage):.3f}')
    elif result_verdict != 'pass':
        reason = (f'the feature is {observed:.3g} m from the mesh against a '
                  f'{tolerance:.3g} m tolerance '
                  f'(bound {upper_bound(observed, covering_radius, reference_uncertainty):.3g} m)')

    return FeatureResult(
        **common, max_distance=observed,
        mean_distance=float(np.average(distances, weights=weight)),
        length_coverage=coverage,
        length_ratio=float(weight[inside].sum() / total),
        endpoint_distance=float(np.max(endpoints)),
        max_tangent_deg=max_tangent, reference_length=reference_length,
        samples=int(len(walk)), verdict=result_verdict, reason=reason)


def measure_features(features, subject, *, tolerance: float, **kwargs
                     ) -> tuple[FeatureResult, ...]:
    return tuple(measure_feature(item, subject, tolerance=tolerance, **kwargs)
                 for item in features)


def section_verdict(surface_verdict: str, results) -> tuple[str, str]:
    """Combine a section's surface verdict with its features (§6.3).

    A critical feature below threshold fails its section **even when the
    area-weighted surface score is high** — which is the entire reason features
    are measured separately.
    """
    order = {'pass': 0, 'warning': 1, 'incomplete': 2, 'fail': 3, 'unrated': 2}
    worst, reason = surface_verdict, ''
    for item in results:
        if not item.critical:
            continue
        if order.get(item.verdict, 0) > order.get(worst, 0):
            worst = item.verdict
            reason = (f'critical feature {item.feature_uuid} '
                      f'{item.verdict}: {item.reason}')
    return worst, reason


# --------------------------------------------------------------------------- #
# §6.4 topology
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TopologyResult:
    """Counts a nearest-distance statistic cannot see."""

    components: int = 0
    boundary_loops: int = 0
    euler_characteristic: int = 0
    triangles: int = 0
    matches_reference: bool = True
    differences: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {'components': self.components,
                'boundary_loops': self.boundary_loops,
                'euler_characteristic': self.euler_characteristic,
                'triangles': self.triangles,
                'matches_reference': self.matches_reference,
                'differences': list(self.differences)}


def topology_of(vertices, triangles) -> TopologyResult:
    """Connected components, boundary loops and Euler characteristic.

    These catch what distance understates: a sealed hole moves no point far, a
    merged component moves nothing at all, and a lost fin can hide inside a
    per-section average. All three change a count.
    """
    triangles = np.asarray(triangles, dtype=np.int64)
    if triangles.size == 0:
        return TopologyResult()

    used = np.unique(triangles)
    lookup = {int(value): index for index, value in enumerate(used)}
    parent = list(range(len(used)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    edges: dict[tuple[int, int], int] = {}
    for tri in triangles:
        nodes = [lookup[int(value)] for value in tri]
        union(nodes[0], nodes[1])
        union(nodes[1], nodes[2])
        for first, second in ((0, 1), (1, 2), (2, 0)):
            key = tuple(sorted((nodes[first], nodes[second])))
            edges[key] = edges.get(key, 0) + 1

    components = len({find(index) for index in range(len(used))})
    boundary = [key for key, count in edges.items() if count == 1]
    loops = _count_loops(boundary)
    euler = len(used) - len(edges) + len(triangles)
    return TopologyResult(components, loops, euler, int(len(triangles)))


def _count_loops(boundary_edges) -> int:
    """Closed rims formed by the edges with a single incident face."""
    if not boundary_edges:
        return 0
    adjacency: dict[int, list[int]] = {}
    for left, right in boundary_edges:
        adjacency.setdefault(left, []).append(right)
        adjacency.setdefault(right, []).append(left)
    seen: set[int] = set()
    loops = 0
    for node in adjacency:
        if node in seen:
            continue
        loops += 1
        stack = [node]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, ()))
    return loops


def compare_topology(subject, reference) -> TopologyResult:
    """Subject topology, with every count that disagrees named."""
    here = topology_of(*subject)
    there = topology_of(*reference)
    differences = []
    for label, mine, theirs in (
            ('components', here.components, there.components),
            ('boundary_loops', here.boundary_loops, there.boundary_loops),
            ('euler_characteristic', here.euler_characteristic,
             there.euler_characteristic)):
        if mine != theirs:
            differences.append(f'{label}: mesh {mine}, reference {theirs}')
    return TopologyResult(
        here.components, here.boundary_loops, here.euler_characteristic,
        here.triangles, not differences, tuple(differences))
