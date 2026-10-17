#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 43 LY2. The step ribbon: the outline as one line under the menu bar.

The ribbon replaces the Outline dock. It does not own any workflow knowledge:
``NavigationView`` still holds the order, the states and what a click does,
and the ribbon draws ``NavigationView.ribbonEntries()`` and activates a step
through ``NavigationView.activateRow`` -- the same door the outline's own
click used, so a step opened from here is opened exactly as before.

Marks (one shape each, none told apart by colour alone):

  ``✓`` done  ``◆`` current  ``○`` not done  ``!`` needs attention
  ``–`` cannot be used yet

The thirteen outline states fold onto these five (``ribbon_mark``). The
engine's own steps come from the engine context (Plan 42 SG8), because they
are the rows the outline already lists under *Mesh setup*; *Mesh setup*
itself is the chapter mark.

Full labels show by default; numbers only is the compact form (Ctrl+B) and is
forced at ``COMPACT_AT_MOST`` px of window width and below (Plan 43 section 4 item 4: at 1280 x 800 the ribbon shows numbers only).
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QByteArray, QEvent, QModelIndex, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QToolButton)

from foammesh.view.theming import ribbon_style as rs
from foammesh.view.theming.metrics import FORM_MARGIN, RADIUS_CONTROL

from .navigation_view import WorkflowRowState, rail_label

#: The ribbon is one line of this height (Plan 43 section 1).
RIBBON_HEIGHT = 36
#: Below this window width the ribbon shows numbers only.
COMPACT_AT_MOST = 1280
#: Kept for callers that read the old name: the first width that is NOT compact.
COMPACT_BELOW = COMPACT_AT_MOST + 1
#: The "?" button is this square.
MARKS_SIZE = 24


class RibbonMark(str, Enum):
    DONE = 'done'
    CURRENT = 'current'
    TODO = 'todo'
    ATTENTION = 'attention'
    LOCKED = 'locked'


MARK_GLYPH = {
    RibbonMark.DONE: '✓',
    RibbonMark.CURRENT: '◆',
    RibbonMark.TODO: '○',
    RibbonMark.ATTENTION: '!',
    RibbonMark.LOCKED: '–',
}

#: Beside the tick of a step that is finished and locked.
LOCK_GLYPH = '▪'

MARK_MEANING = {
    RibbonMark.DONE: 'done (a finished step is locked until you unlock it)',
    RibbonMark.CURRENT: 'the step you are on, or one that is running',
    RibbonMark.TODO: 'not done yet',
    RibbonMark.ATTENTION: 'needs attention: a warning, a failure, a waived '
                          'check',
    RibbonMark.LOCKED: 'cannot be used yet: an earlier step comes first',
}

_STATE_MARK = {
    WorkflowRowState.COMPLETED: RibbonMark.DONE,
    WorkflowRowState.RUNNING: RibbonMark.CURRENT,
    WorkflowRowState.WARNING: RibbonMark.ATTENTION,
    WorkflowRowState.FAILED: RibbonMark.ATTENTION,
    # Plan 43 LY9. Out of date is not done: unlocking a step sends the later
    # ones back to "not done", they are not alarms.
    WorkflowRowState.STALE: RibbonMark.TODO,
    WorkflowRowState.WAIVED: RibbonMark.ATTENTION,
    WorkflowRowState.LOCKED: RibbonMark.LOCKED,
    WorkflowRowState.SKIPPED: RibbonMark.LOCKED,
}


def ribbon_mark(state: str, *, enabled: bool = True,
                current: bool = False) -> RibbonMark:
    """Fold an outline state onto one of the five marks.

    Attention outranks "current": the step you are standing on can still be
    the one that failed, and the chip's own highlight already says where you
    are. A row the tree has disabled reads as ``cannot be used yet`` whatever
    state it was given.
    """
    try:
        folded = _STATE_MARK.get(WorkflowRowState(state))
    except ValueError:
        folded = None
    if folded is RibbonMark.ATTENTION:
        return folded
    if not enabled or folded is RibbonMark.LOCKED:
        return RibbonMark.LOCKED
    if current:
        return RibbonMark.CURRENT
    return folded or RibbonMark.TODO


class RibbonLegend(QFrame):
    """The popover behind the "?": what the five marks mean."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('stepRibbonLegend')
        self.setAccessibleName('Step ribbon marks')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        grid = QGridLayout(self)
        grid.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN,
                                FORM_MARGIN)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.rows = []
        for row, mark in enumerate(RibbonMark):
            glyph = QLabel(MARK_GLYPH[mark])
            glyph.setObjectName('stepRibbonLegendMark')
            meaning = MARK_MEANING[mark]
            text = QLabel(meaning[:1].upper() + meaning[1:])
            text.setObjectName('stepRibbonLegendMeaning')
            grid.addWidget(glyph, row, 0)
            grid.addWidget(text, row, 1)
            self.rows.append((MARK_GLYPH[mark], meaning))


class StepTab(QToolButton):
    """Plan 44 MF4. One step of the ribbon, painted.

    A filled tab with a 1 px outline in its state colour (the current tab's is
    the accent), rounded top corners, no bottom edge and a bar inset along its
    foot. ``text()`` is still the plain text of the step (the mark, the name and
    the cells), so a reader of the chip sees what the painter shows.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('stepRibbonChip')
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedHeight(rs.TAB_HEIGHT)
        self._model = rs.TabModel()
        self._numbers = False
        self.setProperty('state', self._model.state)

    # -- what the tab shows --------------------------------------------- #

    def model(self) -> 'rs.TabModel':
        return self._model

    def setModel(self, model: 'rs.TabModel', *, numbers: bool) -> None:
        self._model = model
        self._numbers = bool(numbers)
        self.setProperty('state', model.state)
        self._setCursor()
        self.update()

    def _setCursor(self) -> None:
        if not self.isEnabled():
            # A tab that cannot be used yet shows the "not allowed" cursor.
            self.setCursor(Qt.CursorShape.ForbiddenCursor)
        elif self._model.chapter:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def changeEvent(self, event):  # noqa: N802 - Qt name
        if event.type() == QEvent.Type.EnabledChange:
            self._setCursor()
        super().changeEvent(event)

    def bodyHeight(self) -> int:
        """The painted height: the current tab also covers the band's line."""
        return self.height() if self._model.current else self.height() - 1

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(rs.TAB_HEIGHT)
        return hint

    # -- painting ------------------------------------------------------- #

    def _font(self) -> QFont:
        font = QFont(self.font())
        model = self._model
        if model.chapter:
            font.setPointSizeF(11.5 * 0.75)
            font.setCapitalization(QFont.Capitalization.AllUppercase)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
            font.setWeight(QFont.Weight.DemiBold)
        else:
            font.setPointSizeF(12.5 * 0.75)
            font.setWeight(QFont.Weight.DemiBold if model.current
                           else QFont.Weight.Normal)
        return font

    def paintEvent(self, event):  # noqa: N802 - Qt name
        model = self._model
        tokens = rs.resolve_tokens()
        colours = rs.tab_colours(model.state, tokens)
        width, body = float(self.width()), float(self.bodyHeight())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hovered = self.isEnabled() and self.underMouse()
        painter.fillPath(rs.top_rounded(0, 0, width, body, RADIUS_CONTROL),
                         QColor(colours.hover if hovered else colours.fill))
        painter.setPen(QPen(QColor(rs.border_colour(model, tokens)), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(rs.outline_path(width, body))
        self._paintBar(painter, tokens, width, body)
        self._paintContent(painter, colours, width, body)
        if self.hasFocus():
            painter.setPen(QPen(QColor(tokens.value('focus.ring')), 1,
                                Qt.PenStyle.DotLine))
            painter.drawRoundedRect(QRectF(2.5, 2.5, width - 5, body - 6),
                                    RADIUS_CONTROL, RADIUS_CONTROL)
        painter.end()

    def _paintBar(self, painter, tokens, width, body) -> None:
        model = self._model
        height = rs.bar_height(model)
        x, w = rs.BAR_INSET, width - 2 * rs.BAR_INSET
        y = body - height
        if w <= 0:
            return
        painter.save()
        painter.setClipPath(rs.top_rounded(x, y, w, height, rs.BAR_RADIUS))
        segments = model.segments if model.chapter and model.segments else ()
        if segments:
            each = w / len(segments)
            for position, state in enumerate(segments):
                painter.fillRect(
                    QRectF(x + position * each, y, each + 0.5, height),
                    QColor(rs.tab_colours(state, tokens).bar))
        else:
            painter.fillRect(QRectF(x, y, w, height),
                             QColor(rs.bar_colour(model, tokens)))
        painter.restore()

    def _paintContent(self, painter, colours, width, body) -> None:
        model = self._model
        font = self._font()
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        glyph_font = QFont(font)
        glyph_font.setPointSizeF(12 * 0.75)
        glyph_font.setWeight(QFont.Weight.Bold if model.glyph == '!'
                             else QFont.Weight.Normal)
        glyph_width, gap = 12.0, 6.0
        number = model.number
        label = '' if self._numbers else model.label
        lock_size = (rs.LOCK_SIZE if (model.locked or model.lock_slot)
                     else 0.0)
        padding = 4.0 if self._numbers else 8.0
        number_width = metrics.horizontalAdvance(number) if number else 0.0
        show_glyph = bool(model.glyph) and not model.chapter
        widths = []
        if show_glyph:
            widths.append(glyph_width)
        if number:
            widths.append(number_width)
        if label:
            used = sum(widths) + gap * len(widths) + (
                gap + lock_size if lock_size else 0.0)
            room = max(0.0, width - 2 * padding - used)
            label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, room)
            widths.append(metrics.horizontalAdvance(label))
        if lock_size:
            widths.append(lock_size)
        total = sum(widths) + gap * max(0, len(widths) - 1)
        x = max(padding, (width - total) / 2.0)
        text_h = body - 2.0
        left = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        painter.setPen(QColor(colours.text))
        if show_glyph:
            painter.setFont(glyph_font)
            painter.drawText(QRectF(x, 0, glyph_width, text_h),
                             Qt.AlignmentFlag.AlignCenter, model.glyph)
            painter.setFont(font)
            x += glyph_width + gap
        if number:
            painter.setOpacity(rs.NUMBER_OPACITY)
            painter.drawText(QRectF(x, 0, number_width + 1, text_h), left,
                             number)
            painter.setOpacity(1.0)
            x += number_width + gap
        if label:
            label_width = metrics.horizontalAdvance(label)
            painter.drawText(QRectF(x, 0, label_width + 1, text_h), left,
                             label)
            x += label_width + gap
        if model.locked and lock_size:
            renderer = QSvgRenderer(QByteArray(
                rs.LOCK_SVG.format(ink=colours.text).encode('utf-8')))
            painter.setOpacity(rs.LOCK_OPACITY)
            renderer.render(painter, QRectF(
                x, (text_h - lock_size) / 2.0, lock_size, lock_size))
            painter.setOpacity(1.0)


class StepRibbon(QFrame):
    """One line of steps. See the module docstring."""

    #: ``{'numbers_only', 'colour_by_state', 'show_locks'}`` after a change.
    optionsChanged = Signal(dict)
    #: Plan 44 F4 (mockup ``flash(`${n}. ${name}: ${why}.`)``). A click on a
    #: tab that cannot be used yet: the sentence saying what opens it. The
    #: window shows it on its status bar; the step is not opened.
    unavailableClicked = Signal(str)

    def __init__(self, navigation, parent=None):
        super().__init__(parent)
        self._navigation = navigation
        self._numbers_only = False
        self._colour = True
        self._show_locks = True
        self._compact = False
        self._entries = []
        self._chips: list[StepTab] = []
        self.setObjectName('stepRibbon')
        self.setAccessibleName(self.tr('Workflow steps'))
        self.setFixedHeight(RIBBON_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # The band: padding 4 8 0, tabs 2 px apart. No scroll area and no
        # trailing stretch, so the tabs fill the line.
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(rs.BAND_SIDE, rs.BAND_TOP, rs.BAND_SIDE, 0)
        self._row.setSpacing(rs.TAB_GAP)
        self._row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Numbers only: "N. Title" of the current step after the tabs.
        self._readout = QLabel(self)
        self._readout.setObjectName('stepRibbonReadout')
        self._readout.setContentsMargins(FORM_MARGIN, 0, 0, 0)
        self._readout.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Fixed)
        self._readout.setFixedHeight(rs.TAB_HEIGHT)
        self._readout.hide()
        self._row.addWidget(self._readout, 0)

        # The "?" is a 24 px ghost button on the band, over the last tab's
        # empty right edge, so the tabs still run from x=8 to x=W-8.
        self._marks = QToolButton(self)
        self._marks.setObjectName('stepRibbonMarks')
        self._marks.setText('?')
        self._marks.setAutoRaise(True)
        self._marks.setFixedSize(MARKS_SIZE, MARKS_SIZE)
        self._marks.setToolTip(self.tr(
            'What the marks beside each step mean  (?)'))
        self._marks.setAccessibleName(
            self.tr('Marks: what the marks beside each step mean'))
        self._marks.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._legend = RibbonLegend(self)
        self._marks.clicked.connect(lambda _c=False: self.showLegend())
        self._placeMarks()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.refresh)
        navigation.outlineChanged.connect(self._schedule)
        self.refresh()

    # -- options ----------------------------------------------------------- #

    def numbersOnly(self) -> bool:
        """The user's choice (Ctrl+B); the compact width may force it on."""
        return self._numbers_only

    def isNumbers(self) -> bool:
        return self._numbers_only or self._compact

    def setNumbersOnly(self, on: bool) -> bool:
        on = bool(on)
        if on == self._numbers_only:
            return False
        self._numbers_only = on
        self._changed()
        return True

    def colourByState(self) -> bool:
        return self._colour

    def setColourByState(self, on: bool) -> bool:
        on = bool(on)
        if on == self._colour:
            return False
        self._colour = on
        self._changed()
        return True

    def showLocks(self) -> bool:
        return self._show_locks

    def setShowLocks(self, on: bool) -> bool:
        on = bool(on)
        if on == self._show_locks:
            return False
        self._show_locks = on
        self._changed()
        return True

    def options(self) -> dict:
        return {'numbers_only': self._numbers_only,
                'colour_by_state': self._colour,
                'show_locks': self._show_locks}

    def setOptions(self, record) -> None:
        """Apply a saved record without announcing it as a user change."""
        record = dict(record or {})
        self._numbers_only = bool(record.get('numbers_only', False))
        self._colour = bool(record.get('colour_by_state', True))
        self._show_locks = bool(record.get('show_locks', True))
        self.refresh()

    def _changed(self) -> None:
        self.refresh()
        self.optionsChanged.emit(self.options())

    # -- drawing ----------------------------------------------------------- #

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def entries(self) -> list:
        return list(self._entries)

    def chips(self) -> list:
        """The chips now showing, in step order."""
        return self._chips[:len(self._entries)]

    def chipFor(self, label: str):
        """The chip of the step whose label is ``label`` (or ``None``)."""
        for entry, chip in zip(self._entries, self.chips()):
            if entry.label == label:
                return chip
        return None

    def marks(self) -> list:
        """``(label, RibbonMark)`` per step, in step order."""
        return [(entry.label, self._markOf(entry)) for entry in self._entries]

    @staticmethod
    def _usable(entry) -> bool:
        """A step the workflow has reached: enabled and not painted locked."""
        return entry.enabled and entry.state != WorkflowRowState.LOCKED.value

    def _markOf(self, entry) -> RibbonMark:
        return ribbon_mark(entry.state, enabled=entry.enabled,
                           current=entry.current)

    def _stateOf(self, entry) -> tuple[str, bool]:
        """``(tab state, needs attention)`` of an entry, for the painter."""
        return rs.tab_state(mark=self._markOf(entry).value,
                            row_state=entry.state, current=entry.current,
                            chapter=entry.chapter, colour=self._colour)

    def _segments(self, position: int) -> tuple:
        """The chapter's bar: one segment per step listed under it."""
        states = []
        for entry in self._entries[position + 1:]:
            if entry.depth == 0:
                break
            states.append(self._stateOf(entry)[0])
        return tuple(states)

    def refresh(self) -> None:
        self._timer.stop()
        self._entries = self._navigation.ribbonEntries()
        numbers = self.isNumbers()
        while len(self._chips) < len(self._entries):
            self._chips.append(self._makeChip())
        self._row.setContentsMargins(
            rs.BAND_SIDE, rs.BAND_TOP,
            rs.BAND_SIDE + (MARKS_SIZE + 4 if numbers else 0), 0)
        readout = ''
        for position, chip in enumerate(self._chips):
            if position >= len(self._entries):
                chip.hide()
                continue
            entry = self._entries[position]
            mark = self._markOf(entry)
            glyph = MARK_GLYPH[mark]
            plain_glyph = glyph
            if mark is RibbonMark.DONE and entry.locked:
                glyph += LOCK_GLYPH
            if mark is RibbonMark.LOCKED and not self._show_locks:
                glyph = ''
                plain_glyph = ''
            name = rail_label(entry.label) if numbers else entry.label
            cells = '' if numbers or not entry.cells else f'  {entry.cells}'
            chip.setText(' '.join(part for part in (glyph, name) if part)
                         + cells)
            meaning = MARK_MEANING[mark].split(' (')[0]
            if mark is RibbonMark.DONE and entry.locked:
                meaning += ', locked'
            if not self._usable(entry) and (
                    not entry.tooltip or entry.tooltip == entry.label):
                # Plan 44 F4. A tab with nothing of its own to say carries
                # the reason its click shows.
                chip.setToolTip(self.unavailableReason(position).rstrip('.'))
            else:
                chip.setToolTip(entry.tooltip if entry.tooltip
                                else f'{entry.label} — {meaning}')
            chip.setAccessibleName(f'{entry.label}, {meaning}')
            chip.setEnabled(self._usable(entry))
            number, title = rs.split_label(entry.label)
            state, attention = self._stateOf(entry)
            chip.setModel(rs.TabModel(
                state=state, glyph=plain_glyph,
                number=rail_label(entry.label) if numbers else number,
                label=title + (f'  {entry.cells}' if entry.cells else ''),
                locked=bool(mark is RibbonMark.DONE and entry.locked),
                lock_slot=bool(mark is RibbonMark.DONE and self._show_locks),
                current=entry.current, attention=attention,
                chapter=entry.chapter,
                segments=self._segments(position) if entry.chapter else ()),
                numbers=numbers)
            for key, value in (('mark', mark.value),
                               ('current', entry.current),
                               ('chapter', entry.chapter),
                               ('colour', self._colour),
                               ('token', entry.token)):
                chip.setProperty(key, value if not isinstance(value, bool)
                                 else ('true' if value else 'false'))
            # The tabs share the line: equal widths, the chapter 0.95 of one;
            # numbers only they are fixed at 58 px.
            if numbers:
                chip.setFixedWidth(rs.NUMBERS_TAB_WIDTH)
                chip.setSizePolicy(QSizePolicy.Policy.Fixed,
                                   QSizePolicy.Policy.Fixed)
                self._row.setStretchFactor(chip, 0)
            else:
                chip.setMinimumWidth(0)
                chip.setMaximumWidth(16777215)
                chip.setSizePolicy(QSizePolicy.Policy.Ignored,
                                   QSizePolicy.Policy.Fixed)
                self._row.setStretchFactor(
                    chip, rs.CHAPTER_STRETCH if entry.chapter
                    else rs.STEP_STRETCH)
            chip.show()
            chip.update()
            if entry.current:
                readout = entry.label
        shown = bool(numbers and readout)
        self._readout.setText(readout)
        self._readout.setVisible(shown)
        self._row.setStretchFactor(self._readout, 1 if shown else 0)
        self._placeMarks()

    def _placeMarks(self) -> None:
        """The "?" sits on the band, 4 px in from the right edge."""
        self._marks.move(self.width() - rs.BAND_SIDE - MARKS_SIZE - 4,
                         rs.BAND_TOP + (rs.TAB_HEIGHT - MARKS_SIZE) // 2)
        self._marks.raise_()

    def _makeChip(self) -> StepTab:
        chip = StepTab(self)
        position = len(self._chips)
        # A disabled tab gets no ``clicked``; its event filter still sees the
        # press (Qt hands a widget's filters every event before the widget
        # refuses it), so a click on a tab that cannot be used yet says why.
        chip.setProperty('ribbonPosition', position)
        chip.installEventFilter(self)
        chip.clicked.connect(
            lambda _c=False, p=position: self._activate(p))
        chip.customContextMenuRequested.connect(
            lambda point, p=position, c=chip: self._menu(p, c, point))
        self._row.insertWidget(position, chip)
        return chip

    # -- acting ------------------------------------------------------------ #

    def _activate(self, position: int) -> bool:
        if position >= len(self._entries):
            return False
        entry = self._entries[position]
        if not self._usable(entry) or not entry.index.isValid():
            return False
        self._navigation.activateRow(QModelIndex(entry.index))
        return True

    #: Plan 44 F4. What opens a step that has no reason of its own yet: the
    #: mockup's ``why(n, 'na')``.
    NEEDS_MESH_FROM = 10

    def unavailableReason(self, position: int) -> str:
        """The sentence a click on an unusable tab shows, ``N. Name: why.``

        The outline's own tooltip is used when it says more than the label
        (a placeholder's "pick a mesher in 3. Mesh setup first", a locked
        row's meaning); a step with nothing to say gets the mockup's rule:
        Quality and Export need a mesh, every other step needs geometry.
        """
        if not 0 <= position < len(self._entries):
            return ''
        entry = self._entries[position]
        label = entry.label
        said = str(entry.tooltip or '').split('\n', 1)[0].strip()
        if said and said != label:
            if said.startswith(label + ' — '):
                said = label + ': ' + said[len(label) + 3:]
            elif not said.startswith(label):
                said = f'{label}: {said}'
        else:
            number = rs.split_label(label)[0].rstrip('.')
            try:
                needs_mesh = int(number) >= self.NEEDS_MESH_FROM
            except ValueError:
                needs_mesh = False
            # The steps are named as the ribbon shows them, number and all,
            # read from the outline rather than spelled here (DP-188).
            if needs_mesh:
                said = self.tr('{0}: Needs a mesh: run {1} first').format(
                    label, self._labelNumbered(self.NEEDS_MESH_FROM - 1,
                                               self.tr('Generate')))
            else:
                said = self.tr('{0}: Needs geometry: finish {1} first').format(
                    label, self._labelNumbered(1, self.tr('Geometry')))
        return said if said.endswith('.') else said + '.'

    def _labelNumbered(self, number: int, fallback: str) -> str:
        """The label of the step numbered ``number``, else ``fallback``."""
        for entry in self._entries:
            if rs.split_label(entry.label)[0].rstrip('.') == str(number):
                return entry.label
        return fallback

    def clickUnavailable(self, position: int) -> bool:
        """Say why the tab at ``position`` cannot be used; never open it."""
        if not 0 <= position < len(self._entries):
            return False
        if self._usable(self._entries[position]):
            return False
        text = self.unavailableReason(position)
        if text:
            self.unavailableClicked.emit(text)
        return bool(text)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt name
        if (event.type() == QEvent.Type.MouseButtonRelease
                and isinstance(watched, StepTab) and not watched.isEnabled()
                and event.button() == Qt.MouseButton.LeftButton
                and watched.rect().contains(event.position().toPoint())):
            position = watched.property('ribbonPosition')
            if position is not None:
                self.clickUnavailable(int(position))
            return True
        return super().eventFilter(watched, event)

    def activateLabel(self, label: str) -> bool:
        for position, entry in enumerate(self._entries):
            if entry.label == label:
                return self._activate(position)
        return False

    def _menu(self, position: int, chip, point) -> None:
        if position >= len(self._entries):
            return
        token = self._entries[position].token
        if token:
            self._navigation.rowMenuRequested.emit(
                token, chip.mapToGlobal(point))

    def step(self, delta: int) -> bool:
        """Ctrl+PageDown (+1) and Ctrl+PageUp (-1): the next usable step.

        Steps the tree has disabled are skipped, so the keys never open a
        page the workflow has not reached. ``False`` when there is none.
        """
        usable = [i for i, entry in enumerate(self._entries)
                  if self._usable(entry)]
        if not usable:
            return False
        here = next((i for i, entry in enumerate(self._entries)
                     if entry.current), None)
        if here is None:
            target = usable[0] if delta > 0 else usable[-1]
        else:
            beyond = ([i for i in usable if i > here] if delta > 0
                      else [i for i in usable if i < here])
            if not beyond:
                return False
            target = beyond[0] if delta > 0 else beyond[-1]
        return self._activate(target)

    def showLegend(self) -> None:
        legend = self._legend
        legend.adjustSize()
        point = self._marks.mapToGlobal(self._marks.rect().bottomRight())
        legend.move(point.x() - legend.width(), point.y())
        legend.show()

    # -- events ------------------------------------------------------------ #

    def resizeEvent(self, event):
        compact = event.size().width() <= COMPACT_AT_MOST
        super().resizeEvent(event)
        if compact != self._compact:
            self._compact = compact
            self.refresh()
        self._placeMarks()

    def paintEvent(self, event):
        """The band: the chrome ground and one 1 px line along its foot."""
        tokens = rs.resolve_tokens()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(tokens.value('background.chrome')))
        painter.fillRect(0, self.height() - 1, self.width(), 1,
                         QColor(tokens.value('border.default')))
        painter.end()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Question or event.text() == '?':
            self.showLegend()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            focusable = [c for c in self.chips() if c.isEnabled()]
            here = next((i for i, c in enumerate(focusable) if c.hasFocus()),
                        None)
            if focusable and here is not None:
                nxt = here + (1 if key == Qt.Key.Key_Right else -1)
                if 0 <= nxt < len(focusable):
                    focusable[nxt].setFocus(Qt.FocusReason.TabFocusReason)
                event.accept()
                return
        super().keyPressEvent(event)
