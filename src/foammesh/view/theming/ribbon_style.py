"""Plan 44 MF4. How a step-ribbon tab is coloured and shaped.

The ribbon draws its tabs itself (a ``border-bottom`` cannot be inset or
rounded), so the look lives here, away from the widget: one state name per
tab, the five ``step.<state>.*`` tokens that name resolves to, and the little
geometry the painter needs.

A tab is one of ``STATES``. ``chapter`` is the Mesh setup tab; ``plain`` is
what the finished, not-done, attention, stale and locked tabs fall back to when
"colour by state" is off. Every colour is read from the running theme at paint
time (``live_tokens``), or from the shipped dark theme when no theme runs
(tools, headless tests), so a theme switch repaints correctly.

Plan 44 section 9 U4. Every tab has a 1 px outline in its state colour, radius
``RADIUS_CONTROL``, a 2 px gap between tabs, no bevel and no gradient; the
current tab's outline is ``accent.default``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainterPath

from .metrics import RADIUS_CONTROL

STATES = ('done', 'current', 'todo', 'attention', 'stale', 'locked', 'na',
          'plain', 'chapter')

#: Between two tabs (Plan 44 section 9 U4; the mockup's 3 px became 2).
TAB_GAP = 2
#: Side padding of the band, and the band's top padding.
BAND_SIDE = 8
BAND_TOP = 4
#: A tab is this tall; the band's own 1 px line is its last row.
TAB_HEIGHT = 32
#: Numbers-only tabs are this wide (1280 px and below).
NUMBERS_TAB_WIDTH = 58
#: The state bar: height, extra height of a current tab that needs attention,
#: and the distance from each side of the tab to the bar (border included).
BAR_HEIGHT = 3
BAR_HEIGHT_ATTENTION = 4
BAR_INSET = 7
BAR_RADIUS = 2
#: The chapter tab is this much as wide as a step tab.
CHAPTER_STRETCH = 95
STEP_STRETCH = 100

#: The padlock of a finished and locked step (replaces the old ``▪``).
LOCK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">'
    '<rect x="2" y="5.2" width="8" height="5.6" rx="1.2" fill="{ink}"/>'
    '<path d="M4 5.4V3.9a2 2 0 0 1 4 0v1.5" fill="none" stroke="{ink}" '
    'stroke-width="1.3"/></svg>')
LOCK_SIZE = 11.0     # R10: the padlock paints 7 px wide by 9 tall (ink)
LOCK_OPACITY = 0.8
NUMBER_OPACITY = 0.7

_NUMBER = re.compile(r'^\s*(\d+(?:\.\d+)*)\.?\s+(.*\S)\s*$')


def split_label(label: str) -> tuple[str, str]:
    """``'3. Mesh setup'`` -> ``('3', 'Mesh setup')``; no number -> ``('', label)``."""
    match = _NUMBER.match(label or '')
    if match:
        return match.group(1), match.group(2)
    return '', (label or '').strip()


@dataclass(frozen=True)
class TabColours:
    fill: str
    text: str
    border: str
    bar: str
    hover: str


@dataclass
class TabModel:
    """What one tab shows. The ribbon fills it; the tab paints it."""

    state: str = 'todo'
    glyph: str = ''
    number: str = ''
    label: str = ''
    locked: bool = False
    #: R10 (DP-2653). A finished tab keeps the lock's place whether or not it is
    #: locked, so locking draws the mark and nothing else moves.
    lock_slot: bool = False
    current: bool = False
    #: A current tab that needs attention keeps the current fill and takes a
    #: 4 px bar in the attention colour.
    attention: bool = False
    chapter: bool = False
    #: The chapter's five-segment bar: the states of the steps under it.
    segments: tuple = field(default_factory=tuple)


_default_tokens = None


def default_tokens():
    """The shipped dark theme, for a process with no running theme."""
    global _default_tokens
    if _default_tokens is None:
        from .tokens import load_theme_tokens
        path = Path(__file__).parents[3] / 'resources' / 'theme' / 'dark.json'
        _default_tokens = load_theme_tokens(path)
    return _default_tokens


def resolve_tokens(tokens=None):
    """``tokens`` if given, else the running theme's, else the dark default."""
    if tokens is not None:
        return tokens
    from .patch_palette import live_tokens
    return live_tokens() or default_tokens()


def _value(tokens, name: str, fallback: str | None = None) -> str:
    try:
        return tokens.value(name)
    except KeyError:
        if fallback is None:
            raise
        return fallback


def tab_colours(state: str, tokens=None) -> TabColours:
    """The five colours of ``state``, read from the theme tokens."""
    tokens = resolve_tokens(tokens)
    if state not in STATES:
        raise ValueError(f'unknown ribbon state {state!r}')
    fill = tokens.value(f'step.{state}.fill')
    if state == 'chapter':
        # The chapter tab has no bar or hover token of its own: its bar is the
        # row of its steps' bars and its hover is the mockup's brightness 1.25.
        hover = QColor(fill).lighter(125).name()
        bar = tokens.value('step.chapter.border')
    else:
        hover = tokens.value(f'step.{state}.fill.hover')
        bar = tokens.value(f'step.{state}.bar')
    return TabColours(fill=fill, text=tokens.value(f'step.{state}.text'),
                      border=tokens.value(f'step.{state}.border'),
                      bar=bar, hover=hover)


def border_colour(model: TabModel, tokens=None) -> str:
    """The outline: the state's, except the current tab's, which is the accent."""
    tokens = resolve_tokens(tokens)
    if model.current:
        return tokens.value('accent.default')
    return tab_colours(model.state, tokens).border


def bar_colour(model: TabModel, tokens=None) -> str:
    """The bar: the state's, or the attention bar of a current tab that needs it."""
    tokens = resolve_tokens(tokens)
    if model.attention:
        return tab_colours('attention', tokens).bar
    return tab_colours(model.state, tokens).bar


def bar_height(model: TabModel) -> int:
    return BAR_HEIGHT_ATTENTION if model.attention else BAR_HEIGHT


def tab_state(*, mark: str, row_state: str = '', current: bool = False,
              chapter: bool = False, colour: bool = True) -> tuple[str, bool]:
    """``(state, attention)`` of one tab.

    ``mark`` is a ``RibbonMark`` value. A current tab keeps the current colours
    even when it needs attention; the attention is then carried by its bar
    (the mockup's ``cur attn-cur``). With colour-by-state off every tab but the
    current one, the chapter and the unusable ones falls back to ``plain``.
    """
    if chapter:
        return ('chapter' if colour else 'plain'), False
    attention = mark == 'attention'
    if current:
        return 'current', attention
    if attention:
        state = 'attention'
    elif mark == 'locked':
        state = 'locked' if row_state == 'skipped' else 'na'
    elif row_state == 'stale':
        state = 'stale'
    elif mark == 'done':
        state = 'done'
    else:
        state = 'todo'
    if not colour and state not in ('na',):
        state = 'plain'
    return state, False


def top_rounded(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    """A rectangle with its two top corners rounded; the bottom is square."""
    r = max(0.0, min(r, w / 2.0, h))
    path = QPainterPath()
    path.moveTo(x, y + h)
    path.lineTo(x, y + r)
    if r:
        path.arcTo(QRectF(x, y, 2 * r, 2 * r), 180, -90)
    path.lineTo(x + w - r, y)
    if r:
        path.arcTo(QRectF(x + w - 2 * r, y, 2 * r, 2 * r), 90, -90)
    path.lineTo(x + w, y + h)
    path.closeSubpath()
    return path


def outline_path(w: float, h: float, r: float = RADIUS_CONTROL) -> QPainterPath:
    """The open 1 px outline: left side, top, right side, no bottom edge."""
    x0, y0, x1 = 0.5, 0.5, w - 0.5
    path = QPainterPath()
    path.moveTo(x0, h)
    path.lineTo(x0, y0 + r)
    path.arcTo(QRectF(x0, y0, 2 * r, 2 * r), 180, -90)
    path.lineTo(x1 - r, y0)
    path.arcTo(QRectF(x1 - 2 * r, y0, 2 * r, 2 * r), 90, -90)
    path.lineTo(x1, h)
    return path
