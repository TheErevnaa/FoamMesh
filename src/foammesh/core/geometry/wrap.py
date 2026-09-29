"""Experimental, bounded uniform-grid surface wrapper (VTK backend)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .diagnostics import assess
from .diagnostics import is_watertight


MAX_RESOLUTION = 256
MAX_MEMORY_BYTES = 768 * 1024 * 1024


class WrapError(ValueError):
    def __init__(self, code: str, message: str, *, details=None):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class WrapEstimate:
    resolution: int
    dimensions: tuple[int, int, int]
    memory_bytes: int
    voxel_size: float
    expected_min_feature: float

    def to_dict(self):
        return self.__dict__.copy()


def estimate(polydata, *, resolution: int = 64,
             smallest_feature: float | None = None) -> WrapEstimate:
    if smallest_feature is not None:
        if float(smallest_feature) <= 0:
            raise ValueError('smallest_feature must be positive')
        bounds = polydata.GetBounds()
        longest = max(bounds[i * 2 + 1] - bounds[i * 2] for i in range(3))
        target_h = float(smallest_feature) / 2
        requested = max(16, round(longest / target_h) + 1)
        if requested > MAX_RESOLUTION:
            raise WrapError(
                'resolution_over_budget',
                f'smallest_feature requires resolution {requested}, '
                f'above the {MAX_RESOLUTION} cap',
                details={
                    'requested_resolution': requested,
                    'maximum_resolution': MAX_RESOLUTION,
                    'minimum_smallest_feature':
                        2 * longest / (MAX_RESOLUTION - 1),
                })
        resolution = int(requested)
    resolution = int(resolution)
    if resolution < 16 or resolution > MAX_RESOLUTION:
        raise WrapError('resolution_over_budget',
                        f'resolution must be 16..{MAX_RESOLUTION}')
    bounds = polydata.GetBounds()
    lengths = tuple(bounds[i * 2 + 1] - bounds[i * 2] for i in range(3))
    diagonal = math.sqrt(sum(value * value for value in lengths))
    if diagonal <= 0:
        raise ValueError('geometry has no finite wrapping extent')
    longest = max(lengths)
    dimensions = tuple(max(16, round(resolution * length / longest))
                       for length in lengths)
    memory = math.prod(dimensions) * 24
    if memory > MAX_MEMORY_BYTES:
        raise WrapError('resolution_over_budget',
                        'estimated memory exceeds hard cap',
                        details={'memory_bytes': memory, 'cap_bytes': MAX_MEMORY_BYTES})
    voxel = longest / max(1, resolution - 1)
    return WrapEstimate(resolution, dimensions, memory, voxel, 2 * voxel)


def wrap(polydata, *, resolution: int = 64, smallest_feature: float | None = None,
         max_gap: float | None = None,
         smoothing_strength: float = 1.0, max_deviation: float | None = None,
         minimum_component_size: int = 27, decimation: float = 0.0,
         regions_to_retain=None, mode: str = 'external', fluid_seeds=None,
         cancelled=None):
    from vtkmodules.all import (
        vtkFlyingEdges3D, vtkImageDilateErode3D, vtkImageThresholdConnectivity,
        vtkPoints, vtkPolyDataConnectivityFilter, vtkQuadricDecimation,
        vtkTriangleFilter, vtkWindowedSincPolyDataFilter)

    from .occupancy import sampled_distance, wall_occupancy

    def checkpoint(stage):
        if cancelled is not None and cancelled():
            raise WrapError('operation_cancelled',
                            f'wrap cancelled after {stage}',
                            details={'stage': stage})

    checkpoint('start')
    # ``smallest_feature`` (Appendix A §5.2) drives the grid: h = feature / 2,
    # so the resolution follows from the longest extent. It overrides the raw
    # resolution knob when supplied.
    sizing = estimate(
        polydata, resolution=resolution, smallest_feature=smallest_feature)
    # Appendix A §5.2: default max gap to close is 4h.
    gap = float(max_gap) if max_gap is not None else sizing.voxel_size * 4
    if gap <= 0 or not math.isfinite(gap):
        raise ValueError('maximum gap must be positive and finite')
    # Appendix A §5.2: default source-to-wrap deviation budget is 1.5h. This
    # bounds smoothing distortion; the hard rejection gate stays opt-in so the
    # deliberate gap-bridging skin (which legitimately sits far from the source)
    # is reported rather than rejected.
    smoothing_budget = (float(max_deviation) if max_deviation is not None
                        else 1.5 * sizing.voxel_size)
    bounds = list(polydata.GetBounds())
    # Preserve the requested spacing after padding.  Keeping the unpadded
    # dimensions here made h grow with the padding and caused a nominal 3h
    # aperture to disappear before morphology even ran.
    padding = max(4 * sizing.voxel_size, gap + sizing.voxel_size)
    # Centre each axis on a grid node.  Besides making repeated runs stable,
    # this avoids the half-voxel phase shift that can quantise a 3h aperture
    # directly from two open voxels to four as padding changes.
    aligned_axes = []
    dimensions = []
    for axis in range(3):
        lower, upper = bounds[2 * axis:2 * axis + 2]
        centre = (lower + upper) / 2
        half_cells = max(8, int(math.ceil(
            ((upper - lower) / 2 + padding) / sizing.voxel_size)))
        aligned_axes.extend((centre - half_cells * sizing.voxel_size,
                             centre + half_cells * sizing.voxel_size))
        dimensions.append(2 * half_cells + 1)
    model_bounds = tuple(aligned_axes)
    dimensions = tuple(dimensions)
    memory_bytes = math.prod(dimensions) * 24
    if memory_bytes > MAX_MEMORY_BYTES:
        raise WrapError('resolution_over_budget',
                        'estimated padded-grid memory exceeds hard cap',
                        details={'memory_bytes': memory_bytes,
                                 'cap_bytes': MAX_MEMORY_BYTES})
    grid_metrics = {**sizing.to_dict(), 'dimensions': dimensions,
                    'memory_bytes': memory_bytes, 'padding': padding}
    stages = [{'stage': 'grid_sizing', 'status': 'done',
               'metrics': grid_metrics}]
    if mode not in {'external', 'internal'}:
        raise ValueError('wrap mode must be external or internal')
    if mode == 'internal':
        if not fluid_seeds:
            raise WrapError('leak_detected', 'internal wrapping requires a fluid seed',
                            details={'breach_locations': []})
        oversized = [
            opening for opening in _boundary_openings(polydata)
            if opening['span'] > gap + sizing.voxel_size * 1e-6]
        if oversized:
            markers = [point for opening in oversized
                       for point in opening['locations']][:50]
            raise WrapError(
                'leak_detected',
                'source opening exceeds the configured maximum gap',
                details={'breach_locations': markers,
                         'opening_span': max(item['span'] for item in oversized),
                         'max_gap': gap})
    # Plan 36 RP5: the distance-occupancy block is shared with the fluid-space
    # field, which samples the same distance only near the surface.
    sampled = sampled_distance(polydata, model_bounds, dimensions)
    checkpoint('distance_field')
    occupancy = wall_occupancy(polydata, model_bounds, dimensions,
                               .75 * sizing.voxel_size, sampled=sampled)
    checkpoint('occupancy')
    stages.extend([
        {'stage': 'distance_field', 'status': 'done',
         'metrics': {'dimensions': dimensions}},
        {'stage': 'occupancy', 'status': 'done',
         'metrics': {'iso_distance': .75 * sizing.voxel_size}},
    ])
    radius = max(0, int(math.ceil(
        gap / (2 * sizing.voxel_size))))
    kernel = 2 * radius + 1
    dilate = vtkImageDilateErode3D()
    dilate.SetInputData(occupancy)
    dilate.SetKernelSize(kernel, kernel, kernel)
    dilate.SetDilateValue(1)
    dilate.SetErodeValue(0)
    dilate.Update()
    erode = vtkImageDilateErode3D()
    erode.SetInputConnection(dilate.GetOutputPort())
    erode.SetKernelSize(kernel, kernel, kernel)
    erode.SetDilateValue(0)
    erode.SetErodeValue(1)
    erode.Update()
    checkpoint('morphological_closing')
    stages.append({'stage': 'morphological_closing', 'status': 'done',
                   'metrics': {'maximum_gap': gap,
                               'radius_voxels': radius,
                               'kernel': kernel}})
    seeds = vtkPoints()
    if mode == 'external':
        for x in (model_bounds[0], model_bounds[1]):
            for y in (model_bounds[2], model_bounds[3]):
                for z in (model_bounds[4], model_bounds[5]):
                    seeds.InsertNextPoint(x, y, z)
    else:
        for seed in fluid_seeds:
            seeds.InsertNextPoint(*map(float, seed))
    classify = vtkImageThresholdConnectivity()
    classify.SetInputConnection(erode.GetOutputPort())
    classify.SetSeedPoints(seeds)
    classify.ThresholdBetween(0, 0)
    classify.SetInValue(1)
    classify.SetOutValue(0)
    classify.ReplaceInOn()
    classify.ReplaceOutOn()
    classify.Update()
    checkpoint('seeded_classification')
    breach_locations = _classification_breaches(
        classify.GetOutput()) if mode == 'internal' else []
    if breach_locations:
        # Padding contact proves the leak; source boundary samples provide the
        # actionable viewport hints at the actual opening instead of markers
        # on the artificial grid boundary.
        breach_locations = _boundary_samples(polydata) or breach_locations
        raise WrapError('leak_detected', 'internal fluid region reaches wrap padding',
                        details={'breach_locations': breach_locations})
    contour = vtkFlyingEdges3D()
    contour.SetInputConnection(classify.GetOutputPort())
    contour.SetValue(0, .5)
    contour.Update()
    triangles = vtkTriangleFilter()
    triangles.SetInputConnection(contour.GetOutputPort())
    triangles.Update()
    output = triangles.GetOutput()
    if output.GetNumberOfCells() == 0:
        raise WrapError('leak_detected', 'wrapper produced no closed skin',
                        details={'breach_locations': []})
    extracted_cells = int(output.GetNumberOfCells())
    connectivity = vtkPolyDataConnectivityFilter()
    connectivity.SetInputData(output)
    connectivity.SetExtractionModeToLargestRegion()
    connectivity.Update()
    checkpoint('surface_extraction')
    output = connectivity.GetOutput()
    if minimum_component_size and output.GetNumberOfCells() < int(minimum_component_size):
        raise WrapError('leak_detected',
                        'retained wrapped component is below minimum size',
                        details={'cells': int(output.GetNumberOfCells()),
                                 'minimum_component_size': int(minimum_component_size)})
    stages.extend([
        {'stage': 'seeded_classification', 'status': 'done',
         'metrics': {'regions_requested': list(regions_to_retain or ()),
                     'retention': 'largest_enclosing_region', 'mode': mode,
                     'seed_count': seeds.GetNumberOfPoints()}},
        {'stage': 'despeckle', 'status': 'done',
         'metrics': {'cells_dropped': extracted_cells - int(output.GetNumberOfCells())}},
        {'stage': 'surface_extraction', 'status': 'done',
         'metrics': {'cells': int(output.GetNumberOfCells())}},
    ])
    # Appendix A §5.2: default smoothing is 20 windowed-sinc iterations, with a
    # bounded retry ladder (20 → 10 → 0) that backs off if smoothing pushes the
    # skin past the deviation budget.
    smooth_iterations = max(0, min(40, round(20 * smoothing_strength)))
    smoothing_attempts = []
    unsmoothed = output
    for iterations in dict.fromkeys((smooth_iterations, min(10, smooth_iterations), 0)):
        candidate = unsmoothed
        if iterations:
            smooth = vtkWindowedSincPolyDataFilter()
            smooth.SetInputData(unsmoothed)
            smooth.SetNumberOfIterations(iterations)
            smooth.SetPassBand(.1)
            smooth.BoundarySmoothingOff()
            smooth.FeatureEdgeSmoothingOff()
            smooth.NormalizeCoordinatesOn()
            smooth.Update()
            candidate = smooth.GetOutput()
        measured = _deviation(polydata, candidate)
        smoothing_attempts.append({'iterations': iterations, 'deviation': measured['max']})
        if measured['max'] <= smoothing_budget:
            output = candidate
            break
        output = candidate
        checkpoint('smoothing_attempt')
    stages.append({'stage': 'deviation_bounded_smoothing', 'status': 'done',
                   'metrics': {'strength': smoothing_strength,
                               'attempts': smoothing_attempts}})
    checkpoint('deviation_bounded_smoothing')
    if decimation > 0:
        reducer = vtkQuadricDecimation()
        reducer.SetInputData(output)
        reducer.SetTargetReduction(min(.9, max(0.0, float(decimation))))
        reducer.VolumePreservationOn()
        reducer.Update()
        candidate = reducer.GetOutput()
        candidate_deviation = _deviation(polydata, candidate)
        if candidate_deviation['max'] <= smoothing_budget:
            output = candidate
    stages.append({'stage': 'decimation', 'status': 'done',
                   'metrics': {'target_reduction': decimation,
                               'cells': int(output.GetNumberOfCells())}})
    checkpoint('decimation')
    repaint = _repaint_patches(polydata, output, 2 * sizing.voxel_size)
    checkpoint('patch_repaint')
    stages.append({'stage': 'patch_repaint', 'status': 'done', 'metrics': repaint})
    if not is_watertight(output):
        breaches = _boundary_samples(output)
        raise WrapError('leak_detected', 'wrapped skin is not watertight',
                        details={'breach_locations': breaches})
    deviation = _deviation(polydata, output)
    new_skin_fraction = _new_skin_fraction(polydata, output, 2 * sizing.voxel_size)
    if max_deviation is not None and deviation['max'] > float(max_deviation):
        raise WrapError('deviation_exceeded', 'wrap exceeds the deviation budget',
                        details={'measured': deviation['max'],
                                 'permitted': float(max_deviation)})
    stages.append({'stage': 'deviation_measurement', 'status': 'done',
                   'metrics': deviation})
    diagnostics = assess(output).to_dict()
    stages.append({'stage': 'rediagnosis', 'status': 'done',
                   'metrics': {'readiness': diagnostics['readiness']}})
    report = {
        'experimental': True, 'backend': 'vtk_uniform_implicit',
        'estimate': grid_metrics, 'max_gap': gap,
        'smoothing_strength': smoothing_strength,
        'smoothing_iterations': smooth_iterations,
        'smoothing_deviation_budget': smoothing_budget,
        'minimum_component_size': int(minimum_component_size),
        'deviation': deviation,
        'new_skin_fraction': new_skin_fraction,
        'diagnostics_after': diagnostics, 'stages': stages,
        'patch_transfer': repaint,
        'limitations': [
            'Uniform resolution; memory grows with resolution cubed.',
            'No sharp-feature preservation; edges round at voxel resolution.',
        ],
    }
    return output, report


def _classification_breaches(image, limit: int = 50):
    """Return classified boundary voxels as physical coordinates (boundary slices only)."""
    from vtkmodules.util.numpy_support import vtk_to_numpy
    dims = image.GetDimensions()
    values = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(
        (dims[2], dims[1], dims[0]))
    origin, spacing = image.GetOrigin(), image.GetSpacing()
    indices = set()
    import numpy as np
    for axis, coordinate in ((0, 0), (0, dims[2] - 1),
                             (1, 0), (1, dims[1] - 1),
                             (2, 0), (2, dims[0] - 1)):
        plane = np.take(values, coordinate, axis=axis)
        for pair in np.argwhere(plane > .5):
            if axis == 0:
                z, y, x = coordinate, int(pair[0]), int(pair[1])
            elif axis == 1:
                z, y, x = int(pair[0]), coordinate, int(pair[1])
            else:
                z, y, x = int(pair[0]), int(pair[1]), coordinate
            indices.add((x, y, z))
            if len(indices) >= limit:
                break
        if len(indices) >= limit:
            break
    return [[origin[i] + index[i] * spacing[i] for i in range(3)]
            for index in indices]


def _boundary_openings(polydata) -> list[dict]:
    """Measure connected source-boundary loops before voxel quantisation.

    The second-largest non-zero bounding-box extent is a conservative opening
    width for a planar loop and the narrow dimension for a long slit.
    """
    from vtkmodules.vtkCommonCore import vtkIdList
    from vtkmodules.vtkFiltersCore import vtkFeatureEdges

    edges = vtkFeatureEdges()
    edges.SetInputData(polydata)
    edges.BoundaryEdgesOn()
    edges.NonManifoldEdgesOff()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.Update()
    boundary = edges.GetOutput()
    adjacency = {}
    ids = vtkIdList()
    for cell_id in range(boundary.GetNumberOfCells()):
        boundary.GetCellPoints(cell_id, ids)
        cell_ids = [ids.GetId(index) for index in range(ids.GetNumberOfIds())]
        for left, right in zip(cell_ids, cell_ids[1:]):
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
    openings, remaining = [], set(adjacency)
    while remaining:
        stack, component = [remaining.pop()], set()
        while stack:
            point_id = stack.pop()
            component.add(point_id)
            for neighbour in adjacency.get(point_id, ()):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    stack.append(neighbour)
        locations = [list(map(float, boundary.GetPoint(point_id)))
                     for point_id in sorted(component)]
        extents = [
            max(point[axis] for point in locations) -
            min(point[axis] for point in locations)
            for axis in range(3)]
        nonzero = sorted(value for value in extents if value > 1e-12)
        span = (nonzero[-2] if len(nonzero) >= 2
                else nonzero[-1] if nonzero else 0.0)
        openings.append({'span': span, 'locations': locations})
    return openings


def _repaint_patches(source, wrapped, radius: float):
    from vtkmodules.vtkCommonCore import vtkIntArray
    from vtkmodules.vtkCommonDataModel import vtkGenericCell, vtkStaticCellLocator
    array = source.GetCellData().GetArray('cadFaceId')
    if array is None:
        return {'method': 'nearest_projection', 'assigned': 0,
                'unassigned': wrapped.GetNumberOfCells(),
                'unassigned_fraction': 1.0, 'per_patch_cells': {},
                'per_patch_area': {}, 'source_patch_area': {},
                'per_patch_recovery_fraction': {},
                'normal_threshold_degrees': 60}
    locator = vtkStaticCellLocator()
    locator.SetDataSet(source)
    locator.BuildLocator()
    transferred = vtkIntArray()
    transferred.SetName('cadFaceId')
    counts, areas, unassigned, unassigned_area, total_area = {}, {}, 0, 0.0, 0.0
    source_areas = {}
    for source_id in range(source.GetNumberOfCells()):
        patch_id = str(int(array.GetTuple1(source_id)))
        source_areas[patch_id] = (
            source_areas.get(patch_id, 0.0) + _cell_area(source.GetCell(source_id)))
    closest = [0.0, 0.0, 0.0]
    cell = vtkGenericCell()
    cell_id, sub_id, distance2 = __import__('vtk').mutable(0), __import__('vtk').mutable(0), __import__('vtk').mutable(0.0)
    for output_id in range(wrapped.GetNumberOfCells()):
        target = wrapped.GetCell(output_id)
        center = [sum(wrapped.GetPoint(target.GetPointId(j))[i]
                      for j in range(target.GetNumberOfPoints())) / target.GetNumberOfPoints()
                  for i in range(3)]
        locator.FindClosestPoint(center, closest, cell, cell_id, sub_id, distance2)
        output_normal = _cell_normal(target)
        source_normal = _cell_normal(cell)
        # CAD/tessellated inputs may carry globally reversed winding.  Patch
        # identity depends on the local tangent plane, so compare the unsigned
        # normal angle while orientation diagnostics remain a separate gate.
        normal_agrees = (output_normal is not None and source_normal is not None and
                         abs(sum(a * b for a, b in zip(output_normal, source_normal))) >= .5)
        value = (int(array.GetTuple1(int(cell_id)))
                 if float(distance2) <= radius * radius and normal_agrees else -1)
        transferred.InsertNextValue(value)
        area = _cell_area(target)
        total_area += area
        if value < 0:
            unassigned += 1
            unassigned_area += area
        else:
            counts[str(value)] = counts.get(str(value), 0) + 1
            areas[str(value)] = areas.get(str(value), 0.0) + area
    wrapped.GetCellData().AddArray(transferred)
    total = max(1, wrapped.GetNumberOfCells())
    recovery = {
        patch_id: areas.get(patch_id, 0.0) / max(source_area, 1e-30)
        for patch_id, source_area in source_areas.items()}
    return {'method': 'nearest_projection', 'assigned': total - unassigned,
            'unassigned': unassigned, 'unassigned_fraction': unassigned / total,
            'unassigned_area_fraction': unassigned_area / max(total_area, 1e-30),
            'per_patch_cells': counts, 'per_patch_area': areas,
            'source_patch_area': source_areas,
            'per_patch_recovery_fraction': recovery,
            'normal_threshold_degrees': 60}


def _cell_normal(cell):
    if cell.GetNumberOfPoints() < 3:
        return None
    a, b, c = (cell.GetPoints().GetPoint(index) for index in range(3))
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    length = sum(value * value for value in cross) ** .5
    return tuple(value / length for value in cross) if length > 1e-30 else None


def _cell_area(cell):
    if cell.GetNumberOfPoints() < 3:
        return 0.0
    a, b, c = (cell.GetPoints().GetPoint(index) for index in range(3))
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    return .5 * sum(value * value for value in cross) ** .5


def _boundary_samples(polydata, limit: int = 50) -> list[list[float]]:
    from vtkmodules.vtkFiltersCore import vtkFeatureEdges
    edges = vtkFeatureEdges()
    edges.SetInputData(polydata)
    edges.BoundaryEdgesOn()
    edges.NonManifoldEdgesOff()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.Update()
    result = edges.GetOutput()
    return [list(map(float, result.GetPoint(index)))
            for index in range(min(limit, result.GetNumberOfPoints()))]


def _deviation(source, wrapped, limit: int = 200_000) -> dict:
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    distances = []
    for left, right in ((source, wrapped), (wrapped, source)):
        implicit = vtkImplicitPolyDataDistance()
        implicit.SetInput(right)
        step = max(1, left.GetNumberOfPoints() // limit)
        distances.extend(abs(float(implicit.EvaluateFunction(left.GetPoint(index))))
                         for index in range(0, left.GetNumberOfPoints(), step))
    distances.sort()
    if not distances:
        return {'max': 0.0, 'p95': 0.0, 'sample_count': 0,
                'method': 'sampled_two_sided', 'histogram': [0] * 10}
    maximum = distances[-1]
    bins = [0] * 10
    if maximum > 0:
        for value in distances:
            bins[min(9, int(10 * value / maximum))] += 1
    else:
        bins[0] = len(distances)
    return {'max': maximum, 'p95': distances[int(.95 * (len(distances) - 1))],
            'sample_count': len(distances), 'method': 'sampled_two_sided',
            'histogram': bins}


def _new_skin_fraction(source, wrapped, threshold: float, limit: int = 10_000) -> float:
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    implicit = vtkImplicitPolyDataDistance()
    implicit.SetInput(source)
    step = max(1, wrapped.GetNumberOfPoints() // limit)
    distances = [abs(float(implicit.EvaluateFunction(wrapped.GetPoint(index))))
                 for index in range(0, wrapped.GetNumberOfPoints(), step)]
    return (sum(value > threshold for value in distances) / len(distances)
            if distances else 0.0)
