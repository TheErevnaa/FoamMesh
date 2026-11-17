"""Plan 50 IP1 (DP-6223): a click goes through a face that is not drawn.

The base grid is drawn as its far walls: a wall turned towards the camera is
culled (``vtkProperty.FrontfaceCullingOn``), so the model inside the box is
what the user sees there. The viewport's pick is a CPU ray cast
(``vtkCellPicker``, DP-2713), and a ray cast knows nothing of culling: it
stopped on the culled near wall, so a click on the model named the wall, the
hover readout described a wall cell nobody could see, and Measure measured
to it.

:func:`past_culled_faces` takes a pick that landed on a culled face and casts
again from just behind it along the same ray, until it lands on something
drawn or runs out of ray. Nothing in it knows what the base grid is: any
prop with face culling on gets the same truth.
"""
from __future__ import annotations

import math

#: How many culled faces one pick looks past (a box has two along any ray;
#: an odd mesh a few more).
MAX_PASSES = 8
#: How far behind a culled hit the next cast starts, as a fraction of the
#: ray's length: past the face, short of anything a user could tell apart.
STEP = 1e-6


def _ray(renderer, x, y):
    """``(near, far)`` world points under display ``(x, y)``."""
    ends = []
    for depth in (0.0, 1.0):
        renderer.SetDisplayPoint(float(x), float(y), depth)
        renderer.DisplayToWorld()
        world = renderer.GetWorldPoint()
        w = world[3] if world[3] else 1.0
        ends.append(tuple(world[i] / w for i in range(3)))
    return ends[0], ends[1]


def _local(prop, direction):
    """*direction* (world) in the prop's own coordinates."""
    getMatrix = getattr(prop, 'GetMatrix', None)
    matrix = getMatrix() if callable(getMatrix) else None
    if matrix is None:
        return direction
    from vtkmodules.vtkCommonMath import vtkMatrix4x4
    inverse = vtkMatrix4x4()
    vtkMatrix4x4.Invert(matrix, inverse)
    local = inverse.MultiplyPoint((*direction, 0.0))
    return local[:3]


def hit_is_culled(picker, direction) -> bool:
    """Whether the face *picker* landed on is one its prop does not draw.

    *direction* is the ray's direction in world coordinates, from the camera
    into the scene. Only a 2-D cell can be culled; a line or a point never is.
    """
    prop = picker.GetProp3D() if hasattr(picker, 'GetProp3D') else None
    if prop is None:
        prop = picker.GetActor()
    getProperty = getattr(prop, 'GetProperty', None)
    prop_property = getProperty() if callable(getProperty) else None
    if prop_property is None:
        return False
    front = bool(prop_property.GetFrontfaceCulling())
    back = bool(prop_property.GetBackfaceCulling())
    if not (front or back):
        return False
    data = picker.GetDataSet()
    cell_id = int(picker.GetCellId())
    if data is None or cell_id < 0 or cell_id >= data.GetNumberOfCells():
        return False
    cell = data.GetCell(cell_id)
    if cell is None or cell.GetCellDimension() != 2:
        return False
    from vtkmodules.vtkCommonDataModel import vtkPolygon
    normal = [0.0, 0.0, 0.0]
    vtkPolygon.ComputeNormal(cell.GetPoints(), normal)
    along = sum(a * b for a, b in zip(normal, _local(prop, direction)))
    if along == 0.0 or not math.isfinite(along):
        return False
    facing = along < 0.0      # the face's front is towards the camera
    return (front and facing) or (back and not facing)


def past_culled_faces(picker, x, y, renderer, new_picker):
    """The pick at display ``(x, y)`` with culled faces looked through.

    *picker* has already picked there and hit something. Returns it when what
    it hit is drawn; otherwise the pick from just behind the culled face on
    the same ray (made by ``new_picker()``, set up like the first), or
    ``None`` when nothing drawn lies behind it.
    """
    try:
        near, far = _ray(renderer, x, y)
    except Exception:                                       # noqa: BLE001
        return picker
    direction = tuple(b - a for a, b in zip(near, far))
    length = math.sqrt(sum(value * value for value in direction))
    if not length or not math.isfinite(length):
        return picker
    unit = tuple(value / length for value in direction)
    for _ in range(MAX_PASSES):
        if not hit_is_culled(picker, unit):
            return picker
        position = picker.GetPickPosition()
        start = tuple(position[i] + unit[i] * length * STEP for i in range(3))
        following = new_picker()
        if not following.Pick3DPoint(start, far, renderer):
            return None
        picker = following
    return picker if not hit_is_culled(picker, unit) else None
