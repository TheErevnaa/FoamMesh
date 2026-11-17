"""Closed bodies that occupy the same space, found before the mesher is asked.

DP-6133. Each geometry is graded alone, so two closed solids whose shells
pass through each other (a cylinder standing in a channel with its caps flush
to the floor and roof) came out as two clean geometries, and the first anyone
heard of it was the volume mesher's "the surface mesh crosses itself".

Three arrangements have intersecting shells and only one of them is a fault:

* touching bodies (a conjugate pair sharing a wall) have intersecting
  *surfaces* but no common volume -- not a fault;
* a body strictly inside another has no intersection at all -- not a fault;
* bodies that share volume and whose shells cross or lie on each other are
  what no mesher can join -- the fault.

So the shell test only nominates a pair, and the pair is reported when a
point just inside one body is also inside the other.
"""
from __future__ import annotations

from pathlib import Path

#: How many surface samples each body offers the other.
_SAMPLES = 200

#: The step inward, as a share of the body's bounding-box diagonal.
_INWARD = 1e-3


def _boxes_meet(first, second) -> bool:
    return all(first[2 * axis] <= second[2 * axis + 1]
               and second[2 * axis] <= first[2 * axis + 1]
               for axis in range(3))


def _inside(polydata, points) -> list[bool]:
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints

    cloud = vtkPolyData()
    holder = vtkPoints()
    for point in points:
        holder.InsertNextPoint(*point)
    cloud.SetPoints(holder)
    selector = vtkSelectEnclosedPoints()
    selector.SetInputData(cloud)
    selector.SetSurfaceData(polydata)
    selector.SetTolerance(1e-9)
    selector.Update()
    return [bool(selector.IsInside(index)) for index in range(len(points))]


def _inward_samples(polydata) -> list[tuple]:
    """Points a hair inside *polydata*, taken on its own faces."""
    from vtkmodules.vtkFiltersCore import vtkPolyDataNormals, vtkTriangleFilter

    triangles = vtkTriangleFilter()
    triangles.SetInputData(polydata)
    triangles.Update()
    normals = vtkPolyDataNormals()
    normals.SetInputData(triangles.GetOutput())
    normals.ComputeCellNormalsOn()
    normals.ComputePointNormalsOff()
    normals.SplittingOff()
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOff()
    normals.Update()
    surface = normals.GetOutput()
    count = int(surface.GetNumberOfCells())
    if not count:
        return []
    low, high = surface.GetBounds()[0::2], surface.GetBounds()[1::2]
    step = _INWARD * sum((h - l) ** 2 for l, h in zip(low, high)) ** 0.5
    stride = max(1, count // _SAMPLES)
    cell_normals = surface.GetCellData().GetNormals()
    pairs = []
    for cell in range(0, count, stride):
        ids = surface.GetCell(cell).GetPointIds()
        corners = [surface.GetPoint(ids.GetId(i)) for i in range(3)]
        centre = tuple(sum(c[a] for c in corners) / 3.0 for a in range(3))
        normal = cell_normals.GetTuple3(cell)
        pairs.append((centre, normal))
    candidates = []
    for centre, normal in pairs:
        candidates.append(tuple(c - step * n for c, n in zip(centre, normal)))
        candidates.append(tuple(c + step * n for c, n in zip(centre, normal)))
    own = _inside(polydata, candidates)
    # Orientation is not trusted: the side that is inside the body itself is
    # the inward one, and a face with neither (a sliver) is left out.
    return [candidates[2 * i + (0 if own[2 * i] else 1)]
            for i in range(len(pairs))
            if own[2 * i] != own[2 * i + 1]]


def share_volume(first, second) -> bool:
    """True when closed surfaces *first* and *second* hold common volume."""
    if not _boxes_meet(first.GetBounds(), second.GetBounds()):
        return False
    for body, other in ((first, second), (second, first)):
        samples = _inward_samples(body)
        if samples and any(_inside(other, samples)):
            return True
    return False


def shells_cross(first, second) -> bool:
    from vtkmodules.vtkFiltersCore import vtkAppendPolyData

    from .diagnostics.checks import self_intersections

    both = vtkAppendPolyData()
    both.AddInputData(first)
    both.AddInputData(second)
    both.Update()
    finding = self_intersections(both.GetOutput())
    return bool(finding.evaluated and finding.count)


def overlapping_pairs(bodies) -> list[tuple[str, str]]:
    """``[(id, id)]`` of the bodies in ``{id: polydata}`` that overlap."""
    keys = sorted(bodies)
    found = []
    for index, one in enumerate(keys):
        for other in keys[index + 1:]:
            first, second = bodies[one], bodies[other]
            if not _boxes_meet(first.GetBounds(), second.GetBounds()):
                continue
            if shells_cross(first, second) and share_volume(first, second):
                found.append((one, other))
    return found


def entry_label(entry) -> str:
    return str(entry.get('name') or Path(str(entry.get('artifact', ''))).stem)
