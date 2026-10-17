"""A layout that wraps its items onto further rows when the width is short.

Plan 43 FIX-LY10 (DP-1947). The Inspect cells column of the Mesh quality tab
sits in a quarter of a 650 px band at 1280 x 800. Its rows (metric and
Measure, limit and two buttons, ...) are each wider than that, and the column
answered with a sideways scroll bar that hid the right hand controls. A row
built on this layout puts what does not fit on the next line instead, so
every control stays on screen at any width and nothing is dropped.
"""
from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout


def _blank() -> QSize:
    return QSize()


class FlowLayout(QLayout):
    """Left to right, top to bottom; an item wider than the row is cut to it."""

    def __init__(self, parent=None, *, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    # -- QLayout ----------------------------------------------------------- #

    def addItem(self, item) -> None:
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = _blank()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    # -- placing ----------------------------------------------------------- #

    def _arrange(self, rect, *, apply: bool) -> int:
        margins: QMargins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        gap = max(self.spacing(), 0)
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            width = min(hint.width(), max(area.width(), 1))
            if item.hasHeightForWidth() and width < hint.width():
                height = item.heightForWidth(width)
            else:
                height = hint.height()
            if x > area.x() and x + width > area.right() + 1:
                x = area.x()
                y += row_height + gap
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, height)))
            x += width + gap
            row_height = max(row_height, height)
        return y + row_height - rect.y() + margins.bottom()
