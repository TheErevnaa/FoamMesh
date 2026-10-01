"""What checkMesh flagged, drawn over the mesh it flagged.

Plan 37 UF18. A check's point sets (``unusedPoints``, ``shortEdges``,
``nearPoints``, ...) are drawn as points and its face and cell sets as the
surfaces checkMesh wrote for them -- never one as the other -- and each
carries a label with its check's name, anchored in the world like the region
labels (``rendering/region_labels.py``) so it follows the camera and reads on
every theme.

The geometry comes from the mesh worker (``quality.check_highlights``), which
has already held it to the budgets of ``core.quality.check_artifacts``. The
layer holds it to them again: a highlight is drawn in the window's process,
and a payload that was not built by that worker must not be able to put an
unbounded actor into the viewport. The layer is keyed by the check revision
it was built from: a highlight from another revision clears what was shown,
so two runs' sets are never on screen together.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FONT_SIZE = 12
POINT_SIZE = 9.0
SURFACE_OPACITY = 0.85

_FALLBACK_TEXT = '#e8ebef'
_FALLBACK_BACKGROUND = '#1f2328'
_FALLBACK_POINTS = '#f0b400'
_FALLBACK_SURFACE = '#e5534b'
_BACKGROUND_OPACITY = 0.75


def label_text(highlight: dict) -> str:
    """``nonOrthoFaces · 760 faces`` / ``unusedPoints · 1 point``."""
    name = str(highlight.get('check') or highlight.get('name') or 'check')
    if highlight.get('kind') == 'surface':
        count = int(highlight.get('polygon_count') or 0)
        noun = 'face' if count == 1 else 'faces'
    else:
        count = int(highlight.get('point_count') or 0)
        noun = 'point' if count == 1 else 'points'
    return f'{name} · {count:,} {noun}'


def _token(name: str, fallback: str) -> str:
    try:
        from foammesh.app import app

        tokens = app.themeManager.tokens if app.themeManager else None
        value = tokens.values.get(name) if tokens is not None else None
        if value:
            return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return fallback


def _rgb(name: str, fallback: str):
    try:
        from foammesh.view.theming.vtk_theme import rgb

        return rgb(_token(name, fallback))
    except Exception:                                         # noqa: BLE001
        value = fallback.lstrip('#')
        return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class HighlightOverBudget(ValueError):
    """A highlight larger than the viewport budget was handed to the layer."""

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name
        self.reason = 'over_budget_geometry'


def _budget_check(highlight: dict, points, polygons) -> None:
    from foammesh.core.quality.check_artifacts import (
        MAX_HIGHLIGHT_POINTS, MAX_HIGHLIGHT_POLYGONS,
    )

    name = str(highlight.get('name'))
    if len(points) > MAX_HIGHLIGHT_POINTS:
        raise HighlightOverBudget(
            name, f'{name}: {len(points):,} points is over the '
                  f'{MAX_HIGHLIGHT_POINTS:,}-point highlight budget')
    count = int(highlight.get('polygon_count') or 0)
    if count > MAX_HIGHLIGHT_POLYGONS:
        raise HighlightOverBudget(
            name, f'{name}: {count:,} faces is over the '
                  f'{MAX_HIGHLIGHT_POLYGONS:,}-face highlight budget')


def highlight_actors(highlight: dict, values: dict) -> list:
    """``[geometry actor, label actor]`` for one worker highlight."""
    import numpy as np
    from vtkmodules.util.numpy_support import (
        numpy_to_vtk, numpy_to_vtkIdTypeArray,
    )
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkRenderingCore import (
        vtkActor, vtkPolyDataMapper, vtkTextActor,
    )

    points = np.ascontiguousarray(
        np.asarray(values[highlight['points_key']], dtype=np.float64)
        .reshape(-1, 3))
    polygons = None
    if highlight.get('kind') == 'surface':
        polygons = np.asarray(values[highlight['polygons_key']],
                              dtype=np.int64)
    _budget_check(highlight, points, polygons)

    vtk_points = vtkPoints()
    vtk_points.SetData(numpy_to_vtk(points, deep=True))
    data = vtkPolyData()
    data.SetPoints(vtk_points)
    cells = vtkCellArray()
    if polygons is not None:
        cells.SetCells(int(highlight.get('polygon_count') or 0),
                       numpy_to_vtkIdTypeArray(
                           np.ascontiguousarray(polygons), deep=True))
        data.SetPolys(cells)
    else:
        count = len(points)
        legacy = np.empty(2 * count, dtype=np.int64)
        legacy[0::2] = 1
        legacy[1::2] = np.arange(count)
        cells.SetCells(count, numpy_to_vtkIdTypeArray(legacy, deep=True))
        data.SetVerts(cells)

    mapper = vtkPolyDataMapper()
    mapper.SetInputData(data)
    mapper.ScalarVisibilityOff()
    # Drawn on the mesh's own faces: pull it forward so it is not z-fought.
    mapper.SetResolveCoincidentTopologyToPolygonOffset()
    actor = vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    if polygons is not None:
        prop.SetColor(*_rgb('status.error', _FALLBACK_SURFACE))
        prop.SetOpacity(SURFACE_OPACITY)
        prop.EdgeVisibilityOn()
        prop.SetEdgeColor(*_rgb('status.error', _FALLBACK_SURFACE))
        actor.SetObjectName('checkHighlightSurface')
    else:
        prop.SetColor(*_rgb('status.warning', _FALLBACK_POINTS))
        prop.SetPointSize(POINT_SIZE)
        prop.SetRenderPointsAsSpheres(True)
        actor.SetObjectName('checkHighlightPoints')
    actor.PickableOff()

    label = vtkTextActor()
    label.SetInput(label_text(highlight))
    anchor = label.GetPositionCoordinate()
    anchor.SetCoordinateSystemToWorld()
    anchor.SetValue(*(points.mean(axis=0) if len(points)
                      else (0.0, 0.0, 0.0)))
    label.SetObjectName('checkHighlightLabel')
    text = _rgb('tooltip.foreground', _FALLBACK_TEXT)
    style = label.GetTextProperty()
    style.SetFontSize(FONT_SIZE)
    style.SetColor(*text)
    style.SetBackgroundColor(*_rgb('tooltip.background',
                                   _FALLBACK_BACKGROUND))
    style.SetBackgroundOpacity(_BACKGROUND_OPACITY)
    style.SetFrame(True)
    style.SetFrameColor(*text)
    style.SetJustificationToCentered()
    style.SetVerticalJustificationToBottom()
    label.PickableOff()
    return [actor, label]


@dataclass
class CheckHighlightLayer:
    """The check highlights one viewport shows, for one check revision.

    *add* and *remove* put an actor into and take it out of the viewport --
    ``DisplayControl.addOverlay`` / ``removeOverlay`` in the window, which
    keeps them out of the Display Control list.
    """

    add: object
    remove: object
    revision: str = ''
    _shown: dict = field(default_factory=dict)

    def shown(self) -> tuple[str, ...]:
        return tuple(self._shown)

    def show(self, result: dict) -> list[dict]:
        """Draw the worker's highlights; return what could not be drawn.

        A result of another revision replaces everything shown: the sets of
        two checks are never drawn over one another.
        """
        revision = str(result.get('revision') or '')
        if revision != self.revision:
            self.clear()
            self.revision = revision
        refused = [dict(item) for item in result.get('refused') or ()]
        values = result.get('values') or {}
        for highlight in result.get('highlights') or ():
            name = str(highlight.get('name'))
            try:
                actors = highlight_actors(highlight, values)
            except HighlightOverBudget as refusal:
                refused.append({'name': name, 'reason': refusal.reason,
                                'message': str(refusal)})
                continue
            except (KeyError, ValueError, TypeError) as error:
                refused.append({'name': name, 'reason': 'unreadable',
                                'message': str(error)})
                continue
            self.hide(name)
            for actor in actors:
                self.add(actor)
            self._shown[name] = actors
        return refused

    def hide(self, name: str) -> None:
        for actor in self._shown.pop(str(name), ()):
            self.remove(actor)

    def clear(self) -> None:
        for name in list(self._shown):
            self.hide(name)
