"""Curves and volumes of the prepared geometry, for the Gmsh proposals.

Plan 41 GN25f, section 4.8.4. Two rows of the Gmsh table need geometry the
field alone does not give: Curves ``segments`` (``N = ceil(L / h_p50)`` per
curve) and Volumes ``target size`` (the median of the field inside the volume).
Both are read from what Preparation published, the same revision the size
field was computed on: the group manifest (faces grouped into patches and the
regions they bound), the prepared source records (which name the source
format) and the staged tessellation.

A source that is an STL carries no curves: a triangulation has edges, not the
CAD curves Gmsh would put a node count on, so a Curves proposal for it would be
invented. Such a case gets a note instead and no Curves proposal. Volumes are
always known (a region is the space inside its boundary patches).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path

import numpy as np

from .staging import _safe, read_stl_solids

#: Source formats whose prepared surface was cut from CAD curves.
CAD_FORMATS = frozenset({'step', 'stp', 'iges', 'igs', 'brep', 'brp'})
#: Sample points tried per volume, inside the volume's bounding box.
VOLUME_SAMPLES = 20000
#: A corner turns the curve by more than this many degrees.
CORNER_DEGREES = 30.0


@dataclass
class Structure:
    """What the Gmsh proposals need to know about curves and volumes."""
    #: ``{patch_uuid: (length of the median-length curve, its sample points)}``
    curves: dict = field(default_factory=dict)
    #: ``{region_uuid: points inside the volume}``
    volumes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def _revision(case_path):
    root = Path(case_path) / 'foammesh' / 'geometry' / 'prepared'
    current = json.loads((root / 'current.json').read_text('utf-8'))
    revision = root / current['revision_id']
    manifest = json.loads(
        (revision / 'prepared-geometry.json').read_text('utf-8'))
    groups = json.loads((revision / 'group-manifest.json').read_text('utf-8'))
    return manifest, groups


def _group_names(group) -> list:
    """The names a staged surface may carry for this group."""
    names = [group.get('display_name'), group.get('solver_name')]
    for ref in group.get('source_refs') or ():
        names += [ref.get('original_name'), ref.get('xde_name')]
    return [_safe(name) for name in names if name]


def _group_triangles(group, staged, cache) -> np.ndarray | None:
    wanted = set(_group_names(group))
    parts = []
    for item in staged:
        if item.name in wanted:
            if item.name not in cache:
                cache[item.name] = np.concatenate(
                    [t for _, t in read_stl_solids(item.path)])
            parts.append(cache[item.name])
    return np.concatenate(parts) if parts else None


# -- curves ------------------------------------------------------------------ #

def _vertex_ids(triangles: np.ndarray, tolerance: float):
    flat = np.round(triangles.reshape(-1, 3) / tolerance).astype(np.int64)
    _, inverse = np.unique(flat, axis=0, return_inverse=True)
    return inverse.reshape(-1, 3)


def boundary_curves(triangles: np.ndarray, corner_degrees=CORNER_DEGREES
                    ) -> list:
    """``[(length, edge midpoints)]``: the open edges of a surface, as curves.

    An edge used by one triangle of the surface is on its boundary. Boundary
    edges chain into curves; a curve ends where it meets a third edge or
    turns by more than ``corner_degrees``. A boundary with no corner (a
    circle) is one curve.
    """
    flat = triangles.reshape(-1, 3)
    span = float(np.ptp(flat, axis=0).max()) or 1.0
    ids = _vertex_ids(triangles, span * 1e-7)
    positions = {}
    for tri, corners in zip(ids, triangles):
        for vertex, point in zip(tri, corners):
            positions.setdefault(int(vertex), point)
    counts = {}
    for a, b, c in ids:
        for u, v in ((a, b), (b, c), (c, a)):
            key = (int(min(u, v)), int(max(u, v)))
            counts[key] = counts.get(key, 0) + 1
    edges = [key for key, n in counts.items() if n == 1]
    around = {}
    for u, v in edges:
        around.setdefault(u, []).append(v)
        around.setdefault(v, []).append(u)
    threshold = math.cos(math.radians(corner_degrees))

    def straight(before, at, after) -> bool:
        first = positions[at] - positions[before]
        second = positions[after] - positions[at]
        norm = np.linalg.norm(first) * np.linalg.norm(second)
        return norm > 0 and float(first @ second) / norm >= threshold

    def is_corner(vertex, before) -> bool:
        ends = around[vertex]
        if len(ends) != 2:
            return True
        after = ends[0] if ends[1] == before else ends[1]
        return not straight(before, vertex, after)

    used, curves = set(), []

    def walk(start, first):
        chain, before, at = [start], start, first
        while True:
            edge = (min(before, at), max(before, at))
            if edge in used:
                break
            used.add(edge)
            chain.append(at)
            if at == start or is_corner(at, before):
                break
            ends = around[at]
            before, at = at, ends[0] if ends[1] == before else ends[1]
        return chain

    # curves that start at a corner or a branch first, then plain loops
    starts = [v for v, ends in around.items()
              if len(ends) != 2 or any(
                  not straight(a, v, b) for a in ends for b in ends if a != b)]
    for vertex in starts + list(around):
        for neighbour in around[vertex]:
            if (min(vertex, neighbour), max(vertex, neighbour)) in used:
                continue
            chain = walk(vertex, neighbour)
            points = np.array([positions[i] for i in chain])
            if len(points) < 2:
                continue
            steps = np.diff(points, axis=0)
            length = float(np.linalg.norm(steps, axis=1).sum())
            if length > 0:
                curves.append((length, (points[:-1] + points[1:]) / 2.0))
    return curves


def representative_curve(curves) -> tuple | None:
    """The median-length curve of a group: the one count the row carries."""
    if not curves:
        return None
    ordered = sorted(curves, key=lambda item: item[0])
    return ordered[len(ordered) // 2]


# -- volumes ----------------------------------------------------------------- #

def inside_points(triangles: np.ndarray, count=VOLUME_SAMPLES, seed=7
                  ) -> np.ndarray:
    """Points inside the closed shell *triangles* (empty when none found)."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints

    flat = triangles.reshape(-1, 3)
    low, high = flat.min(axis=0), flat.max(axis=0)
    samples = np.random.default_rng(seed).uniform(low, high, (count, 3))
    shell = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(flat, dtype=float)))
    shell.SetPoints(points)
    cells = vtkCellArray()
    size = len(triangles)
    connectivity = np.arange(3 * size, dtype=np.int64)
    offsets = np.arange(0, 3 * size + 1, 3, dtype=np.int64)
    cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                  numpy_to_vtkIdTypeArray(connectivity, deep=True))
    shell.SetPolys(cells)
    cloud = vtkPolyData()
    cloud_points = vtkPoints()
    cloud_points.SetData(numpy_to_vtk(np.ascontiguousarray(samples)))
    cloud.SetPoints(cloud_points)
    selector = vtkSelectEnclosedPoints()
    selector.SetInputData(cloud)
    selector.SetSurfaceData(shell)
    selector.SetTolerance(1e-9)
    selector.Update()
    mask = np.array([selector.IsInside(i) for i in range(count)], dtype=bool)
    return samples[mask]


# -- reading a case ---------------------------------------------------------- #

def read_structure(case_path, staged, corner_degrees=CORNER_DEGREES
                   ) -> Structure:
    """The curves and volumes of the prepared geometry of *case_path*."""
    out = Structure()
    try:
        manifest, groups = _revision(case_path)
    except (OSError, KeyError, ValueError):
        out.notes.append('There is no prepared geometry to read curves or '
                         'volumes from.')
        return out
    formats = {record.get('geometry_id'): str(
        record.get('source_format') or '').lower().lstrip('.')
        for record in manifest.get('sources', ())}
    cache = {}
    by_uuid = {g.get('patch_uuid'): g for g in groups.get('groups', ())}
    triangles = {uuid: _group_triangles(g, staged, cache)
                 for uuid, g in by_uuid.items()}
    cad = [uuid for uuid, g in by_uuid.items()
           if formats.get(g.get('geometry_id')) in CAD_FORMATS
           and triangles[uuid] is not None]
    if not cad:
        out.notes.append(
            'The prepared geometry is a triangulated surface (STL): it '
            'carries no curves, so Curves segments and Structure counts get '
            'no proposal.')
    for uuid in cad:
        chosen = representative_curve(
            boundary_curves(triangles[uuid], corner_degrees))
        if chosen is not None:
            out.curves[uuid] = chosen
    for region in groups.get('regions', ()):
        if not region.get('included', True):
            continue
        parts = [triangles.get(u) for u in region.get('boundary_patch_uuids')
                 or ()]
        if not parts or any(part is None for part in parts):
            out.notes.append(
                'Volume "{0}": its boundary patches are not all among the '
                'staged surfaces, so it gets no target size proposal.'.format(
                    region.get('display_name') or region.get('region_uuid')))
            continue
        found = inside_points(np.concatenate(parts))
        if len(found):
            out.volumes[region['region_uuid']] = found
        else:
            out.notes.append(
                'Volume "{0}": no point inside its shell was found, so it '
                'gets no target size proposal.'.format(
                    region.get('display_name') or region.get('region_uuid')))
    return out
