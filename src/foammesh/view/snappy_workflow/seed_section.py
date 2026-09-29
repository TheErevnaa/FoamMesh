"""Place a region's seed on a section plane.

Plan 36 RP9. A point dragged in 3-D has a depth the eye has to guess. The
Regions editor's Section button raises one section plane through the seed,
normal to the world axis the camera looks along most, with the cut tool's
own machinery (the cut is the one the Display Control section draws, live).
While it is up:

* the seed handle is in plane mode (`SeedGizmo.setPlaneMode`): no drag
  changes the coordinate along the normal, so the seed stays on the cut the
  user is looking at;
* pushing the plane with its own handle carries the seed along the normal
  (`CutTool.seedSectionPushed` -> `SeedGizmo.pushPlane`), and a key nudge or
  a typed coordinate along the normal carries the plane with the seed;
* a double-click on the plane drops the seed there.

Leaving Section mode gives the cut tool back exactly what it held.

`planeChanged` and `ended` are the hook the region volume (RP6) listens to,
to draw its filled section on the same plane (`rendering.section_fill`).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from foammesh.view.display_control.cut_tool import nearestAxis


class SeedSection(QObject):
    """Section mode for one seed handle over one cut tool."""

    #: Section mode went on (True) or off (False).
    toggled = Signal(bool)
    #: The plane is now here: ``(origin, normal)``.
    planeChanged = Signal(tuple, tuple)
    #: The plane is gone.
    ended = Signal()

    def __init__(self, gizmo, cutTool, parent=None):
        super().__init__(parent)
        self._gizmo = gizmo
        self._cutTool = cutTool
        self._axis = None
        self._pushing = False

    def isActive(self) -> bool:
        return self._axis is not None

    def axis(self):
        """The axis the plane is normal to, or ``None`` when off."""
        return self._axis

    def plane(self):
        """``(origin, normal)`` of the plane, or ``None`` when off."""
        if self._axis is None:
            return None
        return self._cutTool.seedSectionPlane()

    def setActive(self, active: bool) -> bool:
        """Turn Section mode on or off; whether it is on afterwards."""
        if active:
            return self.start()
        self.stop()
        return False

    def start(self) -> bool:
        """Raise the plane through the seed. False with nothing to cut."""
        if self._axis is not None:
            return True
        direction = self._cutTool.viewDirection() or (0.0, 0.0, 1.0)
        axis = nearestAxis(direction)
        point = self._gizmo.position()
        if not self._cutTool.beginSeedSection(point, axis):
            return False
        self._axis = axis
        self._gizmo.setPlaneMode(axis)
        self._cutTool.seedSectionPushed.connect(self._pushed)
        self._cutTool.seedSectionReleased.connect(self._released)
        self._gizmo.pointMoved.connect(self._seedMoved)
        self.toggled.emit(True)
        self._announce()
        return True

    def stop(self) -> None:
        """Take the plane down and give the cut tool back what it held."""
        if self._axis is None:
            return
        self._axis = None
        self._pushing = False
        for signal, slot in ((self._cutTool.seedSectionPushed, self._pushed),
                             (self._cutTool.seedSectionReleased, self._released),
                             (self._gizmo.pointMoved, self._seedMoved)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            self._gizmo.setPlaneMode(None)
        except RuntimeError:
            pass
        self._cutTool.endSeedSection()
        self.ended.emit()
        self.toggled.emit(False)

    def follow(self, point) -> None:
        """The seed was put at *point* by typing: the plane goes with it."""
        if self._axis is None or self._pushing:
            return
        self._cutTool.moveSeedSection(point)
        self._announce()

    # -- slots --------------------------------------------------------------- #

    def _pushed(self, origin) -> None:
        self._pushing = True
        self._gizmo.pushPlane(origin[self._axis])
        self._announce()

    def _released(self) -> None:
        self._pushing = False
        plane = self._cutTool.seedSectionPlane()
        if plane is not None:
            self._gizmo.pushPlane(plane[0][self._axis], finished=True)
            # The seed may have stopped at the domain's wall: the plane
            # comes back to it rather than cutting where the seed is not.
            self._cutTool.moveSeedSection(self._gizmo.position())
        self._announce()

    def _seedMoved(self, point) -> None:
        if self._pushing:
            return                  # the plane is leading; it is not led
        plane = self._cutTool.seedSectionPlane()
        if plane is not None and plane[0][self._axis] != point[self._axis]:
            self._cutTool.moveSeedSection(point)
            self._announce()

    def _announce(self) -> None:
        plane = self._cutTool.seedSectionPlane()
        if plane is not None:
            self.planeChanged.emit(tuple(plane[0]), tuple(plane[1]))
