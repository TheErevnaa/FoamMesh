"""Are there enough cells where it matters?

Plan 23 §6.5 and §6.6. Kept separate from surface fidelity throughout: a fin
can be geometrically perfect and have one tetrahedron across the channel beside
it, and a dense high-quality mesh can still have a blade edge rounded away.
Neither verdict substitutes for the other.

§6.6 is the part that has to be stated precisely, because "cells across a
1.5 mm channel" has four plausible meanings that disagree by a factor of two.
**The measurement is the traversal, not the ratio.** ``gap / h`` reports 3.0 for
a channel a badly aligned mesh spans with two cells; only walking the cells a
segment actually enters answers the question that was asked. The ratio is
reported alongside so a divergence between them is visible rather than
averaged away, but only the traversal gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: A pair that cannot be measured is not a pair that passes (§6.6 rule 6).
NOT_APPLICABLE = 'not_applicable'
UNRELIABLE = 'unreliable'
MEASURED = 'measured'


class ResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class SizeComparison:
    """Requested against achieved element size (§6.5)."""

    requested: float
    achieved_mean: float
    achieved_min: float
    achieved_max: float
    count: int

    @property
    def ratio(self) -> float:
        """Achieved over requested. Above one means coarser than asked for."""
        return (self.achieved_mean / self.requested) if self.requested else 0.0

    def to_dict(self) -> dict:
        return {'requested': self.requested,
                'achieved_mean': self.achieved_mean,
                'achieved_min': self.achieved_min,
                'achieved_max': self.achieved_max,
                'count': self.count, 'ratio': self.ratio}


def edge_lengths(vertices, triangles) -> np.ndarray:
    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    if triangles.size == 0:
        return np.empty(0)
    lengths = []
    for first, second in ((0, 1), (1, 2), (2, 0)):
        lengths.append(np.linalg.norm(
            vertices[triangles[:, second]] - vertices[triangles[:, first]],
            axis=1))
    return np.concatenate(lengths)


def compare_size(vertices, triangles, *, requested: float) -> SizeComparison:
    """What was asked for against what the boundary actually has."""
    lengths = edge_lengths(vertices, triangles)
    if lengths.size == 0:
        return SizeComparison(float(requested), 0.0, 0.0, 0.0, 0)
    return SizeComparison(
        float(requested), float(lengths.mean()), float(lengths.min()),
        float(lengths.max()), int(lengths.size))


def elements_along(feature_points, vertices, triangles) -> int:
    """How many boundary elements span a feature curve (§6.5).

    Reported per feature rather than averaged, because the point of the metric
    is that one under-resolved edge is not excused by a well-resolved model.
    """
    from ..geometry_fidelity.features import polyline_length

    length = polyline_length(np.asarray(feature_points, dtype=np.float64))
    lengths = edge_lengths(vertices, triangles)
    if length <= 0 or lengths.size == 0:
        return 0
    return int(np.floor(length / float(lengths.mean())))


# --------------------------------------------------------------------------- #
# §6.6: cells across
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChannelResult:
    """Cells across one opposing-surface pair."""

    label: str
    kind: str                      # solid_thickness | fluid_channel | unknown
    status: str                    # measured | unreliable | not_applicable
    minimum: int = 0
    p05: float = 0.0
    median: float = 0.0
    probes: int = 0
    probe_spacing: float = 0.0
    gap_mean: float = 0.0
    #: ``gap / h_requested``. Reported beside the traversal, never instead.
    requested_ratio: float = 0.0
    required: int | None = None
    verdict: str = 'unrated'
    reason: str = ''

    def to_dict(self) -> dict:
        return {
            'label': self.label, 'kind': self.kind, 'status': self.status,
            'minimum': self.minimum, 'p05': self.p05, 'median': self.median,
            'probes': self.probes, 'probe_spacing': self.probe_spacing,
            'gap_mean': self.gap_mean,
            'requested_ratio': self.requested_ratio,
            'required': self.required, 'verdict': self.verdict,
            'reason': self.reason,
        }


def traverse(origins, directions, gaps, cell_locator, *,
             label: str = '', kind: str = 'unknown',
             required: int | None = None, requested_size: float = 0.0,
             probe_spacing: float = 0.0) -> ChannelResult:
    """Count the distinct cells each probe segment actually enters.

    ``cell_locator`` answers "which cell contains this point", so the count is
    what the mesh really provides rather than an arithmetic estimate. Sampling
    along each segment is finer than the local cell size, so a cell cannot be
    stepped over.
    """
    origins = np.asarray(origins, dtype=np.float64)
    directions = np.asarray(directions, dtype=np.float64)
    gaps = np.asarray(gaps, dtype=np.float64)
    if len(origins) == 0:
        return ChannelResult(label, kind, NOT_APPLICABLE, required=required,
                             reason='no probe segments could be generated')

    counts = []
    for origin, direction, gap in zip(origins, directions, gaps):
        if not np.isfinite(gap) or gap <= 0:
            continue
        # Sample the segment's *interior*, at sub-interval centres. Its two
        # endpoints lie exactly on the bounding faces, and a point on a face
        # belongs to the cells on both sides of it -- so including them counts
        # each wall cell twice over and reports one cell more than the channel
        # has. That error runs in the dangerous direction: an under-resolved
        # channel would read as adequate.
        steps = 64
        seen = []
        for step in range(steps):
            point = origin + direction * (gap * (step + 0.5) / steps)
            cell = cell_locator(point)
            if cell is None or cell < 0:
                continue
            if not seen or seen[-1] != cell:
                seen.append(cell)
        counts.append(len({int(value) for value in seen}))

    if not counts:
        return ChannelResult(label, kind, UNRELIABLE, required=required,
                             reason='no probe segment produced a cell traversal')

    counts = np.asarray(counts, dtype=np.int64)
    gap_mean = float(np.mean(gaps[np.isfinite(gaps) & (gaps > 0)]))
    ratio = (gap_mean / requested_size) if requested_size else 0.0
    result = ChannelResult(
        label, kind, MEASURED, int(counts.min()),
        float(np.percentile(counts, 5)), float(np.median(counts)),
        int(len(counts)), float(probe_spacing), gap_mean, ratio, required)

    if required is None:
        # §16.2: no universal default. Measured, reported, not gated.
        return _with(result, 'unrated',
                     'no min_cells_across policy applies, so the channel is '
                     'measured but not gated')
    if result.minimum < int(required):
        return _with(result, 'fail',
                     f'{result.minimum} cells across against a required '
                     f'{int(required)}')
    return _with(result, 'pass', '')


def _with(result: ChannelResult, verdict: str, reason: str) -> ChannelResult:
    from dataclasses import replace

    return replace(result, verdict=verdict, reason=reason)


def pair_surfaces(reference_points, reference_normals, locator, *,
                  pairing_angle_deg: float = 15.0,
                  max_gap: float | None = None):
    """Find opposing surfaces by casting along the inward normal (§6.6 rule 1).

    A pair is only accepted when the far surface genuinely faces back: normals
    within ``pairing_angle_deg`` of opposing. Anything else is ambiguous, and
    §6.6 requires ambiguity to be reported rather than resolved by guessing.
    """
    origins, directions, gaps, rejected = [], [], [], 0
    threshold = np.cos(np.radians(180.0 - float(pairing_angle_deg)))
    points = np.asarray(reference_points, dtype=np.float64)
    normals = np.asarray(reference_normals, dtype=np.float64)

    for point, normal in zip(points, normals):
        inward = -normal
        distance, cell = locator(point, inward)
        if distance is None or not np.isfinite(distance) or distance <= 0:
            rejected += 1
            continue
        if max_gap is not None and distance > max_gap:
            rejected += 1
            continue
        far_normal = cell
        if far_normal is not None:
            alignment = float(np.dot(normal, np.asarray(far_normal)))
            if alignment > threshold:
                rejected += 1
                continue
        origins.append(point)
        directions.append(inward)
        gaps.append(float(distance))
    return (np.asarray(origins), np.asarray(directions), np.asarray(gaps),
            rejected)


@dataclass(frozen=True)
class ResolutionReport:
    """One section's resolution adequacy."""

    section: str
    size: SizeComparison | None = None
    channels: tuple[ChannelResult, ...] = field(default_factory=tuple)
    feature_elements: dict = field(default_factory=dict)
    verdict: str = 'unrated'
    reason: str = ''

    def to_dict(self) -> dict:
        return {
            'section': self.section,
            'size': self.size.to_dict() if self.size else None,
            'channels': [item.to_dict() for item in self.channels],
            'feature_elements': dict(self.feature_elements),
            'verdict': self.verdict, 'reason': self.reason,
        }


def combine(section: str, *, size=None, channels=(), feature_elements=None
            ) -> ResolutionReport:
    """Roll a section's measurements into one verdict, worst-first."""
    order = {'pass': 0, 'unrated': 1, 'warning': 2, 'incomplete': 3, 'fail': 4}
    verdict, reason = 'pass' if channels else 'unrated', ''
    if not channels:
        reason = 'no measurable opposing-surface pair in this section'
    for item in channels:
        if order.get(item.verdict, 0) > order.get(verdict, 0):
            verdict, reason = item.verdict, f'{item.label}: {item.reason}'
    return ResolutionReport(section, size, tuple(channels),
                            dict(feature_elements or {}), verdict, reason)
