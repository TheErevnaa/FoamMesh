"""The colour scale controls (Plan 43 LY12, §4; Plan 44 MF12).

One panel, three homes: the scale popover under the legend (``columns=1``,
250 px), View > Colour scale (``columns=2``, 560 px) and Preferences >
Appearance > Colour scale (a draft, ``live=False``, saved with OK). All read
and write through :mod:`foammesh.support.colour_standard`; in the live homes
every change repaints at once and lasts for the session until *Make this my
standard*.

The mockup's look (``M:1217-1221``): one radio row per scale with a 46 x 10
swatch drawn from the scale's own table and a *standard* tag on the saved
standard's row; Reverse; Bands as a Smooth / Banded segment with a 5-50 slider
and a number box; the Range choices; the three standard actions.

The old combo boxes (``colourScaleCombo``, ``colourScaleBandsMode``,
``colourScaleRange``) stay as the panel's hidden state holders: the rows, the
segment and the range choices write through them, so the facade ids and the
tests that drive them keep working.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from foammesh.support import colour_standard as cs
from foammesh.view.display_control.legend_chip import Segment, ink
from foammesh.view.theming.fonts import caps_font
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_SECTION, GAP_TIGHT, MARGIN_TIGHT, CompactSpinBox)

_RANGE_LABELS = {cs.RANGE_WHOLE: 'Whole data range',
                 cs.RANGE_LIMIT: 'Around the limit'}

#: Panel width with two columns (decision Q6).
TWO_COLUMN_WIDTH = 560
SWATCH_SIZE = (46, 10)
ROW_HEIGHT = 24


class ScaleRow(QAbstractButton):
    """A radio row: indicator, an optional 46 x 10 swatch, the name, a tag."""

    def __init__(self, text: str, parent=None, *, key=None, swatch=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(ROW_HEIGHT)
        self.key = key
        self._swatch = swatch          # callable -> list of QColor, or None
        self._tag = ''

    def setTag(self, tag: str) -> None:
        self._tag = tag
        self.update()

    def tag(self) -> str:
        return self._tag

    def hasSwatch(self) -> bool:
        return self._swatch is not None

    def swatchColours(self) -> list:
        return list(self._swatch()) if self._swatch is not None else []

    def sizeHint(self) -> QSize:
        return QSize(180, ROW_HEIGHT)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink('background.control', '#2a2f37'))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 0, -2, 0), 3, 3)
        accent = ink('accent.default', '#5b93f0')
        cy = self.height() / 2.0
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(accent if self.isChecked() else ink('border.default', '#5a606a'), 1.2))
        painter.drawEllipse(QRectF(8, cy - 6, 12, 12))
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QRectF(11, cy - 3, 6, 6))
        left = 28.0
        if self._swatch is not None:
            colours = self._swatch()
            sw, sh = SWATCH_SIZE
            top = cy - sh / 2.0
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            count = max(1, len(colours))
            for i in range(sw):
                colour = colours[min(count - 1, int(i * count / sw))] if colours else None
                if colour is not None:
                    painter.fillRect(QRectF(left + i, top, 1.0, sh), colour)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor(0, 0, 0, 110))
            painter.drawRoundedRect(QRectF(left + 0.5, top + 0.5, sw - 1, sh - 1), 2, 2)
            left += sw + 8
        font = self.font()
        font.setPixelSize(12)
        painter.setFont(font)
        right = self.width() - 8.0
        if self._tag:
            small = self.font()
            small.setPixelSize(10)
            metrics = QFontMetrics(small)
            width = metrics.horizontalAdvance(self._tag) + 10
            pill = QRectF(right - width, cy - 7, width, 14)
            painter.setFont(small)
            painter.setPen(QPen(ink('border.default', '#3a3f48'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
            painter.setPen(ink('foreground.sub', '#9aa2ae'))
            painter.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), self._tag)
            right -= width + 6
            painter.setFont(font)
        painter.setPen(self.palette().windowText().color())
        label = QFontMetrics(font).elidedText(
            self.text(), Qt.TextElideMode.ElideRight, max(10, int(right - left)))
        painter.drawText(
            QRectF(left, 0, max(10, right - left), self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(accent, 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2.5, 0.5, -2.5, -0.5), 3, 3)

    def enterEvent(self, event):                 # noqa: N802 - Qt naming
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):                 # noqa: N802 - Qt naming
        self.update()
        super().leaveEvent(event)


def _caption(text: str, parent) -> QLabel:
    label = QLabel(text, parent)
    label.setFont(caps_font(10))
    return label


class ColourScalePanel(QWidget):
    changed = Signal(object)

    def __init__(self, parent=None, *, settings=None, live=True, columns=1):
        super().__init__(parent)
        self.setObjectName('colourScalePanel')
        if columns not in (1, 2):
            raise ValueError('columns must be 1 or 2')
        self._settings = settings
        self._live = live
        self._busy = False
        self._columns = columns
        self._draft = cs.saved(settings) if not live else cs.current(settings)
        self._swatchCache: dict = {}

        # -- the state holders (hidden; see the module docstring) ------------- #
        self.scaleCombo = QComboBox(self)
        self.scaleCombo.setObjectName('colourScaleCombo')
        for key, label in cs.SCALES.items():
            suffix = ' ' + self.tr('(standard)') if key == cs.APP_SCALE else ''
            self.scaleCombo.addItem(self.tr(label) + suffix, key)
        self.bandsMode = QComboBox(self)
        self.bandsMode.setObjectName('colourScaleBandsMode')
        self.bandsMode.addItem(self.tr('Smooth'), 0)
        self.bandsMode.addItem(self.tr('Banded'), 1)
        self.rangeCombo = QComboBox(self)
        self.rangeCombo.setObjectName('colourScaleRange')
        for key in cs.RANGES:
            self.rangeCombo.addItem(self.tr(_RANGE_LABELS[key]), key)
        for holder in (self.scaleCombo, self.bandsMode, self.rangeCombo):
            holder.hide()

        # -- the scales: one radio row each, with its own swatch --------------- #
        self.scalesBox = QWidget(self)
        self.scalesBox.setObjectName('colourScaleRows')
        scales = QVBoxLayout(self.scalesBox)
        scales.setContentsMargins(0, 0, 0, 0)
        scales.setSpacing(0)
        self._scaleRows: dict = {}
        scale_group = QButtonGroup(self.scalesBox)
        for key, label in cs.SCALES.items():
            row = ScaleRow(self.tr(label), self.scalesBox, key=key,
                           swatch=lambda k=key: self._swatch(k))
            row.setObjectName('colourScaleRow_' + key)
            row.setAccessibleName(self.tr(label))
            scale_group.addButton(row)
            row.clicked.connect(lambda _c=False, k=key: self._pickScale(k))
            scales.addWidget(row)
            self._scaleRows[key] = row

        self.reverse = QCheckBox(self.tr('Reverse'), self)
        self.reverse.setObjectName('colourScaleReverse')

        # -- bands: Smooth / Banded, a 5-50 slider and a number ---------------- #
        self.bandsBox = QWidget(self)
        bands = QVBoxLayout(self.bandsBox)
        bands.setContentsMargins(0, 0, 0, 0)
        bands.setSpacing(GAP_TIGHT)
        bands.addWidget(_caption(self.tr('Bands'), self.bandsBox))
        self.bandsSegment = Segment(self.bandsBox)
        self.bandsSegment.setObjectName('colourScaleBandsSegment')
        self.bandsSegment.add('smooth', self.tr('Smooth'))
        self.bandsSegment.add('banded', self.tr('Banded'))
        self.bandsSegment.activated.connect(self._pickBands)
        bands.addWidget(self.bandsSegment)
        slider_row = QHBoxLayout()
        slider_row.setContentsMargins(0, 0, 0, 0)
        self.bandsSlider = QSlider(Qt.Orientation.Horizontal, self.bandsBox)
        self.bandsSlider.setObjectName('colourScaleBandsSlider')
        self.bandsSlider.setRange(cs.SLIDER_MIN, cs.SLIDER_MAX)
        self.bandsSlider.setSingleStep(cs.SLIDER_STEP)
        self.bandsSlider.setPageStep(cs.SLIDER_STEP)
        self.bandsSlider.setTickInterval(cs.SLIDER_STEP)
        self.bandsSlider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.bandsSpin = CompactSpinBox(self.bandsBox)
        self.bandsSpin.setObjectName('colourScaleBandsSpin')
        self.bandsSpin.setRange(cs.MIN_BANDS, cs.MAX_BANDS)
        slider_row.addWidget(self.bandsSlider, 1)
        slider_row.addWidget(self.bandsSpin)
        bands.addLayout(slider_row)
        self._lastBands = 20

        # -- range: two radio rows, and a slot for the deviation controls ------- #
        self.rangeBox = QWidget(self)
        self.rangeBox.setObjectName('colourScaleRangeBox')
        rng = QVBoxLayout(self.rangeBox)
        rng.setContentsMargins(0, 0, 0, 0)
        rng.setSpacing(0)
        rng.addWidget(_caption(self.tr('Range'), self.rangeBox))
        self._rangeRows: dict = {}
        range_group = QButtonGroup(self.rangeBox)
        for key in cs.RANGES:
            row = ScaleRow(self.tr(_RANGE_LABELS[key]), self.rangeBox, key=key)
            row.setAccessibleName(self.tr(_RANGE_LABELS[key]))
            range_group.addButton(row)
            row.clicked.connect(lambda _c=False, k=key: self._pickRange(k))
            rng.addWidget(row)
            self._rangeRows[key] = row
        self._extraHost = QVBoxLayout()
        self._extraHost.setContentsMargins(FORM_MARGIN, MARGIN_TIGHT, FORM_MARGIN, 0)
        rng.addLayout(self._extraHost)
        self._extra = None

        self.legend = QCheckBox(self.tr('Show legend'), self)
        self.legend.setObjectName('colourScaleLegend')

        self.status = QLabel(self)
        self.status.setObjectName('colourScaleStatus')
        self.status.setWordWrap(True)

        self.makeStandard = QPushButton(self.tr('Make this my standard'), self)
        self.makeStandard.setObjectName('colourScaleMakeStandard')
        self.goBack = QPushButton(self.tr('Go back to the standard'), self)
        self.goBack.setObjectName('colourScaleGoBack')
        self.appStandard = QPushButton(
            self.tr('Use the app standard, the rainbow scale'), self)
        self.appStandard.setObjectName('colourScaleAppStandard')
        self.actionsBox = QWidget(self)
        actions = QVBoxLayout(self.actionsBox)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(GAP_TIGHT)
        for button in (self.makeStandard, self.goBack, self.appStandard):
            actions.addWidget(button)

        self._arrange(columns)

        self._show(self._draft)
        for signal in (self.scaleCombo.currentIndexChanged,
                       self.bandsMode.currentIndexChanged,
                       self.rangeCombo.currentIndexChanged):
            signal.connect(self._edited)
        for signal in (self.reverse.toggled, self.legend.toggled):
            signal.connect(self._edited)
        self.bandsSlider.valueChanged.connect(self._sliderMoved)
        self.bandsSpin.valueChanged.connect(self._spinEdited)
        self.makeStandard.clicked.connect(self._make)
        self.goBack.clicked.connect(self._back)
        self.appStandard.clicked.connect(self._appDefault)
        if live:
            cs.subscribe(self._followed)

    # -- layout ------------------------------------------------------------- #

    def _arrange(self, columns: int) -> None:
        if columns == 1:
            column = QVBoxLayout(self)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(GAP)
            for part in (self.scalesBox, self.reverse, self.bandsBox,
                         self.rangeBox, self.legend, self.status, self.actionsBox):
                column.addWidget(part)
            return
        # Two columns (decision Q6): the standard, reverse, legend and range on
        # the left; the nine scales and the bands on the right.
        self.setFixedWidth(TWO_COLUMN_WIDTH)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GAP_SECTION)
        left = QVBoxLayout()
        left.setSpacing(GAP)
        right = QVBoxLayout()
        right.setSpacing(GAP)
        for part in (self.status, self.reverse, self.legend, self.rangeBox):
            left.addWidget(part)
        left.addStretch(1)
        left.addWidget(self.actionsBox)
        right.addWidget(_caption(self.tr('Colour scale'), self))
        right.addWidget(self.scalesBox)
        right.addWidget(self.bandsBox)
        right.addStretch(1)
        outer.addLayout(left, 1)
        outer.addLayout(right, 1)

    def columns(self) -> int:
        return self._columns

    def setRangeExtra(self, widget) -> None:
        """Show ``widget`` (the deviation tolerance and scale-to) under the
        Range choices. ``None`` removes the current one without deleting it."""
        if self._extra is not None:
            self._extraHost.removeWidget(self._extra)
            self._extra.hide()
        self._extra = widget
        if widget is not None:
            widget.setParent(self.rangeBox)
            self._extraHost.addWidget(widget)
            widget.show()

    # -- the swatches -------------------------------------------------------- #

    def _swatch(self, key: str) -> list:
        """The scale's colours as drawn now (reverse and bands included),
        sampled from the colour standard's own table."""
        draft = self._draft
        cache_key = (key, draft.reverse, draft.bands)
        found = self._swatchCache.get(cache_key)
        if found is None:
            table = cs.lookup_table(
                'quality', 0.0, 1.0, True, standard=cs.Standard(
                    scale=key, reverse=draft.reverse, bands=draft.bands))
            found = []
            rgb = [0.0, 0.0, 0.0]
            for i in range(SWATCH_SIZE[0]):
                table.GetColor(i / (SWATCH_SIZE[0] - 1), rgb)
                found.append(QColor.fromRgbF(*rgb))
            self._swatchCache[cache_key] = found
        return found

    def swatchFor(self, key: str) -> list:
        return self._swatch(key)

    def scaleRow(self, key: str) -> ScaleRow:
        return self._scaleRows[key]

    # -- reading and showing ------------------------------------------------ #

    def value(self) -> cs.Standard:
        bands = (self.bandsSpin.value()
                 if self.bandsMode.currentData() else 0)
        return cs.normalise({
            'scale': self.scaleCombo.currentData(),
            'reverse': self.reverse.isChecked(),
            'bands': bands,
            'range': self.rangeCombo.currentData(),
            'legend': self.legend.isChecked()})

    def _show(self, standard: cs.Standard) -> None:
        self._busy = True
        try:
            self._draft = standard
            self.scaleCombo.setCurrentIndex(
                self.scaleCombo.findData(standard.scale))
            self.reverse.setChecked(standard.reverse)
            if standard.bands:
                self._lastBands = standard.bands
            self.bandsMode.setCurrentIndex(1 if standard.bands else 0)
            shown = standard.bands or self._lastBands
            self.bandsSpin.setValue(shown)
            self.bandsSlider.setValue(min(cs.SLIDER_MAX,
                                          max(cs.SLIDER_MIN, shown)))
            self.bandsSpin.setEnabled(bool(standard.bands))
            self.bandsSlider.setEnabled(bool(standard.bands))
            self.rangeCombo.setCurrentIndex(
                self.rangeCombo.findData(standard.range))
            self.legend.setChecked(standard.legend)
        finally:
            self._busy = False
        self._syncRows()
        self._refreshStatus()

    def _syncRows(self) -> None:
        """Make the visible rows show what the state holders hold."""
        scale = self.scaleCombo.currentData()
        for key, row in self._scaleRows.items():
            row.setChecked(key == scale)
        standard = cs.saved(self._settings).scale
        for key, row in self._scaleRows.items():
            row.setTag(self.tr('standard') if key == standard else '')
            row.update()
        banded = bool(self.bandsMode.currentData())
        self.bandsSegment.setChecked('banded' if banded else 'smooth')
        rng = self.rangeCombo.currentData()
        for key, row in self._rangeRows.items():
            row.setChecked(key == rng)

    def _refreshStatus(self) -> None:
        if not self._live:
            self.status.setText(self.tr('Saved with OK. {0}').format(
                self.value().describe()))
            self.goBack.setEnabled(False)
            return
        standard = cs.is_standard(self._settings)
        self.status.setText(
            self.tr('This is your standard. {0}') .format(
                cs.current(self._settings).describe()) if standard else
            self.tr('Not the standard. {0}').format(
                cs.current(self._settings).describe()))
        self.goBack.setEnabled(not standard)
        self.makeStandard.setEnabled(not standard)

    # -- editing ------------------------------------------------------------ #

    def _pickScale(self, key: str) -> None:
        self.scaleCombo.setCurrentIndex(self.scaleCombo.findData(key))

    def _pickBands(self, key: str) -> None:
        self.bandsMode.setCurrentIndex(1 if key == 'banded' else 0)

    def _pickRange(self, key: str) -> None:
        self.rangeCombo.setCurrentIndex(self.rangeCombo.findData(key))

    def _sliderMoved(self, value) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            self.bandsSpin.setValue(int(value))
        finally:
            self._busy = False
        self._edited()

    def _spinEdited(self, value) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            self.bandsSlider.setValue(min(cs.SLIDER_MAX,
                                          max(cs.SLIDER_MIN, int(value))))
        finally:
            self._busy = False
        self._edited()

    def _edited(self, *_args) -> None:
        if self._busy:
            return
        value = self.value()
        if value.bands:
            self._lastBands = value.bands
        self.bandsSpin.setEnabled(bool(value.bands))
        self.bandsSlider.setEnabled(bool(value.bands))
        self._draft = value
        if self._live:
            cs.set_current(self._settings, **value.to_record())
        self._syncRows()
        self._refreshStatus()
        self.changed.emit(value)

    def _followed(self, standard) -> None:
        """The standard changed elsewhere (a key, the buttons): show it."""
        if standard != self._draft:
            self._show(standard)
        else:
            self._syncRows()
            self._refreshStatus()

    def _make(self) -> None:
        if self._live:
            cs.make_my_standard(self._settings)
        self._syncRows()
        self._refreshStatus()

    def _back(self) -> None:
        if self._live:
            cs.go_back_to_standard(self._settings)

    def _appDefault(self) -> None:
        if self._live:
            cs.use_app_standard(self._settings)
        else:
            self._show(cs.APP_STANDARD)
            self.changed.emit(cs.APP_STANDARD)
