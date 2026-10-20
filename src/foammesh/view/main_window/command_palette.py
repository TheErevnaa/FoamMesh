#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 40 UXD2: the Ctrl+K command palette (F7a).

One index over three things a user looks for by name:

* the menu actions,
* the outline steps,
* every field of the registry.

Typing narrows the list with a fuzzy match. Choosing an action runs it, a step
opens it, and a field opens its page, scrolls to the field and flashes it.

The index and the matcher know nothing of Qt, so a test can build them over the
registry and an action list without a window. The dialog is a thin popup over
them. A search is a plain pass over a few hundred lower-cased strings: it is
timed on every keystroke (``lastSearchMs``) because the budget is 50 ms.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFrame, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QScrollArea, QVBoxLayout, QWidget)
from foammesh.core.naming import humanise_option
from foammesh.view.theming.metrics import FORM_MARGIN
from foammesh.view.theming.status_colors import style_palette_flash

KIND_ACTION = 'action'
KIND_STEP = 'step'
KIND_FIELD = 'field'
_KIND_ORDER = {KIND_STEP: 0, KIND_ACTION: 1, KIND_FIELD: 2}
_KIND_WORD = {KIND_ACTION: 'Action', KIND_STEP: 'Step', KIND_FIELD: 'Field'}

#: How many rows the list shows.
MAX_RESULTS = 50
#: The budget for one keystroke over the full index (Plan 40 UXD2).
KEYSTROKE_BUDGET_MS = 50.0

_WORD_START = frozenset(' ._-:/>')


@dataclass
class PaletteEntry:
    kind: str
    label: str
    detail: str = ''
    #: Extra words the entry is found by, not shown.
    keywords: str = ''
    enabled: bool = True
    #: What choosing the entry does. ``None`` for an entry that cannot run now.
    run: Callable[[], None] | None = None
    key: str = ''
    #: Plan 44 FX3 (DP-2624). Added to the match score: the page on screen
    #: ranks its own entries above the same entry of another page.
    boost: float = 0.0
    _label: str = field(default='', repr=False)
    _hay: str = field(default='', repr=False)

    def __post_init__(self):
        self._label = self.label.lower()
        self._hay = ' '.join(
            part for part in (self.label, self.detail, self.keywords)
            if part).lower()


# --------------------------------------------------------------------------- #
# the matcher
# --------------------------------------------------------------------------- #

def _match(token: str, text: str):
    """The score of ``token`` in ``text``, or ``None`` for no match."""
    at = text.find(token)
    if at >= 0:
        score = 100.0 - min(at, 40) / 2.0
        if at == 0 or text[at - 1] in _WORD_START:
            score += 30.0
        if len(token) == len(text):
            score += 40.0
        return score
    if len(token) < 2:
        return None
    # A subsequence: every letter in order. Either each letter opens a word
    # (initials: "bl" for "boundary layers") or the letters sit close together
    # (a typo or a dropped letter); letters scattered over a long label are
    # noise, not a match.
    position = -1
    first = -1
    previous = -2
    score = 40.0
    initials = True
    for letter in token:
        position = text.find(letter, position + 1)
        if position < 0:
            return None
        if first < 0:
            first = position
        at_start = position == 0 or text[position - 1] in _WORD_START
        if position == previous + 1:
            score += 4.0
        elif at_start:
            score += 2.0
        else:
            score -= 1.0
        if not at_start and position != previous + 1:
            initials = False
        previous = position
    if not initials and previous - first + 1 > 2 * len(token) + 1:
        return None
    return max(score, 1.0)


def fuzzy_score(query: str, entry: PaletteEntry):
    """How well ``query`` finds ``entry``; ``None`` if some word misses."""
    total = 0.0
    for token in query.lower().split():
        in_label = _match(token, entry._label)
        if in_label is not None:
            total += in_label + 20.0
            continue
        in_hay = _match(token, entry._hay)
        if in_hay is None:
            return None
        total += in_hay
    return total


class CommandIndex:
    """The entries, and the search over them."""

    def __init__(self, entries=()):
        self.entries: list[PaletteEntry] = list(entries)
        self.lastSearchMs = 0.0

    def add(self, entries) -> None:
        self.entries.extend(entries)

    def search(self, query: str, limit: int = MAX_RESULTS) -> list:
        started = time.perf_counter()
        query = ' '.join(str(query or '').split())
        if not query:
            hits = sorted(
                (entry for entry in self.entries
                 if entry.kind != KIND_FIELD),
                key=lambda e: (_KIND_ORDER[e.kind], -e.boost,
                               e.label.lower()))
        else:
            scored = []
            for entry in self.entries:
                score = fuzzy_score(query, entry)
                if score is not None:
                    scored.append((-(score + entry.boost),
                                   _KIND_ORDER[entry.kind],
                                   entry.label.lower(), entry))
            scored.sort(key=lambda row: row[:3])
            hits = [row[3] for row in scored]
        self.lastSearchMs = (time.perf_counter() - started) * 1000.0
        return hits[:limit]


# --------------------------------------------------------------------------- #
# the three sources
# --------------------------------------------------------------------------- #

def _plain(text: str) -> str:
    return str(text or '').replace('&&', '\0').replace('&', '').replace(
        '\0', '&').replace('...', '').replace('…', '').strip()


def action_entries(menubar) -> list:
    """Every menu action, with the menu path as its detail.

    The menus are found with ``findChildren`` and paired with their own
    ``menuAction()``. ``QAction.menu()`` is never called: in PySide6 a menu
    reached that way and then dropped takes the real menu with it.
    """
    from PySide6.QtWidgets import QMenu
    if menubar is None:
        return []
    pairs = [(menu.menuAction(), menu) for menu in menubar.findChildren(QMenu)]
    holders = [pair[0] for pair in pairs]

    def menu_of(action):
        for holder, menu in pairs:
            if holder is action:
                return menu
        return None

    entries: list[PaletteEntry] = []
    walked: list = []
    trails: list = []

    def walk(menu, trail):
        trails.append((menu, trail))
        for action in menu.actions():
            if action.isSeparator():
                continue
            name = _plain(action.text())
            sub = menu_of(action) if action in holders else None
            if sub is not None:
                if any(sub is done for done in walked):
                    continue
                walked.append(sub)
                walk(sub, trail + [name] if name else trail)
                continue
            if not name:
                continue
            shortcut = action.shortcut().toString(
                QKeySequence.SequenceFormat.NativeText)
            detail = ' ▸ '.join(trail)
            if shortcut:
                detail = f'{detail}   {shortcut}' if detail else shortcut
            tip = action.toolTip()
            entries.append(PaletteEntry(
                KIND_ACTION, name, detail,
                keywords=tip if tip and tip != action.text() else '',
                enabled=action.isEnabled(),
                run=(action.trigger if action.isEnabled() else None),
                key=action.objectName()))

    for action in menubar.actions():
        menu = menu_of(action) if action in holders else None
        if menu is not None:
            walked.append(menu)
            walk(menu, [_plain(action.text())])
    # A store menu (Plan 43 LY13, DP-2245): a QMenu whose actions are shown
    # by buttons in a panel and which is not itself in any menu. It is
    # indexed under the menu it belongs to, so the path reads the same.
    for menu in pairs:
        menu = menu[1]
        if any(menu is done for done in walked):
            continue
        parent = menu.parent()
        for owner, trail in list(trails):
            if owner is parent:
                walked.append(menu)
                walk(menu, trail)
                break
    return entries


def step_entries(navigation, *, open_row=None) -> list:
    """Every outline row. ``open_row(index)`` opens one (default: the outline's own)."""
    if navigation is None:
        return []
    opener = open_row or navigation.activateRow
    entries = []
    import re
    for text, enabled, index in navigation.outlineRows():
        # The row's state glyph is drawn into its text; the name is the rest.
        text = re.sub(r'^[^\w]+', '', text) or text
        entries.append(PaletteEntry(
            KIND_STEP, text, 'Outline', enabled=enabled,
            run=(lambda i=index: opener(i)) if enabled else None,
            key='step:' + text))
    return entries


#: ``ui_location`` values that are not a page of their own.
_METHOD_PAGE = frozenset((
    'meshing_method', 'snappy_workflow', 'application.diagnostics',
    'preferences.execution', 'workflow.step'))
_PREPARATION = frozenset((
    'workflow.geometry_repair', 'workflow.reference_readiness'))

_LOCATION_LABEL = {
    'meshing_method': 'Mesh setup',
    'snappy_workflow': 'Mesh setup',
    'application.diagnostics': 'Mesh setup',
    'preferences.execution': 'Mesh setup',
    'workflow.step': 'Mesh setup',
    'workflow.geometry_repair': 'Preparation',
    'workflow.reference_readiness': 'Preparation',
    # Plan 42 SG11 (D4): asked on the Gmsh Edit geometry row.
    'workflow.gmsh.describe_geometry': 'Edit geometry',
    'workflow.gmsh': 'Size fields',
    # Plan 41 GN18: the volume controls are edited on the Structure row.
    'workflow.gmsh.volume_controls': 'Structure',
    'workflow.quality': 'Quality',
    # Plan 41 4.8.2: the card on Castellation and on the Gmsh Sizing row.
    'workflow.automatic_sizing': 'Automatic sizing',
    'workflow.domain_regions': 'Domain and regions',
    'workflow.base_grid': 'Base grid',
    'workflow.surface_features': 'Surface features',
    'workflow.castellation': 'Castellation',
    'workflow.snap': 'Snap',
    'workflow.layers': 'Layers',
    'workflow.gmsh.global_sizing': 'Sizing',
    'workflow.gmsh.boundary_layers': 'Layers',   # DP-1311: the step's name
    'workflow.gmsh.compute': 'Compute',
}


def location_label(location: str) -> str:
    return (_LOCATION_LABEL.get(location)
            or humanise_option(location.rsplit('.', 1)[-1]))


def route_for_location(location: str, tokens):
    """Where the field of ``location`` is edited: ``('token', t)``,
    ``('step', 'preparation')`` or ``None`` when this outline has no such page.
    """
    tokens = set(tokens or ())
    if location in _PREPARATION:
        return ('step', 'preparation')
    if location in _METHOD_PAGE:
        return ('token', 'meshing_method')
    prefix = 'engine_task:'
    rest = location[len('workflow.'):] if location.startswith(
        'workflow.') else location
    if location == 'workflow.gmsh':
        candidates = [prefix + 'gmsh.size_fields']
    elif rest == 'gmsh.describe_geometry':
        # Plan 42 SG11: a hosted task, asked on the Edit geometry row.
        candidates = [prefix + 'gmsh.geometry']
    elif rest.startswith('gmsh.'):
        candidates = [prefix + rest]
    elif rest == 'quality':
        candidates = [prefix + 'snappy.qa', prefix + 'gmsh.qa']
    elif rest == 'automatic_sizing':
        # Plan 41 4.8.2: one Automatic Sizing card for both routes, at the
        # top of Castellation (snappy) and of the Sizing row (Gmsh).
        candidates = [prefix + 'snappy.castellation',
                      prefix + 'gmsh.global_sizing']
    else:
        candidates = [prefix + 'snappy.' + rest]
    for candidate in candidates:
        if candidate in tokens:
            return ('token', candidate)
    return None


def field_entries(registry, *, go_to=None, tokens=()) -> list:
    """One entry per registry field.

    Once an engine's rows are in the outline, a field whose page this
    engine does not have is left out; before the choice every field stays.
    """
    engine_rows = any(str(t).startswith(('engine_task:snappy.',
                                         'engine_task:gmsh.'))
                      for t in tokens or ())
    entries = []
    for descriptor in registry.descriptors():
        where = location_label(descriptor.ui_location)
        words = descriptor.id.replace('.', ' ').replace('_', ' ')
        reachable = route_for_location(descriptor.ui_location, tokens)
        if not reachable and engine_rows:
            # Plan 42 SG19: the palette never lists the other engine's rows.
            continue
        detail = where if reachable or not tokens else (
            f'{where} (other engine)')
        entries.append(PaletteEntry(
            KIND_FIELD, descriptor.title or descriptor.id.rsplit('.', 1)[-1],
            detail, keywords=words,
            enabled=True,
            run=(lambda d=descriptor: go_to(d)) if go_to else None,
            key=descriptor.id))
    return entries


# --------------------------------------------------------------------------- #
# going to a field
# --------------------------------------------------------------------------- #

def _ancestors(widget):
    parent = widget.parentWidget()
    while parent is not None:
        yield parent
        parent = parent.parentWidget()


def _scroll_area_of(widget):
    for parent in _ancestors(widget):
        if isinstance(parent, QScrollArea):
            return parent
    return None


def _open_fold(widget) -> bool:
    for name in ('_advancedHeader', '_commonHeader'):
        header = getattr(widget, name, None)
        if header is not None and hasattr(header, 'isChecked')                 and not header.isChecked():
            if hasattr(widget, '_commonTouched'):
                widget._commonTouched = True
            header.setChecked(True)
            return True
    return False


def reveal_editor(editor, root) -> None:
    """Open whatever folds the field's row away: a filter, Expert, Advanced."""
    try:
        for ancestor in _ancestors(editor):
            filter_text = getattr(ancestor, 'filterText', None)
            if callable(filter_text) and filter_text():
                ancestor.setFilterText('')
        if editor.editor.isVisibleTo(root):
            return
        from foammesh.core.facade.field_tiers import EXPERT
        if editor.tier == EXPERT:
            from foammesh.view.workflow_controls.tiers import (
                expert_visibility)
            expert_visibility().setShown(True)
        if editor.editor.isVisibleTo(root):
            return
        for ancestor in _ancestors(editor):
            _open_fold(ancestor)
    except Exception:                                      # noqa: BLE001
        pass


def flash(widget, milliseconds: int = 900) -> QFrame | None:
    """A frame over ``widget`` that fades after ``milliseconds``."""
    if widget is None:
        return None
    overlay = QFrame(widget)
    overlay.setObjectName('paletteFlash')
    overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    style_palette_flash(overlay)
    overlay.setGeometry(widget.rect())
    overlay.raise_()
    overlay.show()
    QTimer.singleShot(milliseconds, overlay.deleteLater)
    return overlay


def find_editor(root: QWidget, field_id: str):
    """The visible editor of ``field_id`` under ``root``, or ``None``."""
    from foammesh.view.workflow_controls.field_widgets import FieldEditor
    for editor in root.findChildren(FieldEditor):
        if editor.field_id == field_id and editor.editor.isVisibleTo(root):
            return editor
    return None


def reveal_field(root: QWidget, field_id: str) -> bool:
    """Scroll to ``field_id`` on the page now showing, focus and flash it."""
    from foammesh.view.workflow_controls.field_widgets import FieldEditor
    editor = None
    for candidate in root.findChildren(FieldEditor):
        if candidate.field_id != field_id:
            continue
        reveal_editor(candidate, root)
        if candidate.editor.isVisibleTo(root):
            editor = candidate
            break
    if editor is None:
        return False
    area = _scroll_area_of(editor)
    if area is not None:
        area.ensureWidgetVisible(editor)
    editor.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
    flash(editor.editor)
    return True


class _FieldNavigator(QObject):
    """Route to a field's page, then keep looking until its row exists.

    A page opens asynchronously (the step manager awaits the page's own load),
    so the row may not exist on the first look; it is retried for two seconds.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self._window = window
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._look)
        self._field = ''
        self._left = 0

    def go(self, descriptor) -> bool:
        navigation = getattr(self._window, '_navigationView', None)
        if navigation is None:
            return False
        route = route_for_location(
            descriptor.ui_location, navigation.routeTokens())
        if route is None:
            bar = getattr(self._window, 'statusBar', None)
            if callable(bar):
                bar().showMessage(
                    f'{descriptor.title} is on a page this engine does not '
                    f'have.', 5000)
            return False
        kind, target = route
        if kind == 'token':
            navigation.requestBranch(target)
        else:
            from foammesh.db.configurations_schema import Step
            navigation.openStep(Step.GEOMETRY_REPAIR)
        self._field = descriptor.id
        self._left = 20
        self._timer.start()
        QTimer.singleShot(0, self._look)
        return True

    def _look(self) -> None:
        root = self._window
        stack = getattr(getattr(self._window, '_ui', None),
                        'content', None) or root
        if reveal_field(stack, self._field):
            self._timer.stop()
            return
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()


# --------------------------------------------------------------------------- #
# the popup
# --------------------------------------------------------------------------- #

class CommandPalette(QDialog):
    """Ctrl+K. A search box over a list; Enter chooses."""

    def __init__(self, index: CommandIndex, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('commandPalette')
        self.setAccessibleName('Command palette')
        self._index = index
        self._hits: list[PaletteEntry] = []
        self.chosen: PaletteEntry | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        self._input = QLineEdit(self)
        self._input.setObjectName('commandPaletteInput')
        self._input.setPlaceholderText(
            self.tr('Type a command, a step or a setting'))
        self._input.setAccessibleName(self.tr('Search commands'))
        self._input.setClearButtonEnabled(True)
        layout.addWidget(self._input)
        self._list = QListWidget(self)
        self._list.setObjectName('commandPaletteList')
        self._list.setAccessibleName(self.tr('Matches'))
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setUniformItemSizes(True)
        layout.addWidget(self._list, 1)
        self._note = QLabel(self)
        self._note.setObjectName('commandPaletteNote')
        layout.addWidget(self._note)
        self.setMinimumWidth(560)
        self.resize(600, 420)

        self._input.textChanged.connect(self.refresh)
        self._list.itemActivated.connect(lambda _item: self.choose())
        self._list.itemClicked.connect(lambda _item: self.choose())
        self._input.installEventFilter(self)
        self.refresh('')

    # -- reading ----------------------------------------------------------- #

    def hits(self) -> list:
        return list(self._hits)

    def text(self) -> str:
        return self._input.text()

    def setText(self, text: str) -> None:
        self._input.setText(text)

    @property
    def lastSearchMs(self) -> float:
        return self._index.lastSearchMs

    # -- acting ------------------------------------------------------------ #

    def refresh(self, query: str = '') -> None:
        self._hits = self._index.search(query)
        self._list.setUpdatesEnabled(False)
        self._list.clear()
        for entry in self._hits:
            text = f'{entry.label}    {entry.detail}'.rstrip()
            item = QListWidgetItem(
                f'{_KIND_WORD[entry.kind]}   {text}', self._list)
            if not entry.enabled:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(self.tr('Not available now'))
        self._list.setUpdatesEnabled(True)
        if self._hits:
            self._list.setCurrentRow(self._first_enabled())
        self._note.setText(
            self.tr('{0} matches').format(len(self._hits)) if query.strip()
            else self.tr('Enter opens the highlighted row. Esc closes.'))

    def _first_enabled(self) -> int:
        for row, entry in enumerate(self._hits):
            if entry.enabled:
                return row
        return 0

    def step(self, delta: int) -> None:
        count = self._list.count()
        if not count:
            return
        row = self._list.currentRow()
        for _ in range(count):
            row = (row + delta) % count
            if self._hits[row].enabled:
                break
        self._list.setCurrentRow(row)

    def choose(self) -> bool:
        row = self._list.currentRow()
        if not 0 <= row < len(self._hits):
            return False
        entry = self._hits[row]
        if not entry.enabled or entry.run is None:
            return False
        self.chosen = entry
        self.accept()
        QTimer.singleShot(0, entry.run)
        return True

    def eventFilter(self, watched, event):
        if watched is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self.step(1)
                return True
            if key == Qt.Key.Key_Up:
                self.step(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.choose()
                return True
            if key == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:
            top = parent.mapToGlobal(parent.rect().topLeft())
            self.move_to(top.x() + (parent.width() - self.width()) // 2,
                         top.y() + 80)
        self._input.setFocus()
        self._input.selectAll()

    def move_to(self, x: int, y: int) -> None:
        QDialog.move(self, x, y)


# --------------------------------------------------------------------------- #
# the window's side
# --------------------------------------------------------------------------- #

class PaletteController(QObject):
    """Owns the Ctrl+K shortcut and builds a fresh index each time it opens."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._navigator = _FieldNavigator(window, self)
        self._open: CommandPalette | None = None
        self._shortcut = QShortcut(QKeySequence('Ctrl+K'), window)
        self._shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._shortcut.activated.connect(self.show)
        # Plan 41 GN18 (I-05). Gmsh's own keys for the windows it has and we
        # have as tiers; they do nothing off the Gmsh route.
        from foammesh.view.gmsh_workflow.command_entries import (
            install_view_shortcuts, install_window_shortcuts,
            sync_window_shortcuts)
        self._gmshKeys = install_window_shortcuts(window)
        self._viewKeys = install_view_shortcuts(window)
        self._syncKeys = lambda *_: sync_window_shortcuts(
            window, self._gmshKeys)
        try:
            from foammesh.view.engine_context import engine_context
            engine_context().engineChanged.connect(self._resync)
        except Exception:                                    # noqa: BLE001
            pass

    def _resync(self, *_engine) -> None:
        # The Gmsh pages are built after the engine is chosen.
        self._syncKeys()
        QTimer.singleShot(0, self._syncKeys)
        QTimer.singleShot(500, self._syncKeys)

    def buildIndex(self) -> CommandIndex:
        from foammesh.core.facade.fields import REGISTRY
        window = self._window
        navigation = getattr(window, '_navigationView', None)
        ui = getattr(window, '_ui', None)
        menubar = getattr(ui, 'menubar', None) or window.menuBar()
        tokens = navigation.routeTokens() if navigation is not None else ()
        index = CommandIndex()
        self._syncKeys()
        index.add(action_entries(menubar))
        index.add(step_entries(navigation))
        index.add(field_entries(
            REGISTRY, go_to=self._navigator.go, tokens=tokens))
        # Plan 41 GN18 (I-05). Every Gmsh action, found by name.
        from foammesh.view.gmsh_workflow.command_entries import window_entries
        index.add(window_entries(window))
        return index

    def show(self) -> CommandPalette:
        if self._open is not None:
            try:
                self._open.close()
            except RuntimeError:
                pass
        palette = CommandPalette(self.buildIndex(), self._window)
        palette.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._open = palette
        palette.show()
        return palette


def install(window) -> PaletteController:
    """Give ``window`` its Ctrl+K palette."""
    return PaletteController(window)


__all__ = ['CommandIndex', 'CommandPalette', 'PaletteController',
           'PaletteEntry', 'action_entries', 'field_entries', 'fuzzy_score',
           'install', 'reveal_field', 'route_for_location', 'step_entries']
