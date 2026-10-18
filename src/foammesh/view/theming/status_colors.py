"""Semantic status and colour-swatch helpers."""
import re

from PySide6.QtGui import QColor


def set_status(widget, status: str | None) -> None:
    if status not in (None, 'success', 'warning', 'error', 'info'):
        raise ValueError(f'unknown semantic status: {status}')
    widget.setProperty('foammeshStatus', status or '')
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def apply_color_swatch(widget, color: QColor | None) -> None:
    """Paint a small square in ``color``, or clear it when ``color`` is None.

    The colour is written straight into the widget's own stylesheet rather than
    into its palette. The palette route went through a ``palette(window)``
    reference in the global stylesheet, and a widget that already carries an
    application stylesheet does not re-resolve that on a palette change -- so
    every swatch in Display Control stayed the neutral window colour no matter
    what colour its actor was actually drawn in.
    """
    widget.setProperty('foammeshColorSwatch', color is not None)
    widget.setAutoFillBackground(color is not None)
    if color is None:
        widget.setStyleSheet('')
    else:
        widget.setStyleSheet(
            f'background-color: {color.name()};'
            ' border: 1px solid rgba(0, 0, 0, 60); border-radius: 3px;')
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()



def swatch_color(widget) -> QColor | None:
    """The colour :func:`apply_color_swatch` last painted on *widget*.

    Plan 33 W-B. The swatch is a stylesheet, so the only way to ask a row
    what colour it is showing was to parse that stylesheet at the call site.
    Every reader that wants the answer -- the geometry list repainting a row,
    a gate proving the row matches its actor -- asks here instead, so one
    spelling of the declaration cannot drift from another.
    """
    if not widget.property('foammeshColorSwatch'):
        return None
    for declaration in str(widget.styleSheet()).split(';'):
        name, _, value = declaration.partition(':')
        if name.strip() == 'background-color':
            color = QColor(value.strip())
            return color if color.isValid() else None
    return None


def hide_menu_indicator(button) -> None:
    """Draw no menu arrow on a popup tool button (DP-815, moved here by DP-852).

    The application stylesheet already hides the indicator on every
    ``QToolButton``; the button's own rule keeps that true before a theme is
    applied and under any stylesheet that does not, so the gradient it is
    painted with fills the whole face.
    """
    button.setStyleSheet(
        'QToolButton::menu-indicator { image: none; width: 0px; }')


# -- Stylesheets a screen used to write for itself ------------------------
# test_theme_tokens holds that only the theming package writes QSS. These are
# the rules VI13, VI15, UXC3, UXC5, UXD2 and UXE2 each wrote at their own call
# site, kept here byte for byte so the look did not change when they moved.


#: Plan 44 MF1. kind -> (ground token, ground fallback, border token, radius).
#: The fallbacks are the pre-MF0 palette() roles, so a build without the new
#: tokens looks as it did before; with them, every kind takes its overlay.* ground.
OVERLAY_KINDS = {
    'edge': ('overlay.edge', 'border.default', 6),
    'legend': ('overlay.legend', 'border.legend', 6),
    'card': ('overlay.card', 'border.default', 6),
    'chip': ('overlay.legend', 'border.legend', 6),
    'popover': ('overlay.popover', 'border.default', 5),
    'parts': ('overlay.parts', 'border.default', 6),
}

_overlay_panels: list = []        # weakrefs to (widget, kind, radius) styled so far
_theme_hooked = False


def _live_token(name):
    from .elevation import token
    return token(name, None)


def _overlay_sheet(name: str, kind: str, radius) -> str:
    ground_token, border_token, kind_radius = OVERLAY_KINDS[kind]
    ground = _live_token(ground_token) or 'palette(window)'
    border = _live_token(border_token) or 'palette(mid)'
    radius = kind_radius if radius is None else radius
    text = _live_token('foreground.legend')
    glyph = _live_token('foreground.secondary')
    tile = _live_token('selection.background')
    # The active tile's words: foreground.legend on the selection is 4.13:1
    # in dark (Plan 44 MF24 item 8); foreground.strong reads in both themes.
    tile_text = _live_token('foreground.strong') or text or glyph
    sheet = ('#%s { background: %s; border: 1px solid %s; border-radius: %dpx; }'
             % (name, ground, border, radius))
    if text:
        sheet += ' #%s QLabel { color: %s; background: transparent; }' % (name, text)
    if glyph and tile:
        # The tools in a floating panel are 26 px tiles; the active one is
        # filled with the selection colour (mockup M:139-146).
        sheet += (' #%s QToolButton { color: %s; background: transparent;'
                  ' border: none; border-radius: 4px; min-width: 26px;'
                  ' min-height: 26px; }'
                  ' #%s QToolButton:checked { background: %s; color: %s; }'
                  % (name, glyph, name, tile, tile_text))
    return sheet


def style_overlay_panel(widget, kind: str = 'edge', radius: int | None = None) -> None:
    """A panel over the viewport in one of the six overlay looks.

    ``kind`` is ``edge | legend | card | chip | popover | parts``; the ground
    comes from the ``overlay.*`` tokens, the rule from ``border.default`` or
    ``border.legend``, the radius is 6 (popover 5). The rule is keyed on the
    widget's own object name, so set that first. The sheet is rewritten when
    the theme changes.
    """
    if kind not in OVERLAY_KINDS:
        raise ValueError(f'unknown overlay kind: {kind}')
    import weakref

    widget.setStyleSheet(_overlay_sheet(widget.objectName(), kind, radius))
    _overlay_panels[:] = [r for r in _overlay_panels if r[0]() is not None
                          and r[0]() is not widget]
    _overlay_panels.append((weakref.ref(widget), kind, radius))
    _hook_theme_changes()


def restyle_overlay_panels() -> int:
    """Re-read the tokens for every overlay panel styled so far."""
    live = []
    for ref, kind, radius in _overlay_panels:
        widget = ref()
        if widget is None:
            continue
        try:
            widget.setStyleSheet(_overlay_sheet(widget.objectName(), kind, radius))
        except RuntimeError:                                # C++ side already gone
            continue
        live.append((ref, kind, radius))
    _overlay_panels[:] = live
    return len(live)


def _hook_theme_changes() -> None:
    global _theme_hooked
    if _theme_hooked:
        return
    try:
        from foammesh.app import app
        manager = getattr(app, 'themeManager', None)
        if manager is not None:
            manager.themeChanged.connect(lambda *_: restyle_overlay_panels())
            _theme_hooked = True
    except Exception:  # noqa: BLE001 - tools and tests without the app
        pass


def style_floating_panel(widget, radius: int | None = None) -> None:
    """A panel over the viewport. Plan 44 MF1: an alias of
    ``style_overlay_panel(widget, 'edge')``, so every existing caller (VI15's
    camera presets, hover and measure readouts, UXE2's tool rail, the tool
    edges, the notice strip) takes the new look with no edit. ``radius``
    overrides the kind's 6 px."""
    style_overlay_panel(widget, 'edge', radius)


def style_tool_edge_nub(widget) -> None:
    """Plan 43 LY6 / Plan 44 MF1. The nub a folded viewport tool edge leaves
    at its border: an accent tab, radius 3, no glyph colour."""
    accent = _live_token('accent.default') or 'palette(highlight)'
    hover = _live_token('accent.fill') or 'palette(highlight)'
    widget.setStyleSheet(
        '#%s { background: %s; border: none; border-radius: 3px;'
        ' font-size: 8px; padding: 0px; }'
        '#%s:hover { background: %s; }'
        % (widget.objectName(), accent, widget.objectName(), hover))


def style_progress_strip(widget) -> None:
    """VI15's viewport progress strip: window colour over a mid rule."""
    widget.setStyleSheet(
        '#%s { background: palette(window);'
        ' border-bottom: 1px solid palette(mid); }' % widget.objectName())


def style_resource_overlay(widget) -> None:
    """VI13's CPU and RAM panel: light text on a translucent dark plate."""
    widget.setStyleSheet(
        '#%s { background: rgba(30, 30, 30, 170);'
        ' border-radius: 4px; }'
        '#%s QLabel { color: #f0f0f0; }'
        '#%s QToolButton { color: #f0f0f0; border: none; }'
        % ((widget.objectName(),) * 3))


def set_text_colour(label, colour: str) -> None:
    """Draw ``label``'s text in ``colour`` (VI13's amber when RAM is short)."""
    label.setStyleSheet('color: {0};'.format(colour))


def set_bold_text(label) -> None:
    """Draw ``label``'s text bold (UXC5's notification title and warnings)."""
    label.setStyleSheet('font-weight: bold;')


def style_palette_flash(frame) -> None:
    """UXD2's amber flash over the control the command palette went to."""
    frame.setStyleSheet(
        'QFrame#%s { background-color: rgba(255, 190, 0, 90); '
        'border: 2px solid rgb(255, 160, 0); border-radius: 3px; }'
        % frame.objectName())


def style_not_measured_note(label, grey: str) -> None:
    """The legend's "not measured" line, led by the grey the cells are drawn in."""
    label.setStyleSheet('border-left: 10px solid {0}; padding-left: 4px;'.format(grey))


def style_snapshot_flash(veil) -> None:
    """The white veil that flashes over the viewport when a snapshot is taken."""
    veil.setStyleSheet('background: rgba(255, 255, 255, 110);')


def apply_sheet(widget, sheet: str) -> None:
    """Give ``widget`` a style sheet the theming layer composed (the ribbon's)."""
    widget.setStyleSheet(sheet)


def style_scrolling_menu(menu) -> None:
    """A menu that scrolls inside itself when the window is too short for it."""
    menu.setStyleSheet('QMenu { menu-scrollable: 1; }')


def style_tight_headings(header) -> None:
    """A table heading that gives up 3 px of padding each side to fit its column."""
    header.setStyleSheet('QHeaderView::section { padding: 2px 5px; }')


def style_tightest_headings(header) -> None:
    """D3 (DP-2664). The last step before a heading stops being squeezed: 2 px of
    padding each side, when even the tight headings ask more than the table
    has."""
    header.setStyleSheet('QHeaderView::section { padding: 2px 2px; }')


#: Qt-ADS's own rule for the splitter handle between dock areas.
_ADS_HANDLE = re.compile(
    r'ads--CDockContainerWidget\s+ads--CDockSplitter::handle\s*\{[^}]*\}')

#: The dock manager's object name, which scopes its rules in the
#: application sheet (``dock_layout`` names it).
DOCK_MANAGER = 'dockManager'


def _chrome(tokens=None) -> str:
    from .elevation import token
    if tokens is not None:
        value = tokens.values.get('background.chrome') or ''
    else:
        value = token('background.chrome', None) or ''
    return value[:7] or 'palette(window)'


def _dock_rules(base: str, tokens=None) -> str:
    return (
        base
        + ' ads--CDockWidget { border: none; background: palette(window); }'
        ' ads--CDockAreaWidget { border: none; }'
        # MF27: Qt-ADS paints the handle palette(dark), which the theme never
        # sets: a 5 px platform-grey stripe beside a pinned sheet. Its rule
        # names the container too, so this one must, to outrank it. The
        # handle at rest is the chrome ground (Plan 44 sec. 2.2).
        ' ads--CDockContainerWidget ads--CDockSplitter::handle'
        ' { background: %s; }' % _chrome(tokens)
        + ' ads--CDockWidgetTab { background: palette(window); }'
        ' ads--CDockWidgetTab[activeTab="true"] { background: palette(window); }')


def _static_dock_rules(base: str) -> str | None:
    """The dock manager's own sheet with nothing in it that a theme changes:
    Qt-ADS's sheet without its handle rule, and the rules above without
    theirs. ``None`` when Qt-ADS's handle rule is not where it is expected."""
    stripped, count = _ADS_HANDLE.subn('', base)
    if count != 1:
        return None
    return (
        stripped
        + ' ads--CDockWidget { border: none; background: palette(window); }'
        ' ads--CDockAreaWidget { border: none; }'
        ' ads--CDockWidgetTab { background: palette(window); }'
        ' ads--CDockWidgetTab[activeTab="true"] { background: palette(window); }')


def dock_application_rules(tokens) -> str:
    """Plan 45 W1. The dock handle's theme colour, as an application rule.

    It used to sit in the dock manager's own sheet, so every theme switch had
    to set that sheet again -- a re-polish of the 5,500 widgets under the
    manager. Scoped by the manager's name it outranks every application rule
    that reaches a splitter handle, as the manager's own sheet did. The
    manager is itself the container of the root splitter, so the rule names
    it both as that container and as the ancestor of any container inside.
    """
    return ('\nads--CDockContainerWidget#{0} ads--CDockSplitter::handle,'
            ' #{0} ads--CDockContainerWidget ads--CDockSplitter::handle'
            ' {{ background: {1}; }}\n'.format(DOCK_MANAGER, _chrome(tokens)))


def strip_dock_rules(manager) -> None:
    """UXC3: no 1 px rule above a dock's content, on top of Qt-ADS's own QSS.

    That pixel is the difference between the Mesh setup page fitting the
    settings column of a 700 px window and scrolling by one row.

    Plan 45 W1. The one rule that carries a theme colour (the handle) lives
    in the application sheet (:func:`dock_application_rules`), so the
    manager's own sheet never changes on a theme switch. A manager not named
    ``dockManager`` keeps the colour in its own sheet, re-read on a switch.
    """
    import weakref
    base = manager.styleSheet()
    static = _static_dock_rules(base)
    if static is not None and manager.objectName() == DOCK_MANAGER:
        manager.setStyleSheet(static)
        return
    manager.setStyleSheet(_dock_rules(base))
    ref = weakref.ref(manager)
    try:
        from foammesh.app import app
        themes = getattr(app, 'themeManager', None)
    except Exception:  # noqa: BLE001 - tools and tests without the app
        themes = None
    if themes is None:
        return

    def restyle(*_args) -> None:
        live = ref()
        if live is None:
            return
        try:
            live.setStyleSheet(_dock_rules(base))
        except RuntimeError:                    # C++ side already gone
            pass
    themes.themeChanged.connect(restyle)


# -- the viewport tool edges (Plan 43 LY6/LY7, Plan 44 U2) ------------------
# tool_edges.py wrote these two sheets for itself (SG2); they moved here byte
# for byte. The tile's own sheet has to stay on the button: the floating panel
# it sits in carries a ``#panel QToolButton`` rule, and only a sheet on the
# button itself is nearer than that.

_tool_tiles: list = []            # weakrefs to the tiles styled so far
_tiles_hooked = False


def _tool_tile_sheet(name: str) -> str:
    """Plan 44 U2: a 26 px tile; hover on ``background.raised``, checked on
    ``accent.soft`` with an ``accent.default`` ring. The glyph itself is the
    token icon of ``icons.apply_tinted_icon``."""
    from .icons import theme_colour
    raised = theme_colour('background.raised')
    soft = theme_colour('accent.soft')
    ring = theme_colour('accent.default')
    if name.startswith('railTool_') or name == 'toolEdgePin_rail':
        # CHL (DP-2686, V4): a pressed rail tool is the selection ground, the
        # same as every other pressed tool of the window, with the strong ink.
        soft = theme_colour('selection.background')
    return (
        '#%(n)s { background: transparent; border: 1px solid transparent;'
        ' border-radius: 4px; }'
        '#%(n)s:hover { background: %(raised)s; border: 1px solid transparent; }'
        '#%(n)s:checked { background: %(soft)s; border: 1px solid %(ring)s; }'
        '#%(n)s:checked:hover { background: %(soft)s;'
        ' border: 1px solid %(ring)s; }'
        '#%(n)s:disabled { background: transparent; border: 1px solid'
        ' transparent; }'
        % {'n': name, 'raised': raised, 'soft': soft, 'ring': ring})


def style_tool_tile(button) -> None:
    """Style a tool button of a viewport edge as a tile (hover, checked) and
    keep it in step with the theme. Needs the object name set first."""
    import weakref
    global _tiles_hooked
    button.setStyleSheet(_tool_tile_sheet(button.objectName()))
    _tool_tiles[:] = [r for r in _tool_tiles
                      if r() is not None and r() is not button]
    _tool_tiles.append(weakref.ref(button))
    if not _tiles_hooked:
        try:
            from foammesh.app import app
            manager = getattr(app, 'themeManager', None)
            if manager is not None:
                manager.themeChanged.connect(lambda *_a: restyle_tool_tiles())
                _tiles_hooked = True
        except Exception:  # noqa: BLE001 - tools and tests without the app
            pass


def restyle_tool_tiles() -> int:
    """Re-read the tokens for every tile styled so far."""
    live = []
    for ref in _tool_tiles:
        button = ref()
        if button is None:
            continue
        try:
            button.setStyleSheet(_tool_tile_sheet(button.objectName()))
        except RuntimeError:                                # C++ side already gone
            continue
        live.append(ref)
    _tool_tiles[:] = live
    return len(live)


def style_bare_hit_area(widget) -> None:
    """A widget that paints itself inside a larger hit area (the folded
    edge's nub): the application sheet must not fill the area around it."""
    widget.setStyleSheet('#%s { background: transparent; border: none; }'
                         % widget.objectName())
