#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import QObject

from foammesh.rendering.actor_info import RegionMarkerActor
from foammesh.support.mesh import Bounds
from foammesh.app import app
from foammesh.view.theming.patch_palette import slot_order


def _union(boundsList) -> Optional[Bounds]:
    """Merge bounds, skipping any that enclose nothing (min above max)."""
    merged = None
    for bounds in boundsList:
        if bounds is None:
            continue
        values = bounds.toTuple()
        if any(values[2 * axis] > values[2 * axis + 1] for axis in range(3)):
            continue
        if merged is None:
            merged = Bounds(*values)
        else:
            merged.merge(bounds)
    return merged


class ActorGroup(Enum):
    GEOMETRY = auto()
    MESH = auto()


class ActorManager(QObject):
    def __init__(self):
        super().__init__()

        self._actorInfos = {}
        self._visibility = True
        # G4. Whether this scene has ever been framed. The very first fit
        # points the camera somewhere the shape reads as a solid; every fit
        # after that leaves the direction the user chose alone.
        self._framed = False
        self._displayControl = app.window.displayControl

    def isEmpty(self):
        return not self._actorInfos

    def actorInfo(self, key):
        return self._actorInfos.get(key)

    def actorIds(self) -> list:
        """Every actor this manager put in the scene, by id.

        `boundaries()` returns the same keys under a name that is only true
        for one subclass; the view-mode planner needs them from both.
        """
        return [str(key) for key in self._actorInfos]

    def add(self, actorInfo):
        if actorInfo.id() in self._actorInfos:
            # Named, because a bare KeyError here says nothing: this fires when
            # two paths populate the same scene, and the id is the only clue
            # to which element got added twice.
            raise KeyError(
                f'{type(self).__name__} already holds an actor for '
                f'{actorInfo.id()!r}')

        if app.themeManager is not None and app.themeManager.tokens is not None:
            actorInfo.applyTheme(app.themeManager.tokens)
        self._actorInfos[actorInfo.id()] = self._displayControl.add(actorInfo)

    def update(self, id_, dataSet):
        self._actorInfos[id_].setDataSet(dataSet)

    def remove(self, key):
        if actorInfo := self._actorInfos.pop(key, None):
            self._displayControl.remove(actorInfo)

    def getBounds(self) -> Optional[Bounds]:
        """Union of every actor this manager holds, drawn or not.

        DP-813. An actor over an empty data set -- a patch with no faces, a
        face zone with none -- reports VTK's uninitialised ``(1, -1, ...)``,
        and merging it dragged the union's minimum down to 1 and its maximum
        up to -1 on every axis the model does not straddle: a part at x = 1000
        mm measured from x = 1. Empty actors have no place and are skipped;
        a manager holding nothing but empty actors still answers with what
        it has, as it always did, because callers take ``.toTuple()`` of it.
        """
        if self.isEmpty():
            return None
        merged = _union(info.bounds() for info in self._actorInfos.values())
        if merged is None:
            merged = next(iter(self._actorInfos.values())).bounds()
        return merged

    def getDisplayedBounds(self) -> Optional[Bounds]:
        """Union of the actors actually on screen, or ``None`` if none are.

        DP-813. What a section plane is placed against. ``hide()`` takes the
        props out of the renderer and keeps the actors, so ``getBounds``
        still counts a mesh the user has left or geometry a volume mesh
        hid. A part the user switched off is not on screen either, and a
        region seed is a landmark rather than part of the model (DP-679).
        """
        isShown = getattr(self._displayControl, 'isShown', None)
        return _union(
            info.bounds() for info in self._actorInfos.values()
            if info.isVisible()
            and not isinstance(info, RegionMarkerActor)
            and (isShown is None or isShown(info)))

    def assignPatchPalette(self, types, family='patch', keys=None):
        """Give each actor of ``types`` a distinct slot in a categorical palette.

        Assignment is by sorted id rather than insertion order so that the same
        case always colours the same patch the same way -- a patch that changed
        colour every reload would be worse than no colour at all. Anything not
        selected (the internal mesh, overlays) keeps the neutral surface.

        ``keys`` narrows the assignment to a named subset, which is how zones
        get their own palette without competing with the patches for slots.
        """
        # DP-819. In id order, not text order: as text `10` came before `2`.
        selected = slot_order(
            key for key, info in self._actorInfos.items()
            if isinstance(info, types) and (keys is None or key in keys))
        for index, key in enumerate(selected):
            self._actorInfos[key].setPaletteIndex(index, family)

    def setCutMode(self, mode):
        """Choose smooth or whole-cell cutting for every actor in this group."""
        for actorInfo in self._actorInfos.values():
            if hasattr(actorInfo, 'setCutMode'):
                actorInfo.setCutMode(mode)

    def setExplode(self, factor: float):
        """Offset every part along the vector from the scene centre.

        ``factor`` 0 is the assembled mesh; 1 moves each part by its own
        distance from the centre, which is enough to separate a conjugate case
        without throwing the parts out of frame.
        """
        bounds = self.getBounds()
        if bounds is None:
            return

        centre = bounds.center()
        for actorInfo in self._actorInfos.values():
            setOffset = getattr(actorInfo, 'setExplodeOffset', None)
            if setOffset is None:
                continue
            own = actorInfo.bounds().center()
            setOffset([(own[axis] - centre[axis]) * factor for axis in range(3)])

        self._displayControl.refreshView()

    def applyToDisplay(self):
        # What is in the scene decides which inspection tools make sense, so
        # every content change re-asks the question.
        sceneChanged = getattr(self._displayControl, 'sceneChanged', None)
        if sceneChanged is not None:
            sceneChanged()
        self._displayControl.refreshView()

    def applyTheme(self, tokens):
        for actor_info in self._actorInfos.values():
            actor_info.applyTheme(tokens)
        self._displayControl.refreshView()

    def fitDisplay(self):
        # G4. VTK opens on the camera's default direction, which looks
        # straight down an axis: an imported pipe arrived as a flat annulus
        # and a duct as a rectangle, and the only way to tell it was a solid
        # was to drag the view. An isometric first frame shows three faces.
        if not self._framed and not self.isEmpty():
            self._framed = True
            orient = getattr(self._displayControl, 'orientIsometric', None)
            if orient is not None:
                orient()
                return
        self._displayControl.fitView()

    def clear(self):
        for key in tuple(self._actorInfos):
            self.remove(key)
        self.applyToDisplay()
        self._visibility = False
        self._framed = False

    def hide(self):
        for actorInfo in self._actorInfos.values():
            self._displayControl.hide(actorInfo)

        self.applyToDisplay()
        self._visibility = False

    def clip(self, planes):
        for actorInfo in self._actorInfos.values():
            actorInfo.clip(planes)

        self._displayControl.refreshView()

    def slice(self, plane):
        for actorInfo in self._actorInfos.values():
            actorInfo.slice(plane)

        self._displayControl.refreshView()

    def _show(self):
        for actorInfo in self._actorInfos.values():
            self._displayControl.add(actorInfo)

        self._displayControl.refreshView()
        self._visibility = True

    def _updateActorName(self, id_, name):
        self._actorInfos[id_].setName(name)
        # The overlay rows carry the name, so a rename that only touched the
        # actor left the overlay listing the old one beside the new tree row.
        self._displayControl.partsChanged.emit()
