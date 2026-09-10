#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The draggable section-plane gizmo.

``vtkImplicitPlaneWidget2`` ships with everything a section plane needs: a
grabbable normal arrow that rotates the plane, an outline that translates it,
and a plane surface that can be pushed along the normal. Three lines used to
take most of that away -- the normal arrow was drawn at ``opacity 0`` and
outline translation was switched off -- leaving a plane that could only be
pushed along its own normal by a user who already knew it was there.

Everything is restored here, themed rather than left at VTK's defaults, and a
drag-axis lock is added on top: the plane can be constrained to move along X, Y,
Z or its own normal, which is the "which direction should it move" control.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from vtkmodules.vtkCommonCore import vtkCommand
from vtkmodules.vtkCommonDataModel import vtkPlane
from vtkmodules.vtkInteractionWidgets import (
    vtkImplicitPlaneRepresentation, vtkImplicitPlaneWidget2)

from foammesh.view.theming.vtk_theme import rgb


#: Drag constraints accepted by :meth:`PlaneWidget.setDragAxis`. ``None`` is
#: free movement; ``AXIS_NORMAL`` keeps the origin on the plane's own normal,
#: which is what a section sweep wants.
AXIS_X, AXIS_Y, AXIS_Z, AXIS_NORMAL = 0, 1, 2, 3

#: The plane is drawn faintly: solid enough to read as a surface at a glance,
#: transparent enough that the mesh behind it is still the subject.
PLANE_OPACITY = 0.16
OUTLINE_WIDTH = 1.4
NORMAL_WIDTH = 2.0


class PlaneWidget(QObject):
    #: Origin only. Kept because the cut controls have always listened to it.
    planeMoved = Signal(tuple)
    #: Origin and normal together, for controls that track rotation as well.
    planeChanged = Signal(tuple, tuple)
    #: The drag ended. A mesh too large to re-cut live is re-cut here instead.
    interactionFinished = Signal()

    def __init__(self, view):
        super().__init__()

        self._view = view
        self._widget = vtkImplicitPlaneWidget2()
        self._plane = vtkPlane()
        self._dragAxis = None
        self._dragOrigin = None
        self._tokens = None

        rep = vtkImplicitPlaneRepresentation()
        rep.SetPlaceFactor(1.25)  # This must be set prior to placing the widget
        # A section plane that cannot be grabbed is a section plane that does
        # not exist. Both handles are live: the outline translates the origin,
        # the normal arrow rotates the plane.
        rep.OutlineTranslationOn()
        rep.DrawPlaneOn()
        rep.DrawOutlineOn()
        rep.ScaleEnabledOff()
        # Sweeping a plane out past the model is a legitimate way to clear a
        # cut; refusing to let it leave the bounds only makes the gizmo feel
        # stuck at the extremes.
        rep.OutsideBoundsOn()
        rep.ConstrainToWidgetBoundsOff()
        rep.GetPlaneProperty().SetOpacity(PLANE_OPACITY)
        rep.GetSelectedPlaneProperty().SetOpacity(PLANE_OPACITY * 1.6)
        rep.GetNormalProperty().SetLineWidth(NORMAL_WIDTH)
        rep.GetSelectedNormalProperty().SetLineWidth(NORMAL_WIDTH * 1.5)
        rep.GetOutlineProperty().SetLineWidth(OUTLINE_WIDTH)
        rep.GetSelectedOutlineProperty().SetLineWidth(OUTLINE_WIDTH * 1.5)

        self._widget.SetInteractor(view.interactor())
        self._widget.SetRepresentation(rep)

        self._widget.AddObserver(
            vtkCommand.StartInteractionEvent, self._interactionStarted)
        self._widget.AddObserver(vtkCommand.InteractionEvent, self._planeMoved)
        self._widget.AddObserver(
            vtkCommand.EndInteractionEvent, self._interactionEnded)

    # -- theming ----------------------------------------------------------- #

    def applyTheme(self, tokens):
        """Colour the gizmo from the active theme.

        VTK's defaults are a saturated green plane and a red arrow, which on a
        themed viewport read as an error state rather than a tool.
        """
        self._tokens = tokens
        rep = self._representation()
        if rep is None:
            return
        accent = rgb(tokens.value('accent.default'))
        hover = rgb(tokens.value('accent.hover'))
        outline = rgb(tokens.value('viewport.silhouette'))
        rep.GetNormalProperty().SetColor(*accent)
        rep.GetSelectedNormalProperty().SetColor(*hover)
        rep.GetOutlineProperty().SetColor(*outline)
        rep.GetSelectedOutlineProperty().SetColor(*hover)
        rep.GetEdgesProperty().SetColor(*outline)
        rep.GetPlaneProperty().SetColor(*accent)
        rep.GetSelectedPlaneProperty().SetColor(*hover)
        self._view.refresh()

    # -- geometry ---------------------------------------------------------- #

    def setOrigin(self, origin):
        self._representation().SetOrigin(*origin)
        self._view.refresh()

        return self.origin()

    def origin(self):
        origin = [0, 0, 0]
        self._representation().GetOrigin(origin)

        return origin

    def setBounds(self, bounds):
        self._representation().PlaceWidget(bounds.toTuple())

    def setNormal(self, normal):
        self._representation().SetNormal(*normal)
        self._view.refresh()

    def normal(self):
        normal = [0, 0, 1]
        self._representation().GetNormal(normal)

        return normal

    def setDragAxis(self, axis):
        """Constrain dragging to a world axis, the plane normal, or nothing.

        ``vtkImplicitPlaneRepresentation`` has no constraint of its own, so the
        lock is applied by projecting each interactive move back onto the
        allowed direction before the move is published.
        """
        self._dragAxis = axis

    def dragAxis(self):
        return self._dragAxis

    # -- lifecycle --------------------------------------------------------- #

    def on(self, normal=None):
        if normal is not None:
            self._representation().SetNormal(*normal)
        self._widget.On()
        if self._tokens is not None:
            self.applyTheme(self._tokens)

    def off(self):
        self._widget.Off()
        self._view.refresh()

    def isEnabled(self):
        return self._widget.GetEnabled()

    def close(self):
        self._widget.RemoveAllObservers()
        self._widget.Off()
        self._widget = None

    # -- internals --------------------------------------------------------- #

    def _representation(self):
        return self._widget.GetImplicitPlaneRepresentation()

    def _interactionStarted(self, obj, event):
        self._dragOrigin = tuple(self.origin())

    def _interactionEnded(self, obj, event):
        self._dragOrigin = None
        self.interactionFinished.emit()

    def _planeMoved(self, obj, event):
        rep = self._representation()
        origin = self._constrain(self.origin())
        rep.SetOrigin(*origin)
        rep.GetPlane(self._plane)
        self.planeMoved.emit(tuple(origin))
        self.planeChanged.emit(tuple(origin), tuple(self.normal()))

    def _constrain(self, origin):
        if self._dragAxis is None or self._dragOrigin is None:
            return origin

        start = self._dragOrigin
        if self._dragAxis == AXIS_NORMAL:
            normal = self.normal()
            length = sum(value * value for value in normal) ** 0.5
            if not length:
                return origin
            unit = [value / length for value in normal]
            travel = sum(
                (origin[i] - start[i]) * unit[i] for i in range(3))
            return [start[i] + travel * unit[i] for i in range(3)]

        constrained = list(start)
        constrained[self._dragAxis] = origin[self._dragAxis]
        return constrained
