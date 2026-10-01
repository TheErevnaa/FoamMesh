"""The stage comparison on screen (Plan 37 UF11).

Every stage was cut by the same world-space planes, so the sections lie on
top of each other; they are drawn side by side instead: the first where it
is, each next one moved along the plane by the widest section plus a gap
(the move is the same for every point of a section, so the shapes compare as
they are). Each copy carries its ``stage · revision`` label above it and all
are coloured by one mapping -- one legend for all.
"""
from __future__ import annotations

import numpy as np

from foammesh.rendering.section_colour_map import MapperColouring

#: The gap between two copies, as a fraction of the widest one.
SIDE_BY_SIDE_GAP_FRACTION = 0.15


def inPlaneAxis(normal) -> np.ndarray:
    """A unit vector in the plane of *normal* (the side-by-side direction)."""
    n = np.asarray(normal, dtype=float)
    n = n / (np.linalg.norm(n) or 1.0)
    for axis in ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)):
        u = np.cross(n, axis)
        if np.linalg.norm(u) > 0.5:
            return u / np.linalg.norm(u)
    return np.array([1.0, 0.0, 0.0])


def _extentAlong(polyData, axis) -> tuple[float, float]:
    from vtkmodules.util.numpy_support import vtk_to_numpy
    points = polyData.GetPoints() if polyData is not None else None
    if points is None or points.GetNumberOfPoints() == 0:
        return 0.0, 0.0
    along = vtk_to_numpy(points.GetData()) @ axis
    return float(along.min()), float(along.max())


class StageCompareView:
    """One actor and one label per compared stage."""

    def __init__(self):
        self._entries = []          # [(actor, label actor, polyData, text)]
        self._colouring = MapperColouring()
        self._offset = 0.0
        self._axis = np.array([1.0, 0.0, 0.0])

    def actors(self) -> list:
        return [prop for actor, label, _d, _t in self._entries
                for prop in (actor, label)]

    def labels(self) -> list[str]:
        return [text for _a, _l, _d, text in self._entries]

    def offsets(self) -> list[tuple]:
        """Each copy's move from where it was cut."""
        return [tuple(actor.GetPosition()) for actor, _l, _d, _t in
                self._entries]

    def spacing(self) -> float:
        return self._offset

    def polyData(self, index):
        return self._entries[index][2]

    def show(self, entries, normal, mapped=None) -> list:
        """*entries*: ``[(label, vtkPolyData)]`` in order. Returns the props
        added (the caller puts them in the renderer)."""
        from vtkmodules.vtkRenderingCore import (
            vtkActor, vtkBillboardTextActor3D, vtkPolyDataMapper)

        self.clear()
        axis = inPlaneAxis(normal)
        spans = [_extentAlong(data, axis) for _label, data in entries]
        widest = max((high - low for low, high in spans), default=0.0)
        step = widest * (1.0 + SIDE_BY_SIDE_GAP_FRACTION) or 1.0
        self._axis, self._offset = axis, step
        for index, (text, data) in enumerate(entries):
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(data)
            mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.SetObjectName('stageCompareSection')
            actor.PickableOff()
            actor.GetProperty().SetColor(0.72, 0.76, 0.81)
            actor.GetProperty().EdgeVisibilityOn()
            move = axis * step * index
            actor.SetPosition(*move)
            if mapped is not None and mapped.get('kind') not in (None,
                                                                 'none'):
                self._colouring.colour(mapper, mapped)
            label = vtkBillboardTextActor3D()
            label.SetInput(text)
            label.SetObjectName('stageCompareLabel')
            label.PickableOff()
            label.GetTextProperty().SetFontSize(14)
            label.GetTextProperty().SetJustificationToCentered()
            bounds = data.GetBounds() if data.GetNumberOfPoints() else (
                0, 0, 0, 0, 0, 0)
            centre = np.array([(bounds[0] + bounds[1]) / 2,
                               (bounds[2] + bounds[3]) / 2,
                               bounds[5]]) + move
            label.SetPosition(*centre)
            self._entries.append((actor, label, data, text))
        return self.actors()

    def clear(self) -> list:
        """Take every copy down; returns the props to remove."""
        props = self.actors()
        self._colouring.restore()
        self._entries = []
        return props
