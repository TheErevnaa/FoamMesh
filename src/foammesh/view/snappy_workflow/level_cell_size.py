"""The cell a refinement level asks for, said next to the level.

DP-1251. snappyHexMesh halves the base-grid cell once per level, so a level
is only a number until it is read against the cell it starts from -- and the
guided Castellation page asked for surface, feature, volume and band levels
with nothing beside any of them. The legacy Designer dialogs had said it
("cell size (x × y × z)") since R175/DP-179, each with its own copy of the
arithmetic; the guided row editor, which replaced them, said nothing at all.

One place now does the arithmetic and the wording, read by the guided row
editor, the guided tables' tooltips, and both legacy dialogs:

* the base cell is the one `GeometryManager.getCellSize` reports -- per axis,
  in metres, from the block that is actually meshed (standoff, a modelled
  Hex6, hand-written blocks, target-size mode);
* the cell at level *L* is that cell over ``2 ** L`` on every axis;
* it is printed by `format_group`, so the three components share one decimal
  count and one unit off the house ladder (m, mm, µm) -- the same rendering
  DP-179 settled on for the legacy dialogs.

When there is no base cell to divide -- no geometry yet, or no base grid --
the readout says so rather than showing nothing or a cell worked out from a
guessed block.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QLabel

from foammesh.core.quantities import format_group

#: The object name every readout carries, so a test or a style can find them.
READOUT_NAME = 'levelCellSize'


def _tr(text: str) -> str:
    return QCoreApplication.translate('LevelCellSize', text)


def valid_base(cell):
    """*cell* as three positive finite floats, or ``None``."""
    try:
        values = tuple(float(value) for value in cell)
    except (TypeError, ValueError):
        return None
    if len(values) != 3:
        return None
    if not all(math.isfinite(value) and value > 0 for value in values):
        return None
    return values


def current_base_cell():
    """The base cell of the open case, per axis in metres, or ``None``.

    Read from the viewport's geometry manager, which is what knows the block
    that is meshed. Without a window, a geometry or a base grid it is
    ``None``: an empty viewport reports an inverted extent, and a cell taken
    from that is a number that looks like an answer.
    """
    try:
        from foammesh.app import app

        window = app.window
        manager = getattr(window, 'geometryManager', None) if window else None
        if manager is None:
            return None
        return valid_base(manager.getCellSize())
    except Exception:                                       # noqa: BLE001
        return None


def parse_level(level):
    """A refinement level as an ``int`` >= 0, or ``None``."""
    level = getattr(level, 'value', level)
    if isinstance(level, bool) or level is None:
        return None
    try:
        number = float(str(level).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number != int(number):
        return None
    return int(number)


def cell_at_level(base, level):
    """The cell at *level*: the base cell over ``2 ** level`` per axis."""
    base = valid_base(base)
    level = parse_level(level)
    if base is None or level is None:
        return None
    divisor = float(2 ** level)
    return tuple(value / divisor for value in base)


def cell_text(base, level) -> str:
    """``'12.50 × 12.50 × 25.00 mm'``, or ``''`` when it cannot be said."""
    cell = cell_at_level(base, level)
    return format_group(cell) if cell is not None else ''


def readout_text(base, level) -> str:
    """The sentence beside a level field."""
    number = parse_level(level)
    if number is None:
        return _tr('Cell size: enter a level of 0 or more.')
    text = cell_text(base, number)
    if not text:
        return (_tr('Cell size at level %d: available once the base grid '
                    'is set.') % number)
    return _tr('Cell size at level %d: %s') % (number, text)


class BaseCellSource:
    """The base cell, read once per refresh rather than once per keystroke.

    `GeometryManager.getCellSize` checks the case out to read the base grid,
    so a readout that asked on every spin-box step would copy the case on
    every step. The owner calls :meth:`refresh` when the base grid can have
    changed -- a page refresh, a dialog opening -- and everything else reads
    the cached value.
    """

    def __init__(self, reader=None):
        self._reader = reader or current_base_cell
        self._base = None
        self._read = False

    def refresh(self):
        self._base = valid_base(self._reader())
        self._read = True
        return self._base

    def base(self):
        if not self._read:
            return self.refresh()
        return self._base


class LevelCellSizeLabel(QLabel):
    """``Cell size at level 3: 12.50 × 12.50 × 12.50 mm``, kept live.

    Placed under a level field in the row editor. It follows the field as it
    changes and re-reads the base cell each time the editor is shown, so a
    base grid changed between two openings is the one it divides.
    """

    def __init__(self, editor, source: BaseCellSource, parent=None):
        super().__init__(parent)
        self._fieldEditor = editor
        self._source = source
        self.setObjectName(READOUT_NAME)
        self.setWordWrap(True)
        title = str(getattr(editor.descriptor, 'title', '') or '').strip()
        self.setAccessibleName(_tr('Cell size at %s') % (title or _tr('level')))
        editor.valueChanged.connect(self._changed)
        self.update_text()

    def update_text(self, refresh: bool = False) -> str:
        if refresh:
            self._source.refresh()
        text = readout_text(self._source.base(), self._fieldEditor.value())
        self.setText(text)
        return text

    def _changed(self, *_args) -> None:
        self.update_text()

    def showEvent(self, event):                              # noqa: N802
        # `set_value` loads a row with signals blocked, so the value may have
        # changed without a word; and the base grid may have changed since
        # the editor was last open.
        self.update_text(refresh=True)
        super().showEvent(event)


def attach_readouts(panel, keys, source: BaseCellSource) -> dict:
    """A readout under each of *keys* in *panel*'s row editor, and a tooltip
    on each of those columns in its table. Returns ``{key: readout}``."""
    readouts = {}
    for key in keys:
        editor = panel.editor(key)
        if editor is None:
            continue
        readout = LevelCellSizeLabel(editor, source, panel.editorHost())
        panel.addAnnotation(key, readout)
        panel.setCellDetail(
            key, lambda value, _source=source: table_detail(_source, value))
        readouts[key] = readout
    return readouts


def table_detail(source: BaseCellSource, value) -> str:
    """The tooltip on a level cell: the same sentence as the editor's."""
    if parse_level(value) is None:
        return ''
    return readout_text(source.base(), value)
