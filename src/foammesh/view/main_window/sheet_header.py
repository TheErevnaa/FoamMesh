#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 44 MF5: the one header of the settings sheet.

Every step used to head its page with a heading of its own: the engine
branch's ``engineBranchHeading`` (a task name), the Preparation page's
``preparationTitle`` and, in the dock, an Qt-ADS title bar. The sheet now has
one header for all of them (``final-layout.html`` ``.page-h``): a 10 px chip,
``N. Title`` in 16 px 600, ``Mesh setup . k of 5`` on the five setup steps, a
spacer, and the sheet's own controls -- pin, side swap, minimise and the
``...`` menu of the step on screen.

The header owns no behaviour. Its three buttons only *ask*: the sheet carries
them out with the same methods the grip's context menu calls, so there is one
path to each action and no new action ids (Plan 44 Q7a).

The header lives in region B's own column, above the page, so it travels with
the page between the floating frame and the Qt-ADS dock: a pinned sheet has
the same header and no dock title bar.
"""
from __future__ import annotations

import re
from html import escape
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QWidget)
from foammesh.view.theming.metrics import FORM_MARGIN, GAP, MARGIN_TIGHT

#: The five steps of "Mesh setup" are the outline's steps 4 to 8.
SETUP_FIRST = 4
SETUP_LAST = 8
SETUP_COUNT = SETUP_LAST - SETUP_FIRST + 1
#: Plan 45 (user, 2026-10-17): a little smaller than the 16 px tool glyphs.
PIN_SIZE = 14

_ICONS = Path(__file__).parents[3] / 'resources' / 'icons'
#: ``"4. Sizing"``, optionally behind a status glyph such as ``"○ "``.
_STEP = re.compile(r'^\W*?(\d+)\.\s*(.*)$')


def split_step(name: str) -> tuple[int | None, str]:
    """``'○ 4. Sizing'`` -> ``(4, 'Sizing')``; a name with no number -> ``(None, name)``."""
    match = _STEP.match(name or '')
    if match is None:
        return None, (name or '').strip()
    return int(match.group(1)), match.group(2).strip()


def header_text(name: str) -> tuple[str, str]:
    """The title and the subtitle of the header for the step called ``name``."""
    number, title = split_step(name)
    if number is None:
        return title, ''
    text = f'{number}. {title}' if title else f'{number}.'
    if SETUP_FIRST <= number <= SETUP_LAST:
        return text, f'Mesh setup · {number - SETUP_FIRST + 1} of {SETUP_COUNT}'
    return text, ''


def _tokens():
    from foammesh.app import app
    manager = getattr(app, 'themeManager', None)
    return manager.tokens if manager is not None else None


def pin_icon(pinned: bool, size: int = PIN_SIZE) -> QIcon:
    """The push-pin glyph: tilted with a red outlined head when floating,
    upright with a red filled head when pinned. Every colour is a token of the
    theme in use; ``pin.head`` is red in the light and the dark theme alike
    (Plan 45, user 2026-10-17: the pin options read red in both themes).

    CHL (DP-2687, V3): the glyph is also drawn for the checked state of a tool
    button (the rail's pin sits on the ``selection.background`` tile when it
    is pressed): there the needle and the outline are ``foreground.strong``,
    since the quiet grey read 4.3:1 on that tile."""
    tokens = _tokens()

    def token(name: str, fallback: str) -> str:
        return tokens.value(name) if tokens is not None else fallback

    from PySide6.QtGui import QGuiApplication
    screen = QGuiApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    text = (_ICONS / ('pin-on.svg' if pinned else 'pin.svg')).read_text(encoding='utf-8')
    strong = token('foreground.strong', '#e6e8eb')
    head = token('pin.head', '#c9534e')
    # The resting glyph, then the checked one (strong needle, the head stays red).
    variants = [(token('foreground.secondary', '#9aa2ae'), head),
                (strong, head)]
    drawn = []
    for needle, outline in variants:
        colours = {
            '@needle@': needle,
            '@head@': outline,
            '@headStroke@': token('pin.head.stroke', '#8e3330'),
            '@shadow@': token('shadow.low', '#000000'),
            '@shine@': token('accent.text', '#ffffff'),
        }
        body = text
        for key, value in colours.items():
            body = body.replace(key, value[:7])
        renderer = QSvgRenderer(QByteArray(body.encode('utf-8')))
        pixmap = QPixmap(round(size * ratio), round(size * ratio))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(ratio)
        drawn.append(pixmap)
    return _with_checked(QIcon(drawn[0]), *drawn)


def _with_checked(icon: QIcon, off, on) -> QIcon:
    """``icon`` showing ``off`` unchecked and ``on`` checked, in every mode."""
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected,
                 QIcon.Mode.Disabled):
        icon.addPixmap(off, mode, QIcon.State.Off)
        icon.addPixmap(on, mode, QIcon.State.On)
    return icon


class _Elided(QLabel):
    """A one-line label that elides on the right instead of widening its column."""

    def __init__(self, parent=None, grow: bool = False) -> None:
        super().__init__(parent)
        self._full = ''
        self._grow = grow
        self.setSizePolicy(QSizePolicy.Policy.Preferred if grow else QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def setFullText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def fullText(self) -> str:
        return self._full

    def sizeHint(self):
        hint = super().sizeHint()
        if self._grow:
            hint.setWidth(QFontMetrics(self.font()).horizontalAdvance(self._full) + 2)
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(self.width(), 0)))


def subtitle_forms(subtitle: str) -> tuple[str, ...]:
    """The subtitle, longest first: ``'Mesh setup · 2 of 5'``, ``'2 of 5'``,
    ``'2/5'``. Each says where the step is in the setup; the header draws the
    longest one that fits whole."""
    forms = [subtitle]
    if '·' in subtitle:
        place = subtitle.split('·')[-1].strip()
        forms.append(place)
        match = re.fullmatch(r'(\d+) of (\d+)', place)
        if match is not None:
            forms.append(f'{match.group(1)}/{match.group(2)}')
    return tuple(forms)


class _HeadLine(_Elided):
    """``N. Title`` with the subtitle after it, small and grey, as one line.

    Plan 44 FX2 (S3). ``final-layout.html`` draws the subtitle *inside* the
    title's element -- ``<b>N. Title <small>Mesh setup · k of 5</small></b>``,
    the ``b`` the one that elides (``.page-h b{flex:1;...;text-overflow:
    ellipsis}``, ``.page-h small{font-weight:400;color:#8b919a;font-size:
    12px}``). The header drew them as two labels, the title held at 96 px and
    the subtitle given what was left: MEASURED in the MF27 live pass, "Mesh
    s…" at 1920 (360 px sheet) and nothing at all at 1280 (300 px). Here
    they are the one element, and the title is never what gives way first:
    the subtitle is said whole when it fits, else as ``k of 5``, else as
    ``k/5``; only a title too long for the line with even that after it goes
    without (and elides, as the mockup's line does). The whole of both is the
    tooltip, and the subtitle is the accessible description, so nothing is
    out of reach.

    The subtitle's font and colour are read from the ``sheetSubtitle`` label
    (``QLabel#sheetSubtitle`` in ``sheet.qss.tmpl``), so the theme owns them.
    """

    #: Between the title and the subtitle: the mockup's ``&nbsp;`` and space.
    SEPARATION = 7

    def __init__(self, style_source: QLabel, parent=None) -> None:
        super().__init__(parent)
        self._style_source = style_source
        self._sub = ''
        self.setTextFormat(Qt.TextFormat.PlainText)

    def setParts(self, title: str, subtitle: str) -> None:
        self._sub = subtitle
        self._full = title
        whole = f'{title} · {subtitle}' if subtitle else title
        self.setToolTip(whole)
        self.setAccessibleDescription(subtitle)
        self._elide()

    def subtitleShown(self) -> str:
        """The subtitle as the line shows it now ('' = not shown)."""
        return getattr(self, '_shown_sub', '')

    def _sub_font(self):
        self._style_source.ensurePolished()
        return self._style_source.font()

    def _elide(self) -> None:
        width = max(self.width(), 0)
        title, sub = self._full, getattr(self, '_sub', '')
        metrics = QFontMetrics(self.font())
        self._shown_sub = ''
        if not sub:
            self.setTextFormat(Qt.TextFormat.PlainText)
            self.setText(metrics.elidedText(
                title, Qt.TextElideMode.ElideRight, width))
            return
        small = QFontMetrics(self._sub_font())
        room = width - metrics.horizontalAdvance(title) - self.SEPARATION
        for form in subtitle_forms(sub):
            if small.horizontalAdvance(form) <= room:
                self._shown_sub = form
                break
        if not self._shown_sub:
            self.setTextFormat(Qt.TextFormat.PlainText)
            self.setText(metrics.elidedText(
                title, Qt.TextElideMode.ElideRight, width))
            return
        font = self._sub_font()
        colour = self._style_source.palette().color(
            self._style_source.foregroundRole()).name()
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setText(
            f'{escape(title)}<span style="font-size:{font.pixelSize()}px;'
            f'font-weight:{_css_weight(font)};color:{colour}">'
            f'&nbsp; {escape(self._shown_sub)}</span>')


def _css_weight(font) -> int:
    weight = font.weight()
    return int(getattr(weight, 'value', weight))


class SheetHeader(QWidget):
    """Chip, ``N. Title``, subtitle, then pin / side / minimise / ``...``."""

    #: Asks for the pinned state (``True`` = dock the sheet).
    pinRequested = Signal(bool)
    sideRequested = Signal()
    minimiseRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('settingsSheetHeader')
        self.setAccessibleName(self.tr('Settings sheet header'))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, MARGIN_TIGHT)
        row.setSpacing(GAP)

        self._chip = QFrame(self)
        self._chip.setObjectName('sheetChip')
        self._chip.setFixedSize(10, 10)
        row.addWidget(self._chip, 0, Qt.AlignmentFlag.AlignVCenter)

        # Plan 44 FX2 (S3). The subtitle label holds the subtitle's words and
        # its themed font and colour; the title line draws it (`_HeadLine`).
        self._subtitle = _Elided(self, grow=True)
        self._subtitle.setObjectName('sheetSubtitle')
        self._subtitle.setVisible(False)

        self._title = _HeadLine(self._subtitle, self)
        self._title.setObjectName('sheetTitle')
        self._title.setAccessibleName(self.tr('Step on screen'))
        row.addWidget(self._title, 1)

        self._pin = self._button(
            'sheetPinButton', '', self.tr('Pin'), self.tr('Pin the settings sheet'))
        self._pin.setCheckable(True)
        self._pin.setIcon(pin_icon(False))
        self._pin.setIconSize(QSize(PIN_SIZE, PIN_SIZE))
        self._pin.clicked.connect(
            lambda _checked=False: self.pinRequested.emit(not self._pinned))
        self._side = self._button(
            'sheetSideButton', '⇄', self.tr('Move the sheet to the other side'),
            self.tr('Move the sheet to the other side'))
        self._side.clicked.connect(lambda _c=False: self.sideRequested.emit())
        self._minimise = self._button(
            'sheetMinimiseButton', '—', self.tr('Minimise the settings sheet'),
            self.tr('Minimise the settings sheet (Ctrl+\\)'))
        self._minimise.clicked.connect(lambda _c=False: self.minimiseRequested.emit())
        for button in (self._pin, self._side, self._minimise):
            row.addWidget(button, 0)
        self._more_slot = row
        self._more = None
        self._pinned = False

    # -- building ----------------------------------------------------------- #

    def _button(self, name: str, text: str, accessible: str, tip: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(name)
        button.setProperty('sheetButton', True)
        button.setText(text)
        button.setAccessibleName(accessible)
        button.setToolTip(tip)
        button.setAutoRaise(False)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonIconOnly if not text
            else Qt.ToolButtonStyle.ToolButtonTextOnly)
        return button

    # -- reading ------------------------------------------------------------ #

    @property
    def pin_button(self) -> QToolButton:
        return self._pin

    @property
    def side_button(self) -> QToolButton:
        return self._side

    @property
    def minimise_button(self) -> QToolButton:
        return self._minimise

    @property
    def more_button(self):
        return self._more

    def title(self) -> str:
        return self._title.fullText()

    def subtitle(self) -> str:
        return self._subtitle.fullText()

    def subtitle_shown(self) -> str:
        """The subtitle as the header draws it now ('' = not drawn)."""
        return self._title.subtitleShown()

    # -- state -------------------------------------------------------------- #

    def set_step(self, name: str) -> None:
        title, sub = header_text(name)
        self._subtitle.setFullText(sub)
        self._title.setParts(title or self.tr('Settings'), sub)

    def set_pinned(self, pinned: bool) -> None:
        self._pinned = bool(pinned)
        self._pin.blockSignals(True)
        self._pin.setChecked(self._pinned)
        self._pin.blockSignals(False)
        self._pin.setIcon(pin_icon(self._pinned))
        self._pin.setToolTip(
            self.tr('Unpin the settings sheet') if self._pinned
            else self.tr('Pin the settings sheet'))
        self._pin.setProperty('pinned', self._pinned)
        self._restyle(self._pin)

    def set_side(self, side: str) -> None:
        self._side.setProperty('side', side)

    def refresh_icons(self) -> None:
        """The theme changed: the pin is painted with the new tokens."""
        self._pin.setIcon(pin_icon(self._pinned))
        # The subtitle's colour is the theme's too.
        self._title._elide()

    def adopt_more(self, button: QToolButton | None) -> None:
        """Take the step's ``...`` button (the engine branch builds it) into the
        header's right end. ``None`` hides what was taken: the page on screen
        has no ``...`` menu."""
        if button is not None and button is not self._more:
            self._more = button
            button.setParent(self)
            button.setProperty('sheetButton', True)
            self._more_slot.addWidget(button, 0)
        if self._more is not None:
            self._more.setVisible(button is not None)

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
