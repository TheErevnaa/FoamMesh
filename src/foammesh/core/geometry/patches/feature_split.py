#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Split a tessellated surface into sub-surfaces by feature angle.

Plan 28 WP7. An STL carries no faces: a slicer writes one ``solid`` and every
triangle of a box, a duct or a manifold arrives as one surface, which both
engines then treat as one boundary. A meshing tool has to be able to say
"this side is the inlet", and the only information a triangulation offers
for that is where its surface bends.

Two triangles belong to the same sub-surface when they share a *manifold*
edge -- one with exactly two incident triangles -- and their normals differ
by less than the angle. Boundary edges (one triangle) and non-manifold edges
(three or more) never join, because a hole rim or a fin is a feature by
construction. The result is one region per connected patch of smooth
surface, numbered largest first so the numbering is the same on every run.

This is the same threshold the fidelity checker uses for creases
(``DEFAULT_FEATURE_ANGLE_DEG``): the edges snapping is watched for rounding
away are the edges this splits along.

Headless numpy over the VTK connectivity; no Qt, no OCCT. The polydata comes
back with a ``cadFaceId`` cell array, which is the seam every consumer of a
CAD import already reads (the named-solid STL writer, the prepared groups,
the wrap transfer), so a split STL is indistinguishable downstream from a
tessellated CAD body.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..cad.surface_split import FACE_ID_ARRAY
from ..features.manifest import DEFAULT_FEATURE_ANGLE_DEG


class FeatureSplitError(ValueError):
    """The surface cannot be split as asked."""


@dataclass(frozen=True)
class SplitRegion:
    """One sub-surface the pass produced."""

    face_id: int
    cells: int
    area: float
    area_fraction: float

    def to_dict(self) -> dict:
        return {'face_id': int(self.face_id), 'cells': int(self.cells),
                'area': float(self.area),
                'area_fraction': float(self.area_fraction)}


@dataclass
class FeatureSplit:
    """What the pass found, and the tagged surface."""

    polydata: object
    regions: list[SplitRegion] = field(default_factory=list)
    angle_deg: float = DEFAULT_FEATURE_ANGLE_DEG
    min_area_fraction: float = 0.0
    #: Regions below the area fraction that were folded into a neighbour.
    absorbed: int = 0
    #: Edges shared by three or more triangles; these never join.
    non_manifold_edges: int = 0

    @property
    def count(self) -> int:
        return len(self.regions)

    def summary(self) -> dict:
        return {
            'count': self.count, 'angle_deg': float(self.angle_deg),
            'min_area_fraction': float(self.min_area_fraction),
            'absorbed': int(self.absorbed),
            'non_manifold_edges': int(self.non_manifold_edges),
            'regions': [region.to_dict() for region in self.regions],
        }


def _triangles(polydata):
    """Points and the (N, 3) triangle connectivity, as numpy."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    if polydata.GetNumberOfCells() == 0:
        raise FeatureSplitError('the surface has no cells to split')
    if polydata.GetNumberOfPolys() != polydata.GetNumberOfCells():
        raise FeatureSplitError(
            'the surface holds cells that are not polygons; '
            'only a triangulated surface can be split by angle')
    polys = polydata.GetPolys()
    offsets = vtk_to_numpy(polys.GetOffsetsArray())
    connectivity = vtk_to_numpy(polys.GetConnectivityArray())
    if offsets.size < 2 or np.any(np.diff(offsets) != 3):
        raise FeatureSplitError(
            'the surface is not purely triangular; triangulate it first')
    triangles = connectivity.reshape(-1, 3).astype(np.int64)
    points = vtk_to_numpy(polydata.GetPoints().GetData()).astype(np.float64)
    return points, triangles


def _unit_normals(points, triangles):
    """Per-triangle unit normals and areas; a degenerate triangle gets a
    zero normal, which the join treats as smooth so it is absorbed rather
    than becoming a region of its own."""
    a = points[triangles[:, 0]]
    b = points[triangles[:, 1]]
    c = points[triangles[:, 2]]
    cross = np.cross(b - a, c - a)
    doubled = np.linalg.norm(cross, axis=1)
    area = 0.5 * doubled
    normals = np.zeros_like(cross)
    live = doubled > 0
    normals[live] = cross[live] / doubled[live][:, None]
    return normals, area


def _edge_pairs(triangles):
    """Triangle pairs across manifold edges, and the non-manifold edge count.

    Edges are keyed by their sorted point ids, so a shared edge is found by
    equality alone; the STL reader has already merged coincident points.
    """
    count = triangles.shape[0]
    edges = np.concatenate([
        triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
    owner = np.tile(np.arange(count, dtype=np.int64), 3)
    edges.sort(axis=1)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    edges, owner = edges[order], owner[order]
    change = np.ones(edges.shape[0], dtype=bool)
    change[1:] = np.any(edges[1:] != edges[:-1], axis=1)
    starts = np.flatnonzero(change)
    lengths = np.diff(np.append(starts, edges.shape[0]))
    manifold = starts[lengths == 2]
    pairs = np.stack([owner[manifold], owner[manifold + 1]], axis=1)
    return pairs, int(np.count_nonzero(lengths > 2))


def _components(count, pairs):
    """Connected components over ``pairs``: label propagation with pointer
    jumping, which converges in a handful of rounds on any mesh and needs
    nothing beyond numpy."""
    labels = np.arange(count, dtype=np.int64)
    if pairs.shape[0] == 0:
        return labels
    a, b = pairs[:, 0], pairs[:, 1]
    while True:
        la, lb = labels[a], labels[b]
        low = np.minimum(la, lb)
        high = np.maximum(la, lb)
        proposal = labels.copy()
        np.minimum.at(proposal, high, low)
        while True:
            jumped = proposal[proposal]
            if np.array_equal(jumped, proposal):
                break
            proposal = jumped
        if np.array_equal(proposal, labels):
            return labels
        labels = proposal


def _renumber(labels, area):
    """Dense region ids, largest area first, ties by first triangle."""
    unique, inverse = np.unique(labels, return_inverse=True)
    totals = np.zeros(unique.size, dtype=np.float64)
    np.add.at(totals, inverse, area)
    first = np.full(unique.size, labels.size, dtype=np.int64)
    np.minimum.at(first, inverse, np.arange(labels.size, dtype=np.int64))
    order = np.lexsort((first, -totals))
    rank = np.empty(unique.size, dtype=np.int64)
    rank[order] = np.arange(unique.size, dtype=np.int64)
    return rank[inverse], totals[order]


def _absorb_small(region_of, area, pairs, min_fraction):
    """Fold every region under ``min_fraction`` of the total area into its
    largest neighbour, smallest first, until none is left or one has no
    neighbour to go to. Returns the new labels and how many were absorbed."""
    total = float(area.sum())
    if min_fraction <= 0 or total <= 0 or pairs.shape[0] == 0:
        return region_of, 0
    count = int(region_of.max()) + 1
    totals = np.zeros(count, dtype=np.float64)
    np.add.at(totals, region_of, area)
    neighbours: list[set[int]] = [set() for _ in range(count)]
    left, right = region_of[pairs[:, 0]], region_of[pairs[:, 1]]
    across = left != right
    for one, other in zip(left[across].tolist(), right[across].tolist()):
        neighbours[one].add(other)
        neighbours[other].add(one)
    alias = list(range(count))

    def resolve(index):
        while alias[index] != index:
            alias[index] = alias[alias[index]]
            index = alias[index]
        return index

    absorbed = 0
    threshold = min_fraction * total
    pending = sorted(range(count), key=lambda index: totals[index])
    for small in pending:
        if resolve(small) != small or totals[small] >= threshold:
            continue
        candidates = {resolve(item) for item in neighbours[small]} - {small}
        if not candidates:
            continue
        target = max(candidates, key=lambda index: (totals[index], -index))
        alias[small] = target
        totals[target] += totals[small]
        totals[small] = 0.0
        neighbours[target] |= neighbours[small]
        neighbours[target].discard(target)
        neighbours[target].discard(small)
        absorbed += 1
    if not absorbed:
        return region_of, 0
    lookup = np.array([resolve(index) for index in range(count)], dtype=np.int64)
    return lookup[region_of], absorbed


def split_by_feature_angle(polydata, angle_deg: float = DEFAULT_FEATURE_ANGLE_DEG,
                           min_area_fraction: float = 0.0,
                           groups=None) -> FeatureSplit:
    """Tag ``polydata`` with one ``cadFaceId`` per smooth sub-surface.

    ``angle_deg`` is the largest turn between neighbouring triangle normals
    that still counts as the same surface; ``min_area_fraction`` (0 to 1)
    folds regions smaller than that share of the total area into their
    largest neighbour. The input is not modified: the returned polydata is a
    shallow copy carrying the new array.

    ``groups`` is an optional per-cell id -- in practice the solid each
    triangle was read from. Triangles in different groups never join, however
    smoothly the surface runs between them.

    R107. Without that constraint the pass merged faces the file itself had
    kept apart. MEASURED on `venturi.stl` at 45 degrees: `rev1.stl` held
    `inlet` 48, `outlet` 48, `wall_converging` 192, `wall_diverging` 288
    facets and the dialog previewed FOUR segments; the applied split wrote
    `rev2.stl` with `face0` 480, `face1` 48, `face2` 48 -- the two wall halves
    fused across the smooth throat, because this pass works on merged points
    while the preview appended the solids without merging them. Honouring the
    source solids makes the two agree, and it is what the user asked for: a
    file that already says where its boundaries are is not guessing.
    """
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    try:
        angle = float(angle_deg)
    except (TypeError, ValueError) as error:
        raise FeatureSplitError('the feature angle must be a number') from error
    if not 0.0 < angle < 180.0:
        raise FeatureSplitError(
            'the feature angle must be between 0 and 180 degrees')
    try:
        min_fraction = float(min_area_fraction or 0.0)
    except (TypeError, ValueError) as error:
        raise FeatureSplitError(
            'the minimum area fraction must be a number') from error
    if not 0.0 <= min_fraction < 1.0:
        raise FeatureSplitError(
            'the minimum area fraction must be at least 0 and below 1')

    points, triangles = _triangles(polydata)
    normals, area = _unit_normals(points, triangles)
    pairs, non_manifold = _edge_pairs(triangles)
    if groups is not None and pairs.shape[0]:
        labels_in = np.asarray(groups).reshape(-1)
        if labels_in.size != triangles.shape[0]:
            raise FeatureSplitError(
                'the group array must carry one id per triangle')
        # A pair straddling two solids is not adjacency the split may use --
        # not for joining, and not for absorbing a small region either.
        pairs = pairs[labels_in[pairs[:, 0]] == labels_in[pairs[:, 1]]]
    if pairs.shape[0]:
        cosine = np.einsum('ij,ij->i', normals[pairs[:, 0]], normals[pairs[:, 1]])
        degenerate = (np.linalg.norm(normals[pairs[:, 0]], axis=1) == 0) | (
            np.linalg.norm(normals[pairs[:, 1]], axis=1) == 0)
        smooth = (cosine >= np.cos(np.radians(angle))) | degenerate
        joined = pairs[smooth]
    else:
        joined = pairs
    labels = _components(triangles.shape[0], joined)
    region_of, _ = _renumber(labels, area)
    region_of, absorbed = _absorb_small(region_of, area, pairs, min_fraction)
    if absorbed:
        region_of, _ = _renumber(region_of, area)
    totals = np.zeros(int(region_of.max()) + 1, dtype=np.float64)
    np.add.at(totals, region_of, area)
    counts = np.bincount(region_of, minlength=totals.size)
    grand = float(totals.sum())

    output = vtkPolyData()
    output.ShallowCopy(polydata)
    ids = numpy_to_vtk(region_of.astype(np.int32), deep=1)
    ids.SetName(FACE_ID_ARRAY)
    cell_data = output.GetCellData()
    cell_data.RemoveArray(FACE_ID_ARRAY)
    cell_data.AddArray(ids)
    regions = [
        SplitRegion(face_id=index, cells=int(counts[index]),
                    area=float(totals[index]),
                    area_fraction=(float(totals[index]) / grand if grand else 0.0))
        for index in range(totals.size)]
    return FeatureSplit(polydata=output, regions=regions, angle_deg=angle,
                        min_area_fraction=min_fraction, absorbed=absorbed,
                        non_manifold_edges=non_manifold)
