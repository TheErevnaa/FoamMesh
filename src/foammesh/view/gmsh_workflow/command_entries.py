"""Ctrl+K entries and keyboard shortcuts for the Gmsh route (Plan 41 GN18, I-05).

Every action the Gmsh pages offer is found by name in the command palette:

* each Generate action of ``core/gmsh/generate_actions.py`` (1D, 2D, 3D,
  optimise, order, refine, partition, ...), chosen on the Generate panel,
* every button on a Gmsh task page (Apply, Pick, Measure, Preview, Merge, ...),
  read off the pages themselves so a button added later is found without a
  list to keep in step.

The index and the shortcut table know nothing of the window, so a test builds
them over a page list.  Choosing an entry opens the page that owns it first;
it never presses a button that is off, and an entry never starts a job: it
selects the action and puts the focus on Run.  The digit shortcuts do start the
action, as Gmsh's own keys do.

Shortcuts follow Gmsh's own keys where they do not collide with a typed
character or with a key the window already owns:

=====  ===========================  =====================================
Key    Gmsh                         Here
=====  ===========================  =====================================
1      Mesh 1D                      Generate 1D (the Compute page shown)
2      Mesh 2D                      Generate 2D
3      Mesh 3D                      Generate 3D
=====  ===========================  =====================================

F2 and F3 are Gmsh's second names for 2 and 3.  Escape (abort) is not a
palette shortcut: the window's focus-mode exit and every open editor already
own it.  The Generate panel takes it only while one of its runs is going, and
there it aborts the run (Plan 41 I-06); the footer has Cancel too.  F1 (help) stays the viewport cheat sheet.  The digit keys
are shortcuts of the Compute page, so they work only while that page is on
screen, and a digit typed into a field still goes to the field (Qt offers the
key to a focused text box first).

The rest of Gmsh's keys are in ``gmsh_keys.py``, one row each: bound where the
same act exists on this route (the Options, Statistics, Plugins, Visibility
and Merge windows open the tier that holds them, Alt+X/Y/Z turn the view), and
unbound with the reason where it does not or where the window owns the key.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractButton

from foammesh.core.gmsh import generate_actions as actions
from foammesh.core.naming import humanise_option
from foammesh.view.main_window.command_palette import KIND_ACTION, PaletteEntry

#: Gmsh's keys for the Mesh module's first three buttons -> our action id.
GMSH_SHORTCUTS = {'1': 'generate1d', '2': 'generate2d', '3': 'generate3d',
                  'F2': 'generate2d', 'F3': 'generate3d'}

#: The page the Generate actions live on.
GENERATE_TASK = 'gmsh.compute'

#: Buttons that only fold a tier open or shut say so by being checkable; they
#: are still actions ("Mesh statistics"), listed under the page they sit on.
_SKIP_OBJECT_NAMES = frozenset(('gmshGenerateRun', 'gmshGenerateCancel'))


#: What a palette entry says when its page is result-locked (Plan 44 MF18).
LOCKED_REASON = 'Locked — unlock to edit'

#: Plan 44 FX3 (DP-2624). The page on screen ranks its own entries first:
#: every Gmsh page has an "Unlock to edit" chip, and with equal scores the
#: first page built (Describe geometry) won, so the palette sent a locked
#: Quality page's unlock to another page.
CURRENT_PAGE_BOOST = 25.0
#: The detail's mark on the page on screen.
THIS_PAGE = 'this page'
#: The unlock chip's object name on a task page (MF18; the facade's id).
UNLOCK_CHIP = 'engineTaskUnlock'


def page_read_only(page) -> bool:
    """Whether *page* is held read-only by its result lock (Plan 44 MF18)."""
    read_only = getattr(page, 'isReadOnly', None)
    try:
        return bool(callable(read_only) and read_only())
    except Exception:                                        # noqa: BLE001
        return False


def held_read_only(page, button) -> bool:
    """Whether *button* on *page* is one its read-only lock refuses.

    A locked page keeps its buttons enabled (MF18 B10: read-only, not greyed)
    and a guard refuses their presses; the unlock chip, the disclosures and
    the Details link stay live, so they are not held. A page that cannot name
    what it holds is taken to hold every button but the unlock chip.
    """
    if not page_read_only(page):
        return False
    guard = getattr(page, '_readOnly', None)
    held = getattr(guard, 'heldWidgets', None)
    if callable(held):
        try:
            return any(widget is button for widget in held())
        except Exception:                                    # noqa: BLE001
            pass
    return not str(button.objectName()).startswith('engineTaskUnlock')


def _plain(text: str) -> str:
    return str(text or '').replace('&&', '\0').replace('&', '').replace(
        '\0', '&').replace('...', '').replace('…', '').strip()


def generate_entries(open_generate, locked: bool = False) -> list:
    """One entry per Generate action; *open_generate(action_id)* shows it.

    *locked*: the Compute page is result-locked, so choosing an action (which
    sets the page's action) is refused and every entry says why.
    """
    entries = []
    for item in actions.ACTIONS.values():
        detail = f'Gmsh ▸ Generate ▸ {item.group}'
        entries.append(PaletteEntry(
            KIND_ACTION, item.title,
            f'{detail} · {LOCKED_REASON}' if locked else detail,
            keywords=f'mesh generate {item.help}',
            enabled=not locked,
            run=None if locked else (lambda i=item.id: open_generate(i)),
            key='gmsh.action:' + item.id))
    return entries


def button_entries(pages, open_page, current: str = '') -> list:
    """One entry per labelled button on the Gmsh *pages* (``{task_id: page}``).

    *current* is the task whose page is on screen: its entries rank first
    and say "this page" (DP-2624).

    *open_page(task_id, button)* brings the page forward and focuses the
    button.  A disabled button is listed, greyed, with no action.  So is a
    button its result-locked page refuses (Plan 44 MF18): it stays enabled on
    the read-only page, and the entry says "Locked — unlock to edit".
    """
    entries = []
    for task_id, page in pages.items():
        title = getattr(page, 'windowTitle', lambda: '')() or task_id
        seen = set()
        for button in page.findChildren(QAbstractButton):
            name = _plain(button.text())
            if not name or button.objectName() in _SKIP_OBJECT_NAMES:
                continue
            key = f'gmsh.button:{task_id}:{button.objectName() or name}'
            if key in seen:
                continue
            seen.add(key)
            detail = f'Gmsh ▸ {_task_title(task_id, title)}'
            here = bool(current) and task_id == current
            if here:
                detail = f'{detail} · {THIS_PAGE}'
            locked = held_read_only(page, button)
            usable = button.isEnabled() and not locked
            entries.append(PaletteEntry(
                KIND_ACTION, name,
                f'{detail} · {LOCKED_REASON}' if locked else detail,
                keywords=button.toolTip() or button.accessibleDescription(),
                enabled=usable,
                run=(lambda t=task_id, b=button: open_page(t, b))
                if usable else None,
                key=key, boost=CURRENT_PAGE_BOOST if here else 0.0))
    return entries


def current_task(pages) -> str:
    """The task of the Gmsh page on screen, ``''`` when none is."""
    for task_id, page in pages.items():
        try:
            if page.isVisible():
                return task_id
        except RuntimeError:
            continue
    return ''


def is_unlock_chip(button) -> bool:
    """Whether *button* is a task page's result-lock chip."""
    return str(button.objectName()).startswith(UNLOCK_CHIP)


def press_unlock_chip(button) -> bool:
    """Do what pressing the locked chip does: its inline confirm (MF18 K2).

    A chip that is not locked (it reads "Lock", or is asking already) is
    only focused: the palette never locks a page or confirms an unlock.
    """
    chip = button.parent()  # the LockChip that owns the push button
    state = getattr(chip, 'state', None)
    try:
        locked = callable(state) and state() == 'locked'
    except Exception:                                        # noqa: BLE001
        locked = False
    if locked and button.isEnabled():
        button.click()
    if button.isEnabled():
        button.setFocus(Qt.FocusReason.ShortcutFocusReason)
    return locked


def _task_title(task_id: str, title: str) -> str:
    if title and title != task_id:
        return title
    return humanise_option(task_id.split('.', 1)[-1])


def shortcut_table() -> list:
    """``[(key, action id, title)]`` for the help text and the tests."""
    return [(key, action_id, actions.action(action_id).title)
            for key, action_id in GMSH_SHORTCUTS.items()]


def install_shortcuts(page, run_action) -> list:
    """Give *page* Gmsh's digit keys; *run_action(action_id)* is what they do.

    The shortcuts belong to *page*, so they exist only while it is visible.
    """
    made = []
    for key, action_id in GMSH_SHORTCUTS.items():
        shortcut = QShortcut(QKeySequence(key), page)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setObjectName('gmshShortcut' + key)
        shortcut.activated.connect(lambda a=action_id: run_action(a))
        made.append(shortcut)
    return made


# --------------------------------------------------------------------------- #
# the window's side
# --------------------------------------------------------------------------- #

def gmsh_pages(window) -> dict:
    """``{task_id: page}`` for the Gmsh pages of the open engine branch."""
    from foammesh.view.main_window.engine_branch import EngineBranchView

    found = {}
    for branch in window.findChildren(EngineBranchView):
        for task_id, page in (getattr(branch, '_pages', None) or {}).items():
            if task_id.startswith('gmsh.') or task_id == 'common.export':
                found[task_id] = page
    return found


def window_entries(window) -> list:
    """Every Gmsh action entry for the palette of *window* (``[]`` off Gmsh)."""
    pages = gmsh_pages(window)
    if not pages:
        return []
    navigation = getattr(window, '_navigationView', None)

    def show(task_id):
        if navigation is not None:
            navigation.requestBranch('engine_task:' + task_id)

    def open_generate(action_id):
        show(GENERATE_TASK)
        compute = pages.get(GENERATE_TASK)
        select = getattr(compute, 'chooseGenerateAction', None)
        if select is not None:
            select(action_id)

    def open_page(task_id, button):
        show(task_id)
        if is_unlock_chip(button):
            press_unlock_chip(button)
            return
        if button.isEnabled():
            button.setFocus(Qt.FocusReason.ShortcutFocusReason)

    entries = button_entries(pages, open_page, current_task(pages))
    if GENERATE_TASK in pages:
        entries = generate_entries(
            open_generate, page_read_only(pages[GENERATE_TASK])) + entries
    return entries


# --------------------------------------------------------------------------- #
# the window-wide keys of gmsh_keys.py
# --------------------------------------------------------------------------- #

def _show_row(window, task_id: str):
    """Bring the Gmsh row *task_id* forward; its page, or ``None``."""
    pages = gmsh_pages(window)
    page = pages.get(task_id)
    if page is None:
        return None
    navigation = getattr(window, '_navigationView', None)
    if navigation is not None:
        navigation.requestBranch('engine_task:' + task_id)
    return page


def activate_key(window, binding) -> bool:
    """Do what *binding* (a ``gmsh_keys`` tuple) says; False when it cannot.

    Nothing happens, and nothing is stolen, on a case that is not on the Gmsh
    route: the pages are not there.
    """
    from PySide6.QtWidgets import QPushButton
    from . import gmsh_keys as keys

    kind = binding[0]
    if kind == keys.VIEW:
        display = getattr(window, 'displayControl', None)
        view = getattr(display, 'view', None)
        view = view() if callable(view) else None
        if view is None:
            return False
        view.setViewPreset(binding[1])
        return True
    if kind == keys.COLOUR:
        # Plan 43 LY12: steps the session's scale; the standard is untouched.
        from foammesh.support import colour_standard
        colour_standard.step_scale()
        return True
    if kind == keys.QUALITY:
        if not gmsh_route_active(window):
            return False
        show = getattr(window, 'showQualityReport', None)
        if not callable(show):
            return False
        show()
        return True
    if kind == keys.TIER and binding[2] == keys.DISPLAY:
        # Plan 43 LY13 D11: the Visibility and display options are in the
        # side rail's Display popover, not on a page.
        rail = getattr(window, '_viewportRail', None)
        if rail is None or not gmsh_route_active(window):
            return False
        rail.open('display')
        return True
    page = _show_row(window, binding[1])
    if page is None:
        return False
    if kind == keys.TIER:
        header = page.findChild(QPushButton, binding[2])
        if header is None:
            return False
        header.setChecked(True)
        header.setFocus(Qt.FocusReason.ShortcutFocusReason)
    return True


def gmsh_route_active(window) -> bool:
    """True while the open case is on the Gmsh route (its pages exist)."""
    return bool(gmsh_pages(window))


def sync_window_shortcuts(window, shortcuts) -> bool:
    """Enable the Gmsh-only *shortcuts* while Gmsh is the engine, else disable.

    An enabled QShortcut swallows its key even when the handler does
    nothing, so off the Gmsh route they must be disabled (Plan 42 SG19).
    """
    active = gmsh_route_active(window)
    for shortcut in shortcuts:
        shortcut.setEnabled(active)
    return active


def _make_keys(window, bindings) -> list:
    from PySide6.QtWidgets import QWidget

    if not isinstance(window, QWidget):
        return []
    made = []
    for key, binding in bindings:
        shortcut = QShortcut(QKeySequence(key), window)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setObjectName('gmshKey' + key)
        shortcut.activated.connect(
            lambda b=binding: activate_key(window, b))
        made.append(shortcut)
    return made


def install_window_shortcuts(window) -> list:
    """Bind every Gmsh-only key of ``gmsh_keys`` that opens a row or tier.

    The generate keys (1, 2, 3, F2, F3) belong to the Generate page, and the
    view keys (Alt+X/Y/Z) are shared (``install_view_shortcuts``).  These
    belong to the window, are disabled until the Gmsh route is open
    (``sync_window_shortcuts``), and so never swallow a key on snappy.
    """
    from . import gmsh_keys as keys

    made = _make_keys(window, [
        (key, binding) for key, binding in keys.bound().items()
        if binding[0] not in (keys.GENERATE, keys.VIEW, keys.COLOUR)])
    sync_window_shortcuts(window, made)
    return made


def install_view_shortcuts(window) -> list:
    """Alt+X/Y/Z and Alt+Shift+X/Y/Z: standard views on either engine."""
    from . import gmsh_keys as keys

    return _make_keys(window, [
        (key, binding) for key, binding in keys.bound().items()
        if binding[0] in (keys.VIEW, keys.COLOUR)])
