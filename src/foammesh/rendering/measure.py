"""Measure tool: a distance, a cell's size, a face's area (Plan 39 VI17, I8).

The viewport's pick finds a cell and a point on it. This module turns those
into the numbers: the distance between two picked points, the size of a
picked cell (bounding diagonal, volume and, where the mesh records it, the
first-layer thickness), the area of a picked face. Lengths are shown in the
project's display unit (``core.display_unit``), the unit the section Offset
is typed in; areas and volumes in its square and cube.

Nothing here draws or picks; ``MeasureState`` keeps the clicks and the
readout lines and the viewport draws them.
"""
from __future__ import annotations

import math

from foammesh.core import display_unit

#: Optional cell-data array holding the thickness of a layer cell.
LAYER_THICKNESS_ARRAY = 'layerThickness'


def distance(p0, p1) -> float:
    """The straight-line distance between two points, in scene units."""
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(p0, p1)))


def _figures(value: float) -> str:
    """Four significant figures, trailing zeros kept: ``1.000``."""
    return f'{value:#.4g}'.rstrip('.') if value else '0'


def length_text(metres: float) -> str:
    return '{0} {1}'.format(_figures(display_unit.to_display(metres)),
                            display_unit.current())


def area_text(square_metres: float) -> str:
    scale = display_unit.metres_per_unit() ** 2
    return '{0} {1}²'.format(_figures(square_metres / scale),
                             display_unit.current())


def volume_text(cubic_metres: float) -> str:
    scale = display_unit.metres_per_unit() ** 3
    return '{0} {1}³'.format(_figures(cubic_metres / scale),
                             display_unit.current())


def _cell_measure(data, cell: int) -> tuple:
    """``(area, volume)`` of one cell from VTK; the one that does not apply is 0."""
    from vtkmodules.vtkCommonCore import vtkIdList
    from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid
    from vtkmodules.vtkFiltersVerdict import vtkCellSizeFilter

    ids = vtkIdList()
    data.GetCellPoints(int(cell), ids)
    grid = vtkUnstructuredGrid()
    grid.SetPoints(data.GetPoints())
    grid.InsertNextCell(data.GetCellType(int(cell)), ids)
    size = vtkCellSizeFilter()
    size.SetInputData(grid)
    size.ComputeAreaOn()
    size.ComputeVolumeOn()
    size.ComputeLengthOff()
    size.ComputeVertexCountOff()
    size.Update()
    out = size.GetOutput().GetCellData()
    area, volume = out.GetArray('Area'), out.GetArray('Volume')
    return (abs(area.GetValue(0)) if area is not None else 0.0,
            abs(volume.GetValue(0)) if volume is not None else 0.0)


def describe_cell(data, cell: int) -> dict:
    """What a pick of one cell measures.

    ``kind`` is ``'cell'`` for a solid cell and ``'face'`` for a surface cell.
    A cell has a bounding diagonal, a volume and, when recorded, a layer
    thickness; a face has an area.
    """
    if data is None or cell < 0 or cell >= data.GetNumberOfCells():
        return {}
    dimension = data.GetCell(int(cell)).GetCellDimension()
    area, volume = _cell_measure(data, cell)
    if dimension >= 3:
        bounds = data.GetCell(int(cell)).GetBounds()
        diagonal = math.sqrt(sum(
            (bounds[i + 1] - bounds[i]) ** 2 for i in (0, 2, 4)))
        out = {'kind': 'cell', 'diagonal': diagonal, 'volume': volume}
        layer = data.GetCellData().GetArray(LAYER_THICKNESS_ARRAY)
        if layer is not None and layer.GetValue(int(cell)) > 0:
            out['layer'] = float(layer.GetValue(int(cell)))
        return out
    return {'kind': 'face', 'area': area}


def cell_lines(info: dict) -> list:
    """The readout lines for ``describe_cell``'s answer."""
    if info.get('kind') == 'cell':
        lines = ['Cell size: ' + length_text(info['diagonal']) + ' diagonal',
                 'Cell volume: ' + volume_text(info['volume'])]
        if 'layer' in info:
            lines.append('First layer thickness: ' + length_text(info['layer']))
        return lines
    if info.get('kind') == 'face':
        return ['Face area: ' + area_text(info['area'])]
    return []


class MeasureState:
    """The clicks of one measurement and the lines the readout shows."""

    #: DP-2747. What the readout says when a click found nothing to measure.
    MISSED = 'That click is not on the model. Click on a face or an edge.'
    FIRST = 'Click a point on the model.'

    def __init__(self):
        self.active = False
        self.points: list = []
        self.info: dict = {}
        self.missed = False

    def setActive(self, on: bool) -> None:
        self.active = bool(on)
        if not self.active:
            self.clear()

    def clear(self) -> None:
        self.points = []
        self.info = {}
        self.missed = False

    def click(self, point, info: dict | None = None) -> None:
        """A pick: the third point starts a new pair."""
        if len(self.points) >= 2:
            self.points = []
        self.points.append(tuple(float(c) for c in point))
        self.info = info or {}
        self.missed = False

    def miss(self) -> None:
        """A click that found nothing: the points so far are kept."""
        self.missed = True

    def distance(self) -> float | None:
        return distance(*self.points) if len(self.points) == 2 else None

    def lines(self) -> list:
        lines = []
        if self.missed:
            lines.append(self.MISSED)
        value = self.distance()
        if value is not None:
            lines.append('Distance: ' + length_text(value))
        elif len(self.points) == 1:
            lines.append('Click a second point.')
        elif self.active and not self.missed:
            # DP-2747. Measure on with nothing picked said nothing at all.
            lines.append(self.FIRST)
        lines += cell_lines(self.info)
        return lines
