"""The deviation colouring's legend, histogram and figures.

DP-713 (viewport audit 0925 F8). Deviation was painted with no legend at all:
a red patch could be 0.1 mm or 30 mm out, and nothing on screen said which
way. This is what a painted deviation now carries with it --

* a scalar bar titled with the quantity and its unit, on the one range every
  patch shares, with unmeasured faces called out in grey;
* a histogram of the deviation, its bars in the colours of the scale so it
  reads as the legend's distribution, with the tolerance band marked;
* the figures a user decides with: max |d|, mean, RMS and the share of faces
  within a tolerance the user sets (shown once, in the legend's figures).

Plan 39 VI6: the tolerance is part of the picture (the neutral band on the
model widens and narrows as it is edited), the scale is editable, a closed
reference is signed and an open one or a stored run is not, and a Surfaces
list decides which patches are measured and painted at all.

Plan 45 P2 (Q2 a). One scale: the histogram bins |d| over the same 0..scale
the model and the legend are coloured on, each bar in the shared table's
colour at its bin centre, faces past the scale in the last bar, and the
tolerance band from 0 to the tolerance. The figures are the legend's alone;
the card no longer repeats them, and no centred caption does either.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget)

from foammesh.core.quality.geometry_fidelity import live_distance
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_TIGHT, CompactDoubleSpinBox, unit_cell,
)

#: The legend's title: the quantity, then its unit.
DEVIATION_TITLE = 'Deviation |d| from reference (mm)'
#: The same for a scale that has no sign (open reference, stored run).
UNSIGNED_TITLE = 'Distance from reference, unsigned (mm)'
#: Bars in the histogram.
HISTOGRAM_BINS = 24
#: The Surfaces list's height before it scrolls.
SURFACES_HEIGHT = 110
#: Space kept between the panel and the viewport's edges.
PANEL_MARGIN = 8


def deviationScalarBar(lookupTable, tokens=None, signed=True):
    """A vtkScalarBarActor for the deviation scale, titled and themed.

    The title says whether the scale is signed (Plan 39 VI6, F4e).
    """
    from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

    bar = vtkScalarBarActor()
    bar.SetObjectName('deviationLegend')
    bar.SetLookupTable(lookupTable)
    bar.SetTitle(DEVIATION_TITLE if signed else UNSIGNED_TITLE)
    bar.SetNumberOfLabels(5)
    bar.SetLabelFormat('%.3g')
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


def _magnitudes(readout) -> np.ndarray:
    fields = getattr(readout, 'signed_fields', None) or getattr(
        readout, 'fields', None) or {}
    parts = [np.abs(np.asarray(values, dtype=float)).ravel()
             for values in fields.values()]
    magnitude = np.concatenate(parts) if parts else np.zeros(0)
    return magnitude[np.isfinite(magnitude)]


def markDeviationBar(bar, readout, tolerance=None) -> dict:
    """The picture's bar says what the screen's legend says (DP-2738/2739).

    The bar is the one a snapshot carries (the screen draws the Qt legend).
    It stopped at the scale with nothing to say faces lay past it -- a
    snapshot of the elbow read 0 to 0.242 mm with its worst label at
    1.08 mm -- and had no tolerance on it. It now carries

    * an above-range swatch in the top colour, noted with the count of faces
      past the scale and their maximum, when there are any;
    * the tolerance as a tick at its value when it lies on the bar, or in
      the note when it lies above it.

    The marks are written on the shared table (annotations and the
    above-range colour do not change how a value maps to a colour) and are
    rewritten on every refresh. Returns what was marked.
    """
    lut = bar.GetLookupTable()
    if lut is None or readout is None:
        return {}
    high = float(lut.GetTableRange()[1])
    tolerance = abs(float(
        tolerance if tolerance is not None
        else getattr(readout, 'tolerance', 0.0) or 0.0))
    magnitude = _magnitudes(readout)
    above = int(np.count_nonzero(magnitude > high * (1 + 1e-9)))
    maximum = float(magnitude.max()) if magnitude.size else 0.0
    off_scale = tolerance > high * (1 + 1e-9)
    lut.ResetAnnotations()
    if 0.0 < tolerance and not off_scale:
        lut.SetAnnotation(tolerance, f'tolerance ±{tolerance:.3g}')
    # On always: it also draws the swatches' notes ('not measured').
    bar.DrawAnnotationsOn()
    notes = []
    if above:
        faces = 'face' if above == 1 else 'faces'
        notes.append(f'above {high:.3g}: {above:,} {faces}, max {maximum:.3g}')
    if off_scale and tolerance > 0.0:
        notes.append(f'tolerance ±{tolerance:.3g} above the scale')
    if notes:
        count = lut.GetNumberOfTableValues()
        lut.SetAboveRangeColor(lut.GetTableValue(count - 1))
        lut.UseAboveRangeColorOn()
        bar.DrawAboveRangeSwatchOn()
        bar.SetAboveRangeAnnotation('\n'.join(notes))
    else:
        lut.UseAboveRangeColorOff()
        bar.DrawAboveRangeSwatchOff()
        bar.SetAboveRangeAnnotation('')
    return {'above': above, 'max': maximum, 'tolerance_tick':
            bool(tolerance > 0.0 and not off_scale),
            'tolerance_off_scale': bool(off_scale and tolerance > 0.0),
            'note': '\n'.join(notes)}


class DeviationHistogram(QWidget):
    """The distribution of |d| over 0..scale, in the colours of its scale."""

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
                        lookupTable=None, signed: bool = False):
        """``signed`` is accepted for old callers and ignored: the bars are
        always |d| from 0 to the scale (Plan 45 P2)."""
        self._edges = [float(edge) for edge in histogram.get('edges') or ()]
        self._counts = [int(count) for count in histogram.get('counts') or ()]
        self._tolerance = abs(float(tolerance))
        self._lut = lookupTable
        self.update()

    def edges(self) -> list[float]:
        return list(self._edges)

    def counts(self) -> list[int]:
        return list(self._counts)

    def valueRange(self) -> tuple:
        """The axis, ``(0, scale)``; ``()`` when nothing is measured."""
        if len(self._edges) < 2:
            return ()
        return (self._edges[0], self._edges[-1])

    def toleranceBand(self) -> tuple:
        """The shaded band, ``(0, tolerance)``; ``()`` when there is none.

        DP-2739. A tolerance past the axis's end has no band: clipped to the
        axis it shaded the whole histogram, as if every bar were the band's.
        The legend says the tolerance is above the scale.
        """
        if self._tolerance <= 0.0 or len(self._edges) < 2:
            return ()
        if self._tolerance > self._edges[-1]:
            return ()
        return (self._edges[0], self._tolerance)

    def toleranceOffScale(self) -> bool:
        """DP-2739. The tolerance lies past the axis's end."""
        return len(self._edges) >= 2 and self._tolerance > self._edges[-1]

    def axisLabels(self) -> tuple:
        """The numbers under the axis: none.

        DP-2737. The histogram runs on the legend's 0..scale, which the
        legend's bar already numbers; a second numbered axis made two scales
        for one quantity. The bars are in the legend's colours, so a bar is
        read against the bar beside the model.
        """
        return ()

    def barColours(self) -> list:
        """``[(bin centre, (r, g, b))]``, one per bar, as painted."""
        out = []
        for index in range(len(self._counts)):
            centre = 0.5 * (self._edges[index] + self._edges[index + 1])
            colour = self._barColour(centre)
            out.append((centre, (colour.redF(), colour.greenF(),
                                 colour.blueF())))
        return out

    def _barColour(self, value: float) -> QColor:
        if self._lut is None:
            return QColor(self.palette().windowText().color())
        rgb = [0.0, 0.0, 0.0]
        # The model's own table, at the bar's |d| (Plan 45 P2).
        self._lut.GetColor(abs(value), rgb)
        return QColor.fromRgbF(*rgb)

    def paintEvent(self, event):                  # noqa: N802 - Qt naming
        painter = QPainter(self)
        text = self.palette().windowText().color()
        painter.setPen(QPen(text))
        if not self._counts or len(self._edges) < 2:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self.tr('Nothing measured'))
            return
        area = QRectF(4.0, 4.0, self.width() - 8.0, self.height() - 8.0)
        low, high = self._edges[0], self._edges[-1]
        span = (high - low) or 1.0

        def x(value):
            return area.left() + (value - low) / span * area.width()

        # The tolerance band first, so the bars stand in front of it.
        band_range = self.toleranceBand()
        if band_range:
            left, right = x(band_range[0]), x(band_range[1])
            band = QColor(text)
            band.setAlpha(28)
            painter.fillRect(QRectF(left, area.top(), right - left,
                                    area.height()), band)
            if self._tolerance <= high:
                painter.setPen(QPen(text, 1, Qt.PenStyle.DashLine))
                edge = x(self._tolerance)
                painter.drawLine(int(edge), int(area.top()),
                                 int(edge), int(area.bottom()))
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


class DeviationPanel(QFrame):
    """The histogram and the Surfaces list for the deviation on screen.

    The figures (max, mean, RMS, share within tolerance) are the legend's;
    the panel keeps them in :meth:`summary` but does not repeat them.
    """

    toleranceChanged = Signal(float)
    #: The scale the ramp reaches, in mm; 0 asks for the automatic one.
    scaleChanged = Signal(float)
    #: A patch ticked or unticked in the Surfaces list.
    surfaceToggled = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('deviationPanel')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._readout = None
        self._summary = None
        #: What a live measurement is measured against. The panel opens on the
        #: loaded geometry; a Gmsh session with no geometry in the shared step
        #: is measured against its own model and says so (Plan 42 SG2).
        self._referenceName = self.tr('the loaded geometry')

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

        scaleRow = QHBoxLayout()
        scaleRow.setContentsMargins(0, 0, 0, 0)
        scaleLabel = QLabel(self.tr('Scale to'))
        self._scale = CompactDoubleSpinBox()
        self._scale.setObjectName('deviationScale')
        self._scale.setDecimals(4)
        self._scale.setRange(1.0e-6, 1.0e6)
        self._scale.setSingleStep(0.1)
        self._scale.setToolTip(self.tr(
            'The distance where the colours reach full strength. Automatic '
            'is the 98th percentile of the distances, so one bad face does '
            'not wash out the rest.'))
        self._scale.setAccessibleName(self._scale.toolTip())
        scaleLabel.setBuddy(self._scale)
        self._scaleAuto = QToolButton()
        self._scaleAuto.setObjectName('deviationScaleAuto')
        self._scaleAuto.setText(self.tr('Auto'))
        self._scaleAuto.setToolTip(self.tr(
            'Go back to the automatic scale'))
        self._scaleAuto.setAccessibleName(self._scaleAuto.toolTip())
        self._scaleAuto.setEnabled(False)
        scaleRow.addWidget(scaleLabel)
        scaleRow.addWidget(unit_cell(self._scale, 'mm'), 1)
        scaleRow.addWidget(self._scaleAuto)

        self._surfacesCaption = QLabel(self.tr('Surfaces'))
        self._surfacesCaption.setObjectName('deviationSurfacesCaption')
        self._surfaceBox = QWidget()
        self._surfaceBox.setObjectName('deviationSurfaceBox')
        self._surfaceLayout = QVBoxLayout(self._surfaceBox)
        self._surfaceLayout.setContentsMargins(0, 0, 0, 0)
        self._surfaceLayout.setSpacing(0)
        self._surfaceScroll = QScrollArea()
        self._surfaceScroll.setObjectName('deviationSurfaces')
        self._surfaceScroll.setWidgetResizable(True)
        self._surfaceScroll.setFrameShape(QFrame.Shape.NoFrame)
        self._surfaceScroll.setMaximumHeight(SURFACES_HEIGHT)
        self._surfaceScroll.setWidget(self._surfaceBox)
        self._checks: dict[str, QCheckBox] = {}

        layout.addWidget(self._title)
        layout.addWidget(self._source)
        layout.addWidget(self._histogram)
        layout.addLayout(toleranceRow)
        layout.addLayout(scaleRow)
        layout.addWidget(self._surfacesCaption)
        layout.addWidget(self._surfaceScroll)
        # Plan 43 LY11. The tolerance and the scale are controls of the
        # legend's range row (and the Mesh quality surface rows) now; these
        # stay as the panel's own copy of the values, without a place on
        # screen, so one tolerance is read by every figure.
        for hidden in (toleranceLabel, self._tolerance.parentWidget(),
                       scaleLabel, self._scale.parentWidget(),
                       self._scaleAuto):
            if hidden is not None:
                hidden.hide()
        self._tolerance.valueChanged.connect(self._toleranceEdited)
        self._scale.editingFinished.connect(self._scaleEdited)
        self._scaleAuto.clicked.connect(self._scaleReset)
        self.setFixedWidth(300)
        self.hide()
        if parent is not None:
            parent.installEventFilter(self)

    # -- data -------------------------------------------------------------- #

    def setReferenceName(self, name: str):
        """Name what the live measurement is against ('the open Gmsh model')."""
        self._referenceName = str(name or '') or self.tr('the loaded geometry')

    def setReadout(self, readout, tolerance: float | None = None):
        """Show ``readout`` (a `live_distance.DeviationReadout`)."""
        self._readout = readout
        if tolerance is not None:
            self._tolerance.blockSignals(True)
            self._tolerance.setValue(abs(float(tolerance)))
            self._tolerance.blockSignals(False)
        signed = bool(getattr(readout, 'signed', True))
        if getattr(readout, 'source', '') == 'run':
            text = self.tr(
                'Per face, from the stored geometry fidelity run. '
                'Distance from the reference, unsigned.')
        elif signed:
            text = self.tr(
                'Per face, measured now against {0}. '
                'Colour is the size of the distance, |d|; the sign is in the numbers.'
            ).format(self._referenceName)
        else:
            text = self.tr(
                'Per face, measured now against {0}. '
                'Distance from the reference, unsigned.'
            ).format(self._referenceName)
        notice = str(getattr(readout, 'notice', '') or '')
        self._source.setText(text + (' ' + self.tr(notice) if notice else ''))
        scale = float(getattr(readout, 'scale', 0.0) or 0.0)
        if scale > 0.0:
            self._scale.blockSignals(True)
            self._scale.setValue(scale)
            self._scale.blockSignals(False)
        self._setSurfaces(getattr(readout, 'surfaces', ()), readout)
        self._refresh()
        self.show()
        self.raise_()
        self._place()

    def clearReadout(self):
        self._readout = None
        self._summary = None
        self._setSurfaces((), None)
        self._scaleAuto.setEnabled(False)
        self.hide()

    def refresh(self):
        """Redraw the histogram, e.g. after the scale changed."""
        self._refresh()

    # -- the Surfaces list ------------------------------------------------- #

    def _setSurfaces(self, rows, readout):
        """Show ``[(name, ticked, has_reference, why_not)]`` as tick boxes.

        The boxes are kept when the same patches come back, so a tick that is
        being handled is never destroyed from inside its own signal.
        """
        rows = list(rows)
        names = [row[0] for row in rows]
        if names != list(self._checks):
            while self._surfaceLayout.count():
                item = self._surfaceLayout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            self._checks = {}
            for name, _ticked, _has, _why in rows:
                box = QCheckBox(name)
                box.setObjectName('deviationSurface_' + name)
                box.toggled.connect(
                    lambda on, n=name: self.surfaceToggled.emit(n, bool(on)))
                self._checks[name] = box
                self._surfaceLayout.addWidget(box)
        isRun = getattr(readout, 'source', '') == 'run'
        for name, ticked, has, why in rows:
            box = self._checks[name]
            box.blockSignals(True)
            box.setChecked(bool(ticked))
            box.blockSignals(False)
            label = name if has else self.tr('{0}  (no reference)').format(name)
            if isRun and not has:
                label = self.tr('{0}  (not in the run)').format(name)
            box.setText(label)
            # A stored run can only show the patches it measured.
            box.setEnabled(not (isRun and not has))
            box.setToolTip(
                self.tr('Measured and painted') if ticked else (
                    self.tr('Tick to measure this patch anyway')
                    if has or not isRun else
                    self.tr('The stored run has no field for this patch')))
            # Plan 48 GM12 (plan 4.14). A patch that could not be measured
            # says why, in its own words; it is never shown as a zero.
            reason = (getattr(readout, 'unavailable', None) or {}).get(name)
            if reason:
                box.setText(self.tr('{0}  (unavailable)').format(name))
                box.setToolTip(str(reason))
                box.setEnabled(False)
        self._surfacesCaption.setVisible(bool(rows))
        self._surfaceScroll.setVisible(bool(rows))

    def surfaceNames(self) -> list:
        return list(self._checks)

    def surfaceCheck(self, name) -> QCheckBox:
        return self._checks[name]

    def scaleBox(self):
        return self._scale

    def _scaleEdited(self):
        self._scaleAuto.setEnabled(True)
        self.scaleChanged.emit(float(self._scale.value()))

    def _scaleReset(self):
        self._scaleAuto.setEnabled(False)
        self.scaleChanged.emit(0.0)

    def adoptTolerance(self, value: float):
        """Take a tolerance edited elsewhere (the legend), without a signal."""
        self._tolerance.blockSignals(True)
        self._tolerance.setValue(abs(float(value)))
        self._tolerance.blockSignals(False)

    def tolerance(self) -> float:
        return float(self._tolerance.value())

    def setTolerance(self, value: float):
        """Set the tolerance as if typed (it repaints the model)."""
        self._tolerance.setValue(abs(float(value)))

    def summary(self):
        return self._summary

    def sourceText(self) -> str:
        """The sentence saying which reference the deviation is against."""
        return self._source.text()

    def isScaleAuto(self) -> bool:
        """The scale is the automatic one (Auto not lit)."""
        return not self._scaleAuto.isEnabled()

    def readState(self) -> dict:
        """Plan 48 PA1. What the panel says: source, scale, figures, bars."""
        if self._readout is None or self.isHidden():
            return {'shown': False}
        summary = self._summary
        histogram = self._histogram
        return {
            'shown': True, 'source': self.sourceText(),
            'scale_mm': float(self._scale.value()),
            'scale_auto': self.isScaleAuto(),
            'summary': None if summary is None else {
                'count': summary.count, 'unmeasured': summary.unmeasured,
                'max_abs_mm': summary.max_abs, 'mean_mm': summary.mean,
                'rms_mm': summary.rms, 'tolerance_mm': summary.tolerance,
                'within': summary.within,
                'within_fraction': summary.within_fraction},
            'histogram': {
                'edges': histogram.edges(), 'counts': histogram.counts(),
                'range': list(histogram.valueRange()),
                'tolerance_band': list(histogram.toleranceBand()),
                'tolerance_off_scale': histogram.toleranceOffScale()}}

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
        # Plan 45 P2: |d| over the model's own 0..scale; past the scale is
        # counted in the last bar (live_distance.histogram clips).
        magnitudes = [np.abs(np.asarray(v, dtype=np.float64)) for v in values]
        self._histogram.setDistribution(
            live_distance.histogram(magnitudes, HISTOGRAM_BINS,
                                    self.colourRange()),
            summary.tolerance, self._readout.lookup_table)

    def colourRange(self) -> tuple:
        """``(0, scale)``: the range the model and the legend are coloured on."""
        high = 0.0
        value_range = getattr(self._readout, 'value_range', None) or ()
        if len(value_range) == 2:
            high = abs(float(value_range[1]))
        if high <= 0.0:
            high = abs(float(getattr(self._readout, 'scale', 0.0) or 0.0))
        return (0.0, high or 1e-6)

    # -- placement --------------------------------------------------------- #

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self._place()
        return super().eventFilter(watched, event)

    def setClearArea(self, controller) -> None:
        """V16 (DP-2650). Stand in the clear area, off the floating items
        ranked before the panel, not at the view's corner under the sheet."""
        self._clearArea = controller
        self._place()

    def place(self) -> None:
        """The clear-area controller's placer."""
        self._place()

    def _place(self):
        """Bottom left of the clear area, clear of the items placed before
        it; bottom left of the viewport when no clear area is bound."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        # Plan 45 V2. The source sentence wraps: its height depends on the
        # width, and ``adjustSize`` asked the label at another width than the
        # panel's fixed 300 px, so the panel came out a line short and the
        # layout squeezed the histogram -- its axis labels ran under the
        # Surfaces caption. The height is the one the fixed width needs.
        layout = self.layout()
        if layout is not None:
            layout.activate()
            need = layout.minimumSize().height()
            if layout.hasHeightForWidth():
                need = max(need, layout.totalHeightForWidth(self.width()))
            if need > self.height():
                self.resize(self.width(), need)
        controller = getattr(self, '_clearArea', None)
        if controller is not None:
            clear = controller.clearRect()
            wanted = QRect(clear.left() + PANEL_MARGIN,
                           clear.bottom() + 1 - self.height() - PANEL_MARGIN,
                           self.width(), self.height())
            rect = controller.place_clear_of(self, wanted)
            if self.pos() != rect.topLeft():
                self.move(rect.topLeft())
        else:
            self.move(PANEL_MARGIN,
                      max(PANEL_MARGIN,
                          parent.height() - self.height() - PANEL_MARGIN))
        self._fitLayout()

    def _fitLayout(self):
        """Plan 45 V2. Lay the children out on the panel's final size.

        The clear area places the panel again from inside the panel's own
        Resize event (its float watcher), so the nested ``adjustSize``
        laid the children out at the short height and the outer resize
        never reached the layout: live, a 265 px panel kept a 232 px
        layout, the histogram overran its slot and the Surfaces caption
        sat on its "0" axis label."""
        layout = self.layout()
        if layout is None:
            return
        inner = self.contentsRect()
        if layout.geometry() != inner:
            layout.setGeometry(inner)
