"""Plan 43 LY8. The grouped View menu (plan section 6) and its state.

The window used to put some twenty actions in one flat View list, each added
by whichever installer made it. ``install_view_menu`` runs after all of them
and lays the same actions out in the groups of section 6, so that every
existing entry keeps a home (the register's *View menu* rows) and no action
is rebuilt or lost.

Entries for things another package builds are real, checkable actions over
:class:`ViewMenuState`: the choice is kept per user in the settings file, and
the package that draws the thing reads the state (``state.get(key)``) and
subscribes to it (``state.subscribe``).  Bound now (and tested to drive the
thing): the Mesh quality tab (layout, cells beyond the limit, inspect), the
worst values and caption (LY10 binds them with MeshQualityViews.bind),
the label font and size and the legend switch (LY15) and Snapshot.  Waiting
for their owners, and filed open in the fault register (DP-1769): the
settings sheet (LY3), the tool edges (LY6), the Parts panel (LY7), the Step
ribbon colour and locks (LY2, LY9) and the colour scale (LY11, LY12).
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QKeySequence,
                           QLinearGradient, QPainter, QPixmap)
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout,
                               QLabel, QMenu, QSizePolicy, QSlider,
                               QToolButton, QVBoxLayout,
                               QWidget, QWidgetAction)

from foammesh.view.main_window.console_band import exclusive_group
from foammesh.view.main_window.shortcut_registry import adopt as adoptShortcut
from foammesh.view.theming.menus import (
    KIND_BAR, KIND_FLYOUT, ThemedMenu, heading as menu_heading,
    MenuScrollHost, fit_to_room, is_heading, make_scrollable, set_summary,
    strip_mnemonic as menus_strip_mnemonic, themed)
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_SECTION, GAP_TIGHT, MARGIN_TIGHT, MARGIN_WIDE,
    CompactSpinBox)

# --------------------------------------------------------------------------- #
# The state
# --------------------------------------------------------------------------- #

SHEET_MODES = ('floating', 'pinned', 'minimised')
SHEET_SIDES = ('left', 'right')
SHEET_WIDTHS = (300, 360, 440)
SHEET_WIDTH_RANGE = (280, 560)
REVEALS = ('hover', 'click')
RIBBON_LABELS = ('full', 'numbers')
MODEL_BY = ('patches', 'quality', 'deviation')
#: The nine scales of section 4, standard first.
SCALES = (('rainbow', 'Rainbow (Jet)'), ('turbo', 'Turbo'),
          ('viridis', 'Viridis'), ('inferno', 'Inferno'),
          ('plasma', 'Plasma'), ('blackbody', 'Blackbody'),
          ('white_red', 'White to red'), ('cool_warm', 'Cool to warm'),
          ('grayscale', 'Greyscale'))
SCALE_IDS = tuple(key for key, _ in SCALES)
BAND_MODES = ('smooth', 'banded')
BAND_RANGE = (2, 64)
BAND_STEP = (5, 50)
RANGES = ('whole', 'limit')
#: The four label fonts of section 5, each shown in its own face.
FONTS = (('pretendard', 'Pretendard', 'Pretendard'),
         ('arial', 'Arial', 'Arial'),
         ('times', 'Times New Roman', 'Times New Roman'),
         ('courier', 'Courier New', 'Courier New'))
FONT_IDS = tuple(key for key, _, _ in FONTS)
SIZES = (('small', 'Small', 0.88), ('normal', 'Normal', 1.0),
         ('large', 'Large', 1.25), ('xlarge', 'Extra large', 1.5))
SIZE_IDS = tuple(key for key, _, _ in SIZES)
QUALITY_LAYOUTS = ('A', 'B', 'C')

BOOL = 'bool'

#: ``key -> (default, allowed)``.  *allowed* is ``BOOL``, a tuple of values,
#: or ``('int', low, high)``.
STATE_SPEC: dict = {
    'sheet.mode': ('floating', SHEET_MODES),
    'sheet.mode_before': ('floating', ('floating', 'pinned')),
    'sheet.side': ('left', SHEET_SIDES),
    'sheet.width': (360, ('int',) + SHEET_WIDTH_RANGE),
    'tools.pin_top': (False, BOOL),
    'tools.pin_bottom': (False, BOOL),
    'tools.pin_side': (False, BOOL),
    'tools.reveal': ('hover', REVEALS),
    'tools.show_bottom': (True, BOOL),
    'parts.show': (False, BOOL),
    'parts.chip': (True, BOOL),
    'parts.autohide': (True, BOOL),
    'ribbon.labels': ('full', RIBBON_LABELS),
    'ribbon.colour': (True, BOOL),
    'ribbon.locks': (True, BOOL),
    'colour.model_by': ('patches', MODEL_BY),
    'colour.scale': ('rainbow', SCALE_IDS),
    'colour.reverse': (False, BOOL),
    'colour.bands_mode': ('smooth', BAND_MODES),
    'colour.bands': (20, ('int',) + BAND_RANGE),
    'colour.range': ('whole', RANGES),
    'colour.legend': (True, BOOL),
    'labels.font': ('pretendard', FONT_IDS),
    'labels.size': ('normal', SIZE_IDS),
    'labels.worst': (True, BOOL),
    'labels.caption': (True, BOOL),
    'quality.layout': ('B', QUALITY_LAYOUTS),
    'quality.taller': (True, BOOL),
    'quality.beyond_limit': (False, BOOL),
    'quality.inspect': (False, BOOL),
}

#: What "Make this my standard" keeps (section 4).
STANDARD_KEYS = ('colour.scale', 'colour.reverse', 'colour.bands_mode',
                 'colour.bands', 'colour.range', 'colour.legend',
                 'labels.font', 'labels.size')
#: What Ctrl+0 puts back to the defaults; the colour keys go back to the
#: user's standard instead (layout rule 3).
LAYOUT_PREFIXES = ('sheet.', 'tools.', 'parts.', 'ribbon.')
MY_STANDARD = 'colour.my_standard'
#: This menu's scale ids against the colour standard's (LY12).
_CS_SCALE = {'rainbow': 'jet', 'white_red': 'white_to_red',
             'cool_warm': 'coolwarm', 'grayscale': 'gray'}
_CS_KEYS = ('colour.scale', 'colour.reverse', 'colour.bands_mode',
            'colour.bands', 'colour.range', 'colour.legend')


def _valid(key: str, value) -> bool:
    allowed = STATE_SPEC[key][1]
    if allowed == BOOL:
        return isinstance(value, bool)
    if allowed[0] == 'int':
        return (isinstance(value, int) and not isinstance(value, bool)
                and allowed[1] <= value <= allowed[2])
    return value in allowed


class ViewMenuState:
    """The View menu's choices: validated, remembered, observable."""

    def __init__(self, settings=None):
        self._settings = settings
        self._values = {k: spec[0] for k, spec in STATE_SPEC.items()}
        self._mine = None
        self._observers: list = []
        self._cs = None                  # the colour standard, once attached
        self._cs_settings = None
        self._cs_busy = False
        stored = {}
        try:
            stored = settings.getViewMenu() if settings is not None else {}
        except Exception:
            stored = {}
        for key, value in (stored or {}).items():
            if key in STATE_SPEC and _valid(key, value):
                self._values[key] = value
        mine = (stored or {}).get(MY_STANDARD)
        if isinstance(mine, dict):
            self._mine = {k: v for k, v in mine.items()
                          if k in STANDARD_KEYS and _valid(k, v)} or None

    # ---- reading ---------------------------------------------------------

    def get(self, key: str):
        return self._values[key]

    def default(self, key: str):
        return STATE_SPEC[key][0]

    def keys(self) -> tuple:
        return tuple(STATE_SPEC)

    def snapshot(self) -> dict:
        return dict(self._values)

    # ---- writing ---------------------------------------------------------

    def set(self, key: str, value) -> bool:
        """Returns whether the state changed.  A value the key does not allow
        is refused (and nothing changes)."""
        if key not in STATE_SPEC or not _valid(key, value):
            return False
        if self._values[key] == value:
            return False
        self._values[key] = value
        if key == 'sheet.mode' and value != 'minimised':
            self._values['sheet.mode_before'] = value
        self._save()
        if (self._cs is not None and not self._cs_busy
                and key in _CS_KEYS):
            self._colour_push()       # before observers read is_standard()
        self._notify(key)
        return True

    def toggle(self, key: str) -> bool:
        self.set(key, not self._values[key])
        return self._values[key]

    def set_minimised(self, on: bool) -> None:
        """Ctrl+\\: the sheet folds to its tab, or comes back as it was."""
        if on:
            self.set('sheet.mode', 'minimised')
        else:
            self.set('sheet.mode', self._values['sheet.mode_before'])

    def pin_all(self) -> bool:
        """Ctrl+H.  Pins the three tool edges, or unpins them when all three
        are pinned already.  Returns whether they are pinned afterwards."""
        keys = ('tools.pin_top', 'tools.pin_bottom', 'tools.pin_side')
        target = not all(self._values[k] for k in keys)
        for key in keys:
            self.set(key, target)
        return target

    # ---- the colour standard (section 4) ---------------------------------

    @property
    def my_standard(self):
        return dict(self._mine) if self._mine else None

    def standard_values(self) -> dict:
        """The values a view opens with: the user's standard, else the app's."""
        base = {k: STATE_SPEC[k][0] for k in STANDARD_KEYS}
        if self._cs is not None:
            base.update(self._cs_values(self._cs.saved(self._cs_settings)))
            for key in ('labels.font', 'labels.size'):
                base[key] = self._values[key]       # LY15 keeps these itself
        elif self._mine:
            base.update(self._mine)
        return base

    def standard_source(self) -> str:
        if self._cs is not None:
            return 'yours' if self._cs.has_saved(self._cs_settings) else 'app'
        return 'yours' if self._mine else 'app'

    def is_standard(self) -> bool:
        if self._cs is not None:
            return bool(self._cs.is_standard(self._cs_settings))
        return all(self._values[k] == v
                   for k, v in self.standard_values().items())

    def make_my_standard(self) -> None:
        if self._cs is not None:
            self._cs.make_my_standard(self._cs_settings)
            self._notify(MY_STANDARD)
            return
        self._mine = {k: self._values[k] for k in STANDARD_KEYS}
        self._save()
        self._notify(MY_STANDARD)

    def go_back_to_standard(self) -> None:
        if self._cs is not None:
            self._cs.go_back_to_standard(self._cs_settings)
            self._notify(MY_STANDARD)
            return
        for key, value in self.standard_values().items():
            self.set(key, value)
        self._notify(MY_STANDARD)

    def use_app_standard(self) -> None:
        if self._cs is not None:
            self._cs.use_app_standard(self._cs_settings)
            self._notify(MY_STANDARD)
            return
        self._mine = None
        self._save()
        self.go_back_to_standard()

    # ---- one colour standard (LY12) ---------------------------------------
    # The menu's colour keys are the colour standard of
    # ``foammesh.support.colour_standard``: a change here changes the session's
    # standard (every view and legend follows) and a change there shows here.

    def attach_colour(self, colour_standard, settings=None) -> None:
        self._cs = colour_standard
        self._cs_settings = settings
        colour_standard.subscribe(self._colour_followed)
        self._colour_pull()

    def _cs_values(self, standard) -> dict:
        names = {v: k for k, v in _CS_SCALE.items()}
        return {'colour.scale': names.get(standard.scale, standard.scale),
                'colour.reverse': standard.reverse,
                'colour.bands_mode': 'banded' if standard.bands else 'smooth',
                'colour.bands': (standard.bands if standard.bands
                                 else self._values['colour.bands']),
                'colour.range': standard.range,
                'colour.legend': standard.legend}

    def _colour_pull(self) -> None:
        values = self._cs_values(self._cs.current(self._cs_settings))
        self._cs_busy = True
        try:
            for key, value in values.items():
                self.set(key, value)
        finally:
            self._cs_busy = False

    def _colour_followed(self, _standard=None) -> None:
        if not self._cs_busy:
            self._colour_pull()

    def _colour_push(self) -> None:
        banded = self._values['colour.bands_mode'] == 'banded'
        self._cs.set_current(
            self._cs_settings,
            scale=_CS_SCALE.get(self._values['colour.scale'],
                                self._values['colour.scale']),
            reverse=self._values['colour.reverse'],
            bands=self._values['colour.bands'] if banded else 0,
            range=self._values['colour.range'],
            legend=self._values['colour.legend'])

    # ---- Ctrl+0 ----------------------------------------------------------

    def reset_layout(self) -> None:
        for key in STATE_SPEC:
            if key.startswith(LAYOUT_PREFIXES):
                self.set(key, STATE_SPEC[key][0])
        self.go_back_to_standard()

    # ---- observing / keeping ---------------------------------------------

    def subscribe(self, callback: Callable) -> Callable:
        """*callback(key)* after a change; returns the unsubscribe."""
        self._observers.append(callback)
        return lambda: self._observers.remove(callback)

    def _notify(self, key: str) -> None:
        for callback in list(self._observers):
            try:
                callback(key)
            except Exception:
                pass

    def _save(self) -> None:
        if self._settings is None:
            return
        record = {k: v for k, v in self._values.items()
                  if v != STATE_SPEC[k][0]}
        if self._mine:
            record[MY_STANDARD] = dict(self._mine)
        try:
            self._settings.updateViewMenu(record)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# The menu
# --------------------------------------------------------------------------- #

#: Group submenus in the order of section 6, with their object names.
GROUPS = (
    ('menuViewSettingsSheet', 'Settings &sheet'),
    ('menuViewTools', 'Viewport &tools'),
    ('menuViewParts', '&Parts panel'),
    ('menuViewCamera', '&Camera'),
    ('menuViewOverlays', '&Overlays and rendering'),
    ('menuViewRibbon', 'Step &ribbon'),
    ('menuViewColour', 'Colour &scale'),
    ('menuViewLabels', '&Labels and snapshots'),
    ('menuViewQuality', 'Mesh &quality'),
    ('menuConsole', 'Conso&le'),
)

_STOPS = {
    'rainbow': ('#0000ff', '#00ffff', '#00ff00', '#ffff00', '#ff0000'),
    'turbo': ('#30123b', '#28bbec', '#a2fc3c', '#fb8022', '#7a0403'),
    'viridis': ('#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'),
    'inferno': ('#000004', '#781c6d', '#ed6925', '#fcffa4'),
    'plasma': ('#0d0887', '#cc4778', '#f89540', '#f0f921'),
    'blackbody': ('#000000', '#b22222', '#ffa500', '#ffffff'),
    'white_red': ('#ffffff', '#fcae91', '#cb181d'),
    'cool_warm': ('#3b4cc0', '#dddddd', '#b40426'),
    'grayscale': ('#000000', '#ffffff'),
}


def scale_icon(scale: str, width: int = 56, height: int = 12) -> QIcon:
    """A swatch of *scale* for the menu."""
    # Tuple targets: these are drawing tools, not controls, and the AF0
    # inventory lists only a name bound to a widget.
    pixmap, gradient = QPixmap(width, height), QLinearGradient(0, 0, width, 0)
    pixmap.fill(Qt.GlobalColor.transparent)
    stops = _STOPS.get(scale, _STOPS['rainbow'])
    for index, colour in enumerate(stops):
        gradient.setColorAt(index / max(len(stops) - 1, 1), QColor(colour))
    (painter,) = (QPainter(pixmap),)
    painter.fillRect(0, 0, width, height, gradient)
    painter.end()
    return QIcon(pixmap)


class ViewMenu:
    """The built menu: the state, and every entry by object name."""

    def __init__(self, window, menu: QMenu, state: ViewMenuState):
        self.window = window
        self.menu = menu
        self.state = state
        #: object name -> (action, kind, key or None); kind is 'toggle',
        #: 'choice', 'action' or 'existing'.
        self.entries: dict = {}
        #: object name -> callable, for the plain 'action' entries.
        self.handlers: dict = {}
        self.groups: dict = {}
        self._syncs: list = []
        self._keep: list = []
        #: group object name -> (title, callable returning the summary)
        self.summaries: dict = {}

    # ---- building blocks ---------------------------------------------------

    def _register(self, name, action, kind, key=None, value=None):
        action.setObjectName(name)
        self.entries[name] = (action, kind, key, value)
        return action

    def toggle(self, menu, name, label, key, tip=''):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(bool(self.state.get(key)))
        if tip:
            action.setToolTip(tip)
        action.toggled.connect(lambda on, k=key: self.state.set(k, bool(on)))
        self._syncs.append(lambda: _quiet(action, self.state.get(key)))
        return self._register(name, action, 'toggle', key)

    def choice(self, menu, prefix, key, items, tip=''):
        """Exclusive entries; *items* is ``[(value, label)]``."""
        group = exclusive_group(menu)
        made = {}
        for value, label in items:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setActionGroup(group)
            action.setData(value)
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(
                lambda _c=False, k=key, v=value: self._chosen(k, v))
            self._register(f'{prefix}{value}', action, 'choice', key, value)
            made[value] = action

        def sync():
            current = self.state.get(key)
            for value, action in made.items():
                _quiet(action, value == current)
        self._syncs.append(sync)
        sync()
        return made

    def _chosen(self, key, value) -> None:
        # A click on the ticked entry of an exclusive group unticks it in Qt;
        # the state did not change, so say it again.
        self.state.set(key, value)
        self.refresh()

    def plain(self, menu, name, label, handler, tip='', shortcut_text=''):
        action = menu.addAction(
            label + ('\t' + shortcut_text if shortcut_text else ''))
        if tip:
            action.setToolTip(tip)
        self.handlers[name] = handler
        action.triggered.connect(lambda _c=False: handler())
        return self._register(name, action, 'action')

    def existing(self, menu, action, label=None):
        if label:
            action.setText(label)
        menu.addAction(action)
        if action.objectName():
            self.entries.setdefault(action.objectName(),
                                    (action, 'existing', None, None))
        return action

    def submenu(self, parent, name, title):
        sub = ThemedMenu(title, parent, KIND_FLYOUT)
        sub.setObjectName(name)
        parent.addMenu(sub)
        sub.setToolTipsVisible(True)
        self.groups[name] = sub
        return sub

    def summarise(self, sub, title, summary: Callable) -> None:
        """Show ``summary()`` at the right of the group's row (MF11)."""
        self.summaries[sub.objectName()] = (sub, title, summary)
        set_summary(sub, title, summary())

    def refresh_summaries(self, *_args) -> None:
        for sub, title, summary in self.summaries.values():
            try:
                set_summary(sub, title, summary())
            except RuntimeError:                  # the menu was deleted
                pass

    def refresh(self) -> None:
        for sync in self._syncs:
            sync()
        self.refresh_summaries()

    # ---- all the actions of the tree --------------------------------------

    def reachable(self) -> list:
        return reachable_actions(self.menu)


def _quiet(action, checked) -> None:
    action.blockSignals(True)
    action.setChecked(bool(checked))
    action.blockSignals(False)


def reachable_actions(menu: QMenu) -> list:
    """Every plain action in *menu* and its submenus, in order."""
    found = []
    for action in menu.actions():
        sub = action.menu()
        if sub is not None:
            found.extend(reachable_actions(sub))
        elif is_heading(action):
            continue                    # a section heading is not a command (MF11)
        elif not action.isSeparator():
            found.append(action)
            # A panel (Plan 43 LY13) lays real actions out as buttons.
            holder = getattr(action, 'defaultWidget', lambda: None)()
            if holder is not None:
                for button in holder.findChildren(QToolButton):
                    inner = button.defaultAction()
                    if inner is not None and inner not in found:
                        found.append(inner)
                for inner in getattr(holder, 'foammeshActions', ()):
                    if inner not in found:
                        found.append(inner)
    return found


# --------------------------------------------------------------------------- #
# Laying the window's own actions out
# --------------------------------------------------------------------------- #

def install_view_menu(window) -> ViewMenu:
    """Lay the window's View actions out in the groups of section 6."""
    from foammesh.app import app
    ui = window._ui
    menu = ui.menuView
    tr = window.tr
    state = ViewMenuState(getattr(app, 'settings', None))
    view = ViewMenu(window, menu, state)
    themed(menu, KIND_BAR)
    window._viewState = state
    window._viewMenu = view

    # What each installer made.  Nothing is rebuilt: the same QAction objects
    # move, so connections, shortcuts and checks all stay.
    flat = list(menu.actions())
    quality_menu = getattr(window, '_renderQualityMenu', None)
    console_menu = getattr(window, '_consoleMenu', None)
    console_sub_actions = list(console_menu.actions()) if console_menu else []
    for action in flat:
        menu.removeAction(action)
    if console_menu is not None:
        for action in console_sub_actions:
            console_menu.removeAction(action)

    # -- top of the menu -----------------------------------------------------
    reset = window._resetLayoutAction
    # The window set Ctrl+0 and adopted it when it made the action (DP-2107).
    reset.setToolTip(tr(
        'Put the sheet, console, tools, ribbon, Parts panel and colour '
        'scale back to the standard layout'))
    view.existing(menu, reset, tr('Standard la&yout'))

    minimise_sheet = view.toggle(
        menu, 'actionMinimiseSheet', tr('Minimise &sheet'), 'sheet.mode',
        tr('Fold the settings sheet to a tab with the step name'))
    minimise_sheet.toggled.disconnect()
    minimise_sheet.toggled.connect(lambda on: state.set_minimised(bool(on)))
    view._syncs[-1] = lambda: _quiet(
        minimise_sheet, state.get('sheet.mode') == 'minimised')
    minimise_sheet.setShortcut('Ctrl+\\')
    adoptShortcut('view.minimiseSheet', minimise_sheet)
    view.entries['actionMinimiseSheet'] = (
        minimise_sheet, 'toggle', 'sheet.mode', None)

    _minimise_console_row(menu, window._minimiseAction,
                          tr('Minimise c&onsole'))
    menu.addSeparator()

    # -- Settings sheet ------------------------------------------------------
    sheet = view.submenu(menu, 'menuViewSettingsSheet', tr('Settings &sheet'))
    view.choice(sheet, 'actionSheetMode_', 'sheet.mode',
                [('floating', tr('&Floating')), ('pinned', tr('&Pinned')),
                 ('minimised', tr('&Minimised'))])
    menu_heading(sheet, tr('Side'))
    view.choice(sheet, 'actionSheetSide_', 'sheet.side',
                [('left', tr('&Left')), ('right', tr('&Right'))])
    menu_heading(sheet, tr('Width'))
    view.choice(sheet, 'actionSheetWidth_', 'sheet.width',
                [(w, tr('%1 px').replace('%1', str(w)))
                 for w in SHEET_WIDTHS])
    sheet.addSeparator()
    # The dock title bars of Plan 40 UXC3 stay reachable until the sheet
    # replaces the docks (LY3).
    view.existing(sheet, window._arrangeAction)

    # -- Viewport tools ------------------------------------------------------
    tools = view.submenu(menu, 'menuViewTools', tr('Viewport &tools'))
    menu_heading(tools, tr('Pinned = always shown'))
    view.toggle(tools, 'actionPinTopBar', tr('Pin &top bar'),
                'tools.pin_top', tr('Keep the top bar on screen'))
    view.toggle(tools, 'actionPinBottomBar', tr('Pin &bottom bar'),
                'tools.pin_bottom', tr('Keep the bottom bar on screen'))
    view.toggle(tools, 'actionPinSideRail', tr('Pin side &rail'),
                'tools.pin_side', tr('Keep the side rail on screen'))
    pin_all = view.plain(
        tools, 'actionPinAllTools', tr('Pin or unpin &all three'),
        state.pin_all, tr('Pin or unpin all three tool edges'))
    pin_all.setShortcut('Ctrl+H')
    adoptShortcut('view.pinTools', pin_all)
    menu_heading(tools, tr('Hidden tools appear'))
    view.choice(tools, 'actionToolsReveal_', 'tools.reveal',
                [('hover', tr('Tools appear on &hover')),
                 ('click', tr('Tools appear on &click'))])
    menu_heading(tools, tr('Show'))
    view.toggle(tools, 'actionShowBottomBar', tr('Show the bottom &bar'),
                'tools.show_bottom', tr('Show the bottom bar'))
    view.existing(tools, window._viewportRailAction,
                  tr('Show the side &rail'))

    # -- Parts panel ---------------------------------------------------------
    parts = view.submenu(menu, 'menuViewParts', tr('&Parts panel'))
    view.toggle(parts, 'actionShowPartsPanel', tr('&Show parts panel'),
                'parts.show', tr('Open the Parts panel'))
    view.toggle(parts, 'actionPartsChip', tr('Show the Parts &chip'),
                'parts.chip',
                tr('Show the "3 of 3" chip at the top right that opens the '
                   'Parts panel'))
    view.toggle(parts, 'actionPartsAutoHide', tr('&Auto-hide'),
                'parts.autohide',
                tr('Fold the Parts panel away when the pointer leaves it'))

    # -- Camera --------------------------------------------------------------
    camera = view.submenu(menu, 'menuViewCamera', tr('&Camera'))
    for name in ('actionViewFit', 'actionViewZoomSelection',
                 'actionViewAlignAxis', 'actionViewRoll',
                 'actionViewParallelProjection', 'actionViewRotationCenter'):
        view.existing(camera, getattr(ui, name))
    view.plain(camera, 'actionViewCameraPose', tr('Camera &pose…'),
               lambda: _openCameraPose(window),
               tr('Set the camera from three rotations, a translation and a '
                  'scale, or read them back from the view (as in the Gmsh '
                  'Camera tab)'))
    camera.addSeparator()
    view.existing(camera, window._copyViewAction)
    view.existing(camera, window._saveViewAction)

    # -- Overlays and rendering ---------------------------------------------
    overlays = view.submenu(menu, 'menuViewOverlays',
                            tr('&Overlays and rendering'))
    menu_heading(overlays, tr('Overlays'))
    for name in ('actionViewAxis', 'actionViewCubeAxis', 'actionViewRuler'):
        view.existing(overlays, getattr(ui, name))
    view.existing(overlays, window._hoverReadoutAction)
    view.existing(overlays, window._resourceAction)
    # Plan 44 F1 (MF15, decision Q9): the watermark in the clear area's
    # bottom-right corner can be switched off; the view owns the state.
    mark = overlays.addAction(tr('&Watermark'))
    mark.setCheckable(True)
    mark.setToolTip(tr('Show the FoamMesh logo in the bottom-right corner of '
                       'the view'))
    rendering_view = getattr(ui, 'renderingView', None)
    if rendering_view is not None and hasattr(rendering_view,
                                              'setWatermarkShown'):
        shown = rendering_view.isWatermarkShown()
        # A stand-in view (the footer harness) answers with no bool.
        mark.setChecked(shown if isinstance(shown, bool) else True)
        mark.toggled.connect(rendering_view.setWatermarkShown)
        changed = getattr(rendering_view, 'watermarkShownChanged', None)
        if hasattr(changed, 'connect'):
            changed.connect(lambda on: _quiet(mark, on))
    else:
        mark.setChecked(True)
        mark.setEnabled(False)
    view._register('actionViewWatermark', mark, 'existing')
    menu_heading(overlays, tr('Render quality'))
    if quality_menu is not None:
        themed(quality_menu, KIND_FLYOUT)
        overlays.addMenu(quality_menu)
        view.groups['menuRenderQuality'] = quality_menu
        for action in quality_menu.actions():
            if action.data() and not action.objectName():
                action.setObjectName(f'actionRenderQuality_{action.data()}')
            view.entries[action.objectName()] = (
                action, 'existing', None, None)
    for action in getattr(window, '_effectActions', {}).values():
        if not action.objectName():
            action.setObjectName('actionReduceDetail')
        view.existing(overlays, action)
    pause = overlays.addAction(tr('&Pause 3D drawing'))
    pause.setCheckable(True)
    pause.setToolTip(tr(
        'Stop the viewport drawing, for example while a large run needs the '
        'whole graphics card'))
    rendering = ui.rendering
    pause.setChecked(not rendering.isChecked())
    pause.toggled.connect(lambda on: rendering.setChecked(not on))
    rendering.toggled.connect(lambda on: _quiet(pause, not on))
    view._register('actionPauseDrawing', pause, 'existing')

    # -- Step ribbon ---------------------------------------------------------
    ribbon = view.submenu(menu, 'menuViewRibbon', tr('Step &ribbon'))
    full = ribbon.addAction(tr('&Full labels'))
    full.setCheckable(True)
    full.setToolTip(tr('Show the step names on the ribbon'))
    full.triggered.connect(lambda _c=False: _ribbonLabels(window, rail=False))
    view._register('actionRibbonFullLabels', full, 'existing')
    rail = window._railAction
    for choice_action in (full, rail):
        choice_action.setProperty('foammeshMenuRadio', True)
    rail.setToolTip(tr('Show only the step numbers on the ribbon'))
    view.existing(ribbon, rail, tr('&Numbers only'))

    def syncRibbon():
        on = window._stepRibbon.numbersOnly()
        state.set('ribbon.labels', 'numbers' if on else 'full')
        _quiet(full, not on)
        _quiet(rail, on)
    view._syncs.append(syncRibbon)
    ribbon.aboutToShow.connect(syncRibbon)
    ribbon.addSeparator()
    view.toggle(ribbon, 'actionRibbonColourByState', tr('&Colour by state'),
                'ribbon.colour', tr('Colour each step by its state'))
    view.toggle(ribbon, 'actionRibbonShowLocks', tr('Show &locks'),
                'ribbon.locks', tr('Mark the steps that are done and locked'))
    # Plan 43 LY2: these two entries are the ribbon's own colour and locks
    # switches; the window keeps pointing at them, and the keys that move
    # between steps stay in this group (they were View entries before).
    window._ribbonColourAction = view.entries['actionRibbonColourByState'][0]
    window._ribbonLocksAction = view.entries['actionRibbonShowLocks'][0]
    ribbon.addSeparator()
    view.existing(ribbon, window._stepPreviousAction)
    view.existing(ribbon, window._stepNextAction)

    # -- Colour scale --------------------------------------------------------
    # One flyout holding a two-column panel (Plan 43 LY13, decision D13; the
    # mockup's `.sub.wide` with `.cols`): the standard, what to colour by,
    # reverse, legend and range on the left; the nine scales, bands, label
    # font and size and the reset on the right.  Every entry stays a real
    # `QAction` (kept in a store that is not in the menu, so Qt's action tree
    # is unchanged for the tests and the facade inventory) shown by a button
    # whose default action it is; `reachable_actions` reads them back.
    colour = view.submenu(menu, 'menuViewColour', tr('Colour &scale'))
    make_scrollable(colour)
    colour.aboutToShow.connect(lambda: fit_flyout(colour, window))
    store = QMenu(colour)
    store.setObjectName('menuViewColourStore')
    view._keep.append(store)
    swatch = store.addAction(tr('Standard'))
    swatch.setEnabled(False)
    view._register('actionColourStandardSwatch', swatch, 'existing')

    def syncSwatch():
        source = (tr('yours') if state.standard_source() == 'yours'
                  else tr('the app standard'))
        scale = dict(SCALES)[state.standard_values()['colour.scale']]
        text = tr('Standard: %1 (%2)').replace('%1', scale).replace(
            '%2', source)
        if not state.is_standard():
            text += tr(' — not the standard')
        swatch.setText(text)
        swatch.setIcon(scale_icon(state.get('colour.scale')))
    view._syncs.append(syncSwatch)
    syncSwatch()
    view.plain(store, 'actionColourMakeMine', tr('Make this my &standard'),
               state.make_my_standard,
               tr('Keep the scale, reverse, bands, range, legend, font and '
                  'size as your standard on this computer'))
    view.plain(store, 'actionColourGoBack', tr('&Go back to the standard'),
               state.go_back_to_standard,
               tr('Reload the saved standard'))
    view.plain(store, 'actionColourUseApp', tr('Use the &app standard'),
               state.use_app_standard,
               tr('Forget your standard and use Rainbow (Jet)'))
    view.plain(store, 'actionColourReset', tr('&Reset the colour scale'),
               state.go_back_to_standard,
               tr('Put the scale, reverse, bands, range and legend back to '
                  'the standard'))
    model_by = view.choice(
        store, 'actionColourBy_', 'colour.model_by',
        [('patches', tr('&Patches')),
         ('quality', tr('Cell &quality')),
         ('deviation', tr('Surface &deviation'))])
    reverse = view.toggle(store, 'actionColourReverse', tr('&Reverse'),
                          'colour.reverse', tr('Run the scale the other way'))
    legend = view.toggle(store, 'actionColourLegend', tr('Show &legend'),
                         'colour.legend',
                         tr('Show the legend over the viewport'))
    ranges = view.choice(
        store, 'actionColourRange_', 'colour.range',
        [('whole', tr('&Whole data range')),
         ('limit', tr('Around the &limit (the tolerance for deviation)'))])
    scale_actions = view.choice(
        store, 'actionColourScale_', 'colour.scale',
        [(key, tr(label)) for key, label in SCALES])
    for key, action in scale_actions.items():
        action.setIcon(scale_icon(key))
    band_modes = view.choice(
        store, 'actionColourBands_', 'colour.bands_mode',
        [('smooth', tr('S&mooth')), ('banded', tr('&Banded'))])

    # The two-column ColourScalePanel of MF12 (560 px): the standard, reverse,
    # legend and range on the left; the nine scales and the bands on the right.
    # It writes the colour standard itself, which this menu's state follows, so
    # the store's actions above stay as the keys' and the facade's handles.
    # Under it a strip keeps what the panel does not have: what to colour the
    # model by, the label font and size, and the reset.
    from foammesh.view.display_control.colour_scale_panel import (
        ColourScalePanel)
    panel = QWidget()
    panel.setObjectName('viewColourPanel')
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(GAP, MARGIN_TIGHT, GAP, MARGIN_TIGHT)
    outer.setSpacing(GAP_TIGHT)
    scale_panel = ColourScalePanel(
        panel, settings=getattr(state, '_cs_settings', None), live=True,
        columns=2)
    outer.addWidget(scale_panel)
    view.colour_panel = scale_panel

    def button(box, action):
        tool = QToolButton()
        tool.setDefaultAction(action)
        tool.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tool.setAutoRaise(True)
        tool.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        tool.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        tool.setProperty('foammeshColourRow', True)
        tool.setObjectName('viewColour_' + action.objectName())
        box.addWidget(tool)
        return tool

    divider = QFrame()
    divider.setFrameShape(QFrame.Shape.HLine)
    outer.addWidget(divider)
    strip = QHBoxLayout()
    strip.setSpacing(GAP_SECTION)  # SG44: the scale's section gap (was 16)
    by_box, label_box = QVBoxLayout(), QVBoxLayout()
    for box, text in ((by_box, tr('Colour the model by')),
                      (label_box, tr('Label font and size'))):
        head = QLabel(text)
        head.setProperty('foammeshColourHeading', True)
        box.addWidget(head)
    for action in model_by.values():
        button(by_box, action)
    by_box.addStretch(1)
    labels_row = QWidget()
    labels_layout = QHBoxLayout(labels_row)
    labels_layout.setContentsMargins(0, MARGIN_TIGHT // 2, 0, MARGIN_TIGHT // 2)
    font_box, size_box = QComboBox(), QComboBox()
    font_box.setObjectName('viewColourLabelFont')
    size_box.setObjectName('viewColourLabelSize')
    for key, name, family in FONTS:
        font_box.addItem(name, key)
        font_box.setItemData(font_box.count() - 1, QFont(family),
                             Qt.ItemDataRole.FontRole)
    for key, name, _factor in SIZES:
        size_box.addItem(tr(name), key)
    labels_layout.addWidget(font_box, 1)
    labels_layout.addWidget(size_box, 1)
    label_box.addWidget(labels_row)

    def syncLabels():
        for box, key in ((font_box, 'labels.font'), (size_box, 'labels.size')):
            index = box.findData(state.get(key))
            box.blockSignals(True)
            box.setCurrentIndex(max(index, 0))
            box.blockSignals(False)
    font_box.activated.connect(
        lambda i: state.set('labels.font', font_box.itemData(i)))
    size_box.activated.connect(
        lambda i: state.set('labels.size', size_box.itemData(i)))
    view._syncs.append(syncLabels)
    syncLabels()
    button(label_box, view.entries['actionColourReset'][0])
    note = QLabel(tr('Blue is good, red is worst. Deviation is coloured by '
                     'its size, so a surface pushed out or pulled in by the '
                     'same amount gets the same colour.'))
    note.setWordWrap(True)
    note.setProperty('foammeshColourNote', True)
    label_box.addWidget(note)
    label_box.addStretch(1)
    strip.addLayout(by_box, 1)
    strip.addLayout(label_box, 1)
    outer.addLayout(strip)
    panel.setFixedWidth(scale_panel.width() + 2 * GAP)
    # The actions this panel stands for (the keys, the facade and the
    # inventory read them from here).
    panel.foammeshActions = list(store.actions())
    view._keep.append(panel)
    shown = QWidgetAction(colour)
    # CHL (DP-2661, M4): the panel scrolls inside the room the window leaves.
    host = MenuScrollHost(panel)
    host.foammeshActions = panel.foammeshActions
    view._keep.append(host)
    shown.setDefaultWidget(host)
    view._register('actionColourPanel', shown, 'widget')
    view.entries['actionViewBands'] = (shown, 'widget', 'colour.bands', None)
    colour.addAction(shown)
    state.subscribe(lambda _key: syncSwatch())

    # -- Labels and snapshots -----------------------------------------------
    labels = view.submenu(menu, 'menuViewLabels',
                          tr('&Labels and snapshots'))
    menu_heading(labels, tr('Font and size of the labels on the model'))
    fonts = view.submenu(labels, 'menuViewLabelFont', tr('&Font'))
    font_actions = view.choice(
        fonts, 'actionLabelFont_', 'labels.font',
        [(key, name) for key, name, _family in FONTS])
    for key, name, family in FONTS:
        font_actions[key].setFont(QFont(family))
    sizes = view.submenu(labels, 'menuViewLabelSize', tr('&Size'))
    view.choice(sizes, 'actionLabelSize_', 'labels.size',
                [(key, tr(name)) for key, name, _f in SIZES])
    menu_heading(labels, tr('On the model'))
    view.toggle(labels, 'actionLabelWorst', tr('&Worst values on the model'),
                'labels.worst', tr('Write the worst values on the model'))
    view.toggle(labels, 'actionLabelCaption', tr('S&tatistics caption'),
                'labels.caption', tr('Show the statistics caption'))
    view.toggle(labels, 'actionLabelLegend', tr('Show &legend'),
                'colour.legend', tr('Show the legend over the viewport'))
    labels.addSeparator()
    # LY15's one Snapshot popover (also the camera button and Ctrl+Shift+P).
    view.plain(labels, 'actionSnapshot', tr('S&napshot\u2026'),
               lambda: window.showSnapshot(),
               tr('Save or copy the view as a picture'), 'Ctrl+Shift+P')

    # -- Mesh quality --------------------------------------------------------
    quality = view.submenu(menu, 'menuViewQuality', tr('Mesh &quality'))
    layouts = view.submenu(quality, 'menuViewQualityLayout',
                           tr('Tab &layout'))
    view.choice(layouts, 'actionQualityLayout_', 'quality.layout',
                [('A', tr('A: metric cards')),
                 ('B', tr('B: table, distribution and cells')),
                 ('C', tr('C: compact'))])
    view.toggle(quality, 'actionQualityTaller',
                tr('Open the tab &taller (280 px)'), 'quality.taller',
                tr('Open the Mesh quality tab at 280 px'))
    view.toggle(quality, 'actionQualityBeyondLimit',
                tr('Show cells &beyond limit'), 'quality.beyond_limit',
                tr('Show the cells over the limit on the model'))
    view.toggle(quality, 'actionQualityInspect',
                tr('&Inspect element on click'), 'quality.inspect',
                tr('A click on the model reads the element under it'))

    # -- Console -------------------------------------------------------------
    console = window._consoleMenu
    console.setTitle(tr('Conso&le'))
    themed(console, KIND_FLYOUT)
    menu.addMenu(console)
    view.groups['menuConsole'] = console
    view.existing(console, window._bandAction, tr('&Show console'))
    view.existing(console, window._minimiseAction, tr('&Minimised'))
    if not window._consoleAction.objectName():
        window._consoleAction.setObjectName('actionConsole')
    view.existing(console, window._consoleAction,
                  tr('Open at the &Console tab'))
    view.existing(console, window._openOnRunAction)
    notify = getattr(window._jobAttention, 'action', None)
    if notify is not None:
        view.existing(console, notify)
    console.addSeparator()
    for sub_name in ('menuConsoleHeight', 'menuConsoleWidth'):
        sub = next((a.menu() for a in console_sub_actions
                    if a.menu() is not None
                    and a.menu().objectName() == sub_name), None)
        if sub is not None:
            console.addMenu(sub)
    # The earlier "Open at the Console tab" entry of the submenu does the same
    # thing as the Ctrl+Shift+C action above; it stays constructed.
    menu.addSeparator()

    # -- Focus mode and Terminal here ---------------------------------------
    view.existing(menu, window._focusAction, tr('Focus &mode'))
    view.existing(menu, ui.actionTerminalHere)

    _install_summaries(window, view, state, {
        'sheet': (sheet, tr('Settings &sheet')),
        'tools': (tools, tr('Viewport &tools')),
        'parts': (parts, tr('&Parts panel')),
        'camera': (camera, tr('&Camera')),
        'overlays': (overlays, tr('&Overlays and rendering')),
        'ribbon': (ribbon, tr('Step &ribbon')),
        'colour': (colour, tr('Colour &scale')),
        'labels': (labels, tr('&Labels and snapshots')),
        'quality': (quality, tr('Mesh &quality')),
        'console': (console, tr('Conso&le')),
    })
    bind_labels(window, state)
    bind_ribbon(window, state)
    bind_colour(window, state)
    bind_model_by(window, state)
    view.refresh()
    state.subscribe(lambda _key: view.refresh())
    return view


def _plain(text: str) -> str:
    return menus_strip_mnemonic(text)


def _short(name: str) -> str:
    """``Rainbow (Jet)`` -> ``Rainbow``."""
    return name.split(' (')[0]


def _install_summaries(window, view: ViewMenu, state: ViewMenuState,
                       groups: dict) -> None:
    """Plan 44 MF11: each group's row says what the group is set to.

    The text is the group's title, a TAB and the summary (the mockup's
    ``.sum``); it is refreshed on every state change, on every change of the
    window actions a summary reads, and when the menu opens.
    """
    ui = window._ui
    scale_names = dict(SCALES)
    font_names = {key: name for key, name, _family in FONTS}

    def sheet():
        from foammesh.core.naming import humanise_option

        # DP-224: a stored name is spelled for the screen by the one rule.
        mode = humanise_option(state.get('sheet.mode'))
        return f"{mode}, {state.get('sheet.side')}"

    def tools():
        n = sum(1 for key in ('tools.pin_top', 'tools.pin_bottom',
                              'tools.pin_side') if state.get(key))
        return f'{n} pinned' if n else 'Auto-hide'

    def parts():
        if not state.get('parts.show'):
            return 'Off'
        return 'Auto-hide' if state.get('parts.autohide') else 'Pinned'

    def camera():
        return ('Parallel' if ui.actionViewParallelProjection.isChecked()
                else 'Perspective')

    def overlays():
        menu = getattr(window, '_renderQualityMenu', None)
        for action in (menu.actions() if menu is not None else ()):
            if action.isChecked():
                return _plain(action.text()).split('	')[0]
        return ''

    def ribbon():
        return 'Numbers' if state.get('ribbon.labels') == 'numbers'             else 'Full labels'

    def colour():
        name = _short(scale_names.get(state.get('colour.scale'), ''))
        return name if state.is_standard() else '• ' + name

    def labels():
        return _short(font_names.get(state.get('labels.font'), ''))

    def quality():
        return f"Layout {state.get('quality.layout')}"

    def console():
        if not window._bandAction.isChecked():
            return 'Off'
        band = ui.regionCBandController
        if band.is_minimised:
            return 'Minimised'
        return f'{band.band_height} px'

    readers = dict(sheet=sheet, tools=tools, parts=parts, camera=camera,
                   overlays=overlays, ribbon=ribbon, colour=colour,
                   labels=labels, quality=quality, console=console)
    for key, (sub, title) in groups.items():
        view.summarise(sub, title, readers[key])
    view.menu.aboutToShow.connect(view.refresh_summaries)
    for action in (ui.actionViewParallelProjection, window._bandAction,
                   window._minimiseAction):
        action.toggled.connect(view.refresh_summaries)


def _openCameraPose(window):
    from foammesh.view.main_window.camera_pose import show_camera_pose
    return show_camera_pose(window)


def fit_flyout(menu: QMenu, window) -> None:
    """Cap a flyout's height to the window, so a long one scrolls inside
    itself (the Colour scale group at 1280 x 800)."""
    room = window.height() if window is not None else 800
    try:
        room = min(room, window.screen().availableGeometry().height())
    except Exception:                                         # noqa: BLE001
        pass
    fit_to_room(menu, max(160, room - 24))


def bind_labels(window, state: ViewMenuState) -> None:
    """The label font and size and the legend switch drive the viewport
    (LY15's ``setLabelStyle`` and ``setLegendsShown``) and follow it when the
    Snapshot popover or Preferences change them: one setting, many controls."""
    from foammesh.rendering import label_style
    view = window._ui.renderingView
    current = label_style.load()
    state.set('labels.font', current.font)
    state.set('labels.size', current.size)
    shown = view.areLegendsShown()
    if isinstance(shown, bool):
        state.set('colour.legend', shown)

    def follow(key):
        if key in ('labels.font', 'labels.size'):
            now = view.labelStyle()
            want = label_style.normalise(state.get('labels.font'),
                                         state.get('labels.size'))
            if (getattr(now, 'font', None), getattr(now, 'size', None)) != (
                    want.font, want.size):
                view.setLabelStyle(want)
                label_style.save(want)
        elif key == 'colour.legend':
            if view.areLegendsShown() is not state.get('colour.legend'):
                view.setLegendsShown(state.get('colour.legend'))
    state.subscribe(follow)
    _connect(view, 'labelStyleChanged', lambda style: (
        state.set('labels.font', style.font),
        state.set('labels.size', style.size)))
    _connect(view, 'legendsShownChanged',
             lambda on: state.set('colour.legend', bool(on)))


def bind_ribbon(window, state: ViewMenuState) -> None:
    """The ribbon's labels, colour and locks: the menu's three choices drive
    the step ribbon, and the ribbon's own (Ctrl+B, its right-click) show in
    the menu.  The ribbon owns the saved record, so it speaks first."""
    ribbon = getattr(window, '_stepRibbon', None)
    if ribbon is None:
        return

    def fromRibbon(record=None):
        record = ribbon.options()
        state.set('ribbon.labels',
                  'numbers' if record['numbers_only'] else 'full')
        state.set('ribbon.colour', bool(record['colour_by_state']))
        state.set('ribbon.locks', bool(record['show_locks']))

    def follow(key):
        if key == 'ribbon.labels':
            ribbon.setNumbersOnly(state.get(key) == 'numbers')
        elif key == 'ribbon.colour':
            ribbon.setColourByState(state.get(key))
        elif key == 'ribbon.locks':
            ribbon.setShowLocks(state.get(key))
    fromRibbon()
    state.subscribe(follow)
    ribbon.optionsChanged.connect(fromRibbon)
def bind_model_by(window, state: ViewMenuState) -> None:
    """Plan 43 LY11 / LY8 (DP-1771). "Colour the model by" is the legend's
    Patches / Quality / Deviation segment: the menu entry asks the legend
    controller for the mode, and the mode on screen (a segment click, the
    Mesh quality rows, a painted deviation) shows in the menu.  What is on
    screen speaks first at start: the choice is not replayed on a model that
    is not loaded."""
    controller = getattr(window, '_legendController', None)
    if controller is None:
        return

    asking = [False]

    def onScreen():
        return controller.mode() or 'patches'

    def fromSurface(mode=None):
        # A request the surface cannot show yet (no mesh loaded) leaves the
        # choice ticked; the next change on the surface speaks again.
        if not asking[0]:
            state.set('colour.model_by', mode or 'patches')

    def follow(key):
        if key == 'colour.model_by' and onScreen() != state.get(key):
            asking[0] = True
            try:
                controller.modeRequested(state.get(key))
            finally:
                asking[0] = False
    fromSurface(controller.mode())
    state.subscribe(follow)
    controller.onModeShown(fromSurface)


def _minimise_console_row(menu, original, label):
    """MF9. The top level's own row for the shared Minimise console action.

    The Console flyout holds *original* as "Minimised"; one action cannot read
    two ways in two menus. This mirror carries the top level's label, the same
    name (so the entry is found where it always was) and the same checked
    state, and presses the original. It shows the key as
    text only (a second real shortcut would be ambiguous), so Ctrl+` has one
    owner: the original.
    """
    keys = original.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    consoleMinimiseMirror = QAction(label + ('	' + keys if keys else ''), menu)
    consoleMinimiseMirror.setObjectName(original.objectName())
    consoleMinimiseMirror.setCheckable(True)
    consoleMinimiseMirror.setChecked(original.isChecked())
    consoleMinimiseMirror.setToolTip(original.toolTip())
    consoleMinimiseMirror.setEnabled(original.isEnabled())
    consoleMinimiseMirror.triggered.connect(lambda _c=False: original.trigger())

    def follow(*_args):
        if consoleMinimiseMirror.isChecked() != original.isChecked():
            blocked = consoleMinimiseMirror.blockSignals(True)
            consoleMinimiseMirror.setChecked(original.isChecked())
            consoleMinimiseMirror.blockSignals(blocked)
        consoleMinimiseMirror.setEnabled(original.isEnabled())
    original.changed.connect(follow)
    original.toggled.connect(follow)
    menu.addAction(consoleMinimiseMirror)
    return consoleMinimiseMirror


def bind_colour(window, state: ViewMenuState) -> None:
    """Plan 43 LY12. The Colour scale group is the colour standard."""
    from foammesh.support import colour_standard
    settings = None
    try:
        from foammesh.app import app
        candidate = getattr(app, 'settings', None)
        if candidate is not None and hasattr(candidate, 'getColourStandard'):
            settings = candidate
    except Exception:                                         # noqa: BLE001
        settings = None
    state.attach_colour(colour_standard, settings)
    window._colourStandardSettings = settings


def _connect(owner, signal: str, slot) -> None:
    try:
        getattr(owner, signal).connect(slot)
    except AttributeError:      # a stand-in viewport without the signal
        pass


def _ribbonLabels(window, *, rail: bool) -> None:
    if window._stepRibbon.numbersOnly() != rail:
        window.toggleOutlineRail()
