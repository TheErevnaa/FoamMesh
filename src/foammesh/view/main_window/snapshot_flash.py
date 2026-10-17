#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 44 MF22: the white flash that says a snapshot was taken.

The old flash was a plain child widget over the viewport.  The viewport is a
native GL window and draws over plain siblings, so the flash was never seen
(Plan 44 section 3).  This one is drawn *by VTK*: a second renderer in a layer
above the scene holds one white full-window quad, and its opacity runs from
.75 to 0 in 450 ms (the mockup's ``@keyframes fx``).

Rules:

* The picture is taken before the flash starts, and ``clear()`` takes a flash
  still in flight out of the window first, so a flash is never in a saved
  image.
* No flash under reduced motion or Windows "Show animations" off
  (``motion.motion_enabled()``), nor while a render hold is active (DP-347).
  A hold that begins mid-flash ends it at once.
* The renderer is non-interactive, so picking and the camera never see it.
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QVariantAnimation

from foammesh.view.theming import motion

#: The mockup: ``0% opacity .75`` to ``100% opacity 0`` in .45 s, ease-out.
START_OPACITY = 0.75
DURATION_MS = 450
#: Above the seed gizmo's layer (1), so the flash covers everything drawn.
LAYER = 2
NAME = 'snapshotFlash'


class SnapshotFlash(QObject):
    """One flash at a time over one view."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view
        self._window = None
        self._renderer = None
        self._actor = None
        self._animation: QVariantAnimation | None = None
        self.fired = 0

    # -- state -------------------------------------------------------------- #

    def isRunning(self) -> bool:
        return self._renderer is not None

    def opacity(self) -> float:
        actor = self._actor
        return float(actor.GetProperty().GetOpacity()) if actor is not None else 0.0

    def renderer(self):
        return self._renderer

    # -- the flash ------------------------------------------------------------ #

    def fire(self) -> bool:
        """Start a flash; False when it is skipped (motion off, a hold, no
        window to draw into)."""
        self.clear()
        if not motion.motion_enabled():
            return False
        window = self._viewWindow()
        if window is None:
            return False
        try:
            self._build(window)
        except Exception:                                      # noqa: BLE001
            self.clear()
            return False
        self.fired += 1
        self._set(START_OPACITY)
        animation = QVariantAnimation(self)
        animation.setStartValue(START_OPACITY)
        animation.setEndValue(0.0)
        animation.setDuration(DURATION_MS)
        animation.setEasingCurve(QEasingCurve.Type.OutQuad)
        animation.valueChanged.connect(self._step)
        animation.finished.connect(self.clear)
        self._animation = animation
        animation.start()
        return True

    def clear(self) -> None:
        """End the flash now and take it out of the window."""
        animation, self._animation = self._animation, None
        if animation is not None:
            try:
                animation.stop()
                animation.deleteLater()
            except RuntimeError:
                pass
        renderer, window = self._renderer, self._window
        self._renderer = self._window = self._actor = None
        if renderer is None or window is None:
            return
        try:
            window.RemoveRenderer(renderer)
        except Exception:                                      # noqa: BLE001
            return
        self._draw()

    # -- helpers ---------------------------------------------------------------- #

    def _viewWindow(self):
        accessor = getattr(self._view, 'renderWindow', None)
        try:
            return accessor() if callable(accessor) else None
        except Exception:                                      # noqa: BLE001
            return None

    def _build(self, window) -> None:
        from vtkmodules.vtkCommonCore import vtkIdList, vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import (
            vtkActor2D, vtkCoordinate, vtkPolyDataMapper2D, vtkRenderer)

        points = vtkPoints()
        for x, y in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)):
            points.InsertNextPoint(x, y, 0.0)
        quad = vtkCellArray()
        ids = vtkIdList()
        for index in range(4):
            ids.InsertNextId(index)
        quad.InsertNextCell(ids)
        data = vtkPolyData()
        data.SetPoints(points)
        data.SetPolys(quad)

        coordinate = vtkCoordinate()
        coordinate.SetCoordinateSystemToNormalizedViewport()
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(data)
        mapper.SetTransformCoordinate(coordinate)
        actor = vtkActor2D()
        actor.SetMapper(mapper)
        actor.SetObjectName(NAME)
        actor.GetProperty().SetColor(1.0, 1.0, 1.0)
        actor.PickableOff()

        renderer = vtkRenderer()
        renderer.SetLayer(LAYER)
        renderer.InteractiveOff()
        renderer.SetBackgroundAlpha(0.0)
        renderer.AddViewProp(actor)
        if window.GetNumberOfLayers() < LAYER + 1:
            window.SetNumberOfLayers(LAYER + 1)
        window.AddRenderer(renderer)
        self._window, self._renderer, self._actor = window, renderer, actor

    def _set(self, value: float) -> None:
        if self._actor is not None:
            self._actor.GetProperty().SetOpacity(max(0.0, min(1.0, float(value))))
        self._draw()

    def _step(self, value) -> None:
        if self._renderer is None:
            return
        if motion._holding():
            self.clear()
            return
        self._set(float(value))

    def _draw(self) -> None:
        render = getattr(self._view, '_renderUnlessHeld', None) \
            or getattr(self._view, 'refresh', None)
        if callable(render):
            try:
                render()
            except Exception:                                  # noqa: BLE001
                pass
