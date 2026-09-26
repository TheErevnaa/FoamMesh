#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Deterministic VTK surface-readiness checks.

Checks deliberately return bounded highlight samples and never hide an
unavailable/expensive evaluation: ``evaluated=False`` is part of the report.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum

from vtkmodules.vtkCommonDataModel import vtkPolyData
from vtkmodules.vtkFiltersCore import (
    vtkCleanPolyData, vtkFeatureEdges, vtkPolyDataConnectivityFilter, vtkStripper)
from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints

from foammesh.core.quantities import agreeing, count_text


class Severity(str, Enum):
    OK = 'ok'
    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'


@dataclass
class Finding:
    kind: str
    count: int
    severity: Severity
    message: str
    locations: tuple[tuple[float, float, float], ...] = ()
    characteristic_size: float | None = None
    #: DP-223. What that size measures. Four checks publish a length,
    #: the small-fragment check publishes a count of cells, and one
    #: column heading cannot speak for both, so the unit travels with
    #: the number instead of standing over the column.
    characteristic_unit: str | None = None
    repairable_by: tuple[str, ...] = ()
    engine_impact: dict[str, str] | None = None
    evaluated: bool = True
    details: dict | None = None


def _diag(polydata) -> float:
    bounds = polydata.GetBounds()
    return math.sqrt(sum((bounds[i * 2 + 1] - bounds[i * 2]) ** 2 for i in range(3))) \
        if polydata.GetNumberOfPoints() else 0.0


def _feature_output(polydata, *, boundary=False, non_manifold=False):
    feature = vtkFeatureEdges()
    feature.SetInputData(polydata)
    feature.BoundaryEdgesOn() if boundary else feature.BoundaryEdgesOff()
    feature.NonManifoldEdgesOn() if non_manifold else feature.NonManifoldEdgesOff()
    feature.FeatureEdgesOff()
    feature.ManifoldEdgesOff()
    feature.Update()
    return feature.GetOutput()


def _samples(polydata, limit: int = 50) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(float(value) for value in polydata.GetPoint(i))
                 for i in range(min(limit, polydata.GetNumberOfPoints())))


def open_edges(polydata) -> Finding:
    output = _feature_output(polydata, boundary=True)
    count = int(output.GetNumberOfCells())
    stripper = vtkStripper()
    stripper.SetInputData(output)
    stripper.JoinContiguousSegmentsOn()
    stripper.Update()
    loops = stripper.GetOutput()
    loop_metrics = [{'loop_id': i, 'bbox_diagonal': _cell_bbox_diag(loops, i),
                     'perimeter': _cell_perimeter(loops, i)}
                    for i in range(loops.GetNumberOfCells())]
    size = max((item['bbox_diagonal'] for item in loop_metrics), default=None)
    return Finding(
        'open_edges', count, Severity.OK if count == 0 else Severity.ERROR,
        # DP-366. It used to add "(surface boundary closed)" here, which this
        # check has no way of knowing: it counts edges used by exactly one
        # triangle, and an edge used by three is just as open and is counted
        # somewhere else. `surface_not_closed` answers the closure question.
        'No open boundary edges.' if count == 0 else
        f'{count_text(count, "open boundary edge")} in '
        f'{count_text(loops.GetNumberOfCells(), "loop")}.',
        _samples(output), size, 'm', ('tess.fill_holes',),
        # DP-114. Two engines, two consequences, and only one of them was ever
        # written down. snappy carves a background grid and asks the surface
        # which side of it a point is on, so it tolerates free edges and merely
        # risks a leak; Gmsh's OCC route has to fill a volume the surface
        # bounds, and a surface with a hole in it bounds nothing.
        {'snappy': 'Open surfaces can leak during castellation.',
         'gmsh': 'An open surface cannot bound a volume, so Gmsh refuses it.'},
        details={'loops': loop_metrics})


def _cell_bbox_diag(polydata, cell_id: int) -> float:
    cell = polydata.GetCell(cell_id)
    points = [polydata.GetPoint(cell.GetPointId(i)) for i in range(cell.GetNumberOfPoints())]
    if not points:
        return 0.0
    return math.sqrt(sum((max(p[a] for p in points) - min(p[a] for p in points)) ** 2
                         for a in range(3)))


def _cell_perimeter(polydata, cell_id: int) -> float:
    cell = polydata.GetCell(cell_id)
    points = [polydata.GetPoint(cell.GetPointId(i)) for i in range(cell.GetNumberOfPoints())]
    return sum(_distance(points[i], points[i + 1]) for i in range(len(points) - 1))


def _edge_use_census(polydata, cell_ids=None) -> Counter:
    """How many triangle corners use each undirected edge.

    ``cell_ids`` restricts the census to those triangles while keeping the
    parent surface's point numbering, which is the only way to ask the
    question of one region of a merged surface: splitting the region out
    first and appending its faces back together re-numbers the points along
    every seam between two faces, and every one of those shared edges then
    counts as two boundary edges, so a closed region reports as open.
    """
    uses: Counter = Counter()
    ids_to_walk = (range(polydata.GetNumberOfCells()) if cell_ids is None
                   else cell_ids)
    for face_id in ids_to_walk:
        cell = polydata.GetCell(int(face_id))
        ids = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
        for start, end in zip(ids, ids[1:] + ids[:1]):
            uses[tuple(sorted((start, end)))] += 1
    return uses


def surface_not_closed(polydata) -> Finding:
    """Edges that are not shared by exactly two triangles.

    DP-366. A surface bounds a volume when every edge has a triangle on each
    side of it, and there are two ways to fail that: an edge with one triangle
    (a hole) and an edge with three or more (a fin, a doubled sheet). Only the
    first was graded. `open_edges` asks vtkFeatureEdges for boundary edges,
    which is the one-triangle category alone, so `annulus_shell` -- 512
    triangles, 168 edges used four times, not one used once -- reported zero
    open edges and the sentence "surface boundary closed" while VTK's own
    `IsSurfaceClosed`, called two findings later in the same report, said the
    surface was open, and while Gmsh refused it outright.

    The count is the plain census rather than a filter's category, so both
    failures land in one number and the repairs it names follow from which
    kind of edge is actually there.
    """
    uses = _edge_use_census(polydata)
    unclosed = {edge: count for edge, count in uses.items() if count != 2}
    boundary = sum(1 for count in unclosed.values() if count < 2)
    shared = len(unclosed) - boundary
    repairs = ((('tess.fill_holes',) if boundary else ())
               + (('tess.fix_nonmanifold',) if shared else ()))
    parts = []
    if boundary:
        parts.append(f'{count_text(boundary, "edge")} with one triangle')
    if shared:
        parts.append(f'{count_text(shared, "edge")} with more than two')
    return Finding(
        'surface_not_closed', len(unclosed),
        Severity.OK if not unclosed else Severity.ERROR,
        'Every edge is shared by exactly two triangles, so the surface '
        'bounds a volume.' if not unclosed else
        f'The surface does not bound a volume: {", ".join(parts)}.',
        tuple(polydata.GetPoint(edge[0]) for edge in list(unclosed)[:50]),
        repairable_by=repairs,
        engine_impact={
            'snappy': 'Castellation can leak through a surface that does '
                      'not close.',
            'gmsh': 'Gmsh fills a volume the surface bounds, so a surface '
                    'that bounds none is refused.'},
        details={'edges_with_one_triangle': boundary,
                 'edges_with_more_than_two': shared})


def region_shells(polydata, region_cells: dict) -> Finding:
    """Whether each named region's own triangles bound a volume.

    DP-396. A conjugate assembly is several closed bodies that touch, and its
    tessellation read as one surface is *necessarily* not closed: the face the
    bodies share is tessellated once per body, so every edge of it is used
    four times and each of its triangles appears twice. MEASURED on
    ``multiregion/jacketed_pipe.step`` -- 2 solids, 7 CAD faces, 584
    triangles -- the merged surface reports 180 edges used more than twice,
    180 non-manifold edges and 90 duplicate triangles; each body on its own
    reports nothing at all, 176 and 408 triangles with every edge used exactly
    twice. `surface_not_closed` is fatal to Gmsh, so the Gmsh route refused
    the one kind of geometry the campaign exists to mesh, with "No
    acknowledgement changes that" -- while `touching_shells`, in the same
    report, already said the shared face "is an interface between named
    regions, not a defect".

    The question a mesher needs answered is not whether the union closes; it
    is whether each region does, because that is the volume Gmsh fills and
    the cellZone snappy names. So that is what this counts, and it counts the
    regions that do *not* close, leaving a real hole in one body reported as
    one region rather than hidden in a merged total.

    The finding is only ``evaluated`` when the regions between them account
    for every triangle of the surface. Short of that the merged faults are
    not explained by the regions closing, and nothing may be concluded from
    them -- which is exactly the guard :func:`classify` leans on.
    """
    per_region, open_regions = [], []
    covered = 0
    for name, cell_ids in region_cells.items():
        cell_ids = list(cell_ids)
        uses = _edge_use_census(polydata, cell_ids)
        unclosed = [count for count in uses.values() if count != 2]
        covered += len(cell_ids)
        per_region.append({'region': str(name), 'triangles': len(cell_ids),
                           'unclosed_edges': len(unclosed)})
        if unclosed:
            open_regions.append(str(name))
    total = int(polydata.GetNumberOfCells())
    complete = bool(region_cells) and covered == total
    if open_regions:
        message = (f'{count_text(len(open_regions), "region")} does not bound '
                   f'a volume: {", ".join(sorted(open_regions))}.')
    elif complete:
        message = (f'Each of the {len(region_cells)} regions bounds a volume '
                   f'on its own, so where they meet the shared face is an '
                   f'interface and not a hole.')
    else:
        message = (f'{covered:,} of {count_text(total, "triangle")} belong to '
                   f'a region, so the regions do not account for the whole '
                   f'surface.')
    return Finding(
        'open_region_shells', len(open_regions),
        Severity.ERROR if open_regions else Severity.OK, message,
        repairable_by=('tess.fill_holes',) if open_regions else (),
        evaluated=complete,
        engine_impact={
            'gmsh': 'Gmsh fills the volume each region bounds, so a region '
                    'that bounds none is refused — and one that does is not.',
            'snappy': 'Each closed region is a cellZone; a region that does '
                      'not close leaks into its neighbour.'},
        details={'regions': len(region_cells), 'covered_cells': covered,
                 'total_cells': total, 'covers_every_triangle': complete,
                 'per_region': per_region[:50]})


def non_manifold_edges(polydata) -> Finding:
    output = _feature_output(polydata, non_manifold=True)
    count = int(output.GetNumberOfCells())
    incidence = defaultdict(list)
    for face_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(face_id)
        ids = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
        for start, end in zip(ids, ids[1:] + ids[:1]):
            incidence[tuple(sorted((start, end)))].append(face_id)
    edge_faces = [{'point_ids': list(edge), 'face_ids': faces[:50]}
                  for edge, faces in incidence.items() if len(faces) > 2]
    return Finding(
        'non_manifold_edges', count, Severity.OK if count == 0 else Severity.ERROR,
        'No non-manifold edges.' if count == 0 else
        f'{count_text(count, "non-manifold edge")}; region selection may be '
        'ambiguous.',
        _samples(output), repairable_by=('tess.fix_nonmanifold',),
        engine_impact={'snappy': 'Non-manifold edges can leak or split regions.'},
        details={'edge_faces': edge_faces[:50]})


def dropped_facets(polydata, source_file=None) -> Finding:
    """Triangles the file has that the surface being graded does not.

    DP-44. ``vtkSTLReader`` merges coincident points on load, and merging
    collapses a zero-area triangle into nothing -- silently. Every other check
    in this module runs on what survived that, while the artifact staged for
    OpenFOAM can be the file unchanged, so a surface can read watertight,
    score 100 and be rated ready while the mesher chokes on triangles no check
    was ever shown. MEASURED on ``cad/filleted_manifold.stl``: 7436 facets in
    the file, 7424 after the read, and the 12 are exactly the zero-area
    triangles ``surfaceCheck`` calls illegal on the same bytes.

    Counting the file is the only way to see them, so this check needs the
    path and reports ``evaluated=False`` without one rather than answering
    zero.
    """
    from foammesh.core.geometry.importers.stl_importer import facet_count

    declared = facet_count(source_file) if source_file else None
    kept = int(polydata.GetNumberOfCells())
    if declared is None:
        return Finding(
            'dropped_facets', 0, Severity.OK,
            'Not checked: the surface was not graded against a file.',
            evaluated=False)
    count = max(0, declared - kept)
    return Finding(
        'dropped_facets', count, Severity.OK if count == 0 else Severity.ERROR,
        'Every facet in the file survived the read.' if count == 0 else
        f'{count:,} of {count_text(declared, "facet")} in the file '
        f'{agreeing(count, "collapses", "collapse")} when coincident points '
        'are welded; the mesher reads the file, not '
        'the welded copy.',
        repairable_by=() if count == 0 else ('tess.weld',),
        engine_impact=None if count == 0 else {
            'gmsh': 'Refuses a surface carrying degenerate triangles.',
            'snappy': 'Tolerates them, so the two engines disagree about one file.'},
        details={'declared': declared, 'read': kept,
                 'source_file': str(source_file)})


def duplicate_points(polydata) -> Finding:
    clean = vtkCleanPolyData()
    clean.SetInputData(polydata)
    clean.ToleranceIsAbsoluteOn()
    clean.SetAbsoluteTolerance(max(_diag(polydata) * 1e-6, 1e-15))
    clean.PointMergingOn()
    clean.Update()
    referenced = {polydata.GetCell(cell_id).GetPointId(index)
                  for cell_id in range(polydata.GetNumberOfCells())
                  for index in range(polydata.GetCell(cell_id).GetNumberOfPoints())}
    count = max(0, int(len(referenced) - clean.GetOutput().GetNumberOfPoints()))
    return Finding(
        'duplicate_points', count, Severity.OK if count == 0 else Severity.WARNING,
        'No duplicate/coincident points.' if count == 0 else
        f'{count_text(count, "coincident point")}; weld recommended.',
        repairable_by=('tess.weld',),
        engine_impact={'snappy': 'Coincident points can create degenerate triangles.'})


def triangle_quality(polydata) -> list[Finding]:
    diagonal = max(_diag(polydata), 1e-30)
    area_limit = (1e-8 * diagonal) ** 2
    triples: Counter[tuple[int, int, int]] = Counter()
    degenerate, needles = [], []
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        if cell.GetNumberOfPoints() != 3:
            continue
        ids = tuple(cell.GetPointId(i) for i in range(3))
        triples[tuple(sorted(ids))] += 1
        a, b, c = (polydata.GetPoint(i) for i in ids)
        lengths = (_distance(a, b), _distance(b, c), _distance(c, a))
        area = _triangle_area(a, b, c)
        if area < area_limit:
            degenerate.append(_centroid(a, b, c))
        elif max(lengths) / max(min(lengths), 1e-30) > 1000:
            needles.append(_centroid(a, b, c))
    duplicate_count = sum(count - 1 for count in triples.values() if count > 1)
    threshold = max(1, int(polydata.GetNumberOfCells() * .01))
    return [
        Finding('duplicate_triangles', duplicate_count,
                Severity.ERROR if duplicate_count > threshold else
                (Severity.WARNING if duplicate_count else Severity.OK),
                f'{count_text(duplicate_count, "duplicate triangle")}.',
                repairable_by=('tess.dedupe',),
                engine_impact={'snappy': 'Duplicate facets create ambiguous surface ownership.'}),
        Finding('degenerate_triangles', len(degenerate),
                Severity.ERROR if len(degenerate) > threshold else
                (Severity.WARNING if degenerate else Severity.OK),
                f'{count_text(len(degenerate), "zero/near-zero-area triangle")}.',
                tuple(degenerate[:50]),
                math.sqrt(area_limit), 'm', ('tess.dedupe',),
                {'snappy': 'Degenerate facets destabilize feature extraction.'}),
        Finding('needle_triangles', len(needles),
                Severity.ERROR if len(needles) > threshold else
                (Severity.WARNING if needles else Severity.OK),
                f'{count_text(len(needles), "needle triangle")} with aspect '
                'ratio above 1000.',
                tuple(needles[:50]), repairable_by=('tess.collapse_slivers',),
                engine_impact={'snappy': 'Needle facets produce noisy surface normals.'}),
    ]


def _orientation_sentence(disagreements: int, inward: bool) -> str:
    """What is wrong with the winding, in the order a reader can act on.

    DP-83. The old sentence was the disagreement count followed by the
    inside-out clause, so a surface whose triangles all agree with each other
    and all point the wrong way read "0 inconsistent shared edge(s); closed
    surface is inside-out." beside an error verdict -- MEASURED on the wrapped
    `annulus_shell` rev2. A count of zero opening an error is a reader's first
    reason to distrust the whole report: the two readings are independent, so
    they are said independently.
    """
    if not disagreements and not inward:
        return 'Triangle winding is consistent and outward.'
    if not disagreements:
        return ('Triangle winding is consistent, and the whole closed surface '
                'is wound inside-out: every normal points into the solid.')
    if not inward:
        return f'{count_text(disagreements, "inconsistent shared edge")}.'
    return (f'{count_text(disagreements, "inconsistent shared edge")}, and '
            f'the closed surface is wound inside-out.')


def orientation(polydata) -> Finding:
    """Two independent readings of the winding: does it agree, and which way.

    DP-400. The severity used to rest on the second reading alone, so a
    closed surface wound two ways at once was a warning while a closed
    surface wound consistently the wrong way was an error -- the opposite
    order to what each costs. Gmsh reorients a discrete surface that bounds a
    volume, and the shell topology records the global sign either way, so a
    uniformly inward winding survives the run; a winding that disagrees with
    itself does not, because one direction is recorded for the whole shell
    and the boundary layer follows it. MEASURED on `heated_duct.stl`, whose
    44 triangles are closed and enclose a positive volume and whose z = 0 cap
    of 14 is wound inside-out: the layer grew up into the solid, and the
    published mesh carried 342 faces with both their cells on the same side,
    which checkMesh read as 342 open cells. Both readings are errors now.
    """
    edge_directions: defaultdict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    signed_volume = 0.0
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        if cell.GetNumberOfPoints() != 3:
            continue
        ids = [cell.GetPointId(i) for i in range(3)]
        points = [polydata.GetPoint(i) for i in ids]
        signed_volume += _dot(points[0], _cross(points[1], points[2])) / 6.0
        for start, end in zip(ids, ids[1:] + ids[:1]):
            edge_directions[tuple(sorted((start, end)))].append((start, end))
    disagreements = sum(1 for values in edge_directions.values()
                        if len(values) == 2 and values[0] == values[1])
    closed = is_watertight(polydata)
    inward = closed and signed_volume < 0
    count = disagreements + int(inward)
    # An open surface bounds no volume, so a disagreement on one is a repair
    # to offer rather than a run to stop; on a closed one it is the run.
    severity = (Severity.ERROR if inward or (closed and disagreements)
                else Severity.WARNING if count else Severity.OK)
    return Finding(
        'inconsistent_orientation', count, severity,
        _orientation_sentence(disagreements, inward),
        repairable_by=('tess.orient',),
        engine_impact={'snappy': 'Inconsistent normals impair inside/outside classification.'})


def shells_and_fragments(polydata) -> Finding:
    connectivity = vtkPolyDataConnectivityFilter()
    connectivity.SetInputData(polydata)
    connectivity.SetExtractionModeToAllRegions()
    connectivity.Update()
    regions = int(connectivity.GetNumberOfExtractedRegions())
    sizes, region_metrics = [], []
    for region in range(regions):
        selected = vtkPolyDataConnectivityFilter()
        selected.SetInputData(polydata)
        selected.SetExtractionModeToSpecifiedRegions()
        selected.AddSpecifiedRegion(region)
        selected.Update()
        region_surface = selected.GetOutput()
        cell_count = int(region_surface.GetNumberOfCells())
        area = sum(_triangle_area(
            *(region_surface.GetPoint(region_surface.GetCell(cell_id).GetPointId(i))
              for i in range(3)))
            for cell_id in range(region_surface.GetNumberOfCells())
            if region_surface.GetCell(cell_id).GetNumberOfPoints() == 3)
        sizes.append(cell_count)
        region_metrics.append({'region_id': region, 'cells': cell_count, 'area': area})
    small_limit = max(10, int(polydata.GetNumberOfCells() * .001))
    fragments = sum(1 for size in sizes if size < small_limit)
    return Finding(
        'small_fragments', fragments,
        Severity.WARNING if fragments else (Severity.INFO if regions > 1 else Severity.OK),
        f'{count_text(regions, "disconnected shell")}; '
        f'{count_text(fragments, "small fragment")}.',
        # DP-223. `sizes` holds cell counts, so this row is a count and
        # printing it under a heading that reads as a length was a lie.
        characteristic_size=float(min(sizes)) if sizes else None,
        characteristic_unit='cells',
        repairable_by=('tess.drop_fragments',) if fragments else (),
        engine_impact={'snappy': 'Small disconnected shells can create unwanted refinement.'},
        details={'regions': region_metrics})


def unit_sanity(polydata) -> Finding:
    diagonal = _diag(polydata)
    suspicious = diagonal > 0 and (diagonal < .001 or diagonal > 10000.0)
    return Finding(
        'unit_scale', int(suspicious), Severity.WARNING if suspicious else Severity.OK,
        f'Bounding-box diagonal {diagonal:.6g} model units' +
        (' suggests a possible unit mismatch.' if suspicious else ' is within sanity limits.'),
        characteristic_size=diagonal, characteristic_unit='m',
        engine_impact={'snappy': 'A unit mismatch makes cell-size controls misleading.'})


#: DP-684. How far off one plane a surface may lie, as a fraction of its
#: bounding-box diagonal, and still be a section. Tessellated STEP planes sit
#: on their plane to round-off; anything with real thickness is far above this.
PLANAR_TOLERANCE = 1e-6


def planar_section(polydata) -> Finding:
    """Whether every point of the surface lies on one plane (DP-684).

    Such a surface bounds no volume and never can, so being open is not a
    fault in it: it is what a section is. Gmsh meshes it in 2D or
    axisymmetric mode, and the grader needs to know that before it calls
    the section's own outline fatal. The plane is fitted rather than read off
    the bounding box, because a section need not lie on a coordinate plane.
    """
    import numpy
    from vtkmodules.util.numpy_support import vtk_to_numpy

    diagonal = _diag(polydata)
    planar, normal = False, None
    points = polydata.GetPoints()
    if points is not None and polydata.GetNumberOfPoints() >= 3 and diagonal > 0:
        coordinates = vtk_to_numpy(points.GetData()).astype(float)
        centred = coordinates - coordinates.mean(axis=0)
        _u, spread, axes = numpy.linalg.svd(centred, full_matrices=False)
        # the smallest singular value is the RMS distance off the best plane
        # times sqrt(n); the largest point distance is the honest bound
        normal = axes[-1]
        off_plane = float(numpy.abs(centred @ normal).max())
        planar = off_plane <= PLANAR_TOLERANCE * diagonal and spread[1] > 0
    return Finding(
        'planar_section', int(planar), Severity.INFO if planar else Severity.OK,
        ('Every point lies on one plane, so this is a section and bounds no '
         'volume. Gmsh meshes it in 2D or axisymmetric mode, set on Generate '
         'mesh; snappy cannot mesh it.') if planar else
        'The surface is not a single planar section.',
        details=({'normal': [float(value) for value in normal]}
                 if planar else None),
        engine_impact=({'gmsh': 'Meshed as a section in 2D or axisymmetric '
                                'mode; a 3D run refuses it.',
                        'snappy': 'A planar surface encloses no cells.'}
                       if planar else None))


def _why_not_closed(polydata, closure=None) -> str:
    """What stopped the surface closing, in the words `surface_not_closed`
    already used for it.

    DP-408, and DP-366's mistake seen from the other side. `IsSurfaceClosed`
    wants every edge used by exactly two triangles, so an edge used by one
    fails it and an edge used by three fails it just as hard -- and this
    check called both of them "the surface is open". A doubled sheet or a
    fin is not a hole, and a user sent looking for a hole there is none of
    will not find one. `annulus_shell`, DP-366's own case, is exactly that
    surface: 168 edges used four times and not one used once.

    So the reading comes from the edge-use census rather than from
    `vtkFeatureEdges`, which is where DP-366 found the categories that do
    not add up to closure, and it comes from the caller when the caller has
    already taken it -- `check_all` runs `surface_not_closed` in the same
    pass. Reusing that finding is also what keeps the two sentences from
    disagreeing about the same surface.
    """
    if closure is None:
        closure = surface_not_closed(polydata)
    details = closure.details or {}
    boundary = int(details.get('edges_with_one_triangle') or 0)
    shared = int(details.get('edges_with_more_than_two') or 0)
    parts = []
    if boundary:
        parts.append(f'{count_text(boundary, "edge")} with one triangle')
    if shared:
        parts.append(f'{count_text(shared, "edge")} with more than two')
    if not parts:
        # Every edge used twice and VTK still refused it. That is a real
        # state -- two shells nested, an inside-out orientation -- and the
        # honest thing is to claim no reading rather than invent one.
        return 'the surface does not close'
    return 'the surface has ' + ' and '.join(parts)


def fluid_seed(polydata, closure=None) -> Finding:
    if not is_watertight(polydata):
        return Finding(
            'fluid_seed', 0, Severity.INFO,
            'Internal seed search not evaluated: '
            + _why_not_closed(polydata, closure)
            + ', so there is no inside to seed.',
            evaluated=False,
            engine_impact={'snappy': 'locationInMesh must be supplied for internal meshing.'})
    bounds = polydata.GetBounds()
    center = tuple((bounds[i * 2] + bounds[i * 2 + 1]) / 2 for i in range(3))
    points = vtkPolyData()
    from vtkmodules.vtkCommonCore import vtkPoints
    candidates = vtkPoints()
    candidate_values = [center]
    # Deterministic bbox jitter catches off-centre cavities while staying
    # comfortably away from the surface at 20/50/80 percent positions.
    for fx in (.2, .5, .8):
        for fy in (.2, .5, .8):
            for fz in (.2, .5, .8):
                point = (bounds[0] + fx * (bounds[1] - bounds[0]),
                         bounds[2] + fy * (bounds[3] - bounds[2]),
                         bounds[4] + fz * (bounds[5] - bounds[4]))
                if point not in candidate_values:
                    candidate_values.append(point)
    for candidate in candidate_values:
        candidates.InsertNextPoint(candidate)
    points.SetPoints(candidates)
    enclosed = vtkSelectEnclosedPoints()
    enclosed.SetInputData(points)
    enclosed.SetSurfaceData(polydata)
    enclosed.Update()
    working = next((candidate_values[i] for i in range(len(candidate_values))
                    if enclosed.IsInside(i)), None)
    found = working is not None
    return Finding(
        'fluid_seed', 0 if found else 1, Severity.OK if found else Severity.ERROR,
        'A robust interior seed was found.' if found else 'No interior fluid seed was found.',
        (working,) if working else (), repairable_by=(),
        engine_impact={'snappy': 'Internal meshing requires a valid locationInMesh seed.'},
        details={'candidates_tested': len(candidate_values),
                 'working_seed': list(working) if working else None})


def _within_shell_intersections(polydata, budget, *, sample_limit: int = 50):
    """Triangles of one shell that cross each other, under a budget.

    DP-435. This half of ``self_intersections`` was never written. A surface
    with fewer than two shells short-circuited to ``evaluated=False`` with
    "within-shell intersections not evaluated", and a surface with more than
    one got the between-shell half only -- so across 116 readiness blocks in
    the campaign, not one had its triangles checked for crossing each other.
    A skin that crosses itself is one of the standard reasons snappy leaks
    through a castellation and Gmsh refuses a volume.

    Three stages, and the order is the whole performance story. A bin tree
    over every triangle answers "whose bounds could reach mine"; numpy then
    throws out the candidates whose boxes do not actually overlap and the
    ones sharing a vertex, which is most of them; and only what survives
    reaches ``vtkTriangle::TrianglesIntersect``, which is the geometry
    predicate and stays VTK's rather than hand-rolled here.

    MEASURED, sphere skins on this machine: 79,200 triangles in 3.3 s and
    403,200 in 37.4 s. A per-candidate Python loop over the same inputs ran
    at roughly 500 triangles a second and could not finish either. The bin
    count matters as much as the pruning: left automatic the tree used
    19x19x19 bins and handed back 224 candidates per triangle, against 32
    at ``SetNumberOfCellsPerNode(4)``.

    Pairs that share a vertex are skipped -- neighbouring triangles touch by
    construction and reporting that would drown the real finding.

    Returns ``(count, locations, tested)``, or ``None`` when the budget was
    spent first, so the caller can say how far it got rather than guess.
    """
    import numpy as np
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkIdList
    from vtkmodules.vtkCommonDataModel import vtkStaticCellLocator, vtkTriangle

    from .budget import BudgetExceeded, Cancelled, within_shell_workload

    polys = polydata.GetPolys()
    cells = int(polydata.GetNumberOfCells())
    if cells < 2 or polys is None or polys.GetNumberOfCells() != cells:
        return 0, [], cells

    offsets = vtk_to_numpy(polys.GetOffsetsArray())
    if offsets.size != cells + 1 or not np.all(np.diff(offsets) == 3):
        # Not a pure triangle soup. The tessellation the diagnostics run on
        # always is; saying so beats quietly reshaping a quad into nonsense.
        return 0, [], cells

    corners = vtk_to_numpy(polys.GetConnectivityArray()).reshape(-1, 3)
    points = vtk_to_numpy(polydata.GetPoints().GetData()).astype(float)
    triangles = points[corners]
    lower = triangles.min(axis=1)
    upper = triangles.max(axis=1)

    locator = vtkStaticCellLocator()
    # Left automatic this is the difference between 32 candidates a triangle
    # and 224, which is the difference between minutes and half an hour.
    locator.SetNumberOfCellsPerNode(4)
    locator.SetDataSet(polydata)
    locator.BuildLocator()

    found = 0
    locations: list[tuple] = []
    neighbours = vtkIdList()
    bounds = [0.0] * 6

    for index in range(cells):
        if index % 256 == 0:
            progress = f'{index:,} of {cells:,} triangles'
            try:
                budget.check_in(progress=progress)
            except (BudgetExceeded, Cancelled):
                return None
            budget.observe(within_shell_workload(cells, index))
            budget.report(progress, index / max(cells, 1))

        polydata.GetCellBounds(index, bounds)
        locator.FindCellsWithinBounds(bounds, neighbours)
        total = neighbours.GetNumberOfIds()
        if total < 2:
            continue
        candidates = np.fromiter(
            (neighbours.GetId(slot) for slot in range(total)),
            dtype=np.int64, count=total)
        # Each pair is looked at once, from its lower index.
        candidates = candidates[candidates > index]
        if candidates.size == 0:
            continue
        # A bin is coarser than a bounding box; this is the exact overlap.
        candidates = candidates[
            np.all(lower[candidates] <= upper[index], axis=1)
            & np.all(upper[candidates] >= lower[index], axis=1)]
        if candidates.size == 0:
            continue
        mine = corners[index]
        touching = (corners[candidates][:, :, None] == mine[None, None, :]
                    ).any(axis=(1, 2))
        candidates = candidates[~touching]
        if candidates.size == 0:
            continue

        left = triangles[index]
        for candidate in candidates:
            right = triangles[candidate]
            if vtkTriangle.TrianglesIntersect(left[0], left[1], left[2],
                                              right[0], right[1], right[2]):
                found += 1
                if len(locations) < sample_limit:
                    locations.append(tuple(float(value)
                                           for value in left.mean(axis=0)))
    return found, locations, cells


def self_intersections(polydata, *, triangle_limit: int | None = None,
                       budget=None) -> Finding:
    """Pairwise shell intersections, under a budget.

    MEASURED: this check ran for 7 h 24 min on a 37,240-triangle propeller and
    would never have returned. It used to be guarded by ``triangle_limit``
    alone, which is inverted in effect -- it exempted a 400,020-triangle model
    as "too big" while admitting the 37,240-triangle one that hung. Triangle
    count is not the cost; the products of the shell sizes are.

    ``triangle_limit`` is retained only for callers that still pass it. The
    budget is the real gate.
    """
    from .budget import (BudgetExceeded, Cancelled, DiagnosticBudget,
                         pairwise_workload, within_shell_budget)

    cells = int(polydata.GetNumberOfCells())
    if triangle_limit is not None and cells > triangle_limit:
        return Finding(
            'self_intersections', 0, Severity.INFO,
            'Self-intersection check not evaluated: triangle threshold exceeded.',
            evaluated=False,
            engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})
    connectivity = vtkPolyDataConnectivityFilter()
    connectivity.SetInputData(polydata)
    connectivity.SetExtractionModeToAllRegions()
    connectivity.Update()
    count = int(connectivity.GetNumberOfExtractedRegions())

    # DP-435. The within-shell half, which had no implementation at all. It
    # runs whatever the shell count is: one shell that crosses itself is the
    # case the old short-circuit dismissed, and a shell in a set of four can
    # cross itself just as readily as it can cross its neighbours. With more
    # than one shell it is run per shell rather than over the whole surface,
    # or every crossing the pairwise half is about would be counted twice
    # under a name that says the opposite.
    within_budget = within_shell_budget(polydata.GetNumberOfCells())
    if count < 2:
        within = _within_shell_intersections(polydata, within_budget)
        if within is None:
            return Finding(
                'self_intersections', 0, Severity.WARNING,
                within_budget.not_evaluated_reason('within-shell triangles'),
                evaluated=False,
                details={'within_shell_evaluated': False,
                         'shells': count,
                         'workload': within_budget.workload,
                         'limited_by': within_budget.limited_by},
                engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})
        crossings, where, tested = within
        return Finding(
            'self_intersections', crossings,
            Severity.ERROR if crossings else Severity.OK,
            f'{count_text(crossings, "self-intersecting triangle pair")} '
            f'across {tested:,} triangles of a single shell.',
            tuple(where), repairable_by=('tess.detect_intersections',),
            evaluated=True,
            details={'within_shell_evaluated': True,
                     'within_shell': crossings,
                     'triangles_tested': tested,
                     'shells': count, 'pairs_tested': 0},
            engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})
    regions = []
    for region in range(count):
        selected = vtkPolyDataConnectivityFilter()
        selected.SetInputData(polydata)
        selected.SetExtractionModeToSpecifiedRegions()
        selected.AddSpecifiedRegion(region)
        selected.Update()
        copy = vtkPolyData()
        copy.DeepCopy(selected.GetOutput())
        regions.append(copy)

    within = 0, [], 0
    for shell in regions:
        one = _within_shell_intersections(shell, within_budget)
        if one is None:
            within = None
            break
        within = (within[0] + one[0],
                  within[1] + one[1][:max(0, 50 - len(within[1]))],
                  within[2] + one[2])

    sizes = [float(item.GetNumberOfCells()) for item in regions]
    if budget is None:
        from .budget import budget_from_settings

        budget = budget_from_settings('self_intersections')
    budget.check = budget.check or 'self_intersections'
    budget.workload = pairwise_workload(sizes)
    pairs = [(left, right) for left in range(len(regions))
             for right in range(left + 1, len(regions))]
    budget.report(
        f'checking {count_text(len(pairs), "shell pair")} across '
        f'{cells:,} triangles', 0.0)

    intersections = 0
    locations = []
    done_units = 0.0

    def partial_finding(said, extra):
        """DP-435. What the within-shell half found is knowledge too.

        The pairwise half is the expensive one -- 325 shell pairs over
        399,966 triangles is 5.05e10 work units, which is quoted at eleven
        hours -- and when it gave up it discarded the within-shell result
        along with its own, so a surface whose triangles had already been
        measured as crossing each other was reported as though nothing had
        been looked at. Each half now says for itself whether it ran.
        """
        crossed = 0 if within is None else within[0]
        points = list(locations)
        if within is not None:
            points.extend(within[1][:max(0, 50 - len(points))])
        if crossed:
            said = (f'{said} '
                    f'{count_text(crossed, "self-intersecting triangle pair")} '
                    f'within a shell, found before that.')
        details = dict(extra)
        details.update({'within_shell_evaluated': within is not None,
                        'within_shell': crossed,
                        'triangles_tested': 0 if within is None else within[2],
                        'shells': count})
        return Finding(
            'self_intersections', intersections + crossed,
            Severity.ERROR if crossed else Severity.INFO,
            said, tuple(points[:50]),
            repairable_by=('tess.detect_intersections',) if crossed else (),
            evaluated=False, details=details,
            engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})
    for index, (left, right) in enumerate(pairs, 1):
        progress = f'{index - 1} of {len(pairs)} shell pairs'
        try:
            budget.check_in(progress=progress)
        except BudgetExceeded as error:
            # Partial knowledge is still knowledge: report what was found and
            # say plainly that the rest was not looked at, and why.
            return partial_finding(
                budget.not_evaluated_reason(progress),
                {'partial': True, 'pairs_tested': index - 1,
                 'pairs_total': len(pairs), 'workload': budget.workload,
                 'limited_by': budget.limited_by, 'reason': str(error)})
        except Cancelled:
            return partial_finding(
                f'Not evaluated: cancelled after {progress}.',
                {'cancelled': True, 'pairs_tested': index - 1,
                 'pairs_total': len(pairs)})

        outcome = _intersect_pair(regions[left], regions[right], budget)
        if outcome is None:
            return partial_finding(
                budget.not_evaluated_reason(progress),
                {'partial': True, 'pairs_tested': index - 1,
                 'pairs_total': len(pairs), 'workload': budget.workload,
                 'limited_by': budget.limited_by,
                 'reason': 'a single shell pair exceeded the budget'})
        found, sampled = outcome
        intersections += found
        locations.extend(
            tuple(point) for point in sampled[:max(0, 50 - len(locations))])
        done_units += sizes[left] * sizes[right]
        budget.observe(done_units)
        budget.report(f'{index} of {len(pairs)} shell pairs',
                      index / max(len(pairs), 1))

    crossings = 0 if within is None else within[0]
    if within is not None:
        locations.extend(
            point for point in within[1][:max(0, 50 - len(locations))])
    total = intersections + crossings
    said = (f'{count_text(intersections, "pairwise shell intersection curve")}'
            f'; {count_text(crossings, "self-intersecting triangle pair")} '
            f'within a shell.')
    if within is None:
        curves = count_text(intersections, 'pairwise shell intersection curve')
        why = within_budget.not_evaluated_reason('within-shell triangles')
        said = f'{curves}; {why}'
    return Finding(
        'self_intersections', total,
        Severity.ERROR if total else (
            Severity.WARNING if within is None else Severity.OK),
        said,
        # Plan 26 WP7.2. This was `()`, so `repair_plan.suggest` could not emit
        # the action on either route: the tessellated branch filters on
        # `repairable_by`, and the CAD branch's always-include list holds only
        # `cad.*` ids. The action was registered and runnable through the
        # facade and no control ever offered it. Naming it here is the whole
        # fix -- `tess.detect_intersections` reports and changes nothing
        # (band 0), so suggesting it is never destructive.
        tuple(locations[:50]), repairable_by=('tess.detect_intersections',),
        evaluated=within is not None,
        details={'pairs_tested': len(pairs), 'workload': budget.workload,
                 'shells': count,
                 'within_shell_evaluated': within is not None,
                 'within_shell': crossings,
                 'triangles_tested': 0 if within is None else within[2]},
        engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})


def _intersect_pair(left, right, budget):
    """One shell-pair intersection, abandonable.

    ``vtkIntersectionPolyDataFilter`` is a single opaque call that never
    yields, so polling around it is not enough -- the propeller spent seven
    hours inside one.

    MEASURED: abandoning it on a *thread* segfaults the process. The thread
    cannot be killed, so it stays inside VTK mutating objects the caller then
    keeps using. Running it in a child process instead means an overrun can be
    killed outright, which is the whole point of having a budget.

    Returns ``None`` when the pair was abandoned, and a
    ``(cell_count, points)`` pair otherwise.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from vtkmodules.vtkFiltersGeneral import vtkIntersectionPolyDataFilter
    from .budget import BudgetPolicy

    def in_process():
        test = vtkIntersectionPolyDataFilter()
        test.SetInputData(0, left)
        test.SetInputData(1, right)
        test.Update()
        curves = test.GetOutput()
        total = curves.GetNumberOfPoints()
        return (int(curves.GetNumberOfCells()),
                [list(curves.GetPoint(index)) for index in range(min(total, 50))])

    if budget is None or budget.policy is BudgetPolicy.NEVER_LIMIT:
        return in_process()

    remaining = budget.abort_seconds - budget.elapsed
    if remaining <= 0:
        return None

    from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter

    with tempfile.TemporaryDirectory(prefix='foammesh-intersect-') as scratch:
        root = Path(scratch)
        for name, data in (('left.vtp', left), ('right.vtp', right)):
            writer = vtkXMLPolyDataWriter()
            writer.SetFileName(str(root / name))
            writer.SetInputData(data)
            writer.Write()
        result_path = root / 'result.json'
        import time as _time

        # MEASURED: the child is a fresh interpreter, and without this it
        # cannot import foammesh at all -- the parent's runtime sys.path is not
        # inherited. Every launch failed instantly, the fallback below ran the
        # unbounded call instead, and the budget did nothing whatsoever.
        import os

        package_root = Path(__file__).resolve().parents[4]
        environment = dict(os.environ)
        existing = environment.get('PYTHONPATH', '')
        environment['PYTHONPATH'] = (
            f'{package_root}{os.pathsep}{existing}' if existing
            else str(package_root))

        child_started = _time.monotonic()
        try:
            subprocess.run(
                [sys.executable, '-m',
                 'foammesh.core.geometry.diagnostics._intersect_worker',
                 str(root / 'left.vtp'), str(root / 'right.vtp'),
                 str(result_path)],
                timeout=max(remaining, 1.0), check=True,
                capture_output=True, env=environment)
        except subprocess.TimeoutExpired:
            budget.add_external_seconds(_time.monotonic() - child_started)
            budget.report(
                'abandoning a shell pair that exceeded its allowance; the '
                'geometry may have coincident or interpenetrating shells')
            return None
        except (subprocess.SubprocessError, OSError) as error:
            # Never fall back to the unbounded in-process call: that is what
            # silently reinstated a hang the budget was written to prevent.
            # A check that cannot be run safely is reported as not run.
            budget.add_external_seconds(_time.monotonic() - child_started)
            budget.report(
                f'could not run the bounded intersection check: {error}')
            return None
        budget.add_external_seconds(_time.monotonic() - child_started)
        if not result_path.is_file():
            return None
        document = json.loads(result_path.read_text(encoding='utf-8'))
        return int(document.get('cells', 0)), document.get('points') or []


def small_features_vs_target(polydata, target_cell_size: float | None) -> Finding:
    if target_cell_size is None:
        return Finding(
            'small_features_vs_target', 0, Severity.INFO,
            'Small-feature impact not evaluated: target cell size is not configured.',
            evaluated=False,
            engine_impact={'snappy': 'Features below attainable cell size may not snap.'})
    threshold = 2 * float(target_cell_size)
    locations, count, minimum = [], 0, None
    for cell_id in range(polydata.GetNumberOfCells()):
        cell = polydata.GetCell(cell_id)
        ids = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
        for left, right in zip(ids, ids[1:] + ids[:1]):
            length = _distance(polydata.GetPoint(left), polydata.GetPoint(right))
            minimum = length if minimum is None else min(minimum, length)
            if length < threshold:
                count += 1
                if len(locations) < 50:
                    a, b = polydata.GetPoint(left), polydata.GetPoint(right)
                    locations.append(tuple((a[i] + b[i]) / 2 for i in range(3)))
    return Finding(
        'small_features_vs_target', count,
        Severity.INFO if count else Severity.OK,
        f'{count_text(count, "surface edge")} '
        f'{agreeing(count, "is", "are")} smaller than twice target '
        'cell size.',
        tuple(locations), characteristic_size=minimum,
        characteristic_unit='m',
        engine_impact={'snappy': 'Sub-cell features may require higher refinement.'},
        details={'target_cell_size': float(target_cell_size), 'threshold': threshold})


def is_watertight(polydata) -> bool:
    return bool(polydata.GetNumberOfCells() and vtkSelectEnclosedPoints.IsSurfaceClosed(polydata))


def check_all(polydata, *, target_cell_size: float | None = None,
              budget=None, source_file=None) -> list[Finding]:
    """Run every readiness check.

    ``budget`` carries the cancellation flag and progress channel shared by the
    whole import, so one Cancel press stops the run wherever it has reached.

    ``source_file`` is the artifact the surface was read from, when the caller
    has one. Only ``dropped_facets`` uses it, and only it can: every other
    check here is a property of the polydata and cannot see what the read
    already removed.
    """
    # DP-408. The closure census is taken here anyway, and `fluid_seed` needs
    # it to say which way the surface failed to close. Handing the finding on
    # costs nothing, spares a second walk of every edge on every import, and
    # means the two sentences cannot disagree about the same surface.
    closure = surface_not_closed(polydata)
    return [
        open_edges(polydata), closure,
        non_manifold_edges(polydata), duplicate_points(polydata),
        dropped_facets(polydata, source_file),
        *triangle_quality(polydata), orientation(polydata), shells_and_fragments(polydata),
        self_intersections(polydata, budget=budget), unit_sanity(polydata),
        fluid_seed(polydata, closure),
        small_features_vs_target(polydata, target_cell_size),
        planar_section(polydata),
    ]


def _distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _triangle_area(a, b, c):
    return .5 * math.sqrt(sum(value * value for value in _cross(
        tuple(b[i] - a[i] for i in range(3)), tuple(c[i] - a[i] for i in range(3)))))


def _centroid(a, b, c):
    return tuple(float((a[i] + b[i] + c[i]) / 3) for i in range(3))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))
