"""Is a region seed in the fluid? Asked fast enough to answer while it moves.

DP-818. A snappy region is a seed point, and snappy keeps the connected space
that contains it. MEASURED on ``annulus.stl`` (radii 0.06 and 0.1, z 0 to
0.5): a fluid seed typed as (0, 0, 0) sits in the empty core, the run meshed
the core instead of the annular gap, and nothing on screen had said the point
was in the hole.

``validate_fluid_seed`` answers the same question at launch, but it builds a
distance function and an enclosure filter for every point it is asked about,
which is too slow to call on each step of a drag. This builds both once per
geometry and then answers each point with one ray cast and one distance
evaluation.

The rules follow the launch gate so the two cannot disagree about an ordinary
case. A point is *inside* when the assembled surface encloses it, or when any
single closed component does -- the second reading is what keeps the inner
region of a nested multiregion case inside (DP-391's ``jacketed_pipe``, where
the assembly counts two crossings and calls the point outside). A point within
a hair of the surface is *on_surface*. A surface whose regions share a face
(DP-924's ``jacketed_pipe.stl``: fluid and jacket in one file, the inner wall
used by both) is read as the regions it bounds, each shell closed on its own,
as the host classifier reads it (DP-860). A surface with open edges has no
inside, so when neither the assembly, nor any component, nor any shared-face
region is closed the answer is *unknown*, and nothing is claimed.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

INSIDE = 'inside'
OUTSIDE = 'outside'
ON_SURFACE = 'on_surface'
UNKNOWN = 'unknown'

VERDICTS = (INSIDE, OUTSIDE, ON_SURFACE, UNKNOWN)


#: DP-924: the largest surface read as shared-face regions. The reading is
#: pure Python over every triangle; past this the seed is left unknown.
SHARED_FACE_TRIANGLE_CAP = 200_000


def _open_edge_count(polydata, *, non_manifold: bool = True) -> int:
    """Boundary plus non-manifold edges: zero for a watertight surface."""
    from vtkmodules.vtkFiltersCore import vtkFeatureEdges

    edges = vtkFeatureEdges()
    edges.SetInputData(polydata)
    edges.BoundaryEdgesOn()
    edges.SetNonManifoldEdges(bool(non_manifold))
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.Update()
    return int(edges.GetOutput().GetNumberOfCells())


def _shared_face_shells(cleaned) -> list:
    """DP-924: the closed shells a surface with shared faces bounds, or [].

    A multiregion STL such as ``jacketed_pipe.stl`` carries the wall the
    fluid and the jacket share in one file, so its edges there are used by
    four triangles and no single surface closes. DP-860 taught the host
    classifier to read it as the regions it bounds
    (`domain_topology.shared_face_regions`: each region's shell closes on its
    own); the seed is judged against those same shells, so a point in the
    fluid or the jacket is inside. A surface with a boundary edge -- a real
    hole -- is not tried.
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    from foammesh.core.geometry.domain_topology import shared_face_regions

    count = cleaned.GetNumberOfCells()
    if not 4 <= count <= SHARED_FACE_TRIANGLE_CAP:
        return []
    if _open_edge_count(cleaned, non_manifold=False):
        return []
    polys = cleaned.GetPolys()
    if polys is None or polys.GetNumberOfCells() != count:
        return []
    offsets = vtk_to_numpy(polys.GetOffsetsArray())
    if offsets.size != count + 1 or (offsets[1:] - offsets[:-1] != 3).any():
        return []
    triangles = vtk_to_numpy(polys.GetConnectivityArray()).reshape(-1, 3)
    coordinates = [tuple(point) for point in
                   vtk_to_numpy(cleaned.GetPoints().GetData()).tolist()]
    found = shared_face_regions([tuple(face) for face in triangles.tolist()],
                                coordinates)
    if not found:
        return []
    shells = []
    for region in found[0]:
        cells = vtkCellArray()
        for face in region:
            cells.InsertNextCell(3, face)
        shell = vtkPolyData()
        points = vtkPoints()
        points.DeepCopy(cleaned.GetPoints())
        shell.SetPoints(points)
        shell.SetPolys(cells)
        shells.append(shell)
    return shells


def _cleaned(polydata):
    """Triangles with coincident points merged, so a split STL closes up."""
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData, vtkTriangleFilter

    triangles = vtkTriangleFilter()
    triangles.SetInputData(polydata)
    clean = vtkCleanPolyData()
    clean.SetInputConnection(triangles.GetOutputPort())
    clean.Update()
    return clean.GetOutput()


def _assembled(surfaces: Sequence):
    from vtkmodules.vtkFiltersCore import vtkAppendPolyData

    if len(surfaces) == 1:
        return _cleaned(surfaces[0])
    append = vtkAppendPolyData()
    for surface in surfaces:
        append.AddInputData(surface)
    append.Update()
    return _cleaned(append.GetOutput())


class SeedClassifier:
    """Answer inside / outside / on_surface / unknown for any point, cheaply.

    *components* is one surface per closed part the user thinks of as a
    solid -- an imported file, a modelled volume. Several patches of one
    part are passed already appended; the assembly of every component is
    built here.
    """

    def __init__(self, components: Iterable):
        from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
        from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints

        surfaces = [surface for surface in components
                    if surface is not None and surface.GetNumberOfCells() > 0]
        self._testers = []
        self._distance = None
        self._tolerance = 0.0
        if not surfaces:
            return

        assembled = _assembled(surfaces)
        bounds = assembled.GetBounds()
        diagonal = math.sqrt(sum((bounds[axis * 2 + 1] - bounds[axis * 2]) ** 2
                                 for axis in range(3)))
        # The launch gate's tolerance, so "on the surface" means the same
        # distance here as it does when the run is refused for it.
        self._tolerance = max(1e-12, diagonal * 1e-8)

        closed, unclosed = [], []
        if _open_edge_count(assembled) == 0:
            closed.append(assembled)
        else:
            unclosed.append(assembled)
        if len(surfaces) > 1:
            for surface in surfaces:
                cleaned = _cleaned(surface)
                if not cleaned.GetNumberOfCells():
                    continue
                if _open_edge_count(cleaned) == 0:
                    closed.append(cleaned)
                else:
                    unclosed.append(cleaned)
        # DP-924: a surface whose regions share a face closes region by
        # region; each region's shell is judged like any closed component.
        for surface in unclosed:
            closed.extend(_shared_face_shells(surface))

        for surface in closed:
            tester = vtkSelectEnclosedPoints()
            tester.SetTolerance(self._tolerance)
            tester.Initialize(surface)
            # The tester keeps its own locator over the surface; holding the
            # surface as well keeps it alive for as long as the tester is.
            self._testers.append((tester, surface))

        self._distance = vtkImplicitPolyDataDistance()
        self._distance.SetInput(assembled)
        self._assembled = assembled

    @property
    def judges(self) -> bool:
        """Whether any closed surface was found to judge a point against."""
        return bool(self._testers)

    def classify(self, point) -> str:
        """One of :data:`VERDICTS` for *point* (three coordinates)."""
        try:
            x, y, z = (float(value) for value in point)
        except (TypeError, ValueError):
            return UNKNOWN
        if not all(math.isfinite(value) for value in (x, y, z)):
            return UNKNOWN
        if self._distance is None:
            return UNKNOWN
        if abs(float(self._distance.EvaluateFunction((x, y, z)))) \
                <= self._tolerance:
            return ON_SURFACE
        if not self._testers:
            return UNKNOWN
        for tester, _surface in self._testers:
            if tester.IsInsideSurface(x, y, z):
                return INSIDE
        return OUTSIDE


def seed_components(surfaces, geometries=None, *, bounding_hex6=None):
    """The closed parts a seed is judged against, with a cache key.

    Plan 36 RP5. The one surface choice shared by the viewport's classifier
    (``GeometryManager._seedComponents``) and the headless fluid-space field,
    so both judge the same parts. *surfaces* maps a geometry id to its
    polydata; *geometries* maps a geometry id to its db element (anything
    with ``value(key)``), and may be empty for a bare list of surfaces.

    Surfaces are grouped by the volume they belong to, so a modelled solid or
    an imported file counts as one part. Left out: the bounding box (its
    inside is the whole domain, not the fluid), open planes, disks and plates,
    and refinement-only surfaces unless nothing else is left.

    Returns ``(components, key)``: one polydata per part, in group order, and
    a tuple that changes whenever a chosen surface is added, removed or
    modified.
    """
    from foammesh.db.configurations_schema import CFDType, Shape

    open_shapes = (Shape.PLANE.value, Shape.DISK.value, Shape.PLATE.value)
    geometries = geometries or {}

    def value(element, key):
        if element is None:
            return None
        try:
            return element.value(key)
        except (KeyError, LookupError, TypeError, AttributeError):
            return None

    groups, refinement = {}, {}
    for gId, data in surfaces.items():
        if data is None or data.GetNumberOfCells() <= 0:
            continue
        geometry = geometries.get(gId)
        parentId = value(geometry, 'volume')
        parent = geometries.get(parentId) if parentId is not None else None
        if bounding_hex6 is not None and str(parentId) == str(bounding_hex6):
            continue
        if (value(geometry, 'shape') in open_shapes
                or value(parent, 'shape') in open_shapes):
            continue
        group = str(parentId) if parentId is not None else str(gId)
        none = CFDType.NONE.value
        target = (refinement if none in (value(geometry, 'cfdType'),
                                          value(parent, 'cfdType'))
                  else groups)
        target.setdefault(group, []).append((str(gId), data))
    chosen = groups or refinement
    key = tuple(sorted(
        (group, gId, id(data), int(data.GetMTime()))
        for group, members in chosen.items() for gId, data in members))
    components = []
    for group in sorted(chosen):
        members = [data for _gId, data in chosen[group]]
        if len(members) == 1:
            components.append(members[0])
            continue
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData
        append = vtkAppendPolyData()
        for data in members:
            append.AddInputData(data)
        append.Update()
        components.append(append.GetOutput())
    return components, key
