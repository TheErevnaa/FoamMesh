"""Deterministic, preview-safe tessellated surface repair catalogue."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkFiltersCore import (
    vtkCleanPolyData, vtkFeatureEdges, vtkPolyDataNormals, vtkStripper, vtkTriangleFilter)
from vtkmodules.vtkFiltersModeling import vtkFillHolesFilter


@dataclass(frozen=True)
class TessellatedAction:
    operation: Callable
    repairs_kinds: tuple[str, ...]
    defaults: dict
    destructiveness: str
    #: Plan 26 WP7.4. Which risk band this action belongs to, so the page can
    #: group "safe to apply blindly" apart from "moves geometry".
    #:
    #: A band is **not** a rename of ``destructiveness``. That tag carries only
    #: three values across the whole catalogue -- ``low``, ``medium`` and
    #: ``detection_only`` -- so bands 2 and 3 would both map to ``medium``, and
    #: band 4 has no tag at all because CAD healing is not in this catalogue.
    #: The two are related and neither derives the other.
    band: int = 1


def _copy(polydata):
    result = vtkPolyData()
    result.DeepCopy(polydata)
    return result


def _diag(polydata) -> float:
    if not polydata.GetNumberOfPoints():
        return 0.0
    bounds = polydata.GetBounds()
    return math.sqrt(sum((bounds[2 * i + 1] - bounds[2 * i]) ** 2 for i in range(3)))


def _area(polydata, cell_id: int) -> float:
    cell = polydata.GetCell(cell_id)
    if cell.GetNumberOfPoints() != 3:
        return 0.0
    a, b, c = (polydata.GetPoint(cell.GetPointId(i)) for i in range(3))
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    return .5 * math.sqrt(sum(value * value for value in cross))


def _subset(polydata, keep_ids) -> vtkPolyData:
    """Copy selected cells and every point/cell-data array without renumbering points."""
    keep = [int(value) for value in keep_ids]
    cells = vtkCellArray()
    output = vtkPolyData()
    output.SetPoints(polydata.GetPoints())
    output.GetPointData().ShallowCopy(polydata.GetPointData())
    output.GetCellData().CopyAllocate(polydata.GetCellData(), len(keep))
    for new_id, old_id in enumerate(keep):
        cell = polydata.GetCell(old_id)
        cells.InsertNextCell(cell.GetPointIds())
        output.GetCellData().CopyData(polydata.GetCellData(), old_id, new_id)
    output.SetPolys(cells)
    output.Squeeze()
    # Remove orphaned points without welding: retained component bounds and
    # point diagnostics must not include vertices from dropped cells.
    compact = vtkCleanPolyData()
    compact.SetInputData(output)
    compact.PointMergingOff()
    compact.Update()
    return _copy(compact.GetOutput())


def _polydata_from_triangles(polydata, points, triangles, source_cell_ids,
                             source_point_ids=None):
    """Build a triangle surface while preserving point/cell array schemas."""
    from vtkmodules.vtkCommonCore import vtkPoints
    output, vtk_points, cells = vtkPolyData(), vtkPoints(), vtkCellArray()
    vtk_points.SetNumberOfPoints(len(points))
    for point_id, point in enumerate(points):
        vtk_points.SetPoint(point_id, point)
    output.SetPoints(vtk_points)
    output.GetPointData().CopyAllocate(polydata.GetPointData(), len(points))
    source_point_ids = (source_point_ids if source_point_ids is not None else
                        list(range(polydata.GetNumberOfPoints())))
    for point_id, source_id in enumerate(source_point_ids):
        output.GetPointData().CopyData(polydata.GetPointData(), source_id, point_id)
    output.GetCellData().CopyAllocate(polydata.GetCellData(), len(triangles))
    for new_id, (triangle, old_id) in enumerate(zip(triangles, source_cell_ids)):
        cells.InsertNextCell(3, triangle)
        output.GetCellData().CopyData(polydata.GetCellData(), old_id, new_id)
    output.SetPolys(cells)
    return output


def _boundary_loops(polydata):
    edges = vtkFeatureEdges()
    edges.SetInputData(polydata)
    edges.BoundaryEdgesOn()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.NonManifoldEdgesOff()
    edges.Update()
    stripper = vtkStripper()
    stripper.SetInputData(edges.GetOutput())
    stripper.JoinContiguousSegmentsOn()
    stripper.Update()
    loops = stripper.GetOutput()
    result = []
    for cell_id in range(loops.GetNumberOfCells()):
        cell = loops.GetCell(cell_id)
        points = [loops.GetPoint(cell.GetPointId(i)) for i in range(cell.GetNumberOfPoints())]
        if not points:
            continue
        perimeter = sum(math.dist(points[i], points[(i + 1) % len(points)])
                        for i in range(len(points)))
        size = math.sqrt(sum((max(p[a] for p in points) - min(p[a] for p in points)) ** 2
                             for a in range(3)))
        result.append({'loop': cell_id, 'perimeter': perimeter, 'size': size})
    return result


def clean(polydata, tolerance: float | None = None):
    """Weld using an explicit absolute tolerance and report topology collapse."""
    tolerance = max(float(tolerance if tolerance is not None else _diag(polydata) * 1e-6),
                    1e-15)
    f = vtkCleanPolyData()
    f.SetInputData(polydata)
    f.PointMergingOn()
    f.ToleranceIsAbsoluteOn()
    f.SetAbsoluteTolerance(tolerance)
    f.Update()
    out = _copy(f.GetOutput())
    merged = max(0, polydata.GetNumberOfPoints() - out.GetNumberOfPoints())
    collapsed = max(0, polydata.GetNumberOfCells() - out.GetNumberOfCells())
    return out, {'tolerance': tolerance, 'points_merged': merged,
                 'points_removed': merged, 'cells_collapsed': collapsed}


def dedupe(polydata, degenerate_area_frac: float = 1e-16):
    """Remove exact duplicate triangles and triangles below the scale-aware area floor."""
    area_limit = float(degenerate_area_frac) * max(_diag(polydata) ** 2, 1e-30)
    seen, keep = set(), []
    duplicates = degenerate = 0
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        ids = tuple(cell.GetPointId(i) for i in range(cell.GetNumberOfPoints()))
        key = tuple(sorted(ids))
        if key in seen:
            duplicates += 1
        elif len(ids) != 3 or _area(polydata, cell_id) <= area_limit:
            degenerate += 1
        else:
            seen.add(key)
            keep.append(cell_id)
    return _subset(polydata, keep), {
        'duplicates_removed': duplicates, 'degenerate_removed': degenerate,
        'area_threshold': area_limit, 'cell_ids': [i for i in range(
            polydata.GetNumberOfCells()) if i not in set(keep)][:50_000]}


def _carry_face_ids(source, output) -> None:
    """Put *source*'s ``cadFaceId`` back on the cells *output* kept.

    DP-486. vtkFillHolesFilter passes the points through and drops every
    cell array, so a filled multi-solid STL came back with no record of which
    solid each triangle belonged to and was written as one solid under
    vtkSTLWriter's own header -- every ``regions`` entry the dictionary
    generated from the same records then failed with "Unknown region name".
    The kept cells are matched by their point ids, which the filter does not
    renumber; a cell with no match is one the fill created (-1).
    """
    from vtkmodules.vtkCommonCore import vtkIntArray

    source_ids = source.GetCellData().GetArray('cadFaceId')
    if source_ids is None or output.GetCellData().GetArray('cadFaceId') is not None:
        return
    owner = {}
    for cell_id in range(source.GetNumberOfCells()):
        cell = source.GetCell(cell_id)
        key = tuple(sorted(cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())))
        owner.setdefault(key, int(source_ids.GetTuple1(cell_id)))
    carried = vtkIntArray()
    carried.SetName('cadFaceId')
    carried.SetNumberOfTuples(output.GetNumberOfCells())
    for cell_id in range(output.GetNumberOfCells()):
        cell = output.GetCell(cell_id)
        key = tuple(sorted(cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())))
        carried.SetValue(cell_id, owner.get(key, -1))
    output.GetCellData().AddArray(carried)


def fill_holes(polydata, hole_size: float = 1e6, smooth_fill: bool = True):
    """Fill bounded boundary loops and report every loop's threshold outcome."""
    if hole_size <= 0:
        raise ValueError('maximum hole size must be positive')
    loops = _boundary_loops(polydata)
    f = vtkFillHolesFilter()
    f.SetInputData(polydata)
    f.SetHoleSize(float(hole_size))
    f.Update()
    tri = vtkTriangleFilter()
    tri.SetInputData(f.GetOutput())
    tri.Update()
    out = _copy(tri.GetOutput())
    new_cells = max(0, out.GetNumberOfCells() - polydata.GetNumberOfCells())
    _carry_face_ids(polydata, out)
    face_ids = out.GetCellData().GetArray('cadFaceId')
    if face_ids is not None:
        for cell_id in range(polydata.GetNumberOfCells(), out.GetNumberOfCells()):
            face_ids.SetTuple1(cell_id, -1)
    smoothing = {'status': 'disabled', 'points_moved': 0, 'max_move': 0.0}
    if smooth_fill and new_cells:
        old_points = {polydata.GetCell(cell_id).GetPointId(index)
                      for cell_id in range(polydata.GetNumberOfCells())
                      for index in range(polydata.GetCell(cell_id).GetNumberOfPoints())}
        fill_points = {out.GetCell(cell_id).GetPointId(index)
                       for cell_id in range(polydata.GetNumberOfCells(), out.GetNumberOfCells())
                       for index in range(out.GetCell(cell_id).GetNumberOfPoints())}
        movable = fill_points - old_points
        if movable:
            from vtkmodules.vtkFiltersCore import vtkWindowedSincPolyDataFilter
            smoother = vtkWindowedSincPolyDataFilter()
            smoother.SetInputData(out)
            smoother.SetNumberOfIterations(10)
            smoother.SetPassBand(.1)
            smoother.BoundarySmoothingOff()
            smoother.FeatureEdgeSmoothingOff()
            smoother.NormalizeCoordinatesOn()
            smoother.Update()
            candidate = smoother.GetOutput()
            guard = max(_diag(polydata) * 1e-4, 1e-15)
            moves = [math.dist(out.GetPoint(index), candidate.GetPoint(index))
                     for index in movable]
            if max(moves, default=0.0) <= guard:
                for index in movable:
                    out.GetPoints().SetPoint(index, candidate.GetPoint(index))
                out.GetPoints().Modified()
                smoothing = {'status': 'applied', 'points_moved': len(movable),
                             'max_move': max(moves, default=0.0),
                             'deviation_guard': guard}
            else:
                smoothing = {'status': 'skipped_deviation_guard',
                             'points_moved': 0, 'max_move': max(moves),
                             'deviation_guard': guard}
        else:
            smoothing = {'status': 'no_new_interior_points',
                         'points_moved': 0, 'max_move': 0.0}
    # Hole creation can close an formerly ambiguous open shell.  Recompute a
    # consistent outward winding as part of the bounded fill action.
    orientation = {'status': 'not_required'}
    if new_cells:
        out, oriented = consistent_normals(out)
        orientation = {'status': 'recomputed', **oriented}
    outcomes = [{**loop, 'filled': loop['size'] <= hole_size,
                 'reason': 'within threshold' if loop['size'] <= hole_size
                 else 'larger than max_hole_size'} for loop in loops]
    return out, {'holes': outcomes, 'new_cells': new_cells,
                 'created_patch': 'repair_fill_1' if new_cells else None,
                 'smooth_fill': bool(smooth_fill),
                 'fill_smoothing': smoothing,
                 'post_fill_orientation': orientation,
                 'cells_before': polydata.GetNumberOfCells(),
                 'cells_after': out.GetNumberOfCells()}


def consistent_normals(polydata, *, flip: bool = False):
    """Make winding consistent without feature splitting or NM traversal."""
    from .checks import is_watertight
    watertight = is_watertight(polydata)
    f = vtkPolyDataNormals()
    f.SetInputData(polydata)
    f.ConsistencyOn()
    f.SplittingOff()
    f.NonManifoldTraversalOff()
    f.AutoOrientNormalsOn() if watertight else f.AutoOrientNormalsOff()
    f.FlipNormalsOn() if flip else f.FlipNormalsOff()
    f.Update()
    out = _copy(f.GetOutput())
    changed = 0
    for cell_id in range(min(polydata.GetNumberOfCells(), out.GetNumberOfCells())):
        before = [polydata.GetCell(cell_id).GetPointId(i) for i in range(3)]
        after = [out.GetCell(cell_id).GetPointId(i) for i in range(3)]
        changed += int(before != after)
    return out, {'normals': 'recomputed', 'flipped': bool(flip),
                 'cells_flipped': changed, 'watertight': watertight,
                 'global_orientation': 'outward' if watertight else 'ambiguous_open_surface'}


def _cell_components(polydata):
    point_cells = defaultdict(list)
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        for i in range(cell.GetNumberOfPoints()):
            point_cells[cell.GetPointId(i)].append(cell_id)
    remaining, components = set(range(polydata.GetNumberOfCells())), []
    while remaining:
        root = remaining.pop()
        component, queue = [root], deque([root])
        while queue:
            cell_id = queue.popleft()
            cell = polydata.GetCell(cell_id)
            neighbours = {other for i in range(cell.GetNumberOfPoints())
                          for other in point_cells[cell.GetPointId(i)]}
            for other in neighbours & remaining:
                remaining.remove(other)
                component.append(other)
                queue.append(other)
        components.append(component)
    return components


def drop_fragments(polydata, min_area_frac: float = 1e-6, min_cells: int = 10):
    components = _cell_components(polydata)
    total_area = sum(_area(polydata, i) for i in range(polydata.GetNumberOfCells()))
    kept, dropped, retained = [], [], []
    for region, ids in enumerate(components):
        area = sum(_area(polydata, i) for i in ids)
        record = {'region': region, 'cells': len(ids), 'area': area,
                  'cell_ids': ids[:50_000]}
        if len(ids) < int(min_cells) or area < total_area * float(min_area_frac):
            dropped.append(record)
        else:
            kept.extend(ids)
            retained.append(record)
    return _subset(polydata, sorted(kept)), {
        'regions_dropped': dropped, 'regions_kept': retained}


def fix_nonmanifold(polydata, fin_area_max: float | None = None):
    """Remove bounded fins, then split residual non-manifold sheets in place."""
    edge_faces = defaultdict(list)
    areas = [_area(polydata, i) for i in range(polydata.GetNumberOfCells())]
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        ids = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
        for a, b in zip(ids, ids[1:] + ids[:1]):
            edge_faces[tuple(sorted((a, b)))].append(cell_id)
    bad = {edge: faces for edge, faces in edge_faces.items() if len(faces) > 2}
    positive = sorted(area for area in areas if area > 0)
    median = positive[len(positive) // 2] if positive else 0.0
    limit = float(fin_area_max if fin_area_max is not None else 4 * median)
    remove = set()
    for faces in bad.values():
        candidates = sorted(faces, key=lambda item: areas[item])
        for cell_id in candidates[:max(0, len(faces) - 2)]:
            if areas[cell_id] <= limit:
                remove.add(cell_id)
    keep = [i for i in range(polydata.GetNumberOfCells()) if i not in remove]
    out = _subset(polydata, keep)

    # Duplicate the two edge vertices for every sheet beyond the first two.
    # Geometry is unchanged; the sheets become manifold-but-separate.
    edge_faces_after = defaultdict(list)
    triangles = []
    for cell_id in range(out.GetNumberOfCells()):
        cell = out.GetCell(cell_id)
        triangle = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
        triangles.append(triangle)
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            edge_faces_after[tuple(sorted((a, b)))].append(cell_id)
    points = [out.GetPoint(index) for index in range(out.GetNumberOfPoints())]
    point_sources = list(range(out.GetNumberOfPoints()))
    edges_split = 0
    for edge, faces in sorted(edge_faces_after.items()):
        if len(faces) <= 2:
            continue
        for cell_id in faces[2:]:
            replacements = {}
            for point_id in edge:
                replacements[point_id] = len(points)
                points.append(points[point_id])
                point_sources.append(point_id)
            triangles[cell_id] = [replacements.get(value, value)
                                  for value in triangles[cell_id]]
            edges_split += 1
    if edges_split:
        out = _polydata_from_triangles(
            out, points, triangles, list(range(out.GetNumberOfCells())), point_sources)
    remaining = _nonmanifold_edge_count(out)
    return out, {'nm_edges_before': len(bad), 'nm_edges_after': remaining,
                 'fins_removed': len(remove), 'edges_split': edges_split,
                 'unresolved': remaining, 'cell_ids': sorted(remove)[:50_000]}


def _nonmanifold_edge_count(polydata) -> int:
    incidence = defaultdict(int)
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        ids = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
        for a, b in zip(ids, ids[1:] + ids[:1]):
            incidence[tuple(sorted((a, b)))] += 1
    return sum(value > 2 for value in incidence.values())


def _triangle_normal(points, triangle):
    a, b, c = (points[index] for index in triangle)
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    length = math.sqrt(sum(value * value for value in cross))
    return tuple(value / length for value in cross) if length > 1e-30 else None


def collapse_slivers(polydata, min_edge: float | None = None,
                     max_deviation: float | None = None):
    """Collapse short edges only when normals/topology/deviation remain safe."""
    threshold = float(min_edge if min_edge is not None else max(_diag(polydata) * 4e-6, 1e-15))
    guard = float(max_deviation if max_deviation is not None else threshold)
    if threshold > guard:
        raise ValueError('min_edge exceeds max_deviation')
    points = [list(polydata.GetPoint(index)) for index in range(polydata.GetNumberOfPoints())]
    triangles = [[polydata.GetCell(cell_id).GetPointId(i) for i in range(3)]
                 for cell_id in range(polydata.GetNumberOfCells())]
    incident = defaultdict(set)
    edges = set()
    for cell_id, triangle in enumerate(triangles):
        for point_id in triangle:
            incident[point_id].add(cell_id)
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            edges.add(tuple(sorted((a, b))))
    candidates = sorted((math.dist(points[a], points[b]), a, b) for a, b in edges)
    collapsed, max_move = 0, 0.0
    for length, keep, drop in candidates:
        if length >= threshold or keep == drop:
            continue
        affected = incident[keep] | incident[drop]
        normals = [_triangle_normal(points, triangles[index]) for index in affected]
        normals = [value for value in normals if value is not None]
        if normals and any(sum(a * b for a, b in zip(normals[0], value))
                           < math.cos(math.radians(15)) for value in normals[1:]):
            continue
        midpoint = [(points[keep][axis] + points[drop][axis]) / 2 for axis in range(3)]
        move = max(math.dist(midpoint, points[keep]), math.dist(midpoint, points[drop]))
        trial_points = [*points]
        trial_points[keep] = midpoint
        safe = True
        for cell_id in affected:
            old_triangle = triangles[cell_id]
            new_triangle = [keep if value == drop else value for value in old_triangle]
            if len(set(new_triangle)) < 3:
                continue  # the collapsed sliver is intentionally removed
            old_normal = _triangle_normal(points, old_triangle)
            new_normal = _triangle_normal(trial_points, new_triangle)
            if (new_normal is None or (old_normal is not None and
                    sum(a * b for a, b in zip(old_normal, new_normal)) <= 0)):
                safe = False
                break
        if not safe:
            continue
        points[keep] = midpoint
        for cell_id in affected:
            triangles[cell_id] = [keep if value == drop else value
                                  for value in triangles[cell_id]]
        for cell_id in incident[drop]:
            incident[keep].add(cell_id)
        incident[drop].clear()
        collapsed += 1
        max_move = max(max_move, move)
    kept = [(triangle, cell_id) for cell_id, triangle in enumerate(triangles)
            if len(set(triangle)) == 3 and _triangle_normal(points, triangle) is not None]
    out = _polydata_from_triangles(polydata, points,
                                   [item[0] for item in kept],
                                   [item[1] for item in kept])
    return out, {'edges_collapsed': collapsed,
                 'cells_removed': polydata.GetNumberOfCells() - out.GetNumberOfCells(),
                 'max_move': max_move, 'deviation_guard': guard}


def detect_intersections(polydata, triangle_limit: int = 200_000):
    from .checks import self_intersections
    finding = self_intersections(polydata, triangle_limit=triangle_limit)
    return _copy(polydata), {'evaluated': finding.evaluated, 'pairs': [],
                            'curve_points': [list(p) for p in finding.locations],
                            'count': finding.count}


#: Plan 26 WP7.4. What each band means, in the words the page shows.
#:
#: **Ordering rule.** Band 4 runs before tessellation; bands 1 -> 2 -> 3 in
#: order on the tessellation. Welding before healing re-welds a surface the CAD
#: fix would have made unnecessary.
#:
#: The order matters more than it looks for an STL going to Gmsh: bands 1-2 run
#: *before* ``gmsh.merge`` and ``classifySurfaces``, and that classification is
#: acutely sensitive to input quality -- needles produce spurious normals so it
#: invents patch boundaries that do not exist, and non-manifold edges break
#: patch construction outright.
REPAIR_BANDS = {
    0: ('Detection only', 'Reports what it finds and changes nothing.'),
    1: ('Topology hygiene', 'No geometry change.'),
    2: ('Connectivity', 'Topology changes: faces are added, removed or rejoined.'),
    3: ('Shape quality', 'Geometry moves, within a stated tolerance.'),
    4: ('CAD healing', 'Applies to the CAD solid, before tessellation.'),
}

TESSELLATED_ACTIONS = {
    # ``dropped_facets`` (DP-44) belongs here rather than with the sliver
    # actions: the reader already welded the surface in memory, so what the
    # user needs is that weld written back to the artifact. Applying this
    # rewrites the file from the surface everything was graded against, and
    # the file and the report agree from then on.
    'tess.weld': TessellatedAction(clean,
        ('duplicate_points', 'dropped_facets'), {}, 'low', band=1),
    'tess.dedupe': TessellatedAction(dedupe,
        ('duplicate_triangles', 'degenerate_triangles'), {}, 'low', band=1),
    'tess.orient': TessellatedAction(consistent_normals,
        ('inconsistent_orientation',), {}, 'low', band=1),
    'tess.drop_fragments': TessellatedAction(drop_fragments,
        ('small_fragments',), {'min_area_frac': 1e-6, 'min_cells': 10},
        'medium', band=2),
    'tess.fill_holes': TessellatedAction(fill_holes,
        ('open_edges',), {'max_hole_size': 1e6, 'smooth_fill': True},
        'medium', band=2),
    'tess.fix_nonmanifold': TessellatedAction(fix_nonmanifold,
        ('non_manifold_edges',), {}, 'medium', band=2),
    'tess.collapse_slivers': TessellatedAction(collapse_slivers,
        ('needle_triangles',), {}, 'medium', band=3),
    # Band 0, not band 3: it moves nothing. `detect_intersections` returns an
    # unmodified deep copy plus a report, and shipping it under "geometry moves
    # within tolerance" would tell the user something the code contradicts.
    'tess.detect_intersections': TessellatedAction(detect_intersections,
        ('self_intersections',), {}, 'detection_only', band=0),
}


def execute_action(polydata, action: str, params: dict | None = None):
    try:
        spec = TESSELLATED_ACTIONS[action]
    except KeyError as error:
        raise ValueError(f'unknown tessellated repair action: {action}') from error
    values = dict(spec.defaults)
    values.update(params or {})
    if 'hole_size' in values and 'max_hole_size' not in values:
        values['max_hole_size'] = values.pop('hole_size')
    if action == 'tess.fill_holes':
        values['hole_size'] = values.pop('max_hole_size')
    return spec.operation(polydata, **values)


#: Plan 26 WP7.1. The three operation names ``core/mesh/surface_repair.py``
#: published, mapped onto the catalogue that survives.
#:
#: That module was the second of two implementations of one job: it defined
#: three operations against this module's eight, dispatched to *these same*
#: functions, and had two live production consumers plus a main-window menu
#: flow. Retiring it therefore meant re-pointing public surface, not deleting
#: an orphan -- so the old names keep working and resolve here.
LEGACY_ACTION_ALIASES = {
    'clean_duplicates': 'tess.weld',
    'fill_holes': 'tess.fill_holes',
    'consistent_normals': 'tess.orient',
}


def resolve_action(action: str) -> str:
    """The catalogue id for *action*, accepting the retired spellings."""
    name = str(action)
    resolved = LEGACY_ACTION_ALIASES.get(name, name)
    if resolved not in TESSELLATED_ACTIONS:
        raise ValueError(f'unknown tessellated repair action: {action}')
    return resolved


@dataclass(frozen=True)
class RepairOutcome:
    """One applied action, with the health either side of it."""

    action: str
    polydata: object
    before: object
    after: object
    changes: dict

    @property
    def band(self) -> int:
        return TESSELLATED_ACTIONS[self.action].band

    def to_dict(self) -> dict:
        return {
            'operation': self.action,      # the key the old result used
            'action': self.action,
            'band': self.band,
            'destructiveness': TESSELLATED_ACTIONS[self.action].destructiveness,
            'before': self.before.to_dict(), 'after': self.after.to_dict(),
            'changes': dict(self.changes),
        }


def apply_action(polydata, action: str, params: dict | None = None,
                 **overrides) -> RepairOutcome:
    """Run one catalogue action and report the health either side of it.

    ``overrides`` accepts the retired service's keyword arguments --
    ``hole_size`` and ``flip_normals`` -- so a caller that passed them keeps
    working. They are folded into the action's own parameters rather than
    special-cased per action.
    """
    from .report import assess

    resolved = resolve_action(action)
    values = dict(params or {})
    if 'hole_size' in overrides and overrides['hole_size'] is not None:
        if resolved == 'tess.fill_holes':
            if float(overrides['hole_size']) <= 0:
                raise ValueError('maximum hole size must be positive')
            values['max_hole_size'] = overrides['hole_size']
    if 'flip_normals' in overrides and resolved == 'tess.orient':
        values['flip'] = bool(overrides['flip_normals'])
    before = assess(polydata)
    output, changes = execute_action(polydata, resolved, values)
    return RepairOutcome(resolved, output, before, assess(output), changes)


def write_surface(polydata, destination, *, solid_name: str | None = None) -> Path:
    """Write a repaired surface beside its source, in the source's format.

    DP-362. ``solid_name`` is the name the written block must carry. In an
    ASCII STL the header *is* the solid name, and a repair that leaves it to
    vtkSTLWriter gets `solid Visualization Toolkit generated SLA File` --
    four words OpenFOAM cannot key a ``regions`` entry on and the user never
    chose. DP-64 fixed that in the store's own writer and this one, the
    sibling the repair path uses, kept dropping the name: MEASURED on
    `annulus_shell`, whose repaired revision meshed as `FOAM FATAL ERROR:
    Unknown region name annulus_shell`. A repair changes the triangles and
    nothing about what the surface is called.
    """
    from vtkmodules.vtkIOGeometry import vtkOBJWriter, vtkSTLWriter

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == '.stl':
        writer = vtkSTLWriter()
        if solid_name:
            writer.SetHeader(str(solid_name))
    elif suffix == '.obj':
        writer = vtkOBJWriter()
    else:
        raise ValueError('repaired surface copy must use .stl or .obj')
    writer.SetFileName(str(path))
    writer.SetInputData(polydata)
    if writer.Write() != 1:
        raise OSError(f'could not write repaired surface: {path}')
    return path
