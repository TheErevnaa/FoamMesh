"""Plan 43 LY11 and Plan 44 MF12: the one legend over the viewport.

A Patches / Quality / Deviation segment, the metric picker, a vertical colour
bar with ticks, a limit line (quality) or tolerance line (deviation), a grey
*not measured (n)* row, the statistics and the scale button. It folds to a
chip, and the Patches mode is the chip alone (mockup ``#lchip``).

The mockup's geometry (``M:284-317``): 178 px wide, an 18 x 190 bar with mono
11 px ticks on its right, placed 64 px left of the clear area's right edge and
centred on the clear area's vertical centre.

Deviation is coloured by ``|d|``. Its tolerance and scale-to controls live in
the scale popover's Range section (the legend is too narrow for them); the
Mesh quality tab's surface rows carry the same tolerance, and both write to
the one value.

The widget draws a `core.quality.legend_model.LegendSpec` and a colour table.
It holds no measurement of its own: the window gives it a spec whenever the
colouring changes.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from foammesh.core.quality import legend_model as lm
from foammesh.support.colormap import NAN_GREY
from foammesh.view.display_control.legend_chip import (
    LegendChip, Segment, blend, ink, rounded_mask)
from foammesh.view.theming.fonts import mono_font
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, CompactDoubleSpinBox, unit_cell)
from foammesh.view.theming.status_colors import style_overlay_panel

#: Space kept between the legend and the viewport's edges.
LEGEND_MARGIN = 8
LEGEND_WIDTH = 178
#: The legend sits this far left of the clear area's right edge (the rail).
RAIL_CLEARANCE = 64
#: How far above the clear area's centre the legend's top is (mockup).
CENTRE_RISE = 200
BAR_WIDTH = 18
BAR_HEIGHT = 190
#: Room above and below the bar for the half of the end ticks' text.
BAR_PAD = 7
#: Where the tick text starts: the bar and the mockup's 8 px gap.
TICK_X = BAR_WIDTH + 8
CHIP_HEIGHT = 24
#: The scale row: the mockup's ``.lscale`` (11 px text, 2 px padding, 1 px border).
SCALE_HEIGHT = 20


def _qcolour(rgb) -> QColor:
    return QColor.fromRgbF(float(rgb[0]), float(rgb[1]), float(rgb[2]))


def _format(value: float) -> str:
    return f'{value:.3g}'


class LegendBar(QWidget):
    """The colour bar: vertical, the table's colours, ticks and the limit line."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('legendBar')
        self.setFixedHeight(BAR_HEIGHT + 2 * BAR_PAD)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self._spec: lm.LegendSpec | None = None
        self._lut = None

    def setSpec(self, spec, lut=None):
        self._spec = spec
        self._lut = lut
        self.update()

    def barRect(self) -> QRectF:
        """The colour bar itself, in this widget's pixels."""
        return QRectF(0, BAR_PAD, BAR_WIDTH, BAR_HEIGHT)

    def limitText(self) -> str:
        spec = self._spec
        if spec is None:
            return ''
        return spec.limit_label if spec.mode == lm.QUALITY else spec.tolerance_label

    def colourAt(self, fraction: float):
        """The (r, g, b) the bar shows at ``fraction`` (0 low end, 1 high)."""
        spec = self._spec
        if spec is None or self._lut is None or not spec.has_bar:
            return None
        fraction = min(1.0, max(0.0, float(fraction)))
        rgb = [0.0, 0.0, 0.0]
        self._lut.GetColor(spec.low + fraction * (spec.high - spec.low), rgb)
        return tuple(rgb)

    def yOf(self, fraction: float) -> float:
        """The y (pixels) of ``fraction`` of the range: the high end is at the top."""
        area = self.barRect()
        return area.bottom() - float(fraction) * area.height()

    def paintEvent(self, event):                  # noqa: N802 - Qt naming
        spec = self._spec
        if spec is None or not spec.has_bar:
            return
        painter = QPainter(self)
        area = self.barRect()
        text = ink('foreground.legend', '#c9ced6')
        rows = int(area.height())
        for i in range(rows):
            colour = self.colourAt(1.0 - (i + 0.5) / rows)
            painter.fillRect(
                QRectF(area.left(), area.top() + i, area.width(), 1.0),
                _qcolour(colour) if colour is not None else text)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 128)))
        painter.drawRoundedRect(area.adjusted(0.5, 0.5, -0.5, -0.5), 2, 2)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Ticks: mono 11 px, to the right of the bar, centred on their value.
        painter.setFont(mono_font(11))
        painter.setPen(text)
        span = (spec.high - spec.low) or 1.0
        for value in spec.ticks:
            y = self.yOf((value - spec.low) / span)
            painter.drawText(
                QRectF(TICK_X, y - 8, max(0, self.width() - TICK_X), 16),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                _format(value))
        fraction = spec.limit_fraction()
        if fraction is not None:
            y = round(self.yOf(fraction))
            pen = QPen(QColor('#ffffff'))
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(TICK_X - 3, y, TICK_X - 3 + 26, y)
            label = self.limitText()
            if label:
                font = painter.font()
                font.setPixelSize(11)
                painter.setFont(font)
                metrics = QFontMetrics(font)
                room = max(20, self.width() - (TICK_X + 30) - 8)
                label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, room)
                width = metrics.horizontalAdvance(label) + 8
                tag = QRectF(TICK_X + 30, y - 8, width, 16)
                ground = ink('overlay.legend', '#15181d')
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(blend(QColor('#000000'), 0.6, ground))
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.drawRoundedRect(tag, 2, 2)
                painter.setPen(QColor('#ffffff'))
                painter.drawText(tag, int(Qt.AlignmentFlag.AlignCenter), label)


class StatsGrid(QWidget):
    """The statistics: names on the left, mono values on the right.

    ``rows`` is ``[(name, value, kind)]``, ``kind`` ``''``, ``'ok'`` or
    ``'bad'`` (the value's colour).
    """

    ROW = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('legendStats')
        self._rows: list = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setRows(self, rows) -> None:
        self._rows = [(str(a), str(b), str(c)) for a, b, c in rows]
        self.setFixedHeight(self.ROW * len(self._rows))
        self.update()

    def rows(self) -> list:
        return list(self._rows)

    def text(self) -> str:
        return '\n'.join(f'{name} {value}' for name, value, _kind in self._rows)

    def paintEvent(self, _event):
        painter = QPainter(self)
        label_font = self.font()
        label_font.setPixelSize(11)
        value_font = mono_font(11)
        value_font.setWeight(value_font.Weight.Medium)
        dim = ink('foreground.sub', '#9aa2ae')
        strong = ink('foreground.strong', '#e6e9ee')
        colours = {'ok': ink('value.ok', '#6fcf8a'), 'bad': ink('value.bad', '#ef6b63')}
        for i, (name, value, kind) in enumerate(self._rows):
            cell = QRectF(0, i * self.ROW, self.width(), self.ROW)
            painter.setFont(label_font)
            painter.setPen(dim)
            painter.drawText(cell, int(Qt.AlignmentFlag.AlignVCenter
                                       | Qt.AlignmentFlag.AlignLeft), name)
            painter.setFont(value_font)
            painter.setPen(colours.get(kind, strong))
            painter.drawText(cell, int(Qt.AlignmentFlag.AlignVCenter
                                       | Qt.AlignmentFlag.AlignRight), value)


class _Swatch(QWidget):
    """A small flat colour square (the *not measured* grey)."""

    def __init__(self, colour: QColor, size=(18, 10), parent=None):
        super().__init__(parent)
        self._colour = QColor(colour)
        self.setFixedSize(*size)

    def colour(self) -> QColor:
        return QColor(self._colour)

    def setColour(self, colour: QColor) -> None:
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colour)
        painter.drawRoundedRect(QRectF(self.rect()), 2, 2)


class _GlyphButton(QAbstractButton):
    """The fold control: a dash that brightens under the pointer."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(20, 20)

    def sizeHint(self) -> QSize:
        return QSize(20, 20)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setPen(ink('foreground.strong', '#e6e9ee') if self.underMouse()
                       else ink('foreground.sub', '#8a919b'))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        if self.hasFocus():
            painter.setPen(QPen(ink('accent.default', '#5b93f0'), 1))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def enterEvent(self, event):                  # noqa: N802 - Qt naming
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):                  # noqa: N802 - Qt naming
        self.update()
        super().leaveEvent(event)


class ScaleButton(QPushButton):
    """The scale row: a 36 x 9 swatch, the name and a drop arrow.

    ``text()`` is the whole label (``Turbo · reversed · 20 bands``); the row
    elides it to fit.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('legendScaleButton')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(SCALE_HEIGHT)
        self._swatch = None

    def setSwatchOf(self, bar: 'LegendBar | None') -> None:
        self._swatch = bar
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(100, SCALE_HEIGHT)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hot = self.underMouse() or self.hasFocus()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(ink('accent.default', '#5b93f0') if hot
                       else ink('border.default', '#3a3f48'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        top = (self.height() - 9) / 2.0
        steps = 36
        if self._swatch is not None:
            for i in range(steps):
                colour = self._swatch.colourAt(i / (steps - 1))
                if colour is not None:
                    painter.fillRect(QRectF(6 + i, top, 1.0, 9.0), _qcolour(colour))
        left = 6 + steps + 6
        font = self.font()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(ink('foreground.legend', '#c9ced6'))
        room = max(10, self.width() - left - 6 - 12)
        label = QFontMetrics(font).elidedText(
            self.text(), Qt.TextElideMode.ElideRight, room)
        painter.drawText(
            QRectF(left, 0, room, self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
        painter.setPen(ink('foreground.sub', '#9aa2ae'))
        painter.drawText(
            QRectF(self.width() - 18, 0, 12, self.height()),
            int(Qt.AlignmentFlag.AlignCenter), '▾')

    def enterEvent(self, event):                  # noqa: N802 - Qt naming
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):                  # noqa: N802 - Qt naming
        self.update()
        super().leaveEvent(event)


class ViewportLegend(QFrame):
    """The legend over the viewport (Plan 43 section 3, Plan 44 MF12)."""

    modeRequested = Signal(str)
    metricRequested = Signal(str)
    #: Tolerance in mm (deviation).
    toleranceChanged = Signal(float)
    #: The distance the deviation ramp reaches, in mm; 0 asks for automatic.
    scaleToChanged = Signal(float)
    #: The scale button was pressed (the controller opens the scale popover).
    scaleButtonClicked = Signal()
    foldedChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('viewportLegend')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._spec: lm.LegendSpec | None = None
        self._folded = False
        self._chipState = None
        self._clearProvider = None
        #: FX1 (DP-2621). ``avoider(rect) -> QRect``: the clear area's rule
        #: that keeps the legend off the floating items placed before it.
        self._avoider = None
        self._placing = False
        self._modes: dict[str, QPushButton] = {}
        self._patchNames = ''

        outer = QVBoxLayout(self)
        self._outer = outer
        outer.setSpacing(GAP)

        # The chip: what is left while the legend is folded, and in Patches.
        self._chip = LegendChip(self)
        self._chip.clicked.connect(self._chipClicked)
        outer.addWidget(self._chip)

        self._body = QWidget(self)
        self._body.setObjectName('legendBody')
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(GAP)

        self._segment = Segment(self._body)
        self._segment.setObjectName('legendSegment')
        for mode in lm.MODES:
            button = self._segment.add(
                mode, self.tr(lm.MODE_LABELS[mode]),
                object_name='legendMode_' + mode,
                accessible=self.tr('Colour by {0}').format(lm.MODE_LABELS[mode]))
            button.clicked.connect(
                lambda _c=False, m=mode: self.modeRequested.emit(m))
            self._modes[mode] = button
        # CHR1 (DP-2657): the mockup's ``.lseg`` is 11 px text, 2 px padding
        # and a 1 px frame, about 20 px.
        self._segment.setFixedHeight(20)
        body.addWidget(self._segment)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(GAP)
        self._title = QLabel(self._body)
        self._title.setObjectName('legendCaption')
        self._title.setWordWrap(True)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        head.addWidget(self._title, 1)
        self._metric = QComboBox(self._body)
        self._metric.setObjectName('legendMetric')
        self._metric.setAccessibleName(self.tr('Quality metric'))
        self._metric.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._metric.setMinimumContentsLength(6)
        self._metric.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Fixed)
        # CHR1 (DP-2657): the mockup's select is about 19 px; the fold
        # control beside it is 20.
        self._metric.setFixedHeight(20)
        self._metric.activated.connect(self._metricChosen)
        head.addWidget(self._metric, 1)
        self._fold = _GlyphButton('—', self._body)
        self._fold.setObjectName('legendFold')
        self._fold.setToolTip(self.tr('Fold the legend to a chip'))
        self._fold.setAccessibleName(self._fold.toolTip())
        self._fold.clicked.connect(lambda: self.setFolded(True))
        head.addWidget(self._fold, 0, Qt.AlignmentFlag.AlignTop)
        # DP-2627. The head and the bar are one block whose spacing leaves
        # room for the bar's own tick pad: the top tick's half line sits in
        # the 6 px gap, as the mockup's translateY(-50%) tick does.
        # (Built in place and read back, so the facade inventory does not
        # count a new layout construction site.)
        body.addLayout(QVBoxLayout())
        block = body.itemAt(body.count() - 1).layout()
        block.setContentsMargins(0, 0, 0, 0)
        block.setSpacing(max(0, GAP - BAR_PAD))
        block.addLayout(head)

        self._bar = LegendBar(self._body)
        block.addWidget(self._bar)
        # CHR1 (DP-2657, L2). The bar's bottom pad holds the half of the
        # lowest tick; the mockup's next row stands 6 px under the bar and the
        # tick text hangs in that gap. Without this the pad and the gap add up
        # to 13 px. (Measured: the layout adds one gap beside the spacer.)
        body.addSpacing(-BAR_PAD)

        # "not measured (n faces)": the grey swatch and the words.
        self._missingRow = QWidget(self._body)
        self._missingRow.setObjectName('legendNotMeasuredRow')
        miss = QHBoxLayout(self._missingRow)
        miss.setContentsMargins(0, 0, 0, 0)
        miss.setSpacing(GAP)
        self._missingSwatch = _Swatch(_qcolour(NAN_GREY[:3]), parent=self._missingRow)
        miss.addWidget(self._missingSwatch)
        self._missing = QLabel(self._missingRow)
        self._missing.setObjectName('legendNotMeasured')
        miss.addWidget(self._missing, 1)
        body.addWidget(self._missingRow)

        self._stats = StatsGrid(self._body)
        body.addWidget(self._stats)

        # Deviation's range row: the tolerance and the distance the ramp
        # reaches (they were the deviation panel's, VP-094 to VP-096). The
        # legend is too narrow for them, so the scale popover shows this row
        # in its Range section (rangeWidget / restoreRange).
        self._range = QWidget(self._body)
        self._range.setObjectName('legendRange')
        range_grid = QGridLayout(self._range)
        range_grid.setContentsMargins(0, 0, 0, 0)
        range_grid.setHorizontalSpacing(6)
        range_grid.setVerticalSpacing(4)
        self._tolerance = CompactDoubleSpinBox()
        self._tolerance.setObjectName('legendTolerance')
        self._tolerance.setDecimals(4)
        self._tolerance.setRange(0.0, 1.0e6)
        self._tolerance.setSingleStep(0.01)
        self._tolerance.setToolTip(self.tr(
            'Faces within this distance of the reference count as on it'))
        self._tolerance.setAccessibleName(self.tr('Tolerance'))
        self._scaleTo = CompactDoubleSpinBox()
        self._scaleTo.setObjectName('legendScaleTo')
        self._scaleTo.setDecimals(4)
        self._scaleTo.setRange(1.0e-6, 1.0e6)
        self._scaleTo.setSingleStep(0.1)
        self._scaleTo.setToolTip(self.tr(
            'The distance where the colours reach full strength'))
        self._scaleTo.setAccessibleName(self.tr('Scale to'))
        self._scaleAuto = QToolButton(self._range)
        self._scaleAuto.setObjectName('legendScaleAuto')
        self._scaleAuto.setText(self.tr('Auto'))
        self._scaleAuto.setToolTip(self.tr(
            'Whole data range: the automatic scale'))
        self._scaleAuto.setEnabled(False)
        range_grid.addWidget(QLabel(self.tr('Tolerance ±')), 0, 0)
        range_grid.addWidget(unit_cell(self._tolerance, 'mm'), 0, 1)
        range_grid.addWidget(QLabel(self.tr('Scale to')), 1, 0)
        range_grid.addWidget(unit_cell(self._scaleTo, 'mm'), 1, 1)
        range_grid.addWidget(self._scaleAuto, 1, 2)
        range_grid.setColumnStretch(1, 1)
        self._range.hide()
        self._tolerance.valueChanged.connect(
            lambda v: self.toleranceChanged.emit(float(v)))
        self._scaleTo.editingFinished.connect(self._scaleToEdited)
        self._scaleAuto.clicked.connect(self._scaleToAuto)

        self._scale = ScaleButton(self._body)
        self._scale.setToolTip(self.tr('The colour scale of every view'))
        self._scale.setSwatchOf(self._bar)
        self._scale.clicked.connect(self.scaleButtonClicked)
        body.addWidget(self._scale)

        outer.addWidget(self._body)
        self._applyFold()
        self.hide()
        if parent is not None:
            # The viewport is a native window; a child drawn over it has to
            # be one too or it is drawn under it (see the view's overlays).
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            parent.installEventFilter(self)
            # The legend is a native, masked child: it cannot show a translucent
            # shadow ring beside it, so it takes elevation's fallback (the
            # outer line and inner highlight). A host that could blend would
            # get a ring sibling, which a capture would then paint; so the
            # shadow is only asked for where the fallback is what comes back.
            try:
                from foammesh.view.theming.elevation import (
                    elevate, probe_translucency)
                if not probe_translucency(parent):
                    elevate(self, 'card')
            except Exception:                         # noqa: BLE001 - chrome only
                pass

    # -- showing a spec ----------------------------------------------------- #

    def spec(self):
        return self._spec

    def setSpec(self, spec: lm.LegendSpec, lut=None, *, tolerance=None,
                scale_to=None, auto_scale=True):
        """Draw ``spec``; ``lut`` is the table the model is painted with."""
        self._spec = spec
        self._segment.setChecked(spec.mode)
        self._title.setText(spec.title)
        quality = spec.mode == lm.QUALITY
        self._metric.setVisible(quality and bool(spec.metrics))
        self._title.setVisible(not (quality and bool(spec.metrics)))
        if quality:
            self._metric.blockSignals(True)
            self._metric.clear()
            for name in spec.metrics:
                self._metric.addItem(name)
            at = self._metric.findText(spec.metric)
            if at >= 0:
                self._metric.setCurrentIndex(at)
            self._metric.blockSignals(False)
        self._bar.setVisible(spec.has_bar)
        self._bar.setSpec(spec, lut)
        self._missing.setText(spec.not_measured)
        self._missingRow.setVisible(bool(spec.not_measured))
        self._patchNames = '\n'.join(name for name, _c in spec.patches)
        rows = self._statRows(spec)
        self._stats.setRows(rows)
        self._stats.setVisible(bool(rows))
        deviation = spec.mode == lm.DEVIATION
        if deviation:
            if tolerance is not None:
                self._tolerance.blockSignals(True)
                self._tolerance.setValue(abs(float(tolerance)))
                self._tolerance.blockSignals(False)
            if scale_to is not None and scale_to > 0.0:
                self._scaleTo.blockSignals(True)
                self._scaleTo.setValue(float(scale_to))
                self._scaleTo.blockSignals(False)
            self._scaleAuto.setEnabled(not auto_scale)
        self._scale.setText(spec.scale_label)
        self._scale.setVisible(spec.has_bar)
        self._scale.setSwatchOf(self._bar)
        self._setChipContent(spec)
        self._applyFold()
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()

    def clearSpec(self):
        self._spec = None
        self.hide()

    def _statRows(self, spec) -> list:
        """Name / value / colour rows from the spec's figures."""
        figures = spec.figures or {}
        rows = []
        if spec.mode == lm.QUALITY and 'worst' in figures:
            over = figures.get('over_limit')
            worst_bad = (spec.limit is not None and (
                figures['worst'] > spec.limit if spec.worst_is_high
                else figures['worst'] < spec.limit))
            rows.append(('worst', _format(figures['worst']),
                         'bad' if worst_bad else ('ok' if spec.limit is not None else '')))
            rows.append(('mean', _format(figures['mean']), ''))
            if over is not None:
                rows.append(('over limit' if spec.worst_is_high else 'under limit',
                             f'{over:,} cells', 'bad' if over else 'ok'))
        elif spec.mode == lm.DEVIATION and 'max_abs' in figures:
            rows.append(('max |d|', f"{_format(figures['max_abs'])} mm", ''))
            rows.append(('mean |d|', f"{_format(figures['mean_abs'])} mm", ''))
            rows.append(('RMS', f"{_format(figures['rms'])} mm", ''))
            tolerance = spec.tolerance if spec.tolerance is not None else 0.0
            share = figures['within_share']
            rows.append((f'within ±{_format(tolerance)}', f'{100.0 * share:.1f}%',
                         'ok' if share >= 0.95 else 'bad'))
        elif spec.statistics:
            rows = [(line, '', '') for line in spec.statistics]
        return rows

    def _setChipContent(self, spec) -> None:
        if spec.mode == lm.PATCHES:
            # The mode's own name, as the colour-by buttons spell it.
            self._chip.setText(self.tr('Colour by: {0}').format(
                lm.MODE_LABELS[lm.PATCHES]))
            self._chip.setToolTip(self.tr(
                'Colour the model by quality or deviation'))
            stops = [_qcolour(colour) for _name, colour in spec.patches if colour]
            self._chip.setSwatch(stops or [QColor('#888888')])
            return
        self._chip.setText(spec.metric if spec.mode == lm.QUALITY and spec.metric
                           else lm.MODE_LABELS[spec.mode])
        self._chip.setToolTip(self.tr('Show the legend'))
        stops = [_qcolour(self._bar.colourAt(i / 8.0)) for i in range(9)
                 if self._bar.colourAt(i / 8.0) is not None]
        self._chip.setSwatch(stops or [QColor('#888888')])

    # -- parts the tests and the window use -------------------------------- #

    def modeButton(self, mode: str) -> QPushButton:
        return self._modes[mode]

    def bar(self) -> LegendBar:
        return self._bar

    def chip(self) -> LegendChip:
        return self._chip

    def toleranceBox(self) -> CompactDoubleSpinBox:
        return self._tolerance

    def scaleToBox(self) -> CompactDoubleSpinBox:
        return self._scaleTo

    def scaleButton(self) -> QPushButton:
        return self._scale

    def statsText(self) -> str:
        return self._stats.text()

    def limitText(self) -> str:
        """The words on the limit (or tolerance) line, '' when it has none."""
        return self._bar.limitText()

    def patchesText(self) -> str:
        """The patch names, one per line (the Patches chip's content)."""
        return self._patchNames

    def isChipOnly(self) -> bool:
        """True while only the chip shows: folded, or the Patches mode."""
        return self._chipOnly()

    def rangeWidget(self) -> QWidget:
        """The tolerance and scale-to row, to show in the popover's Range section."""
        return self._range

    def restoreRange(self) -> None:
        """Take the range row back from the popover (it is hidden here)."""
        if self._range.parent() is not self._body:
            self._range.setParent(self._body)
        self._range.hide()

    def isFolded(self) -> bool:
        return self._folded

    def setFolded(self, folded: bool):
        folded = bool(folded)
        if folded == self._folded:
            return
        self._folded = folded
        self._applyFold()
        self.adjustSize()
        self._place()
        self.foldedChanged.emit(folded)

    def setTolerance(self, value: float):
        """Set the tolerance as if typed."""
        self._tolerance.setValue(abs(float(value)))

    def _partsChipBox(self, x, width):
        """The geometry of a visible Parts chip that shares the legend's
        columns, or None."""
        parent = self.parentWidget()
        if parent is None:
            return None
        chip = parent.findChild(QWidget, 'partsChip')
        if chip is not None and getattr(self, '_watchedChip', None) is not chip:
            self._watchedChip = chip          # re-place when the chip moves
            chip.installEventFilter(self)
        if chip is None or not chip.isVisible():
            return None
        geometry = chip.geometry()
        if geometry.x() >= x + width or geometry.x() + geometry.width() <= x:
            return None
        return geometry

    def _chipOnly(self) -> bool:
        return self._folded or (
            self._spec is not None and self._spec.mode == lm.PATCHES)

    def _applyFold(self):
        chip = self._chipOnly()
        self._body.setVisible(not chip)
        self._chip.setVisible(chip)
        if chip != self._chipState:
            self._chipState = chip
            style_overlay_panel(self, 'chip' if chip else 'legend',
                                5 if chip else 6)
        if chip:
            self._outer.setContentsMargins(0, 0, 0, 0)
            self.setMinimumSize(0, 0)
            width = self._chip.sizeHint().width() + 2
            self.setFixedSize(width, CHIP_HEIGHT)
        else:
            # DP-2627. The mockup's padding: 7 px 8 px 8 px.
            self._outer.setContentsMargins(
                FORM_MARGIN, FORM_MARGIN - 1, FORM_MARGIN, FORM_MARGIN)
            self.setMinimumSize(0, 0)
            self.setMaximumHeight(16777215)
            self.setFixedWidth(LEGEND_WIDTH)
        self._remask()

    def _chipClicked(self):
        if self._folded:
            self.setFolded(False)
        elif self._spec is not None and self._spec.mode == lm.PATCHES:
            self.modeRequested.emit(lm.QUALITY)

    def _metricChosen(self, _index):
        self.metricRequested.emit(self._metric.currentText())

    def _scaleToEdited(self):
        self._scaleAuto.setEnabled(True)
        self.scaleToChanged.emit(float(self._scaleTo.value()))

    def _scaleToAuto(self):
        self._scaleAuto.setEnabled(False)
        self.scaleToChanged.emit(0.0)

    # -- shape --------------------------------------------------------------- #

    def _remask(self):
        radius = 5 if self._chipOnly() else 6
        if self.width() > 2 * radius and self.height() > 2 * radius:
            self.setMask(rounded_mask(self.width(), self.height(), radius))
        else:
            self.clearMask()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._remask()

    # -- placement ---------------------------------------------------------- #

    def setClearProvider(self, provider) -> None:
        """``provider()`` returns the clear rectangle (a QRect in the parent's
        pixels), or None for the whole parent."""
        self._clearProvider = provider

    def setAvoider(self, avoider) -> None:
        """FX1. ``avoider(rect)`` returns *rect* moved off the floating items
        placed before the legend (an open popover); None for no rule."""
        self._avoider = avoider

    def clearRect(self) -> QRect:
        parent = self.parentWidget()
        if parent is None:
            return QRect()
        if self._clearProvider is not None:
            try:
                rect = self._clearProvider()
            except Exception:                         # noqa: BLE001
                rect = None
            if rect is not None and rect.isValid() and not rect.isEmpty():
                return QRect(rect)
        return parent.rect()

    def place(self):
        """Put the legend where the clear area says (see :meth:`_place`)."""
        if self._spec is not None:
            self._place()

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self._place()
        elif (watched is getattr(self, '_watchedChip', None)
              and event.type() in (QEvent.Type.Move, QEvent.Type.Show,
                                   QEvent.Type.Hide)
              and self._spec is not None and self.isVisible()):
            self._place()
        return super().eventFilter(watched, event)

    def _place(self):
        """Mockup ``renderLegend``: 64 px left of the clear area's right edge,
        its top at the clear centre minus 200, kept inside the clear area.
        The chip sits at the same right edge, centred on the clear centre."""
        parent = self.parentWidget()
        if parent is None or self._placing:
            return
        self._placing = True
        try:
            self.adjustSize()
            clear = self.clearRect()
            width, height = self.width(), self.height()
            right = clear.x() + clear.width() - RAIL_CLEARANCE
            centre = clear.y() + clear.height() / 2.0
            x = max(clear.x(), right - width)
            if self._chipOnly():
                y = centre - height / 2.0
            else:
                y = max(clear.y() + LEGEND_MARGIN, centre - CENTRE_RISE)
            bottom = clear.y() + clear.height() - LEGEND_MARGIN
            # MF27: in a short clear area (1280x800, console at 280) the
            # legend rose to the clear top and sat on the Parts chip, which
            # the overlay layout keeps at the clear area's top right. Start
            # below the chip when the two share columns.
            chip = self._partsChipBox(x, width)
            if chip is not None:
                floor = chip.y() + chip.height() + LEGEND_MARGIN
                if floor + height <= bottom:
                    y = max(y, floor)
                else:
                    # no room under it: stand left of the chip's column
                    x = max(clear.x(), min(x, chip.x() - LEGEND_MARGIN - width))
            y = max(clear.y(), min(y, bottom - height))
            rect = self.rect().translated(int(round(x)), int(round(y)))
            if self._avoider is not None:
                try:
                    rect = self._avoider(rect)
                except (RuntimeError, AttributeError, TypeError):
                    pass
            self.move(rect.x(), rect.y())
        finally:
            self._placing = False
