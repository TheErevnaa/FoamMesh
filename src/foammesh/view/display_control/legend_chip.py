"""Plan 44 MF12: the small painted parts of the colour legend.

The legend, its chip and the colour scale panel are drawn over, or beside, the
native GL view, where a stylesheet cannot give the mockup's looks (a 3-way
segment with the selection colour on the chosen cell, a 28 x 8 swatch inside
a chip). These parts paint themselves from the theme tokens, with the
mockup's colours as the fallback before a theme is installed, so none of them
needs a stylesheet of its own.

* :class:`LegendChip` is the ``#lchip``: a swatch, a label and a drop arrow.
  The legend folds to it, and the Patches mode shows nothing else.
* :class:`Segment` is the 3-way segment (and the Smooth / Banded pair).
* :func:`rounded_mask` is the mask a native window needs for round corners
  (it cannot be translucent; MF5's remedy for the sheet).
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
    QRegion)
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QHBoxLayout, QPushButton, QSizePolicy,
    QWidget)

from foammesh.view.theming.elevation import parse_colour, token
from foammesh.view.theming.metrics import FRAME_WIDTH


def ink(name: str, fallback: str) -> QColor:
    """The theme colour ``name`` (``fallback``, the mockup's, before a theme)."""
    return parse_colour(token(name, fallback)) or parse_colour(fallback)


def blend(top: QColor, alpha: float, ground: QColor) -> QColor:
    """``top`` at ``alpha`` over an opaque ``ground``, as one opaque colour."""
    a = min(1.0, max(0.0, float(alpha)))
    return QColor(
        round(top.red() * a + ground.red() * (1 - a)),
        round(top.green() * a + ground.green() * (1 - a)),
        round(top.blue() * a + ground.blue() * (1 - a)))


def text_on(ground: QColor) -> QColor:
    """White on a dark ground, near black on a light one."""
    luma = 0.299 * ground.red() + 0.587 * ground.green() + 0.114 * ground.blue()
    return QColor('#ffffff') if luma < 150 else QColor('#101216')


def rounded_mask(width: int, height: int, radius: float) -> QRegion:
    """A region of ``width`` x ``height`` with round corners."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    return QRegion(path.toFillPolygon().toPolygon())


class LegendChip(QAbstractButton):
    """``#lchip``: swatch, label and arrow on one 22 px row.

    The frame round it (1 px border, radius 5, the legend's ground) belongs to
    the legend, which becomes this chip; the chip paints the content only.
    """

    HEIGHT = 22
    SWATCH = (28, 8)
    PAD = 8
    GAP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('legendChip')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.HEIGHT)
        self._stops: list = []
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    # -- content ----------------------------------------------------------- #

    def setSwatch(self, stops) -> None:
        """``stops`` are QColors left to right (one makes a flat swatch)."""
        self._stops = [QColor(c) for c in stops]
        self.update()

    def swatchStops(self) -> list:
        return list(self._stops)

    def sizeHint(self) -> QSize:
        font = self.font()
        font.setPixelSize(12)
        width = QFontMetrics(font).horizontalAdvance(self.text())
        return QSize(self.PAD + self.SWATCH[0] + self.GAP + width
                     + self.GAP + 8 + self.PAD, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def setText(self, text: str) -> None:
        super().setText(text)
        self.updateGeometry()

    # -- painting ------------------------------------------------------------ #

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        sw, sh = self.SWATCH
        swatch = QRectF(self.PAD, (self.height() - sh) / 2.0, sw, sh)
        if self._stops:
            gradient = QLinearGradient(swatch.left(), 0, swatch.right(), 0)
            last = max(1, len(self._stops) - 1)
            if len(self._stops) == 1:
                gradient.setColorAt(0.0, self._stops[0])
                gradient.setColorAt(1.0, self._stops[0])
            else:
                for i, colour in enumerate(self._stops):
                    gradient.setColorAt(i / last, colour)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(swatch, 2, 2)
        text = ink('foreground.legend', '#c9ced6')
        font = self.font()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(text)
        left = swatch.right() + self.GAP
        arrow = 8
        painter.drawText(
            QRectF(left, 0, self.width() - left - self.GAP - arrow - self.PAD,
                   self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self.text())
        # The drop arrow: a small filled triangle.
        cx = self.width() - self.PAD - arrow / 2.0
        cy = self.height() / 2.0
        path = QPainterPath()
        path.moveTo(cx - 3.5, cy - 1.5)
        path.lineTo(cx + 3.5, cy - 1.5)
        path.lineTo(cx, cy + 2.5)
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink('foreground.sub', '#9aa2ae'))
        painter.drawPath(path)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ink('accent.default', '#5b93f0'), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)


class _SegButton(QPushButton):
    """One cell of a :class:`Segment`."""

    def __init__(self, text: str, parent, last: bool):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._last = last

    def setLast(self, last: bool) -> None:
        self._last = last
        self.update()

    def sizeHint(self) -> QSize:
        font = self.font()
        font.setPixelSize(11)
        return QSize(QFontMetrics(font).horizontalAdvance(self.text()) + 12, 20)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        parent = self.parentWidget()
        if not self.isEnabled():
            painter.setOpacity(0.4)
        # Fill: clipped to the segment's rounded inner shape.
        clip = QPainterPath()
        if parent is not None:
            clip.addRoundedRect(QRectF(parent.rect()).adjusted(1, 1, -1, -1), 3, 3)
            clip.translate(-self.x(), -self.y())
            painter.setClipPath(clip)
        ground = None
        if self.isChecked():
            ground = ink('selection.background', '#2f5ca8')
        elif self.underMouse() and self.isEnabled():
            ground = ink('background.control', '#2a2f37')
        if ground is not None:
            painter.fillRect(self.rect(), ground)
        painter.setClipping(False)
        if not self._last:
            painter.setPen(ink('border.default', '#3a3f48'))
            painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        font = self.font()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(text_on(ground) if self.isChecked()
                       else ink('foreground.sub', '#aab0b8'))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        if self.hasFocus():
            painter.setPen(QPen(ink('accent.default', '#5b93f0'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def enterEvent(self, event):                     # noqa: N802 - Qt naming
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):                     # noqa: N802 - Qt naming
        self.update()
        super().leaveEvent(event)


class Segment(QWidget):
    """A row of mutually exclusive cells in a 1 px rounded frame.

    The chosen cell takes ``selection.background`` (mockup ``.lseg span.on``).
    Each cell is a real checkable button, so a click, a key press and a test
    ``click()`` all work and the cell keeps its object name.
    """

    #: The key of the cell the user pressed.
    activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._cells: dict = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH)
        row.setSpacing(0)
        self._row = row
        self.setFixedHeight(22)

    def add(self, key: str, text: str, *, object_name: str | None = None,
            accessible: str | None = None) -> QPushButton:
        for cell in self._cells.values():
            cell.setLast(False)
        button = _SegButton(text, self, last=True)
        if object_name:
            button.setObjectName(object_name)
        if accessible:
            button.setAccessibleName(accessible)
        button.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Expanding)
        self._group.addButton(button)
        self._row.addWidget(button, 1)
        self._cells[key] = button
        button.clicked.connect(lambda _c=False, k=key: self.activated.emit(k))
        return button

    def button(self, key: str) -> QPushButton:
        return self._cells[key]

    def keys(self) -> list:
        return list(self._cells)

    def setChecked(self, key: str) -> None:
        cell = self._cells.get(key)
        if cell is not None:
            cell.setChecked(True)

    def checked(self) -> str | None:
        for key, cell in self._cells.items():
            if cell.isChecked():
                return key
        return None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(ink('border.default', '#3a3f48'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
