#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The Snapshot popover (Plan 43 LY15, section 5).

One way in, three entry points: the camera button of the top bar, View >
Camera > Save view as..., and Ctrl+Shift+P.  It holds every option of the three
things it replaces: Capture (a numbered file with a sidecar in the case), Save
view as (size 1x, 2x or 4x, a transparent background, the overlay) and Save
image as (PNG, JPEG, SVG, PDF, EPS, PS).

What you see is what is saved: the Include boxes and the font and size switch
the same things on the view while the popover is open.  The axes triad and the
transparent background are properties of the file, and apply at the save.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from foammesh.core import snapshot as snapshot_core
from foammesh.rendering import label_style
from foammesh.view.theming import elevation, fonts
from foammesh.view.theming.metrics import (
    FORM_LABEL_SPACING, FRAME_WIDTH, GAP, GAP_TIGHT)
from foammesh.view.widget_factory import make

#: Plan 44 MF22 (mockup ``.snap``): the panel is 330 px wide, the label column
#: 78 px, padding 10 12 12, gap 8, and it sits at clear-left + 16, top 56.
PANEL_WIDTH = 330
LABEL_WIDTH = 78
PAD = (12, 10, 12, 12)
LEFT_INSET = 16
TOP_INSET = 56
#: The "XL" the mockup prints for Extra large.
_SHORT_SIZE = {'xlarge': 'XL'}


def _colour(name: str, fallback: str) -> QColor:
    return elevation.parse_colour(elevation.token(name, fallback)) or QColor(fallback)


class _Segment(QAbstractButton):
    """One cell of a segmented control, painted from the theme tokens."""

    def __init__(self, text, data, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.data = data
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 16, 24)

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.rect()
        if self.isChecked():
            painter.fillRect(rect, _colour('selection.background', '#2f5ca8'))
            ink = _colour('foreground.strong', '#e6e8eb')
        else:
            if self.underMouse() and self.isEnabled():
                painter.fillRect(rect, _colour('background.control', '#2a2e35'))
            ink = _colour('foreground.secondary', '#aab0b8')
        if not self.isEnabled():
            ink = _colour('disabled.foreground', '#6f747b')
        painter.setPen(ink)
        painter.setFont(self.font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())
        painter.end()


class SegmentedControl(QWidget):
    """The mockup's ``.seg``: equal cells in a 1 px rounded border, the chosen
    one filled with the selection colour."""

    currentChanged = Signal(object)

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = []
        row = QHBoxLayout(self)
        row.setContentsMargins(FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH)
        # The divider between two cells is painted by paintEvent on the first
        # column of the next cell, so the cells abut (DP-194: no 1 px gap).
        row.setSpacing(0)
        for text, data in items:
            button = _Segment(text, data, self)
            self._group.addButton(button)
            row.addWidget(button, 1)
            self._buttons.append(button)
            button.clicked.connect(
                lambda _checked=False, data=data: self.currentChanged.emit(data))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def buttons(self):
        return list(self._buttons)

    def current(self):
        for button in self._buttons:
            if button.isChecked():
                return button.data
        return None

    def setCurrent(self, data) -> None:
        for button in self._buttons:
            button.setChecked(button.data == data)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The cells are square; the border is not. Clip the corners.
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 4, 4)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        edge = _colour('border.default', '#3a3f48')
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 4, 4)
        for button in self._buttons[:-1]:
            x = button.geometry().right() + 1
            painter.drawLine(x, 1, x, self.height() - 2)
        painter.end()


class SnapshotPopover(QFrame):
    """The options of a snapshot, and the Copy and Save buttons.

    Plan 44 MF22: a translucent, frameless 330 px popup with the ``snap``
    shadow (``elevation.elevate``), a caps header with a close button, segmented
    Size and Text size (the combos behind them stay the model and the way
    scripts and the facade reach the same values) and a two-column Include
    grid.  It is a top-level popup, so it draws over the native viewport.
    """

    copyRequested = Signal()
    saveRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('snapshotPopover')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._view = None
        self._viewSize = (0, 0)
        self._directory: str | None = None
        self._building = True

        outer = make(QVBoxLayout, self)
        outer.setContentsMargins(*PAD)
        outer.setSpacing(GAP)

        header = make(QHBoxLayout)
        title = make(QLabel, self.tr('Snapshot of the view'))
        title.setObjectName('snapshotTitle')
        title.setFont(fonts.caps_font(10.5))
        title.setProperty('foammeshTone', 'muted')
        self.closeButton = make(QToolButton, self)
        self.closeButton.setObjectName('snapshotClose')
        self.closeButton.setText('✕')
        self.closeButton.setAutoRaise(True)
        self.closeButton.setToolTip(self.tr('Close the snapshot (Esc)'))
        self.closeButton.setCursor(Qt.CursorShape.PointingHandCursor)
        self.closeButton.clicked.connect(self.hide)
        header.addWidget(title, 1)
        header.addWidget(self.closeButton)
        outer.addLayout(header)

        grid = make(QGridLayout)
        grid.setHorizontalSpacing(FORM_LABEL_SPACING)
        grid.setVerticalSpacing(GAP)
        grid.setColumnMinimumWidth(0, LABEL_WIDTH)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)
        self._row = 0

        self.nameEdit = make(QLineEdit, self)
        self.nameEdit.setObjectName('snapshotName')
        self.nameEdit.setPlaceholderText(self.tr('numbered for you'))
        self.nameEdit.setToolTip(self.tr(
            'The file name, for example pipe_skewness_2026-10-12_01.png. '
            'Leave it empty to number it; the extension follows the format.'))
        self._addRow(grid, self.tr('File name'), self.nameEdit)

        where = make(QHBoxLayout)
        where.setSpacing(GAP_TIGHT)
        self.folderLabel = make(QLabel, self.tr('case › captures'))
        self.folderLabel.setObjectName('snapshotFolder')
        self.folderLabel.setProperty('foammeshTone', 'muted')
        self.folderLabel.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        self.folderLabel.setToolTip(self.tr(
            "Snapshots go into the case's captures folder with a sidecar that "
            'records the view, the font and what is included'))
        self.folderButton = make(QPushButton, self.tr('Choose…'))
        self.folderButton.setObjectName('snapshotChooseFolder')
        self.folderButton.clicked.connect(self._chooseFolder)
        self.caseButton = make(QPushButton, self.tr('Case'))
        self.caseButton.setObjectName('snapshotCaseFolder')
        self.caseButton.setToolTip(self.tr("Back to the case's captures folder"))
        self.caseButton.clicked.connect(self._useCaseFolder)
        where.addWidget(self.folderLabel, 1)
        where.addWidget(self.folderButton)
        where.addWidget(self.caseButton)
        self._addRow(grid, self.tr('Saved to'), where)

        self.formatCombo = make(QComboBox, self)
        self.formatCombo.setObjectName('snapshotFormat')
        for fmt in snapshot_core.FORMATS:
            self.formatCombo.addItem(
                '{0} ({1})'.format(fmt, self.tr(snapshot_core.FORMAT_LABELS[fmt])),
                fmt)
        self.formatCombo.currentIndexChanged.connect(self._formatChanged)
        self._addRow(grid, self.tr('Format'), self.formatCombo)

        # Size: segmented 1x / 2x / 4x.  The combo is the model; it is never shown.
        self.sizeCombo = make(QComboBox, self)
        self.sizeCombo.setObjectName('snapshotSize')
        for scale in snapshot_core.SCALES:
            self.sizeCombo.addItem('{0}×'.format(scale), scale)
        self.sizeCombo.setCurrentIndex(
            snapshot_core.SCALES.index(snapshot_core.DEFAULT_SCALE))
        self.sizeCombo.currentIndexChanged.connect(self._sizeChanged)
        self.sizeCombo.hide()
        self.sizeSegments = SegmentedControl(
            [('{0}×'.format(scale), scale) for scale in snapshot_core.SCALES],
            self)
        self.sizeSegments.setObjectName('snapshotSizeSegments')
        self._linkSegments(self.sizeSegments, self.sizeCombo)
        self._addRow(grid, self.tr('Size'), self.sizeSegments)
        self.pixelLabel = make(QLabel, '')
        self.pixelLabel.setObjectName('snapshotPixels')
        self.pixelLabel.setProperty('foammeshTone', 'muted')
        self._addRow(grid, '', self.pixelLabel)

        self.fontCombo = make(QComboBox, self)
        self.fontCombo.setObjectName('snapshotFont')
        for key, text in label_style.FONTS.items():
            self.fontCombo.addItem(text, key)
        self._faceFonts()
        self.fontCombo.currentIndexChanged.connect(self._styleChanged)
        self._addRow(grid, self.tr('Font'), self.fontCombo)

        self.textSizeCombo = make(QComboBox, self)
        self.textSizeCombo.setObjectName('snapshotTextSize')
        for key, (text, _factor) in label_style.SIZES.items():
            self.textSizeCombo.addItem(self.tr(text), key)
        self.textSizeCombo.currentIndexChanged.connect(self._styleChanged)
        self.textSizeCombo.hide()
        self.textSizeSegments = SegmentedControl(
            [(_SHORT_SIZE.get(key, self.tr(text)), key)
             for key, (text, _factor) in label_style.SIZES.items()], self)
        self.textSizeSegments.setObjectName('snapshotTextSizeSegments')
        self._linkSegments(self.textSizeSegments, self.textSizeCombo)
        self._addRow(grid, self.tr('Text size'), self.textSizeSegments)

        include_title = make(QLabel, self.tr('Include'))
        include_title.setObjectName('snapshotIncludeTitle')
        include_title.setFont(fonts.caps_font(10.5))
        include_title.setProperty('foammeshTone', 'muted')
        outer.addWidget(include_title)

        self._boxes = {}
        include = make(QGridLayout)
        include.setHorizontalSpacing(10)
        include.setVerticalSpacing(4)
        include.setColumnStretch(0, 1)
        include.setColumnStretch(1, 1)
        entries = (
            ('legend', self.tr('Legend'), 'snapshotLegend'),
            ('worst', self.tr('Worst values on the model'), 'snapshotWorst'),
            ('caption', self.tr('Statistics caption'), 'snapshotCaption'),
            ('triad', self.tr('Axes triad'), 'snapshotTriad'),
            ('transparent', self.tr('Transparent background'),
             'snapshotTransparent'))
        for index, (key, text, name) in enumerate(entries):
            box = make(QCheckBox, text, self)
            box.setObjectName(name)
            box.setChecked(key != 'transparent')
            box.toggled.connect(lambda on, key=key: self._includeToggled(key, on))
            include.addWidget(box, index // 2, index % 2)
            self._boxes[key] = box
        self._boxes['transparent'].setToolTip(self.tr(
            'PNG and SVG only; the background is left clear'))
        self.overlayBox = make(QCheckBox, self.tr('Include the overlay'), self)
        self.overlayBox.setObjectName('snapshotOverlay')
        self.overlayBox.setToolTip(self.tr(
            'Paint the card, the tool rail and the progress strip over the '
            'picture, as the screen shows them (PNG and JPEG)'))
        include.addWidget(self.overlayBox, len(entries) // 2, len(entries) % 2)
        outer.addLayout(include)

        self.note = make(QLabel, '')
        self.note.setObjectName('snapshotNote')
        self.note.setProperty('foammeshTone', 'muted')
        self.note.setWordWrap(True)
        outer.addWidget(self.note)

        buttons = make(QHBoxLayout)
        buttons.setSpacing(GAP)
        buttons.addStretch(1)
        self.copyButton = make(QPushButton, self.tr('Copy'))
        self.copyButton.setObjectName('snapshotCopy')
        self.copyButton.setToolTip(self.tr(
            'Put the picture on the clipboard, with the options above'))
        self.copyButton.clicked.connect(self.copyRequested)
        self.saveButton = make(QPushButton, self.tr('Save'))
        self.saveButton.setObjectName('snapshotSave')
        self.saveButton.setProperty('foammeshRole', 'primary')
        self.saveButton.setDefault(True)
        self.saveButton.clicked.connect(self.saveRequested)
        buttons.addWidget(self.copyButton)
        buttons.addWidget(self.saveButton)
        outer.addLayout(buttons)

        # Translucent and frameless with the "snap" shadow ring in a margin the
        # panel sits inside; the panel itself is PANEL_WIDTH wide.
        self._elevation = elevation.elevate(self, 'snap')
        left, _top, right, _bottom = self._elevation.margins
        self.setFixedWidth(PANEL_WIDTH + left + right)
        self._building = False
        self._syncSegments()
        self._formatChanged()

    # -- layout helpers --------------------------------------------------------- #

    def _addRow(self, grid, label, field) -> None:
        caption = make(QLabel, label)
        caption.setFixedWidth(LABEL_WIDTH)
        grid.addWidget(caption, self._row, 0)
        if isinstance(field, QHBoxLayout):
            grid.addLayout(field, self._row, 1)
        else:
            grid.addWidget(field, self._row, 1)
        self._row += 1

    def _linkSegments(self, segments, combo) -> None:
        """The segmented control and its combo are one value."""
        segments.currentChanged.connect(
            lambda data: combo.setCurrentIndex(max(combo.findData(data), 0)))
        combo.currentIndexChanged.connect(
            lambda _index: segments.setCurrent(combo.currentData()))

    def _syncSegments(self) -> None:
        self.sizeSegments.setCurrent(self.sizeCombo.currentData())
        self.textSizeSegments.setCurrent(self.textSizeCombo.currentData())

    def panelRect(self) -> QRect:
        """The visible panel inside the shadow margin, in widget coordinates."""
        left, top, right, bottom = self._elevation.margins
        return self.rect().adjusted(left, top, -right, -bottom)

    # -- reading and writing the options ------------------------------------- #

    def options(self) -> snapshot_core.SnapshotOptions:
        includes = {key: box.isChecked() for key, box in self._boxes.items()}
        return snapshot_core.SnapshotOptions(
            name=self.nameEdit.text().strip(), directory=self._directory,
            format=self.formatCombo.currentData(),
            scale=int(self.sizeCombo.currentData()),
            font=self.fontCombo.currentData(),
            text_size=self.textSizeCombo.currentData(),
            includes=includes, overlay=self.overlayBox.isChecked())

    def setOptions(self, options: snapshot_core.SnapshotOptions) -> None:
        self._building = True
        try:
            self.nameEdit.setText(options.name)
            self._directory = options.directory
            self._showFolder()
            self.formatCombo.setCurrentIndex(
                max(self.formatCombo.findData(options.format), 0))
            self.sizeCombo.setCurrentIndex(
                max(self.sizeCombo.findData(int(options.scale)), 0))
            self.fontCombo.setCurrentIndex(
                max(self.fontCombo.findData(options.font), 0))
            self.textSizeCombo.setCurrentIndex(
                max(self.textSizeCombo.findData(options.text_size), 0))
            for key, box in self._boxes.items():
                box.setChecked(bool(options.includes.get(key, False)))
            self.overlayBox.setChecked(bool(options.overlay))
        finally:
            self._building = False
        self._formatChanged()

    def attach(self, view) -> None:
        """Read the view's own state, so the boxes show what the view shows."""
        self._view = view
        self._building = True
        try:
            style = view.labelStyle()
            self.fontCombo.setCurrentIndex(max(self.fontCombo.findData(style.font), 0))
            self.textSizeCombo.setCurrentIndex(
                max(self.textSizeCombo.findData(style.size), 0))
            self._boxes['legend'].setChecked(view.areLegendsShown())
            self._boxes['worst'].setChecked(view.isLabelGroupShown('worst'))
            self._boxes['caption'].setChecked(view.isLabelGroupShown('caption'))
        finally:
            self._building = False
        try:
            window = view.renderWindow()
            self._viewSize = tuple(int(v) for v in window.GetSize())
        except Exception:                                     # noqa: BLE001
            self._viewSize = (view.width(), view.height())
        self._sizeChanged()

    def setSuggestedName(self, name: str) -> None:
        self.nameEdit.setPlaceholderText(name)

    def setNote(self, text: str) -> None:
        self.note.setText(text)

    def pixelText(self) -> str:
        return self.pixelLabel.text()

    # -- behaviour ------------------------------------------------------------ #

    def _faceFonts(self) -> None:
        """Each font is shown in its own face."""
        families = {'pretendard': 'Pretendard', 'arial': 'Arial',
                    'times': 'Times New Roman', 'courier': 'Courier New'}
        known = set(QFontDatabase.families())
        for index in range(self.fontCombo.count()):
            family = families[self.fontCombo.itemData(index)]
            if family in known:
                self.fontCombo.setItemData(index, QFont(family), Qt.ItemDataRole.FontRole)

    def _formatChanged(self, *_args) -> None:
        fmt = self.formatCombo.currentData()
        raster = snapshot_core.is_raster(fmt)
        self.sizeCombo.setEnabled(raster)
        self.sizeSegments.setEnabled(raster)
        self.overlayBox.setEnabled(raster)
        self._boxes['transparent'].setEnabled(fmt in ('PNG', 'SVG'))
        self._sizeChanged()

    def _sizeChanged(self, *_args) -> None:
        if not snapshot_core.is_raster(self.formatCombo.currentData()):
            self.pixelLabel.setText(self.tr('vector: no pixel size'))
            return
        width, height = snapshot_core.pixel_size(
            self._viewSize[0], self._viewSize[1], self.sizeCombo.currentData())
        self.pixelLabel.setText(
            '{0} × {1} px'.format(width, height) if self._viewSize[0] else '')

    def _styleChanged(self, *_args) -> None:
        if self._building or self._view is None:
            return
        self._view.setLabelStyle(font=self.fontCombo.currentData(),
                                 size=self.textSizeCombo.currentData())

    def _includeToggled(self, key: str, on: bool) -> None:
        if self._building or self._view is None:
            return
        if key == 'legend':
            self._view.setLegendsShown(on)
        elif key in ('worst', 'caption'):
            self._view.setLabelGroupShown(key, on)

    def _showFolder(self) -> None:
        if self._directory:
            self.folderLabel.setText(Path(self._directory).name)
            self.folderLabel.setToolTip(str(self._directory))
        else:
            self.folderLabel.setText(self.tr('case › captures'))

    def _chooseFolder(self) -> None:
        # A popover closes when a dialog takes focus; reopen it afterwards.
        chosen = QFileDialog.getExistingDirectory(
            self.window(), self.tr('Save the snapshot in'), self._directory or '')
        if chosen:
            self._directory = chosen
            self._showFolder()
        self.show()

    def _useCaseFolder(self) -> None:
        self._directory = None
        self._showFolder()

    def showInClearArea(self, view: QWidget, clear: QRect) -> None:
        """Open over the viewport at clear-left + 16, top 56 (the mockup),
        kept on the screen. *clear* is the clear area in *view* pixels."""
        self.adjustSize()
        left, top, _right, _bottom = self._elevation.margins
        x = clear.left() + LEFT_INSET
        y = max(clear.top(), TOP_INSET)
        point = view.mapToGlobal(QPoint(x, y)) - QPoint(left, top)
        screen = view.screen().availableGeometry() if view.screen() else None
        if screen is not None:
            point.setX(max(screen.left(), min(point.x(),
                                              screen.right() - self.width())))
            point.setY(max(screen.top(), min(point.y(),
                                             screen.bottom() - self.height())))
        self.move(point)
        self.show()

    def showBelow(self, anchor: QWidget, within: QWidget | None = None) -> None:
        """Open under *anchor* (the camera button), its panel whole.

        DP-2742. The panel (not its shadow margin) hangs 4 px under the
        button, its left edge on the button's; when that would run past the
        right of *within* (the window) or the screen, its right edge sits on
        the button's instead. It is then kept inside *within* and the screen,
        so the title is never cut off.
        """
        self.adjustSize()
        left, top, _right, _bottom = self._elevation.margins
        panel = self.panelRect()
        corner = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        x = corner.x()
        bounds = []
        if within is not None:
            bounds.append(QRect(within.mapToGlobal(QPoint(0, 0)), within.size()))
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            bounds.append(screen)
        limit = min([b.right() for b in bounds], default=None)
        if limit is not None and x + panel.width() - 1 > limit:
            x = anchor.mapToGlobal(QPoint(anchor.width(), 0)).x() - panel.width()
        px, py = x, corner.y()
        for box in bounds:
            px = max(box.left(), min(px, box.right() + 1 - panel.width()))
            py = max(box.top(), min(py, box.bottom() + 1 - panel.height()))
        self.move(QPoint(px - left, py - top))
        self.show()
