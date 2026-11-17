"""DP-6120: the wheel scrolls a settings column, not the box under the cursor.

MEASURED live 2026-11-01 on the CHT tubes, Gmsh Structure -> Volume controls:
the column was scrolled with the mouse wheel while the pointer crossed the
form, and the region's Type, which the pointer happened to pass over, went
from Fluid to Solid. Nothing said so; the row would have meshed the hot fluid
as a solid. Qt gives a combo or a number box the wheel, and focus with it,
whenever the pointer is over one.

Plan 44 MF2 keeps stepping a number box on the wheel, so the wheel still
steps a box the user has clicked into. A box without focus that sits inside a
scrolling area passes the wheel on to that area. Boxes take focus on a click
or Tab (``StrongFocus``), no longer on the wheel.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox, QWidget,
)

_BOXES = (QComboBox, QAbstractSpinBox)


def _box_of(widget) -> QWidget | None:
    """The combo or number box *widget* is, or is the editor inside of."""
    node, steps = widget, 0
    while isinstance(node, QWidget) and steps < 3:
        if isinstance(node, _BOXES):
            return node
        node = node.parentWidget()
        steps += 1
    return None


def _scroll_area_of(box: QWidget) -> QAbstractScrollArea | None:
    """The area *box* sits inside, which the wheel would otherwise scroll."""
    node = box.parentWidget()
    while node is not None:
        if isinstance(node, QAbstractScrollArea):
            return node
        node = node.parentWidget()
    return None


class WheelGuard(QObject):
    """Application event filter: see the module docstring."""

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Polish:
            if (isinstance(watched, _BOXES)
                    and watched.focusPolicy() == Qt.FocusPolicy.WheelFocus):
                watched.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            return False
        if kind != QEvent.Type.Wheel:
            return False
        box = _box_of(watched)
        if (box is None or box.hasFocus()
                or watched.window() is not box.window()):   # an open list
            return False
        area = _scroll_area_of(box)
        if area is None:
            return False
        # Handed to the area as `ReadOnlyGuard` does (lock_chip): Qt passes a
        # wheel up to the parent only when it came from the device.
        QApplication.sendEvent(area.viewport(), event)
        return True


_guard: WheelGuard | None = None


def install(application: QApplication | None = None) -> WheelGuard:
    """Install the guard on the application (once)."""
    global _guard
    application = application or QApplication.instance()
    if _guard is None:
        _guard = WheelGuard(application)
        application.installEventFilter(_guard)
    return _guard


def uninstall() -> None:
    """Take the guard off again (tests)."""
    global _guard
    application = QApplication.instance()
    if _guard is not None and application is not None:
        application.removeEventFilter(_guard)
    _guard = None
