"""Plan 40 UXE1 (F7b). One table of commands and keys.

Every keyboard shortcut of the window is a row of ``COMMANDS``: an id, the
words the user reads, the group it is listed under, its default keys and the
scope it is active in.  The code that makes a shortcut (a ``QAction``, a
``QShortcut`` or a button) *adopts* it under its id; the registry then gives
it the keys the user chose, and moves it again when the user rebinds it.

* ``keys(id)`` -- the keys now in effect (the user's, else the default).
* ``assign(id, keys)`` -- rebind; refuses a key that another command in an
  overlapping scope already has, and says which command.
* ``conflicts()`` -- every clash among the keys in effect; empty by design.
* ``sheet_html()`` -- the printable sheet.

Overrides are kept in the settings file (``AppSettings.getShortcuts``).  A
key that was never rebound is not stored, so a later release can change a
default without the user's file pinning the old one.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Callable, Iterable

from PySide6.QtGui import QKeySequence

PORTABLE = QKeySequence.SequenceFormat.PortableText

#: Scopes.  ``window`` keys work anywhere in the main window; the others work
#: only where their widget or page is on screen.
WINDOW = 'window'
VIEWPORT = 'viewport'
PLANE = 'plane'
CONSOLE = 'console'
REGIONS = 'regions'
DETECTION = 'detection'
GMSH = 'gmsh'
#: Esc leaves focus mode; the shortcut is live only in focus mode, when the
#: console is hidden.
FOCUS = 'focus'

#: Scopes that can never be live at the same moment, so one key may mean two
#: things in them.  Everything else overlaps, ``window`` with all.
_APART = (
    {CONSOLE, VIEWPORT}, {CONSOLE, PLANE},
    {REGIONS, DETECTION}, {REGIONS, GMSH}, {DETECTION, GMSH},
    {FOCUS, CONSOLE},
)

GROUP_FILE = 'File'
GROUP_EDIT = 'Edit'
GROUP_VIEW = 'View'
GROUP_VIEWPORT = 'Viewport'
GROUP_PLANE = 'Cut plane'
GROUP_CONSOLE = 'Console'
GROUP_REGIONS = 'Regions'
GROUP_GMSH = 'Gmsh route'
GROUP_HELP = 'Help'
GROUPS = (GROUP_FILE, GROUP_EDIT, GROUP_VIEW, GROUP_VIEWPORT, GROUP_PLANE,
          GROUP_CONSOLE, GROUP_REGIONS, GROUP_GMSH, GROUP_HELP)


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    group: str
    keys: tuple
    scope: str = WINDOW


def _c(id, title, group, keys, scope=WINDOW):
    if isinstance(keys, str):
        keys = (keys,)
    return Command(id, title, group, tuple(keys), scope)


def _gmsh_commands() -> list:
    """The window keys of the Gmsh route, one command each (Plan 41 GN18)."""
    # gmsh_keys is plain data; read it by path so that importing the registry
    # never starts the Gmsh pages (and their imports) as a side effect.
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent / 'gmsh_workflow'
            / 'gmsh_keys.py')
    if path.is_file():
        spec = importlib.util.spec_from_file_location(
            'foammesh_shortcut_registry_gmsh_keys', path)
        gmsh_keys = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gmsh_keys)
    else:
        # DP-2726. A frozen build keeps no .py files on disk, so the path
        # read stopped the packaged app at start-up; it imports the module.
        import importlib

        gmsh_keys = importlib.import_module(
            'foammesh.view.gmsh_workflow.gmsh_keys')

    made = []
    for key, what, binding in gmsh_keys.KEYS:
        if not isinstance(binding, tuple) or binding[0] == gmsh_keys.GENERATE:
            continue
        made.append(_c('gmsh.key.' + key, f'Gmsh: {what}', GROUP_GMSH, key,
                       GMSH))
    return made


def _build() -> tuple:
    rows = [
        _c('file.new', 'New project', GROUP_FILE, 'Ctrl+N'),
        _c('file.open', 'Open project', GROUP_FILE, 'Ctrl+O'),
        _c('file.save', 'Save project', GROUP_FILE, 'Ctrl+S'),
        _c('file.saveAs', 'Save project as', GROUP_FILE, 'Ctrl+Shift+S'),
        _c('file.close', 'Close project', GROUP_FILE, ('Ctrl+W', 'Ctrl+E')),
        _c('file.exit', 'Exit', GROUP_FILE, 'Ctrl+Q'),
        _c('file.preferences', 'Preferences', GROUP_FILE, 'Ctrl+,'),
        _c('edit.undo', 'Undo', GROUP_EDIT, 'Ctrl+Z'),
        _c('edit.redo', 'Redo', GROUP_EDIT, ('Ctrl+Shift+Z', 'Ctrl+Y')),
        _c('view.palette', 'Find an action, step or setting', GROUP_VIEW,
           'Ctrl+K'),
        _c('view.outputBand', 'Output panel', GROUP_VIEW, 'Ctrl+J'),
        _c('view.console', 'Console', GROUP_VIEW, 'Ctrl+Shift+C'),
        _c('view.consoleMinimise', 'Minimise the console', GROUP_VIEW,
           'Ctrl+`'),
        _c('view.standardLayout', 'Standard layout', GROUP_VIEW, 'Ctrl+0'),
        _c('view.outlineRail', 'Step ribbon: full labels or numbers only',
           GROUP_VIEW, 'Ctrl+B'),
        _c('view.stepPrevious', 'Previous step', GROUP_VIEW, 'Ctrl+PgUp'),
        _c('view.stepNext', 'Next step', GROUP_VIEW, 'Ctrl+PgDown'),
        _c('view.focusMode', 'Focus mode', GROUP_VIEW, 'F11'),
        # Plan 43 LY8. The keys of the grouped View menu.
        _c('view.minimiseSheet', 'Minimise the settings sheet', GROUP_VIEW,
           'Ctrl+\\'),
        _c('view.pinTools', 'Pin or unpin all three tool edges', GROUP_VIEW,
           'Ctrl+H'),
        _c('view.focusExit', 'Leave focus mode', GROUP_VIEW, 'Esc', FOCUS),
    ]
    presets = (('View +X', '+x', 'Alt+1'), ('View -X', '-x', 'Alt+2'),
               ('View +Y', '+y', 'Alt+3'), ('View -Y', '-y', 'Alt+4'),
               ('View +Z', '+z', 'Alt+5'), ('View -Z', '-z', 'Alt+6'),
               ('Isometric View', 'isometric', 'Alt+7'))
    for title, preset, key in presets:
        rows.append(_c('viewport.view.' + preset, title, GROUP_VIEWPORT, key,
                       VIEWPORT))
    rows += [
        _c('viewport.capture', 'Capture viewport', GROUP_VIEWPORT,
           'Ctrl+Shift+P', VIEWPORT),
        _c('viewport.zoomSelection', 'Zoom to selection', GROUP_VIEWPORT,
           'Ctrl+Shift+F', VIEWPORT),
        _c('viewport.fit', 'Fit the selection, or everything',
           GROUP_VIEWPORT, 'F', VIEWPORT),
        _c('help.shortcuts', 'Keyboard shortcuts', GROUP_HELP, 'F1'),
        _c('plane.up', 'Push the cut plane forward', GROUP_PLANE, 'PgUp',
           PLANE),
        _c('plane.down', 'Pull the cut plane back', GROUP_PLANE, 'PgDown',
           PLANE),
        _c('plane.upFine', 'Push the cut plane forward, fine', GROUP_PLANE,
           'Shift+PgUp', PLANE),
        _c('plane.downFine', 'Pull the cut plane back, fine', GROUP_PLANE,
           'Shift+PgDown', PLANE),
        _c('plane.handle', 'Hide or show the plane handle', GROUP_PLANE, 'H',
           PLANE),
        _c('console.find', 'Search the console', GROUP_CONSOLE, 'Ctrl+F',
           CONSOLE),
        _c('console.findNext', 'Next match', GROUP_CONSOLE, 'Ctrl+G', CONSOLE),
        _c('console.findPrevious', 'Previous match', GROUP_CONSOLE,
           'Ctrl+Shift+G', CONSOLE),
        _c('console.findClose', 'Close the search bar', GROUP_CONSOLE, 'Esc',
           CONSOLE),
        _c('regions.detect', 'Detect regions', GROUP_REGIONS, 'Alt+T',
           REGIONS),
        _c('regions.merge', 'Merge the selected region', GROUP_REGIONS,
           'Alt+G', REGIONS),
        _c('regions.section', 'Section through the seed', GROUP_REGIONS,
           'Alt+O', REGIONS),
        _c('detection.adjust', 'Adjust the detected row', GROUP_REGIONS,
           'Alt+J', DETECTION),
        _c('detection.merge', 'Merge two detected rows', GROUP_REGIONS,
           'Alt+G', DETECTION),
        _c('detection.accept', 'Accept all detected regions', GROUP_REGIONS,
           'Ctrl+Return', DETECTION),
        _c('gmsh.generate1d', 'Gmsh: mesh lines (1D)', GROUP_GMSH, '1', GMSH),
        _c('gmsh.generate2d', 'Gmsh: mesh surfaces (2D)', GROUP_GMSH,
           '2', GMSH),
        _c('gmsh.generate2dF', 'Gmsh: mesh surfaces (2D), function key',
           GROUP_GMSH, 'F2', GMSH),
        _c('gmsh.generate3d', 'Gmsh: mesh volumes (3D)', GROUP_GMSH, '3',
           GMSH),
        _c('gmsh.generate3dF', 'Gmsh: mesh volumes (3D), function key',
           GROUP_GMSH, 'F3', GMSH),
    ]
    rows += _gmsh_commands()
    return tuple(rows)


COMMANDS: tuple = _build()
BY_ID = {command.id: command for command in COMMANDS}

#: Keys that do their job inside a widget, as that widget's own behaviour. They
#: are listed on the sheet but are not commands and cannot be rebound.
FIXED_KEYS = (
    ('Step ribbon', 'Left / Right', 'Move between steps'),
    ('Step ribbon', 'Enter / Space', 'Open the step under the cursor'),
    ('Step ribbon', '?', 'What the marks beside each step mean'),
    ('Find palette', 'Up / Down / Enter', 'Move through the results and open one'),
    ('Find palette', 'Esc', 'Close the palette'),
    ('Viewport tools', 'Esc', 'Close an open tool popover'),
    ('Regions list', 'Ctrl+Z / Ctrl+Y', 'Undo and redo while the list has focus'),
)


def normalise(key) -> str:
    """The portable text of one key chord; '' when it is not a key."""
    if isinstance(key, QKeySequence):
        return key.toString(PORTABLE)
    return QKeySequence(str(key)).toString(PORTABLE)


def _normalise_all(keys) -> tuple:
    out = []
    for key in keys:
        text = normalise(key)
        if text and text not in out:
            out.append(text)
    return tuple(out)


def scopes_overlap(first: str, second: str) -> bool:
    return not ({first, second} in _APART) or first == second


class ShortcutRegistry:
    def __init__(self):
        self._settings = None
        self._overrides: dict = {}
        self._live: dict = {}
        self._observers: list = []
        #: ``[(command id, keys the site had, keys the table lists)]`` for an
        #: adoption whose code default differs from the table.
        self.mismatches: list = []

    # ---- persistence -----------------------------------------------------

    def attach(self, settings) -> None:
        """Read the user's overrides from *settings* (an ``AppSettings``)."""
        self._settings = settings
        stored = {}
        try:
            stored = settings.getShortcuts() if settings is not None else {}
        except Exception:
            stored = {}
        self._overrides = {}
        for command_id, keys in (stored or {}).items():
            if command_id in BY_ID and isinstance(keys, (list, tuple)):
                self._overrides[command_id] = _normalise_all(keys)
        self._applyAll()

    def detach(self) -> None:
        self._settings = None
        self._overrides = {}
        self._applyAll()

    def _save(self) -> None:
        if self._settings is None:
            return
        try:
            self._settings.updateShortcuts(
                {key: list(value) for key, value in self._overrides.items()})
        except Exception:
            pass

    # ---- reading ---------------------------------------------------------

    def command(self, command_id: str) -> Command:
        return BY_ID[command_id]

    def commands(self) -> tuple:
        return COMMANDS

    def default(self, command_id: str) -> tuple:
        return _normalise_all(BY_ID[command_id].keys)

    def keys(self, command_id: str) -> tuple:
        if command_id in self._overrides:
            return self._overrides[command_id]
        return self.default(command_id)

    def isChanged(self, command_id: str) -> bool:
        return command_id in self._overrides

    def display(self, command_id: str) -> str:
        """The keys in the user's own words ('Ctrl+W, Ctrl+E')."""
        return ', '.join(
            QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)
            for key in self.keys(command_id))

    # ---- conflicts -------------------------------------------------------

    def holders(self, key: str, scope: str, skip: str = '') -> list:
        """Commands that have *key* in effect in a scope overlapping *scope*."""
        wanted = normalise(key)
        found = []
        for command in COMMANDS:
            if command.id == skip or not scopes_overlap(command.scope, scope):
                continue
            if wanted in self.keys(command.id):
                found.append(command)
        return found

    def conflictsFor(self, command_id: str, keys: Iterable) -> list:
        """``[(key, other command)]`` that *keys* would clash with."""
        scope = BY_ID[command_id].scope
        clashes = []
        for key in _normalise_all(keys):
            for other in self.holders(key, scope, skip=command_id):
                clashes.append((key, other))
        return clashes

    def conflicts(self) -> list:
        """Every clash in effect: ``[(key, first id, second id)]``."""
        seen = []
        for index, first in enumerate(COMMANDS):
            for second in COMMANDS[index + 1:]:
                if not scopes_overlap(first.scope, second.scope):
                    continue
                for key in self.keys(first.id):
                    if key in self.keys(second.id):
                        seen.append((key, first.id, second.id))
        return seen

    # ---- writing ---------------------------------------------------------

    def assign(self, command_id: str, keys) -> tuple:
        """Rebind a command.  Returns ``(True, '')`` or ``(False, why)``.

        Nothing changes when a key is taken: the message names the command
        that has it.  An empty list unbinds the command.
        """
        if command_id not in BY_ID:
            return False, f'There is no command {command_id!r}.'
        if isinstance(keys, str):
            keys = (keys,)
        wanted = _normalise_all(keys)
        if len(wanted) != len([k for k in keys if str(k).strip()]):
            return False, 'That is not a key the keyboard can make.'
        clashes = self.conflictsFor(command_id, wanted)
        if clashes:
            key, other = clashes[0]
            shown = QKeySequence(key).toString(
                QKeySequence.SequenceFormat.NativeText)
            return False, (f'{shown} already belongs to "{other.title}". '
                           f'Give that one another key first.')
        if wanted == self.default(command_id):
            self._overrides.pop(command_id, None)
        else:
            self._overrides[command_id] = wanted
        self._save()
        self._apply(command_id)
        self._notify(command_id)
        return True, ''

    def reset(self, command_id: str) -> None:
        if self._overrides.pop(command_id, None) is not None:
            self._save()
            self._apply(command_id)
            self._notify(command_id)

    def resetAll(self) -> None:
        changed = list(self._overrides)
        self._overrides = {}
        self._save()
        for command_id in changed:
            self._apply(command_id)
        self._notify('')

    def subscribe(self, callback: Callable) -> Callable:
        self._observers.append(callback)
        return lambda: self._observers.remove(callback)

    def _notify(self, command_id: str) -> None:
        for callback in list(self._observers):
            try:
                callback(command_id)
            except Exception:
                pass

    # ---- the live objects ------------------------------------------------

    def adopt(self, command_id: str, target):
        """Put *target* (a QAction, QShortcut or button) under the registry.

        What the site gave it is compared with the table, so a default that
        drifts from the table is caught by a test, and then the user's keys
        replace it.  Returns *target*.
        """
        seen = _normalise_all(_target_keys(target))
        listed = self.default(command_id)
        wanted = listed[:1] if _is_shortcut(target) else listed
        if seen != wanted:
            self.mismatches.append((command_id, seen, listed))
        self._live.setdefault(command_id, []).append(target)
        self._applyTo(command_id, target)
        return target

    def adopted(self, target) -> bool:
        return any(target is other for objects in self._live.values()
                   for other in objects)

    def liveObjects(self, command_id: str) -> list:
        return [t for t in self._live.get(command_id, []) if _alive(t)]

    def _apply(self, command_id: str) -> None:
        self._live[command_id] = self.liveObjects(command_id)
        for target in self._live[command_id]:
            self._applyTo(command_id, target)

    def _applyAll(self) -> None:
        for command_id in list(self._live):
            self._apply(command_id)

    def _applyTo(self, command_id: str, target) -> None:
        keys = self.keys(command_id)
        sequences = [QKeySequence(key) for key in keys]
        if _is_shortcut(target):
            target.setKey(sequences[0] if sequences else QKeySequence())
        elif hasattr(target, 'setShortcuts'):
            target.setShortcuts(sequences)
        else:
            target.setShortcut(sequences[0] if sequences else QKeySequence())

    #: Shortcuts made by code that does not call ``adopt`` itself are found by
    #: the name they were given: ``(prefix, id for a key)``.
    _NAMED = {'gmshKey': lambda key: 'gmsh.key.' + key,
              'gmshShortcut': lambda key: _GENERATE_ID.get(key, '')}

    def sweep(self, root) -> int:
        """Adopt every named shortcut under *root* not adopted yet.

        The Gmsh route makes its window keys and its Generate keys itself
        (Plan 41 GN18), as ``QShortcut`` objects named ``gmshKey<key>`` and
        ``gmshShortcut<key>``; the pages are built when an engine is chosen,
        so this runs when the window is told something changed. Returns how
        many were adopted.
        """
        from PySide6.QtGui import QShortcut

        taken = 0
        for shortcut in root.findChildren(QShortcut):
            if self.adopted(shortcut):
                continue
            name = shortcut.objectName()
            for prefix, to_id in self._NAMED.items():
                if name.startswith(prefix):
                    command_id = to_id(name[len(prefix):])
                    if command_id in BY_ID:
                        self.adopt(command_id, shortcut)
                        taken += 1
                    break
        return taken

    # ---- the printable sheet --------------------------------------------

    def rows(self) -> list:
        """``[(group, title, keys text, where)]`` for the dialog and sheet."""
        out = []
        for group in GROUPS:
            for command in COMMANDS:
                if command.group == group:
                    out.append((group, command.title,
                                self.display(command.id) or 'not set',
                                _WHERE.get(command.scope, '')))
        for where, key, what in FIXED_KEYS:
            out.append((where, what, key, 'fixed'))
        return out

    def sheet_html(self) -> str:
        parts = ['<html><head><meta charset="utf-8"><style>'
                 'body{font-family:sans-serif;font-size:9pt}'
                 'h1{font-size:15pt}h2{font-size:11pt;margin:10px 0 2px}'
                 'table{border-collapse:collapse;width:100%}'
                 'td{padding:1px 6px;border-bottom:1px solid #bbb}'
                 'td.k{white-space:nowrap;font-family:monospace}'
                 '</style></head><body><h1>FoamMesh keyboard shortcuts</h1>']
        current = None
        for group, title, keys, where in self.rows():
            if group != current:
                if current is not None:
                    parts.append('</table>')
                parts.append(f'<h2>{escape(group)}</h2><table>')
                current = group
            parts.append(
                f'<tr><td>{escape(title)}</td>'
                f'<td class="k">{escape(keys)}</td>'
                f'<td>{escape(where)}</td></tr>')
        if current is not None:
            parts.append('</table>')
        parts.append('</body></html>')
        return ''.join(parts)

    def write_sheet_pdf(self, path: str) -> bool:
        from PySide6.QtGui import QPageLayout, QPageSize, QTextDocument
        from PySide6.QtPrintSupport import QPrinter

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(path))
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        document = QTextDocument()
        document.setHtml(self.sheet_html())
        document.print_(printer)
        return True


_GENERATE_ID = {'1': 'gmsh.generate1d', '2': 'gmsh.generate2d',
                'F2': 'gmsh.generate2dF', '3': 'gmsh.generate3d',
                'F3': 'gmsh.generate3dF'}

_WHERE = {
    WINDOW: 'anywhere', FOCUS: 'in focus mode', VIEWPORT: 'in the viewport', PLANE: 'with a cut plane',
    CONSOLE: 'in the console', REGIONS: 'Regions step',
    DETECTION: 'region detection', GMSH: 'Gmsh route',
}


def _alive(target) -> bool:
    try:
        import shiboken6
        return shiboken6.isValid(target)
    except Exception:
        return True


def _is_shortcut(target) -> bool:
    from PySide6.QtGui import QShortcut
    return isinstance(target, QShortcut)


def _target_keys(target) -> list:
    if _is_shortcut(target):
        return [target.key()]
    if hasattr(target, 'shortcuts'):
        return list(target.shortcuts())
    return [target.shortcut()]


REGISTRY = ShortcutRegistry()


def adopt(command_id: str, target):
    """Module-level shorthand for ``REGISTRY.adopt``."""
    return REGISTRY.adopt(command_id, target)
