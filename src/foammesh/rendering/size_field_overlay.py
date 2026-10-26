"""The size field and the Size conformity map, drawn through the overlay layout.

Plan 41 GN25c (section 4.8.2, 4.8.3; Plan 39 VI15). Before any mesh exists the
field is a colour map on the prepared surfaces, on a log scale, with the O5
feature edges as lines and the thin regions marked. After a mesh exists the
cells that are too coarse or too fine for the field are drawn as points
coloured by ``cell size / field size``. Everything goes in as a transient
actor (``addOverlay``) and a legend (``addLegend``), so the overlay layout
places the legend and Display Control lists nothing.
"""
from __future__ import annotations

import numpy as np
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

from foammesh.view.theming.vtk_theme import apply_scalar_bar_theme, rgb

FIELD_LEGEND_KEY = 'sizeField'
CONFORMITY_LEGEND_KEY = 'sizeConformity'
FIELD_TITLE = 'Size field (model length)'
CONFORMITY_TITLE = 'Size conformity (cell size / field)'

#: Cells at or above this ratio are drawn as too coarse, at or below the
#: reciprocal as too fine: the same 2 and 0.5 the counts use.
CONFORMITY_RANGE = (0.25, 4.0)


def _polydata(points: np.ndarray) -> vtkPolyData:
    data = vtkPolyData()
    vpoints = vtkPoints()
    vpoints.SetData(numpy_to_vtk(np.ascontiguousarray(points, dtype=float),
                                 deep=True))
    data.SetPoints(vpoints)
    return data


def _cells(connectivity: np.ndarray) -> vtkCellArray:
    connectivity = np.asarray(connectivity, dtype=np.int64)
    width = connectivity.shape[1]
    count = len(connectivity)
    flat = np.empty((count, width + 1), dtype=np.int64)
    flat[:, 0] = width
    flat[:, 1:] = connectivity
    array = vtkCellArray()
    array.SetCells(count, numpy_to_vtkIdTypeArray(flat.ravel(), deep=True))
    return array


def surface_data(arrays: dict) -> vtkPolyData | None:
    """The prepared surfaces with the field size at each node."""
    points = np.asarray(arrays['points'])
    triangles = np.asarray(arrays['triangles'])
    if not len(points) or not len(triangles):
        return None
    data = _polydata(points)
    data.SetPolys(_cells(triangles))
    sizes = numpy_to_vtk(np.ascontiguousarray(
        10.0 ** np.asarray(arrays['log_size'], dtype=float)), deep=True)
    sizes.SetName('field_size')
    data.GetPointData().SetScalars(sizes)
    return data


def lookup_table(low: float, high: float):
    """The standard scale on a log10 axis, red at the finest size."""
    from foammesh.support import colour_standard
    # Fine (small) is the red end, as on the standard scale: small is "worst".
    return colour_standard.lookup_table(
        'size_field', max(low, 1e-30), max(high, low * 1.0001, 1e-29),
        worst_is_high=False, log=True)


def _legend(table, title: str, key: str, tokens, labels=5):
    bar = vtkScalarBarActor()
    bar.SetObjectName(key + 'Legend')
    bar.SetLookupTable(table)
    bar.SetTitle(title)
    bar.SetNumberOfLabels(labels)
    bar.SetLabelFormat('%.3g')
    bar.UnconstrainedFontSizeOn()
    bar.SetTitleRatio(0.5)
    bar.SetBarRatio(0.25)
    bar.SetWidth(0.2)
    bar.SetHeight(0.45)
    if tokens is not None:
        apply_scalar_bar_theme(bar, tokens)
    return bar


def _line_actor(segments: np.ndarray, colour: str, name: str, width=3.0):
    segments = np.asarray(segments, dtype=float)
    if not len(segments):
        return None
    data = _polydata(segments.reshape(-1, 3))
    data.SetLines(_cells(np.arange(len(segments) * 2).reshape(-1, 2)))
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(data)
    mapper.ScalarVisibilityOff()
    mapper.SetResolveCoincidentTopologyToPolygonOffset()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.SetObjectName(name)
    prop = actor.GetProperty()
    prop.SetLighting(False)
    prop.SetLineWidth(width)
    prop.SetColor(*rgb(colour))
    actor.PickableOff()
    return actor


def _thin_actor(arrays: dict, colour: str):
    thin = np.asarray(arrays['thin']).astype(bool)
    triangles = np.asarray(arrays['triangles'])
    if not thin.any():
        return None
    data = _polydata(np.asarray(arrays['points']))
    data.SetPolys(_cells(triangles[thin]))
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(data)
    mapper.ScalarVisibilityOff()
    mapper.SetResolveCoincidentTopologyToPolygonOffset()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.SetObjectName('sizeFieldThinRegions')
    prop = actor.GetProperty()
    prop.SetLighting(False)
    prop.SetColor(*rgb(colour))
    prop.SetOpacity(0.9)
    actor.PickableOff()
    return actor


class SizeFieldOverlay:
    """Owns the actors and legends the size field puts in the viewport."""

    def __init__(self, display, tokens=None):
        self._display = display
        self._tokens = tokens
        self._actors: list = []
        self._legends: list = []
        self.field_actor = None

    def _token(self, role, fallback):
        try:
            return self._tokens.value(role) if self._tokens is not None \
                else fallback
        except Exception:                                    # noqa: BLE001
            return fallback

    # -- the field before a mesh exists ------------------------------------ #

    def show_field(self, arrays: dict, *, features=True, thin=True) -> dict:
        """Draw the field on the surfaces; returns what is on screen."""
        self.clear_field()
        data = surface_data(arrays)
        if data is None:
            return {'surface': False, 'features': 0, 'thin_faces': 0}
        sizes = 10.0 ** np.asarray(arrays['log_size'], dtype=float)
        table = lookup_table(float(sizes.min()), float(sizes.max()))
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.SetLookupTable(table)
        mapper.SetScalarRange(table.GetRange())
        mapper.SetScalarModeToUsePointData()
        mapper.ScalarVisibilityOn()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('sizeFieldSurface')
        actor.PickableOff()
        self.field_actor = actor
        self._add(actor)
        drawn = {'surface': True, 'features': 0, 'thin_faces': 0}
        if features:
            lines = _line_actor(arrays['features'],
                                self._token('foreground.primary', '#000000'),
                                'sizeFieldFeatures')
            if lines is not None:
                self._add(lines)
                drawn['features'] = int(len(arrays['features']))
        if thin:
            marked = _thin_actor(arrays,
                                 self._token('status.warning', '#c77700'))
            if marked is not None:
                self._add(marked)
                drawn['thin_faces'] = int(np.asarray(arrays['thin']).sum())
        bar = _legend(table, FIELD_TITLE, FIELD_LEGEND_KEY, self._tokens)
        self._legends.append(FIELD_LEGEND_KEY)
        self._display.addLegend(FIELD_LEGEND_KEY, bar)
        return drawn

    # -- one field, sampled where Gmsh asks for a size (Plan 48 GM8) -------- #

    def show_samples(self, points, sizes, *, title: str = FIELD_TITLE) -> int:
        """Points of one size field, coloured by its size on the shared scale.

        The same log scale and the same legend as the field on the surfaces
        (``show_field``), so one field and the whole background read alike.
        Returns the number of points drawn.
        """
        self.clear_field()
        points = np.asarray(points, dtype=float).reshape(-1, 3)
        sizes = np.asarray(sizes, dtype=float)
        keep = np.isfinite(sizes) & (sizes > 0)
        if len(points) != len(sizes) or not keep.any():
            return 0
        points, sizes = points[keep], sizes[keep]
        data = _polydata(points)
        data.SetVerts(_cells(np.arange(len(points)).reshape(-1, 1)))
        values = numpy_to_vtk(np.ascontiguousarray(sizes), deep=True)
        values.SetName('field_size')
        data.GetPointData().SetScalars(values)
        table = lookup_table(float(sizes.min()), float(sizes.max()))
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.SetLookupTable(table)
        mapper.SetScalarRange(table.GetRange())
        mapper.SetScalarModeToUsePointData()
        mapper.ScalarVisibilityOn()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('sizeFieldSamples')
        actor.GetProperty().SetPointSize(4.0)
        actor.GetProperty().SetLighting(False)
        actor.GetProperty().RenderPointsAsSpheresOn()
        actor.PickableOff()
        self.field_actor = actor
        self._add(actor)
        bar = _legend(table, title, FIELD_LEGEND_KEY, self._tokens)
        self._legends.append(FIELD_LEGEND_KEY)
        self._display.addLegend(FIELD_LEGEND_KEY, bar)
        return int(len(points))

    # -- the conformity of a mesh ------------------------------------------ #

    def show_conformity(self, centres: np.ndarray, ratio: np.ndarray) -> int:
        """Points at the cells outside the 0.5..2 band, coloured by ratio."""
        self.clear_conformity()
        centres = np.asarray(centres, dtype=float).reshape(-1, 3)
        ratio = np.asarray(ratio, dtype=float)
        keep = np.isfinite(ratio) & (ratio > 0)
        if not keep.any():
            return 0
        centres, ratio = centres[keep], ratio[keep]
        data = _polydata(centres)
        data.SetVerts(_cells(np.arange(len(centres)).reshape(-1, 1)))
        values = numpy_to_vtk(np.ascontiguousarray(ratio), deep=True)
        values.SetName('conformity')
        data.GetPointData().SetScalars(values)
        table = lookup_table(*CONFORMITY_RANGE)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.SetLookupTable(table)
        mapper.SetScalarRange(table.GetRange())
        mapper.ScalarVisibilityOn()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('sizeConformityCells')
        actor.GetProperty().SetPointSize(4.0)
        actor.GetProperty().SetLighting(False)
        actor.GetProperty().RenderPointsAsSpheresOn()
        self._add(actor)
        bar = _legend(table, CONFORMITY_TITLE, CONFORMITY_LEGEND_KEY,
                      self._tokens)
        self._legends.append(CONFORMITY_LEGEND_KEY)
        self._display.addLegend(CONFORMITY_LEGEND_KEY, bar)
        return int(len(centres))

    # -- bookkeeping --------------------------------------------------------- #

    def _add(self, actor) -> None:
        self._actors.append(actor)
        self._display.addOverlay(actor)

    def _clear(self, names, legend) -> None:
        for actor in [a for a in self._actors if a.GetObjectName() in names]:
            self._actors.remove(actor)
            self._display.removeOverlay(actor)
        if legend in self._legends:
            self._legends.remove(legend)
            self._display.removeLegend(legend)

    def clear_field(self) -> None:
        self._clear({'sizeFieldSurface', 'sizeFieldFeatures',
                     'sizeFieldThinRegions', 'sizeFieldSamples'},
                    FIELD_LEGEND_KEY)
        self.field_actor = None

    def clear_conformity(self) -> None:
        self._clear({'sizeConformityCells'}, CONFORMITY_LEGEND_KEY)

    def clear(self) -> None:
        self.clear_field()
        self.clear_conformity()

    def is_showing_field(self) -> bool:
        return self.field_actor is not None

    def names(self) -> list[str]:
        return [actor.GetObjectName() for actor in self._actors]
