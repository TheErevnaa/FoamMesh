"""Plan 43 LY13 (D4): an act a page owns but never draws.

A task page used to build four QPushButtons (*Preview*, *Update*, *Revert and
edit* and the stage button) and hide them: the footer drop-down and the
header menu press them. A hidden widget is a control no user can see, so the
acts are plain objects now. They keep what the page and the menus need from a
button -- an enabled state, a label, a tooltip, a ``clicked`` signal and
``click()`` -- and nothing that could be drawn.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class PageAction(QObject):
    """The state of one page act: enabled, labelled, pressable, never drawn."""

    clicked = Signal()

    def __init__(self, text: str = '', parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._text = str(text)
        self._enabled = True
        self._toolTip = ''
        self._description = ''

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = str(text)

    def isEnabled(self) -> bool:
        return self._enabled

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def toolTip(self) -> str:
        return self._toolTip

    def setToolTip(self, text: str) -> None:
        self._toolTip = str(text)

    def accessibleDescription(self) -> str:
        return self._description

    def setAccessibleDescription(self, text: str) -> None:
        self._description = str(text)

    # Never drawn: these answer the questions a widget would.
    def setVisible(self, _visible: bool) -> None:
        return None

    def isVisible(self) -> bool:
        return False

    def isVisibleTo(self, _ancestor) -> bool:
        return False

    def isHidden(self) -> bool:
        return True

    def hide(self) -> None:
        return None

    def click(self) -> None:
        if self._enabled:
            self.clicked.emit()
