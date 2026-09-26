"""The deviation colouring's legend, histogram and figures.

DP-713 (viewport audit 0925 F8). Deviation was painted with no legend at all:
a red patch could be 0.1 mm or 30 mm out, and nothing on screen said which
way. This is what a painted deviation now carries with it --

* a scalar bar titled with the quantity and its unit, on the one range every
  patch shares, with unmeasured faces called out in grey;
* a histogram of the signed deviation, its bars in the colours of the scale so
  it reads as the legend's distribution, with the tolerance band marked;
* the figures a user decides with: max |d|, mean, RMS and the share of faces
  within a tolerance the user sets.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
    QWidget)

from foammesh.core.quality.geometry_fidelity import live_distance
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_TIGHT, CompactDoubleSpinBox, unit_cell,
)

#: The legend's title: the quantity, then its unit.
DEVIATION_TITLE = 'Deviation from reference (mm)'
#: Bars in the histogram.
HISTOGRAM_BINS = 24
#: Space kept between the panel and the viewport's edges.
PANEL_MARGIN = 8


def deviationScalarBar(lookupTable, tokens=None):
    """A vtkScalarBarActor for the deviation scale, titled and themed."""
    from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

    bar = vtkScalarBarActor()
    bar.SetObjectName('deviationLegend')
    bar.SetLookupTable(lookupTable)
    bar.SetTitle(DEVIATION_TITLE)
    bar.SetNumberOfLabels(5)
    bar.SetLabelFormat('%+.3g')
    bar.UnconstrainedFontSizeOn()
    bar.SetTitleRatio(0.5)
    bar.SetBarRatio(0.25)
    # Unmeasured faces are grey on screen; the legend says what grey means.
    bar.DrawNanAnnotationOn()
    bar.SetNanAnnotation('not measured')
    # Wide enough that the centred title stays inside the viewport.
    bar.SetWidth(0.2)
    bar.SetHeight(0.45)
    bar.SetPosition(0.78, 0.05)
    if tokens is not None:
        from foammesh.view.theming.vtk_theme import apply_scalar_bar_theme
        apply_scalar_bar_theme(bar, tokens)
    return bar


class DeviationHistogram(QWidget):
    """The signed deviation's distribution, in the colours of its scale."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('deviationHistogram')
        self.setMinimumSize(260, 110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        self._edges: list[float] = []
        self._counts: list[int] = []
        self._tolerance = 0.0
        self._lut = None

    def setDistribution(self, histogram: dict, tolerance: float,
                        lookupTable=None):
        self._edges = [float(edge) for edge in histogram.get('edges') or ()]
        self._counts = [int(count) for count in histogram.get('counts') or ()]
        self._tolerance = abs(float(tolerance))
        self._lut = lookupTable
        self.update()

    def edges(self) -> list[float]:
        return list(self._edges)

    def counts(self) -> list[int]:
        return list(self._counts)

    def _barColour(self, value: float) -> QColor:
        if self._lut is None:
            return QColor(self.palette().windowText().color())
        rgb = [0.0, 0.0, 0.0]
        self._lut.GetColor(value, rgb)
        return QColor.fromRgbF(*rgb)

    def paintEvent(self, event):                  # noqa: N802 - Qt naming
        painter = QPainter(self)
        text = self.palette().windowText().color()
        painter.setPen(QPen(text))
        if not self._counts or len(self._edges) < 2:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self.tr('Nothing measured'))
            return
        labelHeight = float(self.fontMetrics().height() + 2)
        area = QRectF(4.0, 4.0, self.width() - 8.0,
                      self.height() - 8.0 - labelHeight)
        low, high = self._edges[0], self._edges[-1]
        span = (high - low) or 1.0

        def x(value):
            return area.left() + (value - low) / span * area.width()

        # The tolerance band first, so the bars stand in front of it.
        if self._tolerance > 0.0:
            left = x(max(low, -self._tolerance))
            right = x(min(high, self._tolerance))
            band = QColor(text)
            band.setAlpha(28)
            painter.fillRect(QRectF(left, area.top(), right - left,
                                    area.height()), band)
            dashed = QPen(text, 1, Qt.PenStyle.DashLine)
            painter.setPen(dashed)
            for edge in (-self._tolerance, self._tolerance):
                if low <= edge <= high:
                    painter.drawLine(int(x(edge)), int(area.top()),
                                     int(x(edge)), int(area.bottom()))
        tallest = max(self._counts) or 1
        for index, count in enumerate(self._counts):
            if count <= 0:
                continue
            start, end = self._edges[index], self._edges[index + 1]
            height = max(1.0, count / tallest * area.height())
            bar = QRectF(x(start), area.bottom() - height,
                         max(1.0, x(end) - x(start) - 1.0), height)
            painter.fillRect(bar, self._barColour(0.5 * (start + end)))
            painter.setPen(QPen(text, 0.5))
            painter.drawRect(bar)
        painter.setPen(QPen(text))
        painter.drawLine(int(area.left()), int(area.bottom()),
                         int(area.right()), int(area.bottom()))
        labels = QRectF(area.left(), area.bottom() + 2, area.width(),
                        labelHeight)
        painter.drawText(labels, Qt.AlignmentFlag.AlignLeft,
                         f'{low:+.3g}')
        painter.drawText(labels, Qt.AlignmentFlag.AlignHCenter, '0')
        painter.drawText(labels, Qt.AlignmentFlag.AlignRight,
                         f'{high:+.3g} mm')


class DeviationPanel(QFrame):
    """Histogram and figures for the deviation on screen, over the viewport."""

    toleranceChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('deviationPanel')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._readout = None
        self._summary = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(FORM_MARGIN, GAP, FORM_MARGIN, GAP)
        layout.setSpacing(GAP_TIGHT)
        self._title = QLabel(self.tr(DEVIATION_TITLE))
        self._title.setObjectName('deviationCaption')
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._source = QLabel()
        self._source.setObjectName('deviationSource')
        self._source.setWordWrap(True)
        self._histogram = DeviationHistogram(self)
        self._stats = QLabel()
        self._stats.setObjectName('deviationStats')
        self._stats.setWordWrap(True)
        self._stats.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        toleranceRow = QHBoxLayout()
        toleranceRow.setContentsMargins(0, 0, 0, 0)
        toleranceLabel = QLabel(self.tr('Tolerance ±'))
        self._tolerance = CompactDoubleSpinBox()
        self._tolerance.setObjectName('deviationTolerance')
        self._tolerance.setDecimals(4)
        self._tolerance.setRange(0.0, 1.0e6)
        self._tolerance.setSingleStep(0.01)
        self._tolerance.setToolTip(self.tr(
            'Faces within this distance of the reference count as on it'))
        self._tolerance.setAccessibleName(self._tolerance.toolTip())
        toleranceLabel.setBuddy(self._tolerance)
        toleranceRow.addWidget(toleranceLabel)
        toleranceRow.addWidget(unit_cell(self._tolerance, 'mm'), 1)

        layout.addWidget(self._title)
        layout.addWidget(self._source)
        layout.addWidget(self._histogram)
        layout.addWidget(self._stats)
        layout.addLayout(toleranceRow)
        self._tolerance.valueChanged.connect(self._toleranceEdited)
        self.setFixedWidth(300)
        self.hide()
        if parent is not None:
            parent.installEventFilter(self)

    # -- data -------------------------------------------------------------- #

    def setReadout(self, readout, tolerance: float | None = None):
        """Show ``readout`` (a `live_distance.DeviationReadout`)."""
        self._readout = readout
        if tolerance is not None:
            self._tolerance.blockSignals(True)
            self._tolerance.setValue(abs(float(tolerance)))
            self._tolerance.blockSignals(False)
        self._source.setText(
            self.tr('Per face, from the stored geometry fidelity run.')
            if getattr(readout, 'source', '') == 'run' else
            self.tr('Per face, measured now against the loaded geometry. '
                    'Blue is short of the surface, red is past it.'))
        self._refresh()
        self.show()
        self.raise_()
        self._place()

    def clearReadout(self):
        self._readout = None
        self._summary = None
        self.hide()

    def tolerance(self) -> float:
        return float(self._tolerance.value())

    def setTolerance(self, value: float):
        self._tolerance.setValue(abs(float(value)))

    def summary(self):
        return self._summary

    def statsText(self) -> str:
        return self._stats.text()

    def histogram(self) -> DeviationHistogram:
        return self._histogram

    def _toleranceEdited(self, value):
        self._refresh()
        self.toleranceChanged.emit(float(value))

    def _refresh(self):
        if self._readout is None:
            return
        values = self._readout.values()
        summary = live_distance.summarize(values, self.tolerance())
        self._summary = summary
        self._histogram.setDistribution(
            live_distance.histogram(values, HISTOGRAM_BINS,
                                    self._readout.value_range),
            summary.tolerance, self._readout.lookup_table)
        lines = [
            # A figure never wraps away from its name or its unit.
            self.tr('max |d| {0:.3g} mm   mean\u00a0{1:+.3g}\u00a0mm   '
                    'RMS\u00a0{2:.3g}\u00a0mm').format(
                        summary.max_abs, summary.mean, summary.rms),
            self.tr('{0:.1f}% of {1:,} faces within ±{2:.3g} mm').format(
                100.0 * summary.within_fraction, summary.count,
                summary.tolerance)]
        if summary.unmeasured:
            lines.append(self.tr('{0:,} faces not measured (grey)').format(
                summary.unmeasured))
        self._stats.setText('\n'.join(lines))

    # -- placement --------------------------------------------------------- #

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self._place()
        return super().eventFilter(watched, event)

    def _place(self):
        """Bottom left of the viewport, clear of the parts list above it."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        self.move(PANEL_MARGIN,
                  max(PANEL_MARGIN,
                      parent.height() - self.height() - PANEL_MARGIN))
