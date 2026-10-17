"""Plan 44 MF12: the colour scale popover under the legend's scale button.

A 250 px top-level popup (mockup ``.scalepop``, ``M:310``): ``background.raised``,
radius 5, the ``menu`` shadow of :mod:`foammesh.view.theming.elevation`. It
holds the one-column :class:`ColourScalePanel` under a small caps header, so a
choice repaints the model and the legend at once, and in the deviation mode
the legend's tolerance and scale-to boxes appear in its Range section (the
legend is too narrow to carry them).

The popover is opened beside the legend, on the viewport side of it, and
closes on a click outside or Escape like any popup.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from foammesh.core.quality import legend_model as lm
from foammesh.view.display_control.colour_scale_panel import ColourScalePanel
from foammesh.view.display_control.legend_chip import ink
from foammesh.view.theming import elevation
from foammesh.view.theming.fonts import caps_font
from foammesh.view.theming.metrics import FORM_MARGIN, GAP

WIDTH = 250
#: Pixels between the legend and the popover.
#: Pixels kept between the popover and the screen's edge.
SCREEN_MARGIN = 8


class ScalePopover(QWidget):
    #: Told when the popover closes (the legend gets its range row back).
    closed = Signal()

    def __init__(self, parent=None, *, legend=None, settings=None):
        super().__init__(parent, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint)
        self.setObjectName('scalePopover')
        self._legend = legend
        layout = QVBoxLayout(self)
        layout.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        layout.setSpacing(GAP)
        self._header = QLabel(self.tr('Colour scale · one for every view'), self)
        self._header.setObjectName('scalePopoverHeader')
        self._header.setFont(caps_font(10))
        self._header.setWordWrap(True)
        layout.addWidget(self._header)
        self.panel = ColourScalePanel(self, settings=settings, live=True, columns=1)
        layout.addWidget(self.panel)
        self._ring = elevation.elevate(self, 'menu', paint_panel=False)
        left, top, right, bottom = self._margins()
        self.setFixedWidth(WIDTH + left + right)

    # -- shape --------------------------------------------------------------- #

    def _margins(self) -> tuple:
        """The transparent margin the shadow ring needs (0 in the fallback)."""
        return self._ring.margins if self._ring.mode == 'window' else (0, 0, 0, 0)

    def panelRect(self) -> QRectF:
        left, top, right, bottom = self._margins()
        return QRectF(self.rect()).adjusted(left, top, -right, -bottom)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(ink('background.raised', '#262a31'))
        painter.setPen(ink('border.default', '#3a3f48'))
        painter.drawRoundedRect(self.panelRect().adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)

    # -- opening and closing -------------------------------------------------- #

    def popupFor(self, legend=None) -> None:
        """Open beside ``legend`` (left of it), its top level with the legend's."""
        legend = legend or self._legend
        self._legend = legend
        deviation = (legend is not None and legend.spec() is not None
                     and legend.spec().mode == lm.DEVIATION)
        self.panel.setRangeExtra(legend.rangeWidget() if deviation else None)
        self.adjustSize()
        left, top, right, bottom = self._margins()
        if legend is not None:
            anchor = legend.mapToGlobal(QPoint(0, 0))
        else:
            anchor = QPoint(100, 100)
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        panel_w, panel_h = self.width() - left - right, self.height() - top - bottom
        x = anchor.x() - GAP - panel_w
        if x < area.left() + SCREEN_MARGIN:                  # no room on the left
            x = anchor.x() + (legend.width() if legend is not None else 0) + GAP
        y = min(anchor.y(), area.bottom() - SCREEN_MARGIN - panel_h)
        x = max(area.left() + SCREEN_MARGIN, min(x, area.right() - SCREEN_MARGIN - panel_w))
        y = max(area.top() + SCREEN_MARGIN, y)
        self.move(x - left, y - top)
        self.show()
        self.raise_()

    def hideEvent(self, event) -> None:               # noqa: N802 - Qt naming
        super().hideEvent(event)
        self.panel.setRangeExtra(None)
        if self._legend is not None:
            self._legend.restoreRange()
        self.closed.emit()
