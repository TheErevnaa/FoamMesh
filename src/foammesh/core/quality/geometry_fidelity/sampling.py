"""Deterministic adaptive surface sampling with a proven covering radius.

Plan 23 §6.1 and §6.1.1. Everything the fidelity gate claims rests on this
module, because the gate does not compare the *observed* maximum deviation
against a tolerance -- a sampler that misses the one place a feature is wrong
reports a small maximum, and a threshold applied to it is a false green.

It gates on an upper bound instead:

    H_upper = H_observed + r + epsilon_reference

where ``r`` is the **covering radius**: the guaranteed maximum distance from any
point of the sampled surface to its nearest sample. That guarantee is what makes
the bound true rather than reassuring, so ``r`` is computed here, not estimated.

The argument is short and worth stating, because it is the reason the whole
design works. Distance to a closed set is 1-Lipschitz, so for any surface point
``x`` and its nearest sample ``s``, ``|d(x) - d(s)| <= |x - s| <= r``. Therefore
the true maximum over the surface cannot exceed the sampled maximum by more
than ``r``.

To make ``r`` a fact rather than a hope, the sampler subdivides each triangle
until every sub-triangle's circumradius is at or below the target, and samples
every sub-triangle vertex. Any surface point lies in some sub-triangle, and its
distance to that triangle's nearest vertex is at most that triangle's
circumradius -- so the largest achieved circumradius *is* the covering radius.

Sampling vertices and face centres alone, as the existing repair-deviation
measurement does, has no such property: a coarse chord can have zero error at
every node and large error in between.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

#: Below this the target is numerically meaningless (§16.3). Absolute floor,
#: paired with a relative one derived from the component being measured.
ABSOLUTE_FLOOR = 1e-12
#: Relative floor as a fraction of the component's characteristic length.
RELATIVE_FLOOR = 1e-9

#: Refusing to subdivide for ever. A triangle needing more than this many levels
#: is either degenerate or the target is below the floor; both are reported
#: rather than pursued.
MAX_SUBDIVISION_LEVELS = 12

#: Hard ceiling on sub-triangles, independent of the level cap.
#:
#: Each level quadruples the count, so the level cap alone permits 4**12 --
#: sixteen million sub-triangles per input triangle, tens of millions of points,
#: and a check that never returns. The level cap bounds *depth*; this bounds
#: *work*, and work is what actually runs out. Hitting it truncates, which can
#: only weaken a verdict.
MAX_SUBDIVIDED_TRIANGLES = 1 << 18


class SamplingError(ValueError):
    pass


@dataclass(frozen=True)
class SampleSet:
    """Sample points on a surface, and what they are guaranteed to cover."""

    points: np.ndarray            # (n, 3)
    #: Which input triangle each sample came from, so a hotspot maps back.
    source_triangles: np.ndarray  # (n,)
    #: The proven covering radius: no surface point is further than this from
    #: its nearest sample.
    covering_radius: float
    #: What was asked for. ``covering_radius <= target`` unless truncated.
    target_radius: float
    #: True when subdivision stopped before reaching the target, so the bound is
    #: weaker than requested and the verdict must be `incomplete`, not `pass`.
    truncated: bool = False
    levels: int = 0

    @property
    def count(self) -> int:
        return int(len(self.points))

    def to_dict(self) -> dict:
        return {
            'method': 'adaptive_midpoint_subdivision',
            'sample_count': self.count,
            'covering_radius': self.covering_radius,
            'target_radius': self.target_radius,
            'truncated': self.truncated,
            'levels': self.levels,
        }


def floor_for(characteristic_length: float) -> float:
    """The smallest target worth asking for on a body of this size (§16.3)."""
    return max(ABSOLUTE_FLOOR,
               RELATIVE_FLOOR * abs(float(characteristic_length or 0.0)))


def circumradius(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Circumradius of each triangle, vectorised.

    A degenerate triangle has no finite circumcircle; it is reported as zero
    rather than as infinity, because a sliver contributes no area and covering
    it is not what limits the bound.
    """
    ab = np.linalg.norm(b - a, axis=-1)
    bc = np.linalg.norm(c - b, axis=-1)
    ca = np.linalg.norm(a - c, axis=-1)
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=-1)
    numerator = ab * bc * ca
    return np.divide(numerator, 4.0 * area,
                     out=np.zeros_like(numerator), where=area > 0)


def sample_triangles(vertices: np.ndarray, triangles: np.ndarray, *,
                     target_radius: float,
                     characteristic_length: float | None = None,
                     max_samples: int | None = None) -> SampleSet:
    """Sample a triangulated surface so the covering radius is at most the target.

    Deterministic: triangles are processed in input order, each subdivision is
    the same midpoint split every time, and duplicate points are removed by a
    stable first-seen rule. Re-reading the same mesh therefore yields
    byte-identical samples, which is what lets a report be cached and a
    regression fixture mean anything.
    """
    vertices = np.ascontiguousarray(vertices, dtype=np.float64)
    triangles = np.ascontiguousarray(triangles, dtype=np.int64)
    if triangles.ndim != 2 or triangles.shape[1] != 3:
        raise SamplingError('triangles must be an (n, 3) index array')

    target = float(target_radius)
    if not np.isfinite(target) or target <= 0:
        raise SamplingError('target radius must be positive and finite')
    if characteristic_length is not None:
        target = max(target, floor_for(characteristic_length))

    if triangles.size == 0:
        return SampleSet(np.empty((0, 3)), np.empty(0, dtype=np.int64),
                         0.0, target)

    corners = vertices[triangles]                      # (m, 3, 3)
    current = corners
    origin = np.arange(len(triangles), dtype=np.int64)
    levels = 0
    truncated = False

    while True:
        radii = circumradius(current[:, 0], current[:, 1], current[:, 2])
        achieved = float(radii.max()) if radii.size else 0.0
        if achieved <= target or levels >= MAX_SUBDIVISION_LEVELS:
            truncated = achieved > target
            break
        if len(current) * 4 > MAX_SUBDIVIDED_TRIANGLES:
            truncated = True
            break
        if max_samples is not None and len(current) * 4 * 3 > max_samples:
            # The budget is spent. Stopping here is safe because a larger
            # covering radius only ever weakens the bound -- it can move a
            # verdict toward warning or incomplete, never toward pass.
            truncated = True
            break
        current, origin = _subdivide(current, origin)
        levels += 1

    radii = circumradius(current[:, 0], current[:, 1], current[:, 2])
    covering = float(radii.max()) if radii.size else 0.0

    flat = current.reshape(-1, 3)
    sources = np.repeat(origin, 3)
    points, keep = _unique_stable(flat)
    return SampleSet(points, sources[keep], covering, target, truncated, levels)


def _subdivide(corners: np.ndarray, origin: np.ndarray):
    """One midpoint split: each triangle becomes four similar ones.

    Similar, so the subdivision is stable -- a sliver stays a sliver rather than
    degenerating further, and the circumradius halves predictably each level.
    """
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
    ab, bc, ca = 0.5 * (a + b), 0.5 * (b + c), 0.5 * (c + a)
    children = np.concatenate([
        np.stack([a, ab, ca], axis=1),
        np.stack([ab, b, bc], axis=1),
        np.stack([ca, bc, c], axis=1),
        np.stack([ab, bc, ca], axis=1),
    ])
    return children, np.tile(origin, 4)


def _unique_stable(points: np.ndarray):
    """Deduplicate while keeping first-seen order.

    ``np.unique`` sorts, which would make the sample order depend on
    coordinates rather than on traversal -- and two meshes that differ only by a
    rigid transform would then sample in a different order.
    """
    _values, first = np.unique(points, axis=0, return_index=True)
    keep = np.sort(first)
    return points[keep], keep


def upper_bound(observed: float, covering_radius: float,
                reference_uncertainty: float = 0.0) -> float:
    """``H_upper`` -- the number qualification is allowed to compare (§6.1.1)."""
    return (float(observed) + max(float(covering_radius), 0.0)
            + max(float(reference_uncertainty), 0.0))


def verdict(observed: float, tolerance: float, *, covering_radius: float,
            reference_uncertainty: float = 0.0,
            truncated: bool = False) -> str:
    """Gate on the bound, never on the observed maximum.

    The middle case is the one that matters: an observed value inside tolerance
    whose bound is not. That is the case that previously produced a silent
    green, and it is actionable -- refining ``r`` and re-running resolves it.
    """
    tolerance = float(tolerance)
    if observed > tolerance:
        return 'fail'
    if truncated:
        return 'incomplete'
    if upper_bound(observed, covering_radius, reference_uncertainty) <= tolerance:
        return 'pass'
    return 'warning'


def required_radius(observed: float, tolerance: float,
                    reference_uncertainty: float = 0.0) -> float:
    """The covering radius a `pass` would have needed.

    Reported alongside a `warning` so refinement is a decision the user can
    make rather than a mystery. Non-positive means no achievable radius would
    have passed -- the observed value and the reference uncertainty already
    exhaust the tolerance.
    """
    return float(tolerance) - float(observed) - float(reference_uncertainty)
