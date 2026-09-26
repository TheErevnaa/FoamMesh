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

DP-695 (viewport audit 0925 F9). The drawn plane surface spans the whole model
at ``PlaceFactor 1.25``, and it was pickable: every left-drag that started over
the model pushed or turned the cut instead of rotating the camera, so parts
appeared and vanished as the cut swept through them. The surface is still
drawn but no longer grabs; only the handles do (normal arrow, origin sphere,
outline). A lock stops even the handles from taking a drag, so a placed
section can sit still while the camera moves around it.

DP-735. A locked plane had no way back short of unticking Lock in the section
controls. A Ctrl+drag on a handle now moves a locked plane for that one drag:
a press observer that runs before the widget's own lets the widget take the
press, and the lock closes again when the drag (or a press on no handle) ends.
A plain Ctrl+click still selects; only a press on a handle is taken.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from vtkmodules.vtkCommonCore import vtkCommand
from vtkmodules.vtkRenderingCore import vtkPropCollection
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

#: The widget's own interactor observers sit at priority 0.5
#: (``vtkAbstractWidget``); the Ctrl gate has to see a press before they do.
_CTRL_GATE_PRIORITY = 1.0

#: ``vtkImplicitPlaneRepresentation`` interaction states (DP-792): the ones
#: that translate the plane (a Ctrl+drag is ``Moving``), and the one that
#: turns it.
_TRANSLATING_STATES = (vtkImplicitPlaneRepresentation.Moving,
                       vtkImplicitPlaneRepresentation.MovingOutline,
                       vtkImplicitPlaneRepresentation.MovingOrigin,
                       vtkImplicitPlaneRepresentation.Pushing)
_ROTATING_STATE = vtkImplicitPlaneRepresentation.Rotating


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
        self._dragStartTravel = None
        self._tokens = None
        self._locked = False
        self._ctrlDrag = False
        self._interacting = False
        self._gateTags = []

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
        _stopPlaneSurfaceGrabbing(rep)

        self._widget.AddObserver(
            vtkCommand.StartInteractionEvent, self._interactionStarted)
        self._widget.AddObserver(vtkCommand.InteractionEvent, self._planeMoved)
        self._widget.AddObserver(
            vtkCommand.EndInteractionEvent, self._interactionEnded)
        interactor = view.interactor()
        if interactor is not None and hasattr(interactor, 'AddObserver'):
            self._gateTags = [
                (interactor, interactor.AddObserver(
                    vtkCommand.LeftButtonPressEvent, self._ctrlPressGate,
                    _CTRL_GATE_PRIORITY)),
                (interactor, interactor.AddObserver(
                    vtkCommand.LeftButtonReleaseEvent, self._ctrlReleaseGate,
                    _CTRL_GATE_PRIORITY)),
            ]

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

    def setLocked(self, locked: bool):
        """Keep the plane where it is: the handles stay drawn but take no drag.

        A locked plane hands every mouse event to the camera, which is what a
        user rotating around a placed section expects. Ctrl+drag on a handle
        still moves it (DP-735).
        """
        # DP-793. A live re-cut re-syncs the handles on every move, which
        # re-asserts the same lock; closing an open Ctrl drag there stopped
        # it after one move. The drag's end closes it (_interactionEnded).
        if bool(locked) == self._locked and self._ctrlDrag:
            return
        self._locked = bool(locked)
        self._ctrlDrag = False
        self._setProcessEvents(not self._locked)

    def isLocked(self) -> bool:
        return self._locked

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
        for interactor, tag in self._gateTags:
            interactor.RemoveObserver(tag)
        self._gateTags = []
        self._widget.RemoveAllObservers()
        self._widget.Off()
        self._widget = None

    # -- internals --------------------------------------------------------- #

    def _representation(self):
        return self._widget.GetImplicitPlaneRepresentation()

    def _setProcessEvents(self, process: bool):
        setProcessEvents = getattr(self._widget, 'SetProcessEvents', None)
        if setProcessEvents is not None:
            setProcessEvents(1 if process else 0)

    def _ctrlPressGate(self, interactor, event):
        """DP-735. Ctrl opens a locked plane to the press about to reach it."""
        if (self._widget is None or not self._locked
                or not interactor.GetControlKey()):
            return
        self._ctrlDrag = True
        self._setProcessEvents(True)

    def _ctrlReleaseGate(self, interactor, event):
        # A press that found no handle started no drag: close the lock now.
        # A drag in progress needs this release, so it closes in
        # _interactionEnded instead.
        if self._ctrlDrag and not self._interacting:
            self._relock()

    def _relock(self):
        self._ctrlDrag = False
        if self._locked:
            self._setProcessEvents(False)

    def _interactionStarted(self, obj, event):
        self._interacting = True
        self._dragOrigin = tuple(self.origin())
        direction = self._dragDirection()
        self._dragStartTravel = (None if direction is None
                                 else self._pointerTravel(direction))

    def _interactionEnded(self, obj, event):
        self._interacting = False
        self._dragOrigin = None
        self._dragStartTravel = None
        if self._ctrlDrag:
            self._relock()
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
        # DP-792. VTK moves the origin sphere within the plane, so projecting
        # that move onto the normal is zero by construction and the handle
        # a user reaches for moved nothing. Read the travel off the pointer.
        state = self._interactionState()
        if state == _ROTATING_STATE:
            return list(start)
        if state in _TRANSLATING_STATES and self._dragStartTravel is not None:
            direction = self._dragDirection()
            travel = (None if direction is None
                      else self._pointerTravel(direction))
            if travel is not None:
                shift = travel - self._dragStartTravel
                return [start[i] + shift * direction[i] for i in range(3)]

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

    def _interactionState(self):
        getState = getattr(self._representation(), 'GetInteractionState', None)
        return None if getState is None else getState()

    def _dragDirection(self):
        """The unit direction the drag constraint allows, or None."""
        if self._dragAxis is None:
            return None
        if self._dragAxis == AXIS_NORMAL:
            vector = self.normal()
        else:
            vector = [0.0, 0.0, 0.0]
            vector[self._dragAxis] = 1.0
        length = sum(value * value for value in vector) ** 0.5
        if not length:
            return None
        return [value / length for value in vector]

    def _pointerTravel(self, direction):
        """How far along ``direction`` from the drag start the pointer is.

        The parameter of the point on the line ``start + t * direction``
        nearest the ray under the cursor. None when there is no pointer to
        read, or when the line runs along the view ray and the pointer says
        nothing about it.
        """
        start = self._dragOrigin
        rep = self._representation()
        getRenderer = getattr(rep, 'GetRenderer', None)
        renderer = getRenderer() if getRenderer is not None else None
        interactor = self._widget.GetInteractor() if hasattr(
            self._widget, 'GetInteractor') else None
        if start is None or renderer is None or interactor is None:
            return None
        x, y = interactor.GetEventPosition()
        ends = []
        for depth in (0.0, 1.0):
            renderer.SetDisplayPoint(x, y, depth)
            renderer.DisplayToWorld()
            world = renderer.GetWorldPoint()
            if not world[3]:
                return None
            ends.append([world[i] / world[3] for i in range(3)])
        ray = [ends[1][i] - ends[0][i] for i in range(3)]
        length = sum(value * value for value in ray) ** 0.5
        if not length:
            return None
        ray = [value / length for value in ray]
        cosine = sum(direction[i] * ray[i] for i in range(3))
        denominator = 1.0 - cosine * cosine
        if denominator < 1e-6:
            return None
        offset = [start[i] - ends[0][i] for i in range(3)]
        along_ray = sum(ray[i] * offset[i] for i in range(3))
        along_line = sum(direction[i] * offset[i] for i in range(3))
        return (cosine * along_ray - along_line) / denominator


def _stopPlaneSurfaceGrabbing(rep):
    """Make the drawn plane surface unpickable, leaving the handles live.

    ``vtkImplicitPlaneRepresentation`` picks from a list that includes the cut
    surface; a press on it starts a push along the normal. The surface actor
    is the one wearing the plane property (or its selected twin).
    """
    getActors = getattr(rep, 'GetActors', None)
    if getActors is None:
        return
    props = vtkPropCollection()
    getActors(props)
    wanted = (rep.GetPlaneProperty(), rep.GetSelectedPlaneProperty())
    for index in range(props.GetNumberOfItems()):
        actor = props.GetItemAsObject(index)
        getProperty = getattr(actor, 'GetProperty', None)
        if getProperty is None:
            continue
        if any(getProperty() is prop for prop in wanted):
            actor.PickableOff()
