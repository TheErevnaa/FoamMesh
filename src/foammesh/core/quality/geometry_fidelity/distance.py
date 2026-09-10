"""Bidirectional surface distance, section-scoped.

Plan 23 §6.1 and §6.2. Two properties matter more than the arithmetic.

**It is bidirectional, and the directions answer different questions.**
Mesh-to-reference finds boundary that moved — unsnapped or displaced. Only
reference-to-mesh finds geometry that is *absent*: a fin the mesher dropped
contributes nothing to a mesh-to-reference sweep, because every mesh point it
would have explained is still close to some other part of the reference. A
one-directional check cannot see a missing feature at all.

**The locator is section-scoped, never global.** §4 forbids measuring against
the union of all surfaces: a face assigned to one patch but sitting on a nearby
one would score well against the union and be wrong. Each section is measured
against its own reference, and the nearest *other* section is reported
separately so a leak is visible rather than absorbed.

Statistics are area-weighted where a weighting is meaningful. An unweighted mean
over samples would let a densely subdivided sliver count as heavily as the
panel beside it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: Points per locator query batch. Chunked because a single call over a
#: multi-million-point sweep holds every intermediate at once, and the budget
#: needs somewhere to check in between.
CHUNK = 20_000


class DistanceError(ValueError):
    pass


def as_polydata(vertices: np.ndarray, triangles: np.ndarray):
    """A vtkPolyData for the locator, built without copying through Python."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    polydata = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(
        np.asarray(vertices, dtype=np.float64)), deep=True))
    polydata.SetPoints(points)

    cells = vtkCellArray()
    triangles = np.asarray(triangles, dtype=np.int64)
    if triangles.size:
        flat = np.hstack([
            np.full((len(triangles), 1), 3, dtype=np.int64), triangles]).ravel()
        cells.SetCells(len(triangles), numpy_to_vtkIdTypeArray(
            np.ascontiguousarray(flat), deep=True))
    polydata.SetPolys(cells)
    return polydata


class SurfaceLocator:
    """Closest-point queries against one section's surface."""

    def __init__(self, vertices: np.ndarray, triangles: np.ndarray):
        from vtkmodules.vtkCommonDataModel import vtkStaticCellLocator

        if len(triangles) == 0:
            raise DistanceError('a locator needs at least one triangle')
        self.polydata = as_polydata(vertices, triangles)
        self.cell_count = int(len(triangles))
        self._locator = vtkStaticCellLocator()
        self._locator.SetDataSet(self.polydata)
        self._locator.BuildLocator()
        self._normals = _triangle_normals(vertices, triangles)

    def closest(self, points: np.ndarray, *, budget=None):
        """Return ``(distance, cell_id)`` for each query point.

        ``budget`` is polled between chunks rather than per point: a check-in
        per query would dominate the query itself.
        """
        from vtkmodules.vtkCommonCore import reference as vtk_reference
        from vtkmodules.vtkCommonDataModel import vtkGenericCell

        points = np.ascontiguousarray(points, dtype=np.float64)
        distances = np.empty(len(points), dtype=np.float64)
        cells = np.full(len(points), -1, dtype=np.int64)
        cell = vtkGenericCell()
        closest = [0.0, 0.0, 0.0]
        # VTK's out-parameters are reference wrappers, not numpy scalars.
        cell_id, sub_id, squared = (vtk_reference(0), vtk_reference(0),
                                    vtk_reference(0.0))

        for start in range(0, len(points), CHUNK):
            stop = min(start + CHUNK, len(points))
            for index in range(start, stop):
                self._locator.FindClosestPoint(
                    points[index].tolist(), closest, cell, cell_id, sub_id,
                    squared)
                distances[index] = float(np.sqrt(max(float(squared), 0.0)))
                cells[index] = int(cell_id)
            if budget is not None:
                budget.check_in(progress=f'{stop}/{len(points)} samples')
        return distances, cells

    def normals_at(self, cell_ids: np.ndarray) -> np.ndarray:
        valid = np.clip(cell_ids, 0, self.cell_count - 1)
        return self._normals[valid]


def _triangle_normals(vertices, triangles) -> np.ndarray:
    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    if len(triangles) == 0:
        return np.zeros((0, 3))
    a, b, c = (vertices[triangles[:, 0]], vertices[triangles[:, 1]],
               vertices[triangles[:, 2]])
    normals = np.cross(b - a, c - a)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    return np.divide(normals, lengths, out=np.zeros_like(normals),
                     where=lengths > 0)


@dataclass(frozen=True)
class Distribution:
    """One direction's distance statistics."""

    count: int
    mean: float
    rms: float
    p95: float
    p99: float
    p999: float
    maximum: float
    coverage: float               # fraction within tolerance
    tolerance: float = 0.0

    @classmethod
    def of(cls, distances: np.ndarray, *, tolerance: float,
           weights: np.ndarray | None = None) -> 'Distribution':
        distances = np.asarray(distances, dtype=np.float64)
        if distances.size == 0:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, float(tolerance))
        if weights is None:
            weights = np.ones_like(distances)
        weights = np.asarray(weights, dtype=np.float64)
        total = float(weights.sum()) or 1.0
        inside = float(weights[distances <= tolerance].sum()) / total
        return cls(
            count=int(distances.size),
            mean=float(np.average(distances, weights=weights)),
            rms=float(np.sqrt(np.average(distances ** 2, weights=weights))),
            p95=float(np.percentile(distances, 95)),
            p99=float(np.percentile(distances, 99)),
            p999=float(np.percentile(distances, 99.9)),
            maximum=float(distances.max()),
            coverage=inside, tolerance=float(tolerance))

    def to_dict(self) -> dict:
        return {
            'count': self.count, 'mean': self.mean, 'rms': self.rms,
            'p95': self.p95, 'p99': self.p99, 'p999': self.p999,
            'max': self.maximum, 'coverage': self.coverage,
            'tolerance': self.tolerance,
        }


@dataclass(frozen=True)
class NormalError:
    """Angular agreement between subject and reference surface normals."""

    count: int
    mean_deg: float
    p95_deg: float
    max_deg: float
    #: Samples whose normals oppose. Reported separately because an unsigned
    #: angle hides a reversed patch inside a large-but-plausible number.
    reversed_fraction: float = 0.0

    @classmethod
    def of(cls, subject: np.ndarray, reference: np.ndarray) -> 'NormalError':
        if len(subject) == 0:
            return cls(0, 0.0, 0.0, 0.0)
        cosines = np.clip(np.einsum('ij,ij->i', subject, reference), -1.0, 1.0)
        angles = np.degrees(np.arccos(np.abs(cosines)))
        return cls(
            count=int(len(angles)), mean_deg=float(angles.mean()),
            p95_deg=float(np.percentile(angles, 95)),
            max_deg=float(angles.max()),
            reversed_fraction=float((cosines < 0).mean()))

    def to_dict(self) -> dict:
        return {'count': self.count, 'mean_deg': self.mean_deg,
                'p95_deg': self.p95_deg, 'max_deg': self.max_deg,
                'reversed_fraction': self.reversed_fraction}


@dataclass(frozen=True)
class DirectionalResult:
    """One direction of a bidirectional comparison."""

    direction: str                # mesh_to_reference | reference_to_mesh
    distribution: Distribution
    covering_radius: float
    truncated: bool = False
    #: Per-sample distances, kept for the hotspot field.
    distances: np.ndarray = field(default_factory=lambda: np.empty(0))

    def to_dict(self) -> dict:
        return {'direction': self.direction, 'covering_radius': self.covering_radius,
                'truncated': self.truncated, **self.distribution.to_dict()}


def measure(subject, reference, *, tolerance: float,
            target_radius: float | None = None, budget=None,
            reference_uncertainty: float = 0.0) -> dict:
    """Compare two surfaces in both directions.

    ``subject`` and ``reference`` are ``(vertices, triangles)`` pairs. The
    target covering radius defaults to §16.3's tenth of the tolerance.
    """
    from .sampling import sample_triangles, upper_bound, verdict

    target = float(target_radius if target_radius is not None
                   else 0.10 * tolerance)
    directions = []
    for name, (source, against) in (
            ('mesh_to_reference', (subject, reference)),
            ('reference_to_mesh', (reference, subject))):
        vertices, triangles = source
        samples = sample_triangles(vertices, triangles, target_radius=target)
        locator = SurfaceLocator(*against)
        distances, cells = locator.closest(samples.points, budget=budget)
        directions.append((
            DirectionalResult(
                name,
                Distribution.of(distances, tolerance=tolerance),
                samples.covering_radius, samples.truncated, distances),
            samples, locator, cells))

    forward, forward_samples, forward_locator, forward_cells = directions[0]
    subject_normals = _triangle_normals(*subject)
    sample_normals = subject_normals[
        np.clip(forward_samples.source_triangles, 0,
                max(len(subject_normals) - 1, 0))] if len(subject_normals) \
        else np.zeros((len(forward_samples.points), 3))
    normals = NormalError.of(
        sample_normals, forward_locator.normals_at(forward_cells))

    observed = max(item[0].distribution.maximum for item in directions)
    radius = max(item[0].covering_radius for item in directions)
    truncated = any(item[0].truncated for item in directions)
    return {
        'method': 'sampled_hausdorff',
        'directions': [item[0].to_dict() for item in directions],
        'normals': normals.to_dict(),
        'observed_max': observed,
        'covering_radius': radius,
        'reference_uncertainty': float(reference_uncertainty),
        'upper_bound': upper_bound(observed, radius, reference_uncertainty),
        'tolerance': float(tolerance),
        'verdict': verdict(observed, tolerance, covering_radius=radius,
                           reference_uncertainty=reference_uncertainty,
                           truncated=truncated),
        'truncated': truncated,
        '_results': [item[0] for item in directions],
    }
