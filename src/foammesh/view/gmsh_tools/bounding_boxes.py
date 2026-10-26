"""Bounding boxes of the model's entities (Plan 48 GM11b; Gmsh Alt+B).

Gmsh draws a box around every geometry entity (``Geometry.BoundingBoxes``)
and resets the view to the boxes of the visible ones (Alt+Shift+1).  The
boxes are taken from ``geometry.vtp`` -- the cells carry ``gmshEntityDim`` and
``gmshEntityTag`` -- so they are the entities' own extents, and the layer is
a line actor on the viewport that never touches the model.

The functions are plain numpy / vtk with no window; :class:`BoundingBoxLayer`
needs a view with ``addActor``, ``removeActor`` and ``refresh``.
"""
from __future__ import annotations

import numpy as np

#: The twelve edges of a box, as pairs of corner indices (corner bit order is
#: x = 1, y = 2, z = 4).
_EDGES = ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7),
          (0, 4), (1, 5), (2, 6), (3, 7))

ACTOR_NAME = 'gmshBoundingBoxes'


def entity_boxes(polydata) -> dict:
    """``{(dim, tag): (xmin, xmax, ymin, ymax, zmin, zmax)}`` of a model.

    *polydata* is the ``vtkPolyData`` of ``geometry.vtp``.  A model without
    the two entity arrays has no boxes (an empty dict).
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy

    if polydata is None:
        return {}
    cell_data = polydata.GetCellData()
    dims = cell_data.GetArray('gmshEntityDim')
    tags = cell_data.GetArray('gmshEntityTag')
    if dims is None or tags is None:
        return {}
    dims = vtk_to_numpy(dims).astype(np.int64)
    tags = vtk_to_numpy(tags).astype(np.int64)
    points_object = polydata.GetPoints()
    if points_object is None:
        return {}
    points = vtk_to_numpy(points_object.GetData()).astype(float)
    low = np.empty((len(dims), 3))
    high = np.empty((len(dims), 3))
    at = 0
    # Cell data follows the order verts, lines, polys, strips.
    for cells in (polydata.GetVerts(), polydata.GetLines(),
                  polydata.GetPolys(), polydata.GetStrips()):
        count = cells.GetNumberOfCells()
        if not count:
            continue
        offsets = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
        connect = vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
        where = points[connect]
        starts = offsets[:-1]
        low[at:at + count] = np.minimum.reduceat(where, starts, axis=0)
        high[at:at + count] = np.maximum.reduceat(where, starts, axis=0)
        at += count
    if at != len(dims):
        return {}
    boxes: dict = {}
    keys = dims * 1_000_000_007 + tags
    for key in np.unique(keys):
        mask = keys == key
        lo = low[mask].min(axis=0)
        hi = high[mask].max(axis=0)
        dim, tag = int(dims[mask][0]), int(tags[mask][0])
        boxes[(dim, tag)] = (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
    return boxes


def union(boxes) -> tuple | None:
    """The box around every box in *boxes*, or ``None`` when there are none."""
    boxes = list(boxes)
    if not boxes:
        return None
    array = np.asarray(boxes, dtype=float)
    return (array[:, 0].min(), array[:, 1].max(), array[:, 2].min(),
            array[:, 3].max(), array[:, 4].min(), array[:, 5].max())


def visible_bounds(boxes: dict, hidden=()) -> tuple | None:
    """The box around the entities that are not hidden (Alt+Shift+1)."""
    hidden = {tuple(h) for h in hidden}
    return union(box for key, box in boxes.items() if key not in hidden)


def box_lines(boxes) -> tuple:
    """``(points, lines)`` of the twelve edges of every box, for one actor."""
    points = []
    lines = []
    for box in boxes:
        x0, x1, y0, y1, z0, z1 = box
        base = len(points)
        for corner in range(8):
            points.append((x1 if corner & 1 else x0,
                           y1 if corner & 2 else y0,
                           z1 if corner & 4 else z0))
        lines.extend((base + a, base + b) for a, b in _EDGES)
    return points, lines


class BoundingBoxLayer:
    """The boxes in the viewport, shown and hidden as one.

    *view* has ``addActor``, ``removeActor`` and ``refresh``; *polydata* is a
    callable returning the model's ``vtkPolyData`` (or ``None``) and *hidden*
    a callable returning the hidden entities, both read when the boxes are
    drawn so that they follow the model on screen.
    """

    def __init__(self, view_provider, polydata, hidden=lambda: ()):
        self._view = view_provider
        self._polydata = polydata
        self._hidden = hidden
        self._actor = None
        self._shown = False
        self._boxes: dict = {}

    def shown(self) -> bool:
        return self._shown

    def boxes(self) -> dict:
        return dict(self._boxes)

    def count(self) -> int:
        return len(self._boxes) if self._shown else 0

    def setShown(self, shown: bool) -> bool:
        """Show or hide the boxes; returns whether they are shown."""
        self._remove()
        self._shown = bool(shown)
        if self._shown:
            self.refresh()
        else:
            self._refreshView()
        return self._shown

    def toggle(self) -> bool:
        return self.setShown(not self._shown)

    def refresh(self) -> int:
        """Redraw for the model on screen; the number of boxes drawn."""
        self._remove()
        self._boxes = entity_boxes(self._polydata())
        if not self._shown or not self._boxes:
            self._refreshView()
            return 0
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        points, lines = box_lines(self._boxes.values())
        vtk_points = vtkPoints()
        for point in points:
            vtk_points.InsertNextPoint(*point)
        cells = vtkCellArray()
        for a, b in lines:
            cells.InsertNextCell(2)
            cells.InsertCellPoint(a)
            cells.InsertCellPoint(b)
        data = vtkPolyData()
        data.SetPoints(vtk_points)
        data.SetLines(cells)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName(ACTOR_NAME)
        actor.GetProperty().SetColor(0.55, 0.55, 0.60)
        actor.GetProperty().SetLineWidth(1.0)
        actor.GetProperty().SetLighting(False)
        actor.PickableOff()
        view = self._view()
        if view is not None:
            view.addActor(actor)
        self._actor = actor
        self._refreshView()
        return len(self._boxes)

    def visibleBounds(self) -> tuple | None:
        """Alt+Shift+1: the box around what is visible now."""
        boxes = self._boxes or entity_boxes(self._polydata())
        return visible_bounds(boxes, self._hidden())

    def resetToVisible(self) -> tuple | None:
        """Fit the view to the visible entities; the bounds used, or None."""
        bounds = self.visibleBounds()
        view = self._view()
        if bounds is None or view is None:
            return None
        fly = getattr(view, 'flyToBounds', None)
        if callable(fly):
            fly(bounds)
        return bounds

    def _remove(self) -> None:
        if self._actor is not None:
            view = self._view()
            if view is not None:
                view.removeActor(self._actor)
            self._actor = None

    def _refreshView(self) -> None:
        view = self._view()
        if view is not None:
            view.refresh()
