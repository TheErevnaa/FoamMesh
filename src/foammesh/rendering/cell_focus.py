"""Go to one cell, or one set of cells, and show which one it is.

Plan 39 VI8 (F5b). A ranked worst-cell list is a list a user can read and not
act on until clicking a row takes the camera there. This module is that move:
the camera dollies to the cell's centroid at twenty cell diameters, the cell is
drawn as an outlined highlight (its own faces when the volume is loaded, a box
of the cell's size when it is not), and the move is a step in the view history
so Back returns to where the user was.

It touches the scene through the viewport widget only -- an actor added and
removed like the quality legend -- so it works the same over a boundary-only
preview, a loaded volume and a native ``.msh``.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

#: How far the camera stands off a cell, in cell diameters (Plan 39 §4.4).
DOLLY_DIAMETERS = 20.0
#: When the cell has no size (a Gmsh element row carries none), the camera
#: stands off by this fraction of the model's largest dimension instead.
FALLBACK_EXTENT_FRACTION = 0.05
_WARNING = '#ffb300'


class CellFocus:
    """The camera move and the highlight, for one view."""

    def __init__(self, view):
        self._view = view
        self._actor = None
        self._focused = None

    # -- state ------------------------------------------------------------- #

    def isActive(self) -> bool:
        return self._actor is not None

    def focused(self):
        """The row last flown to, or None."""
        return None if self._focused is None else dict(self._focused)

    def bounds(self):
        """The bounds of what the highlight draws, or None."""
        return None if self._actor is None else tuple(self._actor.GetBounds())

    # -- moves ------------------------------------------------------------- #

    def flyToCell(self, centroid, size: float = 0.0, *, dataSet=None,
                  cell: int | None = None) -> bool:
        """Go to ``centroid`` and outline the cell there. False if it cannot."""
        try:
            centre = tuple(float(value) for value in centroid)
        except (TypeError, ValueError):
            return False
        if len(centre) != 3 or not all(math.isfinite(v) for v in centre):
            return False
        size = float(size or 0.0)
        if not (size > 0.0) or not math.isfinite(size):
            extent = 0.0
            reader = getattr(self._view, 'modelExtent', None)
            if reader is not None:
                try:
                    extent = float(reader())
                except Exception:                                  # noqa: BLE001
                    extent = 0.0
            size = extent * FALLBACK_EXTENT_FRACTION / DOLLY_DIAMETERS
        if not (size > 0.0):
            return False

        self._outline(centre, size, dataSet, cell)
        moved = self._view.flyToPoint(centre, DOLLY_DIAMETERS * size)
        self._view.refresh()
        self._focused = {'centroid': list(centre), 'size': size,
                         'cell': cell} if moved else None
        return bool(moved)

    def flyToBounds(self, bounds) -> bool:
        """Frame a set of cells by its bounds, Back-able."""
        moved = self._view.flyToBounds(bounds)
        self._view.refresh()
        return bool(moved)

    def clear(self) -> None:
        """Take the highlight off. The camera stays where the user put it."""
        if self._actor is not None:
            try:
                self._view.removeActor(self._actor)
            except Exception:                                      # noqa: BLE001
                logger.debug('cell highlight already gone', exc_info=True)
            self._actor = None
        self._focused = None

    # -- the highlight ----------------------------------------------------- #

    def _outline(self, centre, size, dataSet, cell) -> None:
        self.clear()
        from vtkmodules.vtkRenderingCore import vtkActor, vtkDataSetMapper

        source = None
        if dataSet is not None and cell is not None:
            try:
                from foammesh.core.quality import extract_selected_cells

                source = extract_selected_cells(dataSet, [int(cell)])
                if source is None or source.GetNumberOfCells() < 1:
                    source = None
            except Exception:                                      # noqa: BLE001
                logger.debug('could not extract cell %s', cell, exc_info=True)
                source = None
        if source is None:
            from vtkmodules.vtkFiltersSources import vtkCubeSource

            box = vtkCubeSource()
            box.SetCenter(*centre)
            box.SetXLength(size)
            box.SetYLength(size)
            box.SetZLength(size)
            box.Update()
            source = box.GetOutput()

        mapper = vtkDataSetMapper()
        mapper.SetInputData(source)
        mapper.ScalarVisibilityOff()
        # In front of the cells around it: the highlight is the answer to
        # "which one", so it may not be hidden by the face it shares.
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        actor = vtkActor()
        actor.SetObjectName('qualityCellFocus')
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetRepresentationToWireframe()
        prop.SetLineWidth(3.0)
        prop.SetLighting(False)
        prop.SetColor(*self._colour())
        prop.SetOpacity(1.0)
        actor.PickableOff()
        self._view.addActor(actor)
        self._actor = actor

    @staticmethod
    def _colour():
        colour = _WARNING
        try:
            from foammesh.app import app

            if app.themeManager is not None and app.themeManager.tokens is not None:
                colour = app.themeManager.tokens.value('status.warning')
        except Exception:                                          # noqa: BLE001
            pass
        from PySide6.QtGui import QColor

        parsed = QColor(colour)
        if not parsed.isValid():
            parsed = QColor(_WARNING)
        return parsed.redF(), parsed.greenF(), parsed.blueF()
