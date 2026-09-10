"""Measure every section's resolution adequacy and build the report.

Plan 23 §6.5 and §6.6, assembled. ``assessment`` holds the individual
measurements -- requested against achieved size, elements along a feature,
cells across an opposing-surface pair -- and this module walks the published
boundary section by section, feeds each one to those measurements and rolls
the answers into the same report container geometry fidelity uses, so the
summary and the waiver read one shape of artifact.

Two locators do the geometric work, and both are built once per case rather
than per section:

* a **ray locator** over the whole boundary answers "how far, along the inward
  normal, is the opposing surface", which is how §6.6 finds a channel;
* a **cell locator** over the volume answers "which cell holds this point",
  which is the traversal §6.6 gates on.

Both are injectable so the measurement can be exercised without VTK and
without a mesh on disk.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from ..geometry_fidelity.features import polyline_points
from ..geometry_fidelity.report import FidelityReport, SectionResult, build
from . import assessment

TASK_ID = 'common.resolution'

#: Probe segments per section. The traversal is the expensive part -- 64 cell
#: lookups per probe -- and a section is characterised by its thinnest
#: channel, not by the number of probes cast into a wide one.
MAX_PROBES_PER_SECTION = 200

#: §6.5 has no hard threshold; a boundary whose mean edge is more than this
#: many times the requested size is flagged, not failed. Failing is the
#: traversal's business.
SIZE_WARNING_RATIO = 2.0


@dataclass(frozen=True)
class RequestedSize:
    """The element size the case asked its engine for.

    ``value`` is ``None`` when it cannot be established; the reason says why,
    and every size comparison is then `unrated` rather than measured against a
    number nobody asked for.
    """

    value: float | None
    source: str = 'unavailable'      # configured | derived | unavailable
    reason: str = ''

    def to_dict(self) -> dict:
        return {'value': self.value, 'source': self.source,
                'reason': self.reason}


# --------------------------------------------------------------------------- #
# Surface geometry
# --------------------------------------------------------------------------- #

def section_surface(mesh, section) -> tuple[np.ndarray, np.ndarray]:
    """One section's boundary as ``(vertices, triangles)``."""
    from foammesh.core.mesh.poly_mesh_boundary import triangulate_faces

    vertices, triangles, _ = triangulate_faces(mesh, section.face_ids)
    return np.asarray(vertices, dtype=np.float64), np.asarray(triangles)


def boundary_surface(mesh) -> tuple[np.ndarray, np.ndarray]:
    """The whole boundary, for the ray locator.

    Every patch, not only the matched sections: the far side of a channel may
    well be a patch nothing declared, and a channel whose far wall is missing
    from the locator reads as open space.
    """
    from foammesh.core.mesh.poly_mesh_boundary import triangulate_faces

    face_ids = np.arange(mesh.internal_face_count, mesh.face_count)
    vertices, triangles, _ = triangulate_faces(mesh, face_ids)
    return np.asarray(vertices, dtype=np.float64), np.asarray(triangles)


def triangle_geometry(vertices, triangles):
    """``(centroids, unit normals, areas)`` per triangle.

    OpenFOAM orders boundary-face vertices so the right-hand normal points out
    of the domain; ``triangulate_faces`` keeps that order, so these normals
    point out of the fluid and their negation is the direction into it.
    """
    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    if triangles.size == 0:
        return (np.empty((0, 3)), np.empty((0, 3)), np.empty(0))
    a, b, c = (vertices[triangles[:, 0]], vertices[triangles[:, 1]],
               vertices[triangles[:, 2]])
    cross = np.cross(b - a, c - a)
    doubled = np.linalg.norm(cross, axis=1)
    areas = doubled / 2.0
    safe = np.where(doubled > 0, doubled, 1.0)
    normals = cross / safe[:, None]
    return (a + b + c) / 3.0, normals, areas


def choose_probes(count: int, limit: int) -> np.ndarray:
    """Evenly spread indices, so a long patch is sampled along its length."""
    if count <= 0:
        return np.empty(0, dtype=np.int64)
    if count <= limit:
        return np.arange(count)
    return np.unique(np.linspace(0, count - 1, int(limit)).astype(np.int64))


# --------------------------------------------------------------------------- #
# Locators
# --------------------------------------------------------------------------- #

class BoundaryRayLocator:
    """``(point, direction) -> (distance to the far surface, its normal)``.

    Built over the whole boundary once. A hit closer than ``skip`` is the
    origin's own triangle and is ignored: the cast starts on a face, and a
    face counts as its own opposing surface at distance zero otherwise.
    """

    def __init__(self, vertices, triangles, *, max_gap: float):
        from vtkmodules.vtkCommonCore import vtkIdList, vtkPoints
        from vtkmodules.vtkFiltersGeneral import vtkOBBTree

        from ..geometry_fidelity.distance import as_polydata

        self.max_gap = float(max_gap)
        self.skip = max(self.max_gap * 1e-6, 1e-12)
        self.polydata = as_polydata(np.asarray(vertices, dtype=np.float64),
                                    np.asarray(triangles))
        self._normals = triangle_geometry(vertices, triangles)[1]
        self._tree = vtkOBBTree()
        self._tree.SetDataSet(self.polydata)
        self._tree.BuildLocator()
        self._points, self._ids = vtkPoints(), vtkIdList()

    def __call__(self, point, direction):
        point = np.asarray(point, dtype=np.float64)
        direction = np.asarray(direction, dtype=np.float64)
        start = point + direction * self.skip
        end = point + direction * self.max_gap
        self._points.Reset()
        self._ids.Reset()
        self._tree.IntersectWithLine(start.tolist(), end.tolist(),
                                     self._points, self._ids)
        for index in range(self._points.GetNumberOfPoints()):
            hit = np.asarray(self._points.GetPoint(index))
            distance = float(np.linalg.norm(hit - point))
            if distance <= self.skip:
                continue
            cell = int(self._ids.GetId(index))
            normal = self._normals[cell] if 0 <= cell < len(self._normals) else None
            return distance, normal
        return None, None


class VolumeCellLocator:
    """``point -> cell id`` over the published volume mesh.

    The grid is assembled from the polyMesh's own owner/neighbour lists as
    polyhedra, so the cell ids it returns are the polyMesh's cell ids and the
    traversal count is over the cells the solver will actually see -- not
    over a re-read of the case that might resolve a different time directory.
    """

    def __init__(self, mesh):
        from vtkmodules.vtkCommonCore import vtkIdList, vtkPoints
        from vtkmodules.vtkCommonDataModel import (
            VTK_POLYHEDRON, vtkStaticCellLocator, vtkUnstructuredGrid,
        )
        from vtkmodules.util.numpy_support import numpy_to_vtk

        points = np.ascontiguousarray(mesh.points, dtype=np.float64)
        vtk_points = vtkPoints()
        vtk_points.SetData(numpy_to_vtk(points, deep=True))
        grid = vtkUnstructuredGrid()
        grid.SetPoints(vtk_points)
        grid.Allocate(int(mesh.cell_count))

        owner = np.asarray(mesh.owner, dtype=np.int64)
        neighbour = np.asarray(mesh.neighbour, dtype=np.int64)
        offsets = np.asarray(mesh.face_offsets, dtype=np.int64)
        face_vertices = np.asarray(mesh.face_vertices, dtype=np.int64)
        cell_of = np.concatenate([owner, neighbour])
        face_of = np.concatenate([np.arange(len(owner)),
                                  np.arange(len(neighbour))])
        order = np.argsort(cell_of, kind='stable')
        counts = np.bincount(cell_of, minlength=int(mesh.cell_count))
        starts = np.concatenate([[0], np.cumsum(counts)])
        sorted_faces = face_of[order]

        # VTK's polyhedron cell is given as a face stream: the face count,
        # then each face as its vertex count followed by its vertex ids.
        stream_ids = vtkIdList()
        for cell in range(int(mesh.cell_count)):
            faces = sorted_faces[starts[cell]:starts[cell + 1]]
            stream: list[int] = [int(len(faces))]
            for face in faces:
                ids = face_vertices[offsets[face]:offsets[face + 1]].tolist()
                stream.append(len(ids))
                stream.extend(ids)
            stream_ids.SetNumberOfIds(len(stream))
            for index, value in enumerate(stream):
                stream_ids.SetId(index, int(value))
            grid.InsertNextCell(VTK_POLYHEDRON, stream_ids)

        self.grid = grid
        self._locator = vtkStaticCellLocator()
        self._locator.SetDataSet(grid)
        self._locator.BuildLocator()

    def __call__(self, point):
        cell = int(self._locator.FindCell(
            np.asarray(point, dtype=np.float64).tolist()))
        return None if cell < 0 else cell


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #

def _required_cells(features) -> int | None:
    values = [int(item.policy.min_cells_across) for item in features
              if getattr(getattr(item, 'policy', None),
                         'min_cells_across', None) is not None]
    return max(values) if values else None


def _feature_elements(features, vertices, triangles) -> dict:
    elements = {}
    for item in features:
        geometry = getattr(item, 'geometry', None) or {}
        if geometry.get('kind') not in (None, 'polyline'):
            continue
        points = polyline_points(item)
        if len(points) < 2:
            continue
        elements[str(item.feature_uuid)] = assessment.elements_along(
            points, vertices, triangles)
    return elements


def _size_verdict(size, verdict: str, reason: str) -> tuple[str, str]:
    """§6.5 flags a boundary far coarser than asked for; it never fails it."""
    if size is None or size.count == 0 or not size.requested:
        return verdict, reason
    if size.ratio > SIZE_WARNING_RATIO and verdict in ('pass', 'unrated'):
        return 'warning', (
            f'mean boundary edge {size.achieved_mean:.4g} is '
            f'{size.ratio:.2f}x the requested {size.requested:.4g}')
    return verdict, reason


def measure_section(section, *, mesh, requested: RequestedSize,
                    ray_locator, cell_locator, features=(),
                    max_gap: float, max_probes: int = MAX_PROBES_PER_SECTION
                    ) -> SectionResult:
    name = str(getattr(section, 'solver_name', '') or '')
    uuid = str(getattr(section, 'patch_uuid', '') or '')
    status = str(getattr(section, 'status', 'matched') or 'matched')
    if status != 'matched' or not uuid:
        return SectionResult(name, uuid, 'unrated',
                             f'section is {status}, not joined to a prepared '
                             'patch, so nothing declares what it must resolve')

    vertices, triangles = section_surface(mesh, section)
    if len(triangles) == 0:
        return SectionResult(name, uuid, 'unrated',
                             'the section has no boundary faces')

    size = (assessment.compare_size(vertices, triangles,
                                    requested=requested.value)
            if requested.value else None)
    features = tuple(features or ())
    required = _required_cells(features)
    feature_elements = _feature_elements(features, vertices, triangles)

    centroids, normals, areas = triangle_geometry(vertices, triangles)
    chosen = choose_probes(len(centroids), max_probes)
    origins, directions, gaps, rejected = assessment.pair_surfaces(
        centroids[chosen], normals[chosen], ray_locator, max_gap=max_gap)
    spacing = float(math.sqrt(areas.mean())) if len(areas) else 0.0
    channel = assessment.traverse(
        origins, directions, gaps, cell_locator, label=name,
        kind='fluid_channel', required=required,
        requested_size=float(requested.value or 0.0), probe_spacing=spacing)
    combined = assessment.combine(name, size=size, channels=(channel,),
                                  feature_elements=feature_elements)
    verdict, reason = _size_verdict(size, combined.verdict, combined.reason)
    if size is None and verdict == 'pass':
        # A channel count without a requested size is still a real
        # measurement; the size comparison is what is missing, and the
        # report says so rather than passing as if it had been made.
        reason = requested.reason or 'no requested size to compare against'
    return SectionResult(
        name, uuid, verdict, reason,
        deviation=size.achieved_mean if size else None,
        tolerance=requested.value, tolerance_source=requested.source,
        ratio=size.ratio if size else None,
        metrics={'resolution': combined.to_dict(),
                 'probes': {'candidates': int(len(centroids)),
                            'cast': int(len(chosen)),
                            'paired': int(len(origins)),
                            'rejected': int(rejected)}})


def measure_case(*, mesh, sections: Sequence, requested: RequestedSize,
                 evidence, policy, task_id: str = TASK_ID,
                 features_for: Callable | None = None,
                 ray_locator=None, cell_locator=None,
                 budget_seconds: float | None = None,
                 max_probes: int = MAX_PROBES_PER_SECTION) -> FidelityReport:
    """Measure every section and build the report.

    ``features_for(section)`` returns the features a section carries, from
    which ``min_cells_across`` gates the traversal; without one the channel
    is measured but unrated (§16.2). Sections left when the budget runs out
    are `incomplete`, named as such, never dropped.
    """
    started = time.monotonic()
    points = np.asarray(mesh.points, dtype=np.float64)
    if len(points):
        span = points.max(axis=0) - points.min(axis=0)
        max_gap = float(np.linalg.norm(span)) or 1.0
    else:
        max_gap = 1.0

    results: list[SectionResult] = []
    measurable = [item for item in sections
                  if str(getattr(item, 'status', 'matched')) == 'matched'
                  and getattr(item, 'patch_uuid', '')]
    if measurable:
        if ray_locator is None:
            vertices, triangles = boundary_surface(mesh)
            ray_locator = BoundaryRayLocator(vertices, triangles,
                                             max_gap=max_gap)
        if cell_locator is None:
            cell_locator = VolumeCellLocator(mesh)

    for section in sections:
        if (budget_seconds is not None
                and time.monotonic() - started > float(budget_seconds)):
            results.append(SectionResult(
                str(getattr(section, 'solver_name', '') or ''),
                str(getattr(section, 'patch_uuid', '') or ''),
                'incomplete',
                f'the {budget_seconds:g} s diagnostic budget was spent '
                'before this section was measured'))
            continue
        features = tuple(features_for(section) or ()) if features_for else ()
        results.append(measure_section(
            section, mesh=mesh, requested=requested,
            ray_locator=ray_locator, cell_locator=cell_locator,
            features=features, max_gap=max_gap, max_probes=max_probes))
    return build(task_id, results, evidence=evidence, policy=policy)
