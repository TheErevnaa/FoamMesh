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
        'No open edges (surface boundary closed).' if count == 0 else
        f'{count} open boundary edge(s) in {loops.GetNumberOfCells()} loop(s).',
        _samples(output), size, ('tess.fill_holes',),
        {'snappy': 'Open surfaces can leak during castellation.'},
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
        f'{count} non-manifold edge(s); region selection may be ambiguous.',
        _samples(output), repairable_by=('tess.fix_nonmanifold',),
        engine_impact={'snappy': 'Non-manifold edges can leak or split regions.'},
        details={'edge_faces': edge_faces[:50]})


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
        f'{count} coincident point(s); weld recommended.',
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
                f'{duplicate_count} duplicate triangle(s).',
                repairable_by=('tess.dedupe',),
                engine_impact={'snappy': 'Duplicate facets create ambiguous surface ownership.'}),
        Finding('degenerate_triangles', len(degenerate),
                Severity.ERROR if len(degenerate) > threshold else
                (Severity.WARNING if degenerate else Severity.OK),
                f'{len(degenerate)} zero/near-zero-area triangle(s).', tuple(degenerate[:50]),
                math.sqrt(area_limit), ('tess.dedupe',),
                {'snappy': 'Degenerate facets destabilize feature extraction.'}),
        Finding('needle_triangles', len(needles),
                Severity.ERROR if len(needles) > threshold else
                (Severity.WARNING if needles else Severity.OK),
                f'{len(needles)} needle triangle(s) with aspect ratio above 1000.',
                tuple(needles[:50]), repairable_by=('tess.collapse_slivers',),
                engine_impact={'snappy': 'Needle facets produce noisy surface normals.'}),
    ]


def orientation(polydata) -> Finding:
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
    inward = is_watertight(polydata) and signed_volume < 0
    count = disagreements + int(inward)
    return Finding(
        'inconsistent_orientation', count,
        Severity.ERROR if inward else (Severity.WARNING if count else Severity.OK),
        'Triangle winding is consistent and outward.' if count == 0 else
        f'{disagreements} inconsistent shared edge(s)' +
        ('; closed surface is inside-out.' if inward else '.'),
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
        f'{regions} disconnected shell(s); {fragments} small fragment(s).',
        characteristic_size=float(min(sizes)) if sizes else None,
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
        characteristic_size=diagonal,
        engine_impact={'snappy': 'A unit mismatch makes cell-size controls misleading.'})


def fluid_seed(polydata) -> Finding:
    if not is_watertight(polydata):
        return Finding(
            'fluid_seed', 0, Severity.INFO,
            'Internal seed search not evaluated because the surface is open.',
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
    from .budget import BudgetExceeded, Cancelled, DiagnosticBudget, pairwise_workload

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
    if count < 2:
        return Finding(
            'self_intersections', 0, Severity.INFO,
            'Pairwise shell intersections evaluated; within-shell intersections not evaluated.',
            evaluated=False,
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

    sizes = [float(item.GetNumberOfCells()) for item in regions]
    if budget is None:
        from .budget import budget_from_settings

        budget = budget_from_settings('self_intersections')
    budget.check = budget.check or 'self_intersections'
    budget.workload = pairwise_workload(sizes)
    pairs = [(left, right) for left in range(len(regions))
             for right in range(left + 1, len(regions))]
    budget.report(
        f'checking {len(pairs)} shell pair(s) across {cells:,} triangles', 0.0)

    intersections = 0
    locations = []
    done_units = 0.0
    for index, (left, right) in enumerate(pairs, 1):
        progress = f'{index - 1} of {len(pairs)} shell pairs'
        try:
            budget.check_in(progress=progress)
        except BudgetExceeded as error:
            # Partial knowledge is still knowledge: report what was found and
            # say plainly that the rest was not looked at, and why.
            return Finding(
                'self_intersections', intersections, Severity.INFO,
                budget.not_evaluated_reason(progress),
                tuple(locations[:50]), evaluated=False,
                details={'partial': True, 'pairs_tested': index - 1,
                         'pairs_total': len(pairs),
                         'workload': budget.workload,
                         'limited_by': budget.limited_by,
                         'reason': str(error)},
                engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})
        except Cancelled:
            return Finding(
                'self_intersections', intersections, Severity.INFO,
                f'Not evaluated: cancelled after {progress}.',
                tuple(locations[:50]), evaluated=False,
                details={'cancelled': True, 'pairs_tested': index - 1,
                         'pairs_total': len(pairs)},
                engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})

        outcome = _intersect_pair(regions[left], regions[right], budget)
        if outcome is None:
            return Finding(
                'self_intersections', intersections, Severity.INFO,
                budget.not_evaluated_reason(progress),
                tuple(locations[:50]), evaluated=False,
                details={'partial': True, 'pairs_tested': index - 1,
                         'pairs_total': len(pairs),
                         'workload': budget.workload,
                         'limited_by': budget.limited_by,
                         'reason': 'a single shell pair exceeded the budget'},
                engine_impact={'snappy': 'Intersecting skins can make region selection ambiguous.'})
        found, sampled = outcome
        intersections += found
        locations.extend(
            tuple(point) for point in sampled[:max(0, 50 - len(locations))])
        done_units += sizes[left] * sizes[right]
        budget.observe(done_units)
        budget.report(f'{index} of {len(pairs)} shell pairs',
                      index / max(len(pairs), 1))

    return Finding(
        'self_intersections', intersections,
        Severity.ERROR if intersections else Severity.OK,
        f'{intersections} pairwise shell intersection curve(s).',
        # Plan 26 WP7.2. This was `()`, so `repair_plan.suggest` could not emit
        # the action on either route: the tessellated branch filters on
        # `repairable_by`, and the CAD branch's always-include list holds only
        # `cad.*` ids. The action was registered and runnable through the
        # facade and no control ever offered it. Naming it here is the whole
        # fix -- `tess.detect_intersections` reports and changes nothing
        # (band 0), so suggesting it is never destructive.
        tuple(locations[:50]), repairable_by=('tess.detect_intersections',),
        evaluated=True,
        details={'pairs_tested': len(pairs), 'workload': budget.workload},
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
        f'{count} surface edge(s) are smaller than twice target cell size.',
        tuple(locations), characteristic_size=minimum,
        engine_impact={'snappy': 'Sub-cell features may require higher refinement.'},
        details={'target_cell_size': float(target_cell_size), 'threshold': threshold})


def is_watertight(polydata) -> bool:
    return bool(polydata.GetNumberOfCells() and vtkSelectEnclosedPoints.IsSurfaceClosed(polydata))


def check_all(polydata, *, target_cell_size: float | None = None,
              budget=None) -> list[Finding]:
    """Run every readiness check.

    ``budget`` carries the cancellation flag and progress channel shared by the
    whole import, so one Cancel press stops the run wherever it has reached.
    """
    return [
        open_edges(polydata), non_manifold_edges(polydata), duplicate_points(polydata),
        *triangle_quality(polydata), orientation(polydata), shells_and_fragments(polydata),
        self_intersections(polydata, budget=budget), unit_sanity(polydata),
        fluid_seed(polydata), small_features_vs_target(polydata, target_cell_size),
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
