"""The background mesh's box, drawn where a region seed is placed.

Plan 36 RP1. A region's seed only means something inside the box
``blockMesh`` builds, and Domain & Regions never showed that box (F4). This
draws it as a faint shell with solid edges: the faces are tinted just enough
to read as a volume without hiding the geometry inside, and the three walls
behind the model are drawn a little stronger than the three in front, so the
box reads as a room the geometry sits in rather than a cage over it.

Which walls are behind is decided on every render from the camera, by the
GPU: each wall is wound so its normal points out of the box, the front walls
are one actor with back faces culled and the back walls another with front
faces culled. A wall whose outward normal points away from the camera is a
back face, so it is drawn by the back-wall actor alone.
"""
from __future__ import annotations

#: The six walls' tint, and the stronger tint of the three behind the model.
FACE_OPACITY = 0.04
BACK_WALL_OPACITY = 0.08

#: Used when no theme is loaded (tests, early start-up).
_FALLBACK_ACCENT = '#3f8ae0'

#: Each wall as four corner indices, counter-clockwise seen from outside, so
#: its winding normal points out of the box. Corners are numbered as
#: ``hexPolyData`` numbers them: 0-3 the bottom (zmin) ring, 4-7 the top.
WALLS = (
    ('xMin', (0, 4, 7, 3)), ('xMax', (1, 2, 6, 5)),
    ('yMin', (0, 1, 5, 4)), ('yMax', (3, 7, 6, 2)),
    ('zMin', (0, 3, 2, 1)), ('zMax', (4, 5, 6, 7)),
)


def corners(bounds):
    xMin, xMax, yMin, yMax, zMin, zMax = (float(value) for value in bounds)
    return [(xMin, yMin, zMin), (xMax, yMin, zMin), (xMax, yMax, zMin),
            (xMin, yMax, zMin), (xMin, yMin, zMax), (xMax, yMin, zMax),
            (xMax, yMax, zMax), (xMin, yMax, zMax)]


def wallsPolyData(bounds):
    """The six walls as quads, each wound outward."""
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    points = vtkPoints()
    for corner in corners(bounds):
        points.InsertNextPoint(*corner)
    quads = vtkCellArray()
    for _name, wall in WALLS:
        quads.InsertNextCell(4, wall)
    polyData = vtkPolyData()
    polyData.SetPoints(points)
    polyData.SetPolys(quads)
    return polyData


def blocksPolyData(blocks):
    """RP13 #5. The outer walls of several blocks, each wound outward.

    *blocks* is a sequence of eight corners each, in blockMesh's order. A
    wall two blocks share is inside the domain, so it is left out: what is
    tinted is the domain's skin, not the joints between its blocks.
    """
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    def key(block, wall):
        return frozenset(tuple(round(value, 12) for value in block[index])
                         for index in wall)

    counts = {}
    for block in blocks:
        for _name, wall in WALLS:
            counts[key(block, wall)] = counts.get(key(block, wall), 0) + 1
    points = vtkPoints()
    quads = vtkCellArray()
    for block in blocks:
        base = points.GetNumberOfPoints()
        for corner in block:
            points.InsertNextPoint(*(float(value) for value in corner))
        for _name, wall in WALLS:
            if counts[key(block, wall)] > 1:
                continue
            quads.InsertNextCell(4, [base + index for index in wall])
    polyData = vtkPolyData()
    polyData.SetPoints(points)
    polyData.SetPolys(quads)
    return polyData


#: The twelve edges of a block, as corner index pairs.
_BLOCK_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7),
                (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def blockEdgesPolyData(blocks):
    """RP13 #5. Every block's twelve edges as lines, each drawn once."""
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    points = vtkPoints()
    lines = vtkCellArray()
    seen = set()
    for block in blocks:
        for first, second in _BLOCK_EDGES:
            a = tuple(float(value) for value in block[first])
            b = tuple(float(value) for value in block[second])
            edge = frozenset((tuple(round(v, 12) for v in a),
                              tuple(round(v, 12) for v in b)))
            if edge in seen:
                continue
            seen.add(edge)
            start = points.InsertNextPoint(*a)
            end = points.InsertNextPoint(*b)
            lines.InsertNextCell(2, [start, end])
    polyData = vtkPolyData()
    polyData.SetPoints(points)
    polyData.SetLines(lines)
    return polyData


def _accent() -> str:
    from foammesh.app import app

    try:
        tokens = app.themeManager.tokens if app.themeManager else None
        if tokens is not None:
            value = tokens.value('accent.default')
            if value:
                return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return _FALLBACK_ACCENT


def _wallActor(polyData, colour, opacity, *, backWalls: bool):
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

    mapper = vtkPolyDataMapper()
    mapper.SetInputData(polyData)
    actor = vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    prop.SetColor(*colour)
    prop.SetOpacity(opacity)
    prop.SetLighting(False)
    if backWalls:
        prop.FrontfaceCullingOn()
    else:
        prop.BackfaceCullingOn()
    actor.PickableOff()
    return actor


def domainBoxActor(bounds, colour: str | None = None, blocks=None):
    """A non-pickable ``vtkAssembly`` named ``domainBox`` spanning *bounds*.

    *bounds* is ``(xmin, xmax, ymin, ymax, zmin, zmax)``; *colour* a
    ``#rrggbb`` string, the theme's ``accent.default`` when left out.

    RP13 #5. *blocks*, when given, are the corners of each background block
    of a domain that is not a box (an L, a step): each block's outline is
    drawn instead of the hull, which would claim cells where there are none.
    """
    from vtkmodules.vtkRenderingCore import vtkAssembly

    from foammesh.rendering.vtk_loader import hexPolyData, polyDataToFeatureActor
    from foammesh.view.theming.vtk_theme import rgb

    tint = rgb(colour or _accent())
    walls = blocksPolyData(blocks) if blocks else wallsPolyData(bounds)
    front = _wallActor(walls, tint, FACE_OPACITY, backWalls=False)
    front.SetObjectName('domainBox:front')
    back = _wallActor(walls, tint, BACK_WALL_OPACITY, backWalls=True)
    back.SetObjectName('domainBox:back')

    xMin, xMax, yMin, yMax, zMin, zMax = (float(value) for value in bounds)
    if blocks:
        edges = _linesActor(blockEdgesPolyData(blocks))
    else:
        edges = polyDataToFeatureActor(
            hexPolyData((xMin, yMin, zMin), (xMax, yMax, zMax)))
    edges.SetObjectName('domainBox:edges')
    prop = edges.GetProperty()
    prop.SetRepresentationToWireframe()
    prop.SetColor(*tint)
    prop.SetLineWidth(1.5)
    prop.SetLighting(False)
    edges.PickableOff()

    box = vtkAssembly()
    for part in (back, front, edges):
        box.AddPart(part)
    box.SetObjectName('domainBox')
    # There is no Display Control row for the box, so a click on it must
    # still resolve to whatever surface is behind it.
    box.PickableOff()
    return box


def _linesActor(polyData):
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

    mapper = vtkPolyDataMapper()
    mapper.SetInputData(polyData)
    actor = vtkActor()
    actor.SetMapper(mapper)
    return actor


# -- Plan 36 RP4: the walls a seed's shadows fall on -------------------------- #

def cameraOctant(bounds, camera) -> tuple[int, int, int]:
    """Which side of the box's centre the camera looks from, per axis.

    ``+1`` on an axis means the camera is on the high side, so that axis's
    far wall is the low one. A parallel camera has no position that matters,
    only a direction, so it is read from where it looks instead.
    """
    if camera.GetParallelProjection():
        side = tuple(-value for value in camera.GetDirectionOfProjection())
    else:
        position = camera.GetPosition()
        side = tuple(position[axis]
                     - 0.5 * (float(bounds[2 * axis]) + float(bounds[2 * axis + 1]))
                     for axis in range(3))
    return tuple(1 if value >= 0 else -1 for value in side)


def backWalls(bounds, octant):
    """The three walls behind everything from *octant*: ``(axis, coordinate)``."""
    return tuple((axis, float(bounds[2 * axis] if octant[axis] > 0
                              else bounds[2 * axis + 1]))
                 for axis in range(3))
