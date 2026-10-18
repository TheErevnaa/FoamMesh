"""Plan 45 start-up: Qt's native-sibling rule, applied once the window is built.

The viewport is a native (GL) window. By Qt's rule, a widget that gets a native
window makes its siblings native too, and every ancestor, and so every
ancestor's siblings: that is what keeps the rail, the tool edges, the legend and
the popovers over the GL view instead of under it. Applied while the window is
being built, though, it also made native the docks, the regions and their
contents before the dock manager moved them into place -- and a native window
moved to a new parent is destroyed and made again. MEASURED live 18 October
2026: building with the rule held off and applied once at the end showed the
window ~0.25 s sooner (median of 7 launches, 3.86 s -> 3.61 s).

:func:`held_off` holds the rule off while the window is built; on leaving, the
rule is back on for the rest of the session and is applied once, as Qt would
have: every parent of a native child forces native children (today's children
and the ones it is given later). Whether anything is drawn under the GL view
is measured by ``tests/unit/test_plan45_native_siblings.py``.
"""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QWidget

_RULE_OFF = Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings


@contextmanager
def held_off():
    """Build the window inside this; the rule is applied when it ends."""
    built = []
    QCoreApplication.setAttribute(_RULE_OFF, True)
    try:
        yield built.append
    finally:
        QCoreApplication.setAttribute(_RULE_OFF, False)
        for window in built:
            apply_rule(window)


def apply_rule(window) -> None:
    """Force native children under every parent of a native widget in *window*.

    Qt applies the rule when a widget's native-window attribute is turned on,
    to that widget's parent (``enforceNativeChildren``): turning it on for one
    child that is not native yet applies it to the whole family. A parent
    whose children are all native already needs nothing.
    """
    if not isinstance(window, QWidget):
        return
    native = Qt.WidgetAttribute.WA_NativeWindow
    parents = {id(widget.parentWidget()): widget.parentWidget()
               for widget in window.findChildren(QWidget)
               if not widget.isWindow() and widget.internalWinId()}
    for family in parents.values():
        for child in family.children():
            if (isinstance(child, QWidget) and not child.isWindow()
                    and not child.testAttribute(native)):
                child.setAttribute(native, True)
                break
