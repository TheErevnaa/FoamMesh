"""The y+ -> first-layer-height helper, in one place (Plan 43 LY13 D1, C-134).

The estimate used to live only on the legacy Boundary layers page (removed in
Plan 43 LY13 D1), which the guided path never reached, so the guided layer surfaces (the snappy layer-group
editor and the Gmsh boundary-layer stack) asked for a first-layer height with
no way to work one out. Every surface now asks through :func:`ask_first_layer_height`
and fills its own field through :func:`fill_editor`; the maths is
:func:`foammesh.core.mesh.sizing.first_layer_height`, which answers in metres
(model length units).
"""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QInputDialog, QPushButton

from foammesh.core.mesh.sizing import first_layer_height
from widgets import async_dialog as dialogs

#: Stable context so the sentences translate the same on every surface.
_CONTEXT = 'YPlusHelper'

BUTTON_TEXT = 'Calculate first-layer height from y+…'
BUTTON_DESCRIPTION = 'Calculate the first-layer height from a target y+.'


def _tr(text: str) -> str:
    return QCoreApplication.translate(_CONTEXT, text)


def prompts() -> tuple:
    """The five questions asked, each with its default (SI units)."""
    return (
        (_tr('Target y+'), 1.0), (_tr('Velocity (m/s)'), 10.0),
        (_tr('Density (kg/m³)'), 1.225),
        (_tr('Dynamic viscosity (Pa·s)'), 1.81e-5),
        (_tr('Reference length (m)'), 1.0))


def ask_first_layer_height(parent, then=None):
    """Ask the five questions; ``then(height in metres)``, or ``then(None)``
    on cancel.

    Plan 50 IP19 (DP-6360). Each question opens without a nested event loop
    and the next one follows its answer. The return value is the height when
    every answer came at once (a test answering the dialog), else ``None``.
    """
    values = []
    result = []

    def finish(height):
        result.append(height)
        if then is not None:
            then(height)

    def askNext():
        label, default = prompts()[len(values)]
        dialogs.getDouble(
            parent, _tr('y+ helper'), label, default, 1e-12, 1e12, 8,
            then=answered)

    def answered(value, accepted):
        if not accepted:
            finish(None)
            return
        values.append(value)
        if len(values) < len(prompts()):
            askNext()
        else:
            finish(first_layer_height(*values))

    askNext()
    return result[0] if result else None


def fill_editor(editor, metres: float) -> None:
    """Put *metres* in a ``FieldEditor`` and announce it like a typed value."""
    editor.set_value(float(metres))
    editor.valueChanged.emit(editor.field_id, editor.value())


class YPlusButton(QPushButton):
    """The helper's button; pressing it asks, then hands the height on.

    *apply* is called with the height in metres and returns ``True`` when it
    wrote the height somewhere (so the caller decides what "somewhere" is: a
    field on the Gmsh page, a field in the snappy group editor).
    """

    def __init__(self, apply, parent=None):
        super().__init__(_tr(BUTTON_TEXT), parent)
        self.setObjectName('yPlusHelperButton')
        # DP-186. The button already paints the whole sentence; a name that
        # rewrites it is a second label nobody can say out loud.
        self.setAccessibleDescription(_tr(BUTTON_DESCRIPTION))
        self._apply = apply
        self.last_height = None
        self.clicked.connect(self._pressed)

    def _pressed(self, *_args) -> None:
        ask_first_layer_height(self.window() or self, then=self._answered)

    def _answered(self, height) -> None:
        if height is None:
            return
        self.last_height = height
        self._apply(height)
