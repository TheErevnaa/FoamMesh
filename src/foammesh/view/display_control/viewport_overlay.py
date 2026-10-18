#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Controls pinned to the picture rather than parked in another region.

Display Control is a docked tree somewhere else on screen. While you are
looking at a mesh and want one zone gone, that is a trip away from the thing
you are looking at -- and it is a trip you make constantly.

This overlay is a *view* of the same actor state Display Control edits, never a
second source of truth: both drive the same ``ActorInfo`` objects, and anything
hidden here shows as hidden there. It also carries the legend, so a screenshot
explains itself, and the section controls, so the plane can be aimed without
opening a panel.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

from foammesh.view.theming.icons import apply_tinted_icon
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT, apply_bar_metrics

from .region_picker import ALL
from .section_panel import SectionPanel


#: Plan 44 F1 (MF21, mockup ``.prow i``): a 10 x 10 swatch.
SWATCH = 10
#: The small role text after a part's name (mockup ``.prow small``), as a
#: share of the row's font.
ROLE_FONT_SHARE = 0.85
ROW_ICON = QSize(14, 14)
#: Plan 45 V8. The eye and the isolate glyphs of a part row are drawn with
#: strokes at least this wide on screen. Their SVGs stroke 20 to 32 units of a
#: 512 viewBox -- 0.55 to 0.9 px at 14 px, the isolate's satellites dashed at
#: 60% opacity on top of that -- and drew as a grey smudge.
ROW_GLYPH_STROKE_PX = 1.25
#: DP-712. The parts list may take this share of the viewport's height, and
#: scrolls past it. A fixed 180 px showed six rows of a 40-part mesh on a
#: tall window and still covered half of a short one.
PARTS_HEIGHT_SHARE = 0.4
PARTS_MIN_HEIGHT = 96
#: Plan 45 M. In the Parts popover the list shows this many rows without a
#: scrollbar and scrolls from the next one on.
PARTS_VISIBLE_ROWS = 10
#: Plan 35 CR3. Patches listed by name under an outline; the rest are counted.
NOTICE_PATCHES = 12
#: UXB5. The card never grows past this share of the viewport's width, nor
#: past a hard ceiling, and a line that is still too long is elided with its
#: whole text in the tooltip -- it is never wrapped. A line that wraps splits
#: a measurement ("99.22 x" / "99.19 x 600.02 mm") across two rows.
CARD_WIDTH_SHARE = 0.6
CARD_MAX_WIDTH = 420
CARD_MIN_WIDTH = 220

_RUN_ID = re.compile(r'^[a-z][a-z0-9]*-[0-9a-f]{8,}$')
_CELL_COUNT = re.compile(r'^[\d,]+ cells?$')


def result_chip_text(text: str) -> str:
    """The run line as the card shows it: the verdict and the format.

    UXB5 (#16, #17). A run's own sentence is ``<run id> . <verdict> .
    <n> cells . <format>``. The card already says how many cells the mesh has
    on the line above, and the run id means nothing to a user who did not
    read the log, so both leave the line. The whole sentence is the tooltip.
    """
    parts = [part.strip() for part in str(text or '').split(' · ')]
    kept = [part for part in parts
            if part and not _RUN_ID.match(part) and not _CELL_COUNT.match(part)]
    shown = ' · '.join(kept)
    return shown[:1].upper() + shown[1:]


def summary_items(text: str) -> list[str]:
    """One mesh sentence as the lines of the card, one measurement each.

    ``Castellation: 7,688 cells, 1,426 boundary faces, 99.22 x 99.19 x
    600.02 mm`` becomes the stage, then each count, then the extent. The
    thousands separator is a comma without a space, so splitting on a comma
    and a space never cuts a number.
    """
    text = str(text or '').strip()
    if not text:
        return []
    head, colon, rest = text.partition(': ')
    if not colon:
        return [text]
    return [head] + [item.strip() for item in rest.split(', ') if item.strip()]


class _ElidedLine(QLabel):
    """One line of the card: never wrapped, elided on the right, whole text
    in the tooltip."""

    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self._full = ''
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full = str(text or '')
        self._elide()
        self.updateGeometry()

    def fullText(self) -> str:
        return self._full

    def isElided(self) -> bool:
        return self.text() != self._full

    def sizeHint(self):
        base = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 2,
                     base.height())

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        room = max(self.width() - 2, 0)
        shown = self._full
        if room and self.fontMetrics().horizontalAdvance(self._full) > room:
            shown = self.fontMetrics().elidedText(
                self._full, Qt.TextElideMode.ElideRight, room)
        super().setText(shown)
        self.setToolTip(self._tip() or (self._full if shown != self._full
                                        else ''))

    def _tip(self) -> str:
        return str(self.property('foammeshTip') or '')

    def setTip(self, tip: str):
        self.setProperty('foammeshTip', str(tip or ''))
        self._elide()


def part_role(info) -> str:
    """Plan 44 F1 (MF21). The small role text of a part, from what the
    actor already says it is: its id's category (cell zone, face zone,
    region, the volume), a seed marker, the mesh outline, or else its actor
    kind (patch, volume, geometry). ``''`` when the actor says nothing.

    The mockup's rows read ``wall · pipe side``: a boundary *type* and a
    description. Neither is carried by a part today, so neither is shown.
    """
    from PySide6.QtCore import QCoreApplication

    def tr(text):
        return QCoreApplication.translate('PartRow', text)

    if type(info).__name__ == 'RegionMarkerActor':
        return tr('seed')
    try:
        key = str(info.id())
    except Exception:                                       # noqa: BLE001
        key = ''
    if key == 'meshOutline':
        return tr('outline')
    pieces = key.split(':')
    for category, role in (('cellZones', 'cell zone'),
                           ('faceZones', 'face zone'),
                           ('regions', 'region')):
        if category in pieces[:-1]:
            return tr(role)
    if pieces[-1] == 'internalMesh':
        return tr('volume')
    kind = getattr(info, 'type', None)
    try:
        kind = kind() if callable(kind) else kind
    except Exception:                                       # noqa: BLE001
        kind = None
    return {'BOUNDARY': tr('patch'), 'MESH': tr('volume'),
            'GEOMETRY': tr('geometry')}.get(getattr(kind, 'name', ''), '')


def _swatch(color: QColor) -> QPixmap:
    pixmap = QPixmap(SWATCH, SWATCH)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(QColor(0, 0, 0, 90))
    painter.drawRoundedRect(0, 0, SWATCH - 1, SWATCH - 1, 2, 2)
    painter.end()
    return pixmap


class _PartsScroll(QScrollArea):
    """The parts list. In the Parts popover its preferred height is exactly
    that of the first ``PARTS_VISIBLE_ROWS`` rows (headings between them
    included), and it takes whatever more the popover is given."""

    def __init__(self):
        super().__init__()
        self.rowsHeight = None   # callable -> int, set by the overlay

    def sizeHint(self):
        hint = super().sizeHint()
        if self.rowsHeight is not None:
            height = self.rowsHeight()
            if height is not None:
                hint.setHeight(height)
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        if self.rowsHeight is not None:
            height = self.rowsHeight()
            if height is not None:
                hint.setHeight(min(height, PARTS_MIN_HEIGHT))
        return hint


class PartRow(QWidget):
    """One part: its colour, its name, an eye, and a solo button."""

    visibilityToggled = Signal(str, bool)
    soloRequested = Signal(str)
    #: DP-712. A click on the row: ``(key, additive)``, additive with Ctrl.
    selectRequested = Signal(str, bool)

    def __init__(self, key: str, name: str, color: QColor, parent=None,
                 role: str = ''):
        super().__init__(parent)
        self._key = key
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self.tr('Click to select {0}. Ctrl+click adds it '
                                'to the selection.').format(name))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(MARGIN_TIGHT, 0, MARGIN_TIGHT, 0)
        layout.setSpacing(GAP_TIGHT)

        self._swatch = QLabel()
        self._swatch.setPixmap(_swatch(color))
        self._name = QLabel(name)
        self._name.setToolTip(name)
        # Plan 44 F1 (MF21). The mockup's small role text after the name;
        # hidden when the part has no role.
        self._role = QLabel(role or '')
        self._role.setObjectName('overlayPartRole')
        role_font = QFont(self._role.font())
        if role_font.pointSizeF() > 0:
            role_font.setPointSizeF(role_font.pointSizeF() * ROLE_FONT_SHARE)
        self._role.setFont(role_font)
        self._role.setVisible(bool(role))

        # The bulb pair already in the icon set is this app's existing idiom
        # for "this thing is drawn / this thing is not".
        self._eye = QToolButton()
        self._eye.setObjectName('overlayPartEye')
        self._eye.setCheckable(True)
        self._eye.setChecked(True)
        self._eye.setIconSize(ROW_ICON)
        self._eye.setToolTip(self.tr('Show or hide {0}').format(name))
        self._eye.setAccessibleName(self._eye.toolTip())
        self._eye.setAutoRaise(True)
        self._setEyeIcon(True)

        # Plan 45 V8. The glyphs are token icons, drawn again on every theme
        # change: tinted once, from the palette of the moment, the isolate
        # glyph kept the dark theme's pale ink on the light popover (and the
        # eye kept it until it was next pressed).
        self._solo = QToolButton()
        self._solo.setObjectName('overlayPartSolo')
        apply_tinted_icon(self._solo, ':/graphicsIcons/isolate.svg',
                          ROW_ICON, ROW_GLYPH_STROKE_PX)
        self._solo.setAutoRaise(True)
        self._solo.setToolTip(self.tr('Show only {0}').format(name))
        self._solo.setAccessibleName(self._solo.toolTip())

        layout.addWidget(self._swatch)
        layout.addWidget(self._name, 0)
        layout.addWidget(self._role, 1)
        layout.addWidget(self._eye)
        layout.addWidget(self._solo)

        self._eye.toggled.connect(
            lambda checked: self.visibilityToggled.emit(self._key, checked))
        self._solo.clicked.connect(lambda: self.soloRequested.emit(self._key))

    def key(self):
        return self._key

    def role(self) -> str:
        """The small role text shown after the name ('' when none)."""
        return self._role.text()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            additive = bool(event.modifiers()
                            & Qt.KeyboardModifier.ControlModifier)
            self.selectRequested.emit(self._key, additive)
            event.accept()
            return
        super().mousePressEvent(event)

    def isSelected(self) -> bool:
        return self._selected

    def setSelected(self, selected: bool):
        """Mark the row as selected, the way the tree marks its rows."""
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        font = QFont(self._name.font())
        font.setBold(selected)
        self._name.setFont(font)
        self.setProperty('selected', selected)
        self.update()

    def paintEvent(self, event):
        # Painted here rather than through the palette: a theme's style sheet
        # overrides a palette's background, and the row must read as selected
        # under any theme.
        if self._selected:
            painter = QPainter(self)
            highlight = QColor(self.palette().color(QPalette.ColorRole.Highlight))
            highlight.setAlpha(90)
            painter.fillRect(self.rect(), highlight)
            highlight.setAlpha(255)
            painter.fillRect(0, 0, 3, self.height(), highlight)
            painter.end()
        super().paintEvent(event)

    def setColor(self, color: QColor):
        self._swatch.setPixmap(_swatch(color))

    def setVisibleState(self, visible: bool):
        was = self._eye.blockSignals(True)
        self._eye.setChecked(visible)
        self._setEyeIcon(visible)
        self._eye.blockSignals(was)

    def _setEyeIcon(self, visible: bool) -> None:
        apply_tinted_icon(self._eye, ':/graphicsIcons/bulb-on.svg' if visible
                          else ':/graphicsIcons/bulb-off.svg',
                          ROW_ICON, ROW_GLYPH_STROKE_PX)


class GmshEntityGroup(QFrame):
    """Plan 42 SG9. The "Gmsh entities" group of the Parts list.

    One row per Gmsh model entity (point, curve, surface, volume), with the
    same eye and solo buttons as a part. It has rows only while a Gmsh model is
    open; with none, the whole frame is hidden.
    """

    entityToggled = Signal(int, int, bool)
    entitySolo = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('overlayGmshEntities')
        self._rows = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._heading = QLabel(self.tr('Gmsh entities'), self)
        self._heading.setObjectName('overlayGmshEntitiesHeading')
        font = QFont(self._heading.font())
        font.setBold(True)
        self._heading.setFont(font)
        self._heading.setIndent(MARGIN_TIGHT)
        column.addSpacing(GAP_TIGHT)
        column.addWidget(self._heading)
        self._host = QWidget(self)
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName('overlayGmshEntitiesScroll')
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setMaximumHeight(PARTS_MIN_HEIGHT * 2)
        self._scroll.setWidget(self._host)
        column.addWidget(self._scroll)
        self._loading = QLabel(self.tr('Loading Gmsh entities…'), self._host)
        self._loading.setObjectName('overlayGmshEntitiesLoading')
        self._loading.setIndent(MARGIN_TIGHT)
        self._loading.setVisible(False)
        self.setVisible(False)

    def setLoading(self, loading: bool) -> None:
        """FIX-K (SG9). The rows are being read: the frame says so instead of
        standing empty (or hidden) until they arrive."""
        loading = bool(loading) and not self._rows
        if loading:
            self._layout.addWidget(self._loading)
        else:
            self._layout.removeWidget(self._loading)
        self._loading.setVisible(loading)
        self.setVisible(loading or bool(self._rows))

    def isLoading(self) -> bool:
        return not self._loading.isHidden()

    @staticmethod
    def _key(dim: int, tag: int) -> str:
        return '%d:%d' % (int(dim), int(tag))

    def setEntities(self, entries, hidden=()) -> None:
        """``entries`` is ``(dim, tag, name)``; ``hidden`` the ``(dim, tag)``
        pairs the eye has off. No entries hides the frame."""
        for row in self._rows.values():
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        while self._layout.count():
            self._layout.takeAt(0)
        self._loading.setVisible(False)
        hidden = {(int(d), int(t)) for d, t in hidden}
        for dim, tag, name in entries:
            key = self._key(dim, tag)
            row = PartRow(key, name, QColor('#8a8a8a'), self._host)
            row.setVisibleState((int(dim), int(tag)) not in hidden)
            row.visibilityToggled.connect(self._toggled)
            row.soloRequested.connect(self._solo)
            self._layout.addWidget(row)
            self._rows[key] = row
        self.setVisible(bool(self._rows))

    def setHidden(self, hidden) -> None:
        """Move the eyes to match ``hidden`` without rebuilding the rows."""
        hidden = {self._key(d, t) for d, t in hidden}
        for key, row in self._rows.items():
            row.setVisibleState(key not in hidden)

    def keys(self) -> list:
        return list(self._rows)

    def headingText(self) -> str:
        return self._heading.text()

    def _toggled(self, key, visible) -> None:
        dim, tag = (int(x) for x in key.split(':'))
        self.entityToggled.emit(dim, tag, bool(visible))

    def _solo(self, key) -> None:
        dim, tag = (int(x) for x in key.split(':'))
        self.entitySolo.emit(dim, tag)


class RegionPicker(QWidget):
    """DP-711 (viewport audit 0925 F1). Which regions and volume parts to show.

    A checkable list: All, then each region (for a multi-region case) with
    its cell zones -- or, for a mesh whose regions carry no zones, the parts
    named after each region point -- indented beneath it. Hidden when there
    is nothing to choose between.
    """

    filterChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('regionPicker')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._title = QLabel(self.tr('Regions'))
        self._list = QListWidget()
        self._list.setObjectName('regionPickerList')
        self._list.setAccessibleName(self.tr('Regions to show'))
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self._title)
        layout.addWidget(self._list)
        self._list.itemChanged.connect(self._itemChanged)
        self.setVisible(False)

    def setEntries(self, entries, checked=None):
        """``entries`` is ``[(key, label, depth)]``; ``checked`` the keys on."""
        entries = list(entries)
        checked = ({key for key, _label, _depth in entries}
                   if checked is None else set(checked))
        was = self._list.blockSignals(True)
        self._list.clear()
        if entries:
            self._add(ALL, self.tr('All'), 0,
                      all(key in checked for key, _l, _d in entries))
            for key, label, depth in entries:
                self._add(key, label, depth + 1, key in checked)
        self._list.blockSignals(was)
        rows = self._list.count()
        if rows:
            height = self._list.sizeHintForRow(0) * min(rows, 6) + 4
            self._list.setFixedHeight(height)
        self.setVisible(bool(entries))

    def _add(self, key, label, depth, on):
        item = QListWidgetItem(('    ' * depth) + label)
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if on
                           else Qt.CheckState.Unchecked)
        item.setToolTip(self.tr('Show or hide {0}').format(label))
        self._list.addItem(item)

    def keys(self) -> list:
        return [self._list.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(1, self._list.count())]

    def checkedKeys(self) -> list:
        return [item.data(Qt.ItemDataRole.UserRole)
                for item in (self._list.item(row)
                             for row in range(1, self._list.count()))
                if item.checkState() == Qt.CheckState.Checked]

    def setChecked(self, key, on: bool):
        """Tick or untick one row as a click would."""
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                item.setCheckState(Qt.CheckState.Checked if on
                                   else Qt.CheckState.Unchecked)
                return

    def _itemChanged(self, item):
        was = self._list.blockSignals(True)
        rows = [self._list.item(row) for row in range(self._list.count())]
        state = item.checkState()
        if item.data(Qt.ItemDataRole.UserRole) == ALL:
            for other in rows[1:]:
                other.setCheckState(state)
        else:
            everything = all(other.checkState() == Qt.CheckState.Checked
                             for other in rows[1:])
            rows[0].setCheckState(Qt.CheckState.Checked if everything
                                  else Qt.CheckState.Unchecked)
        self._list.blockSignals(was)
        self.filterChanged.emit(self.checkedKeys())


#: The QSS radius of ``QFrame#viewportOverlay`` (viewport.qss.tmpl).
OVERLAY_RADIUS = 6


class ViewportOverlay(QFrame):
    """A collapsible panel floating over the viewport."""

    visibilityToggled = Signal(str, bool)
    soloRequested = Signal(str)
    explodeChanged = Signal(float)
    #: UXB5. Whether the viewport holds anything to act on. The toolbar
    #: enables its model tools from this, and nothing else.
    sceneEmptyChanged = Signal(bool)
    #: DP-711. The Region picker's ticked keys (see `region_picker`).
    regionFilterChanged = Signal(list)
    #: DP-712. A part row was clicked: ``(key, additive)``.
    partSelectionRequested = Signal(str, bool)
    #: Plan 35 CR3. A button under the preview notice: ``'retry'``,
    #: ``'build_anyway'`` or ``'load_volume'``.
    previewActionRequested = Signal(str)
    #: Plan 40 UXE2. With the rail in charge the card is the one-line readout
    #: and pressing it asks for the *Parts* popover instead of opening a list
    #: on top of the model.
    partsRequested = Signal()
    #: Plan 43 LY7. The Parts chip at the top right reads "3 of 3": how many
    #: parts are shown, and how many there are.
    chipCountsChanged = Signal(int, int)
    #: Plan 43 LY6. How many parts are selected, for the bottom bar's
    #: "Selection" summary.
    selectionCountChanged = Signal(int)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # DP-2707. A native window over the GL view cannot be see-through, so
        # the corners outside the 6 px radius painted black; cut them off.
        from .legend_chip import rounded_mask
        self.setMask(rounded_mask(self.width(), self.height(), OVERLAY_RADIUS))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('viewportOverlay')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._rows: dict[str, PartRow] = {}
        self._groupLabels: list[QLabel] = []
        self._expanded = False
        self._railMode = False
        self._cellsText = ''

        layout = QVBoxLayout(self)
        # DP-191. The overlay had two more pixels under its content than
        # over it, so what it painted sat above its own centre.
        apply_bar_metrics(self, layout)
        layout.setSpacing(GAP_TIGHT)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._toggle = QToolButton()
        self._toggle.setObjectName('overlayPartsToggle')
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setAutoRaise(True)
        self._toggle.setToolTip(self.tr('Show the parts and section controls'))
        self._toggle.setAccessibleName(self._toggle.toolTip())
        # UXB5. The chip is a readout, one line: how many parts are shown. It
        # carries no button of its own -- Show all is the toolbar's, once.
        self._chip = _ElidedLine()
        self._chip.setObjectName('overlayChip')
        # Wide enough for "12 of 12 mesh parts shown" without eliding; a chip
        # that reads "12 of" tells the user nothing about what is hidden.
        self._chip.setMinimumWidth(150)
        header.addWidget(self._toggle)
        header.addWidget(self._chip, 1)
        layout.addLayout(header)

        # DP-95/DP-96. What the viewport is showing, and how big it is.
        # It sits above the run line because it answers the first question a
        # finished stage raises -- "what did that make" -- and because the
        # run line is often empty while this one is not.
        # UXB5. One measurement to a line, none of them wrapped: the sentence
        # used to flow, and broke "99.22 x 99.19 x 600.02 mm" in two.
        self._mesh = QWidget()
        self._mesh.setObjectName('overlayMesh')
        self._meshLayout = QVBoxLayout(self._mesh)
        self._meshLayout.setContentsMargins(0, 0, 0, 0)
        self._meshLayout.setSpacing(0)
        self._meshLines: list[_ElidedLine] = []
        self._meshSentence = ''
        self._mesh.setVisible(False)
        layout.addWidget(self._mesh)

        # F-37. Which run produced what is on screen, and how that run ended.
        # Without it a refused candidate and an accepted mesh are the same
        # picture, and the user is asked to judge cells with no way to tell
        # which run they belong to.
        self._result = _ElidedLine()
        self._result.setObjectName('overlayResult')
        self._resultSentence = ''
        self._result.setVisible(False)
        layout.addWidget(self._result)

        # Plan 35 CR3. What the picture is when it is not the whole mesh: a
        # decimated or surface-only preview, or the outline and patch list
        # drawn when no preview could be built -- with the way out.
        self._notice = QWidget()
        self._notice.setObjectName('overlayPreviewNotice')
        noticeLayout = QVBoxLayout(self._notice)
        noticeLayout.setContentsMargins(0, 0, 0, 0)
        noticeLayout.setSpacing(GAP_TIGHT)
        self._noticeText = QLabel()
        self._noticeText.setObjectName('overlayPreviewText')
        self._noticeText.setWordWrap(True)
        self._noticePatches = QLabel()
        self._noticePatches.setObjectName('overlayPreviewPatches')
        self._noticePatches.setWordWrap(True)
        noticeLayout.addWidget(self._noticeText)
        noticeLayout.addWidget(self._noticePatches)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        self._noticeButtons: dict[str, QPushButton] = {}
        for action, label in (('retry', self.tr('Try again')),
                              ('build_anyway', self.tr('Build anyway')),
                              ('load_volume', self.tr('Load full volume'))):
            button = QPushButton(label)
            button.setObjectName(f'overlayPreview_{action}')
            button.clicked.connect(
                lambda _checked=False, a=action:
                self.previewActionRequested.emit(a))
            buttons.addWidget(button)
            self._noticeButtons[action] = button
        buttons.addStretch(1)
        noticeLayout.addLayout(buttons)
        self._notice.setVisible(False)
        layout.addWidget(self._notice)

        self._body = QWidget()
        bodyLayout = QVBoxLayout(self._body)
        bodyLayout.setContentsMargins(0, 0, 0, 0)
        bodyLayout.setSpacing(GAP_TIGHT)

        # UXE2. Region picker, parts list and Explode are one widget, so the
        # rail's Parts popover can take them as they are.
        self._partsBody = QWidget()
        self._partsBody.setObjectName('overlayPartsBody')
        partsBody = QVBoxLayout(self._partsBody)
        partsBody.setContentsMargins(0, 0, 0, 0)
        partsBody.setSpacing(GAP_TIGHT)
        bodyLayout.addWidget(self._partsBody)

        self._regionPicker = RegionPicker()
        self._regionPicker.filterChanged.connect(self.regionFilterChanged)
        partsBody.addWidget(self._regionPicker)

        self._partsHost = QWidget()
        self._partsLayout = QVBoxLayout(self._partsHost)
        self._partsLayout.setContentsMargins(0, 0, 0, 0)
        self._partsLayout.setSpacing(0)

        scroll = _PartsScroll()
        scroll.rowsHeight = self._railRowsHeight
        scroll.setObjectName('overlayPartsScroll')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._partsHost)
        self._partsScroll = scroll
        partsBody.addWidget(scroll)
        # Plan 42 SG9. The Gmsh model's entities, listed while Gmsh is active.
        self._gmshEntities = GmshEntityGroup()
        partsBody.addWidget(self._gmshEntities)
        # DP-712. The cap follows the viewport the overlay floats over.
        self._capPartsHeight()
        self._capWidth()
        if parent is not None:
            parent.installEventFilter(self)

        # WP7.5. Cheap once zones have their own colours, and the fastest way
        # to see what is actually in a conjugate case.
        explodeRow = QWidget()
        explodeLayout = QHBoxLayout(explodeRow)
        explodeLayout.setContentsMargins(0, 0, 0, 0)
        explodeLayout.setSpacing(GAP_TIGHT)
        explodeLabel = QLabel(self.tr('Explode'))
        self._explode = QSlider(Qt.Orientation.Horizontal)
        self._explode.setObjectName('overlayExplode')
        self._explode.setRange(0, 100)
        self._explode.setValue(0)
        self._explode.setToolTip(
            self.tr('Push the parts apart along the vector from the centre'))
        self._explode.setAccessibleName(self._explode.toolTip())
        explodeLayout.addWidget(explodeLabel)
        explodeLayout.addWidget(self._explode, 1)
        partsBody.addWidget(explodeRow)
        self._explode.valueChanged.connect(
            lambda value: self.explodeChanged.emit(value / 100.0))

        self._sectionPanel = SectionPanel()
        self._sectionToggle = QToolButton()
        self._sectionToggle.setObjectName('overlaySectionToggle')
        apply_tinted_icon(self._sectionToggle, ':/graphicsIcons/section.svg',
                          ROW_ICON, ROW_GLYPH_STROKE_PX)
        self._sectionToggle.setText(self.tr('Cut plane'))
        self._sectionToggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._sectionToggle.setAutoRaise(True)
        self._sectionToggle.setCheckable(True)
        self._sectionToggle.setToolTip(
            self.tr('Aim the cut plane without opening a panel'))
        bodyLayout.addWidget(self._sectionToggle)
        bodyLayout.addWidget(self._sectionPanel)
        self._sectionPanel.setVisible(False)

        layout.addWidget(self._body)
        self._body.setVisible(False)

        self._toggle.clicked.connect(self.toggleExpanded)
        self._sectionToggle.toggled.connect(self._sectionPanel.setVisible)
        self._sectionToggle.toggled.connect(lambda _checked: self.adjustSize())

        self._partsTotal = 0
        self._sceneEmpty = True
        self.setChip(0, 0)

    # -- state ------------------------------------------------------------- #

    def sectionPanel(self) -> SectionPanel:
        return self._sectionPanel

    def gmshEntities(self) -> GmshEntityGroup:
        """Plan 42 SG9. The "Gmsh entities" group of the Parts list."""
        return self._gmshEntities

    # -- UXE2: the rail takes the lists ------------------------------------ #

    def setRailMode(self, on: bool):
        """The card is the one-line readout; the lists live in popovers."""
        self._railMode = bool(on)
        # Plan 45 (user 2026-10-17): the rail's Parts button is the one way
        # into the Parts list, so the card's arrow is not shown beside it.
        self._toggle.setVisible(not self._railMode)
        if self._railMode:
            self._body.setVisible(False)
            self._expanded = False
            self._toggle.setArrowType(Qt.ArrowType.RightArrow)
            self._toggle.setToolTip(self.tr('Open the parts list'))
            self._toggle.setAccessibleName(self._toggle.toolTip())
            self._chip.setMinimumWidth(0)
        self._capPartsHeight()
        self._partsScroll.updateGeometry()
        self._refreshChip()
        self.adjustSize()

    def isRailMode(self) -> bool:
        return self._railMode

    def takePartsPanel(self) -> QWidget:
        """The counts, the region picker, the parts list and Explode, as one
        widget for the Parts popover. Taken once; the card keeps feeding it."""
        panel = getattr(self, '_partsPanel', None)
        if panel is None:
            panel = QWidget()
            panel.setObjectName('overlayPartsPanel')
            # Plan 45 M: the list is the one scroll area; the popover must
            # not wrap this panel in a second one.
            panel.setProperty('popoverOwnsScroll', True)
            column = QVBoxLayout(panel)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(GAP_TIGHT)
            column.addWidget(self._mesh)
            column.addWidget(self._partsBody)
            self._partsPanel = panel
        return panel

    def chipText(self) -> str:
        """The line as it is drawn."""
        return self._chip.fullText()

    def _refreshChip(self):
        base = getattr(self, '_chipBase', '')
        if self._railMode and self._partsTotal:
            # Plan 45: "2 of 2 parts" was said three times -- here, on the
            # Parts tab and on the bottom bar. The bottom bar's Parts button
            # is its one home (always on screen, so isolation is never
            # invisible); this line keeps what only it says, the cell count.
            base = self._cellsText
        self._chip.setText(base)

    def event(self, event):
        # UXB5. The card is Fixed and sized by hand, so a line that grows
        # after the size was taken -- the style bolds the chip once it is
        # polished -- would be squeezed into the old width and elide. A
        # layout request is the card learning its lines changed.
        result = super().event(event)
        if (event.type() == QEvent.Type.LayoutRequest
                and not getattr(self, '_adjusting', False)):
            self._adjusting = True
            try:
                self.adjustSize()
            finally:
                self._adjusting = False
        return result

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self._capPartsHeight()
            self._capWidth()
        return super().eventFilter(watched, event)

    def widthCap(self) -> int:
        return self.maximumWidth()

    def _capWidth(self):
        """UXB5. The card is as wide as its longest line, up to a share of the
        viewport; a longer line elides instead of wrapping."""
        parent = self.parentWidget()
        width = parent.width() if parent is not None and parent.width() else 700
        cap = max(CARD_MIN_WIDTH, min(CARD_MAX_WIDTH,
                                      int(width * CARD_WIDTH_SHARE)))
        if cap != self.maximumWidth():
            self.setMaximumWidth(cap)
            self.adjustSize()

    def partsHeightCap(self) -> int:
        return self._partsScroll.maximumHeight()

    def _railRowsHeight(self):
        """The height of the first ten rows (and the headings and gaps among
        them), measured from the layout; None outside the rail."""
        if not self._railMode:
            return None
        layout = self._partsLayout
        total = 0
        rows = 0
        through = 0
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if isinstance(item.widget(), PartRow):
                if rows == PARTS_VISIBLE_ROWS:
                    break
                rows += 1
                total += item.sizeHint().height()
                through = total
            else:
                total += item.sizeHint().height()
        return through if rows == PARTS_VISIBLE_ROWS else total

    def _capPartsHeight(self):
        """DP-712. At most 40% of the viewport, and scroll past it."""
        if self._railMode:
            # The popover sizes the list: ten rows, then the wheel.
            cap = 16777215
            if cap != self._partsScroll.maximumHeight():
                self._partsScroll.setMaximumHeight(cap)
            return
        parent = self.parentWidget()
        height = parent.height() if parent is not None else 450
        cap = max(PARTS_MIN_HEIGHT, int(height * PARTS_HEIGHT_SHARE))
        if cap != self._partsScroll.maximumHeight():
            self._partsScroll.setMaximumHeight(cap)
            if self._expanded:
                self.adjustSize()

    def regionPicker(self) -> RegionPicker:
        return self._regionPicker

    def setRegionEntries(self, entries, checked=None):
        """Offer these regions and parts; hidden when there is no choice."""
        self._regionPicker.setEntries(entries, checked)
        self.adjustSize()

    def isExpanded(self) -> bool:
        return self._expanded

    def setExpanded(self, expanded: bool):
        if self._railMode:
            if expanded:
                self.partsRequested.emit()
            return
        self._expanded = bool(expanded)
        self._body.setVisible(self._expanded)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded
            else Qt.ArrowType.RightArrow)
        self.adjustSize()

    def toggleExpanded(self):
        self.setExpanded(not self._expanded)

    def setMesh(self, text: str):
        """Say what mesh is on screen and how big it is, or hide the line.

        DP-96. The toolbar's cell count is the only other always-visible
        statement of size, and it says how many cells and nothing else: not
        which stage made them, not whether this is a surface or a volume,
        not how big the thing is in metres. A user reading `36,533 cells`
        cannot tell a 0.4 m duct from a 0.4 mm one.

        UXB5. Each measurement is a line of its own and none wraps.
        """
        text = str(text or '')
        self._meshSentence = text
        found = re.search(r'([\d,]+) cells?', text)
        self._cellsText = found.group(0) if found else ''
        self._refreshChip()
        items = summary_items(text)
        while len(self._meshLines) > len(items):
            line = self._meshLines.pop()
            self._meshLayout.removeWidget(line)
            line.deleteLater()
        while len(self._meshLines) < len(items):
            line = _ElidedLine()
            self._meshLines.append(line)
            self._meshLayout.addWidget(line)
        for line, item in zip(self._meshLines, items):
            line.setText(item)
            line.setTip(text)
        self._mesh.setToolTip(text)
        self._mesh.setVisible(bool(items))
        self.adjustSize()

    def meshText(self) -> str:
        return self._meshSentence

    def meshLines(self) -> list[str]:
        """The lines the card draws for the mesh, as they are drawn."""
        return [line.fullText() for line in self._meshLines]

    def setResult(self, text: str):
        """Name the run whose mesh is on screen, or hide the line (F-37).

        UXB5. The line shows the verdict and the format; the run id and the
        cell count (the mesh line above counts them) are in the tooltip.
        """
        text = str(text or '')
        self._resultSentence = text
        shown = result_chip_text(text)
        self._result.setText(shown)
        self._result.setTip(text)
        self._result.setVisible(bool(shown))
        self.adjustSize()

    def resultText(self) -> str:
        """The run's whole sentence (what the tooltip carries)."""
        return self._resultSentence

    def resultLine(self) -> str:
        """The line as it is drawn."""
        return self._result.fullText()

    def setPreviewNotice(self, notice):
        """Say what the picture is when it is not the whole mesh (CR3).

        ``notice`` is ``MeshManager.previewNotice()``: ``{'text', 'patches',
        'actions'}``, or None to hide the line.
        """
        notice = notice or {}
        text = str(notice.get('text') or '')
        patches = list(notice.get('patches') or [])
        actions = set(notice.get('actions') or [])
        self._noticeText.setText(text)
        self._noticeText.setToolTip(text)
        lines = [self.tr('{0}: {1:,} faces').format(name, int(count))
                 for name, count in patches[:NOTICE_PATCHES]]
        if len(patches) > NOTICE_PATCHES:
            lines.append(self.tr('and {0} more patches').format(
                len(patches) - NOTICE_PATCHES))
        self._noticePatches.setText('\n'.join(lines))
        self._noticePatches.setVisible(bool(lines))
        for action, button in self._noticeButtons.items():
            button.setVisible(action in actions)
        self._notice.setVisible(bool(text))
        self._notice.updateGeometry()
        self._updateSceneEmpty()
        self.adjustSize()

    def previewNoticeWidget(self) -> QWidget:
        """Plan 43 LY7. The notice, for the Viewport notice strip to adopt.
        It is the widget it always was; the card no longer holds it."""
        return self._notice

    def previewNoticeText(self) -> str:
        # The notice's own flag, not its place in the card: the notice strip
        # holds it now (LY7).
        return self._noticeText.text() if not self._notice.isHidden() else ''

    def previewActions(self) -> list[str]:
        return [action for action, button in self._noticeButtons.items()
                if not self._notice.isHidden() and not button.isHidden()]

    def setChip(self, shown: int, total: int, noun: str = '',
                detail: str = ''):
        """The "3 of 11 mesh parts shown" chip.

        A user who forgets they isolated something reads a partial mesh as a
        broken one, so isolation is never allowed to be invisible.

        F-44. ``noun`` says *what* is being counted. It used to be every prop
        in the scene, so one duct read "14 parts" -- the surfaces it was
        meshed from, its patches, its internal volume and its zones, added up
        because the renderer holds them all.
        """
        noun = noun or self.tr('parts')
        self._partsTotal = int(total or 0)
        if not total:
            self._chipBase = self.tr('Nothing loaded')
            self._chip.setProperty('partial', False)
        else:
            self._chipBase = self.tr(
                '{0} of {1} {2} shown').format(shown, total, noun)
            self._chip.setProperty('partial', shown < total)
        self._refreshChip()
        self.chipCountsChanged.emit(int(shown or 0), int(total or 0))
        # CP-09 item 6. The count says how many; the detail says which, by
        # name, split into boundary patches and cell zones and grouped by
        # region. A count on its own cannot answer "is my inlet still there".
        self._chipDetail = str(detail or '')
        self._chip.setTip(self._chipDetail)
        self._chip.style().unpolish(self._chip)
        self._chip.style().polish(self._chip)
        self._updateSceneEmpty()
        self.adjustSize()

    def isSceneEmpty(self) -> bool:
        """UXB5. True while there is nothing in the viewport to act on.

        A preview that could not be built still draws the mesh's outline, and
        that is something: only no parts and no outline is empty.
        """
        return self._sceneEmpty

    def _updateSceneEmpty(self):
        empty = (not getattr(self, '_partsTotal', 0)
                 and self._notice.isHidden())
        if empty != self._sceneEmpty:
            self._sceneEmpty = empty
            self.sceneEmptyChanged.emit(empty)

    def setParts(self, parts, groups=None, roles=None):
        """Rebuild the rows. ``parts`` is (key, name, QColor, visible).

        Plan 44 F1 (MF21). ``roles`` maps a key to the small role text shown
        after its name (``part_role``); a key with none shows no role.

        DP-712. ``groups`` maps a key to the heading it files under -- its
        region, and within a region whether it is a boundary or a zone. The
        rows are listed heading by heading, in the order the headings first
        appear; with one heading or none, no heading is drawn.
        """
        for row in self._rows.values():
            self._partsLayout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for label in self._groupLabels:
            self._partsLayout.removeWidget(label)
            label.deleteLater()
        self._groupLabels = []
        # And the gaps left above the headings.
        while self._partsLayout.count():
            self._partsLayout.takeAt(0)

        parts = list(parts)
        groups = dict(groups or {})
        roles = dict(roles or {})
        order: dict[str, list] = {}
        for part in parts:
            order.setdefault(groups.get(part[0], ''), []).append(part)
        headed = len(order) > 1
        for heading, members in order.items():
            if headed:
                label = QLabel(heading or self.tr('Other'), self._partsHost)
                label.setObjectName('overlayPartGroup')
                font = QFont(label.font())
                font.setBold(True)
                label.setFont(font)
                # The heading's own step in from the rows and down from the
                # group above, without writing padding into the bar.
                label.setIndent(MARGIN_TIGHT)
                self._partsLayout.addSpacing(GAP_TIGHT)
                self._partsLayout.addWidget(label)
                self._groupLabels.append(label)
            for key, name, color, visible in members:
                row = PartRow(key, name, color, self._partsHost,
                              role=roles.get(key, ''))
                row.setVisibleState(visible)
                row.visibilityToggled.connect(self.visibilityToggled)
                row.soloRequested.connect(self.soloRequested)
                row.selectRequested.connect(self.partSelectionRequested)
                self._partsLayout.addWidget(row)
                self._rows[key] = row

        self._partsScroll.updateGeometry()
        shown = sum(1 for _key, _name, _color, visible in parts if visible)
        # The caller that knows the mesh re-sets the chip with the mesh's own
        # nouns and detail straight after (`MainWindow.rebuildOverlayParts`);
        # this keeps a scene with no mesh in it counted.
        self.setChip(shown, len(parts))
        self.selectionCountChanged.emit(0)

    def chipDetail(self) -> str:
        """What the chip says the scene is made of, by name."""
        return getattr(self, '_chipDetail', '')

    def groupHeadings(self) -> list[str]:
        return [label.text() for label in self._groupLabels]

    def partKeys(self) -> list[str]:
        return list(self._rows)

    def setSelectedParts(self, keys):
        """DP-712. Mark the selected parts' rows, from any selection source."""
        keys = set(keys)
        for key, row in self._rows.items():
            row.setSelected(key in keys)
        self.selectionCountChanged.emit(len(self.selectedParts()))

    def selectedParts(self) -> list[str]:
        return [key for key, row in self._rows.items() if row.isSelected()]

    def explodeSlider(self) -> QSlider:
        """The Explode slider of the Parts panel (Plan 43 LY6: the bottom bar
        keeps a twin that moves with it)."""
        return self._explode

    def updateParts(self, states):
        """Refresh colour and eye state without rebuilding the rows."""
        for key, color, visible in states:
            row = self._rows.get(key)
            if row is None:
                continue
            row.setColor(color)
            row.setVisibleState(visible)
