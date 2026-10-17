"""Snappy workflow page: ``snappy.domain_regions``.

Plan 30 WP-09 / F-17. The legacy Designer page edited the ``region``
collection through cards of its own; the collection is registered
(``regions.items``) and the shared child-control table is what every other
collection in the app is edited with, so the port keeps one editor idiom
rather than a second one for this task alone.

Plan 31 adds the other half of the same question. A material point only means
something once OpenFOAM can tell inside from outside, and until now the app
never said which surfaces it could tell that for: every staged surface was
written ``type triSurface``, so a surface with a pinhole in it silently
answered "no" and any refinement region or cellZone built on it was warned
about in a log and dropped. The seven fields named here are the ones that
decide that -- whether the surfaces are declared closed, how narrow a gap
counts as closed, and how finely the surfaces are searched -- so they belong
on the page where the user is already reasoning about what encloses what.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QToolButton, QVBoxLayout, QWidget)

from foammesh.app import app
from foammesh.view.main_window.shortcut_registry import adopt as adoptShortcut
from foammesh.core import display_unit
from foammesh.core.mesh.domain_box import domain_box
from foammesh.core.mesh.presentation import count_text
from foammesh.db.configurations_schema import Step
from foammesh.rendering.domain_box_actor import domainBoxActor
from foammesh.rendering.farfield_actor import farfieldActor, farfieldLabelActor
from foammesh.rendering.region_labels import regionLabelActors
from foammesh.view.facade_client import query
from foammesh.view.region.seed_feedback import MESSAGES as SEED_MESSAGES, SeedFeedback
from foammesh.view.theming.metrics import MARGIN_TIGHT
from foammesh.view.theming.patch_palette import region_zone_colours
from foammesh.view.theming.status_colors import set_status
from foammesh.view.workflow_controls.child_controls import ChildControlPanel
from foammesh.view.workflow_controls.field_widgets import _is_length

from .base import SnappyTaskPage
from .region_detection_panel import (
    RegionDetectionPanel, chip_icon, format_volume, offer_detection)


#: The coordinates a seed is typed into, in the order of a point.
_POINT_KEYS = ('point.x', 'point.y', 'point.z')


def _point_cell_text(panel, key: str, value):
    """One seed coordinate as its table cell shows it, or None.

    DP-920: six significant figures, the stored value in the tooltip.
    DP-2637: the coordinate is stored in metres and the column heading
    names the project display unit (`ChildControlPanel._heading` writes
    ``X (mm)`` for a length), so the cell shows it in that unit too. It
    printed the stored metres, so a seed at 11.4 mm read ``0.0114`` under
    ``X (mm)``. The tooltip keeps the stored metres, labelled when the
    display unit is not the metre.
    """
    if key not in panel.POINT_KEYS or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    descriptor = getattr(panel, '_descriptors', {}).get(key)
    length = descriptor is None or _is_length(descriptor)
    shown = display_unit.to_display(number) if length else number
    text = format(shown, f'.{panel.POINT_DIGITS}g')
    stored = str(value)
    if stored == text:
        return text, ''
    if length and display_unit.current() != 'm':
        stored = f'{stored} m'
    return text, stored


def _geometryManager():
    window = app.window
    return getattr(window, 'geometryManager', None) if window else None


def _isViewport(widget) -> bool:
    """Whether *widget* is the 3D view (a VTK render window interactor)."""
    while widget is not None:
        if any('RenderWindowInteractor' in kind.__name__
               for kind in type(widget).__mro__):
            return True
        widget = widget.parentWidget()
    return False


#: Plan 36 RP10. Alt and an arrow nudges the seed along an axis while the
#: editor is open; Shift makes the step ten times larger.
_NUDGE_KEYS = {
    Qt.Key.Key_Left: (0, -1), Qt.Key.Key_Right: (0, 1),
    Qt.Key.Key_Down: (1, -1), Qt.Key.Key_Up: (1, 1),
    Qt.Key.Key_PageDown: (2, -1), Qt.Key.Key_PageUp: (2, 1),
}


class _PlacementKeys(QObject):
    """Plan 36 RP10. The open editor's keys, wherever the focus is in it.

    Ctrl+Z undoes the last placement and Ctrl+Y or Ctrl+Shift+Z redoes it;
    Alt and an arrow key (or Page Up/Down for Z) nudges the seed. They are
    claimed at ``ShortcutOverride`` so the main window's Undo -- which undoes
    the last *saved* edit, not a seed not yet saved -- does not take them
    while the editor, or the viewport it places into, has the focus.
    """

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    def _ours(self, watched) -> bool:
        dialog = self._panel._openDialog
        if dialog is None or not isinstance(watched, QWidget):
            return False
        try:
            return dialog.isAncestorOf(watched) or watched is dialog \
                or _isViewport(watched)
        except RuntimeError:
            return False

    def _action(self, event):
        if event.matches(QKeySequence.StandardKey.Undo):
            return 'undo'
        if event.matches(QKeySequence.StandardKey.Redo) or (
                event.key() == Qt.Key.Key_Y
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return 'redo'
        modifiers = event.modifiers()
        if (modifiers & Qt.KeyboardModifier.AltModifier
                and not modifiers & Qt.KeyboardModifier.ControlModifier
                and event.key() in _NUDGE_KEYS):
            return 'nudge'
        return None

    def eventFilter(self, watched, event):                    # noqa: N802
        kind = event.type()
        if kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False
        if not self._ours(watched):
            return False
        action = self._action(event)
        if action is None:
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        panel = self._panel
        if action == 'undo':
            panel.undoPlacement()
        elif action == 'redo':
            panel.redoPlacement()
        else:
            axis, sign = _NUDGE_KEYS[event.key()]
            steps = 10 if event.modifiers() \
                & Qt.KeyboardModifier.ShiftModifier else 1
            panel.nudgeSeed(axis, sign * steps)
        return True


class RegionSeedPanel(ChildControlPanel):
    """The regions table, whose editor says where the seed it holds is.

    DP-818. The editor was three number boxes and nothing else, so a seed in
    the empty core of a pipe looked exactly like one in the fluid. While the
    editor is open the seed is drawn in the viewport, the walls are faded so
    it can be seen through them, and a line under the coordinates says in
    words -- and in green or red -- whether the point is inside the geometry.
    Every change to X, Y or Z is judged again as it is typed.

    Plan 36 RP2. The editor is not modal. A modal dialog locks the viewport,
    and the seed is placed in the viewport, so the same form is docked under
    the table instead (a tool window when the column is too narrow to hold
    it). While it is open the table and its buttons are disabled, so nothing
    else can edit the collection under it; OK and Cancel finish the edit
    through `_finishEditor` rather than through a return code.

    Plan 36 RP3. The seed drawn while the form is open is a handle
    (`SeedGizmo`): dragging it in the viewport writes X, Y and Z here as it
    moves, and typing into X, Y or Z moves it. It is kept inside the box the
    background mesh spans (`setSeedBoundsProvider`) and Ctrl snaps it to one
    base-grid cell.

    Plan 36 RP7. Detect… opens "how many fluid regions?"
    (`RegionDetectionPanel`) under the table, the way the editor is docked,
    and the table is locked while it is open. What it accepts is written in
    one `geometry.fluid_regions.apply`, so one Undo takes it back.

    Plan 36 RP8. Each row carries its region's colour -- the zone palette,
    in numeric id order, the colour the viewport draws it in -- and, once
    the domain has been labelled, the volume of the space its seed is in.
    Two seeds in one space are said under the table, in the same words
    launch uses to warn or refuse.

    Plan 36 RP10. A seed is placed by keyboard as well as by mouse: Add
    opens the editor, the coordinates are typed, Alt and an arrow
    nudges the seed along X or Y (Page Up/Down for Z, Shift for ten steps)
    and Enter is OK. Every drag, nudge or typed coordinate is one placement,
    which Ctrl+Z takes back and Ctrl+Y puts again -- the fields and the
    handle move together. A detection accept is one saved edit, which the
    main window's Undo takes back whole; the note under the table says so.
    **Merge** drops the selected row's seed when the fluid-space field says
    another row's seed is in the same space, and otherwise says why not.
    """

    #: Plan 36 RP10. The shortcuts the buttons here take; the tooltips name
    #: them, and none shadows a menu mnemonic.
    DETECT_SHORTCUT = 'Alt+T'
    MERGE_SHORTCUT = 'Alt+G'
    SECTION_SHORTCUT = 'Alt+O'
    #: Placements kept per editor session.
    PLACEMENT_LIMIT = 100

    #: Plan 36 RP2 (D5). Narrower than this, the form floats beside the
    #: column rather than squeezing into it.
    DOCK_MIN_WIDTH = 320

    #: DP-920 (RP12 live pass). The seed coordinates, shown to six
    #: significant figures. Detection stores the float32 voxel centres it
    #: found, so an accepted seed at x = -0.005 was printed as
    #: `-0.005000000074505795`: 18 digits in each of three columns pushed Z
    #: out of the 360-420 px settings column (DP-570 fitted them at the
    #: width a typed coordinate takes). The stored value is untouched and
    #: is in the cell's tooltip.
    POINT_KEYS = ('point.x', 'point.y', 'point.z')
    POINT_DIGITS = 6

    def _cell_text(self, key: str, value) -> tuple[str, str]:
        shown = _point_cell_text(self, key, value)
        return shown if shown is not None else super()._cell_text(key, value)

    #: Plan 36 RP2. True while the editor is open, False when it closes.
    editingChanged = Signal(bool)
    #: Plan 36 RP13 #8. From the event bus, whatever thread it publishes on:
    #: the stored rows may have changed, or the case is being switched.
    _changedElsewhere = Signal()
    _caseSwitched = Signal()

    def __init__(self, facade_client, collection_id: str, title: str,
                 *args, **kwargs):
        # Built with no parent (DP-312): the dialog it is shown in adopts it.
        self.seedFeedback = SeedFeedback()
        annotations = dict(kwargs.pop('annotations', None) or {})
        annotations.setdefault('point.z', self.seedFeedback)
        # M6. Where the geometry is, under the coordinates, in the shown unit.
        self.boundsHint = QLabel()
        self.boundsHint.setObjectName('regionBoundsHint')
        self.boundsHint.setWordWrap(True)
        annotations.setdefault('point.x', self.boundsHint)
        super().__init__(facade_client, collection_id, title, *args,
                         annotations=annotations, **kwargs)
        # M8. What an empty table means for a region, in the live behaviour's
        # words (a run with none adopts one fluid region named "fluid").
        self._emptyDescription = self.tr(
            'No regions yet. Add one, or Detect the spaces the surfaces '
            'close off. Proceeding with none adds a default Fluid region '
            'named "fluid".')
        self._watched = []
        self._placingRegion = None
        self._editorAccepted = None
        self._openDialog = None
        self._gizmo = None
        self._seedBounds = None
        self._section = None
        self._sectionButton = None
        # Plan 36 RP10. The placements made while the editor is open.
        self._placements = []
        # Plan 37 #1(d): the suggested seed, asked once per Add.
        self._freshPoint = None
        self._placementIndex = -1
        self._placementKeys = _PlacementKeys(self)
        self._adjusting = None
        self._labelActors = []
        self._dock = QFrame(self)
        self._dock.setObjectName('regionEditorDock')
        self._dock.setFrameShape(QFrame.Shape.StyledPanel)
        dockLayout = QVBoxLayout(self._dock)
        dockLayout.setContentsMargins(0, 0, 0, 0)
        self._dock.setVisible(False)
        self.layout().addWidget(self._dock)
        # Plan 36 RP7. Detect… has a row of its own under Add, Edit
        # and Remove: a fourth button on their row is 346 px, wider than
        # the narrowest settings column (RP2's floating-editor width).
        self._detection = None
        self._highlighter = None
        self._detect = QPushButton(self.tr('Detect…'), self)
        self._detect.setObjectName('regionDetect')
        self._detect.setShortcut(QKeySequence(self.DETECT_SHORTCUT))
        adoptShortcut('regions.detect', self._detect)
        self._detect.setToolTip(self.tr(
            'Find the spaces the surfaces close off, and place a region in '
            'each one you keep (%s).') % self.DETECT_SHORTCUT)
        self._detect.clicked.connect(self.openDetection)
        # Plan 36 RP10. Merge shares Detect's row: Add, Edit and Remove
        # already fill theirs at the narrowest column.
        self._merge = QPushButton(self.tr('Merge'), self)
        self._merge.setObjectName('regionMerge')
        self._merge.setShortcut(QKeySequence(self.MERGE_SHORTCUT))
        adoptShortcut('regions.merge', self._merge)
        self._merge.setToolTip(self.tr(
            'Drop the selected region when its seed is in the same space as '
            "another region's: one seed per space is enough (%s).")
            % self.MERGE_SHORTCUT)
        self._merge.clicked.connect(self.mergeSelected)
        detectRow = QHBoxLayout()
        detectRow.setContentsMargins(0, 0, 0, 0)
        detectRow.addWidget(self._detect)
        detectRow.addWidget(self._merge)
        detectRow.addStretch(1)
        layout = self.layout()
        buttons = self._buttonRow()
        layout.insertLayout(
            layout.indexOf(buttons) + 1 if buttons is not None
            else layout.indexOf(self._dock), detectRow)
        # Plan 36 RP8. Two seeds in one space, said under the table.
        self._seedSpaces = {}
        self._seedWarning = QLabel(self)
        self._seedWarning.setObjectName('regionSeedWarning')
        self._seedWarning.setWordWrap(True)
        self._seedWarning.setVisible(False)
        set_status(self._seedWarning, 'warning')
        layout.insertWidget(layout.indexOf(self.table) + 1, self._seedWarning)
        # Plan 36 RP10. What Merge and Detect last did, under the warning.
        self._note = QLabel(self)
        self._note.setObjectName('regionNote')
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.insertWidget(layout.indexOf(self._seedWarning) + 1, self._note)
        # Plan 36 RP13 #8. The editor follows the case and the stored rows.
        self._editCase = None
        self._openValues = None
        self._keptRows = None
        self._elsewhere = self._buildElsewhereBar()
        layout.insertWidget(layout.indexOf(self._dock), self._elsewhere)
        self._changedElsewhere.connect(self._onChangedElsewhere,
                                       Qt.ConnectionType.QueuedConnection)
        self._caseSwitched.connect(self._onCaseSwitched)
        self._subscribeToCase()
        self._describe()
        self._decorateRows()

    def _describe(self) -> None:
        """Plan 36 RP10. Names and descriptions a screen reader reads."""
        described = self.tr(
            'One row per region: its name, type and seed point. A chip in '
            "the region's colour marks each row; the tooltip says its "
            'space and volume.')
        # Plan 33 FORM-03: an empty table's description carries the empty
        # state, as every other child table's does.
        if not self._rows:
            described = f'{self._emptyDescription} {described}'
        self.table.setAccessibleDescription(described)
        self._detect.setAccessibleName(self.tr('Detect regions'))
        self._detect.setAccessibleDescription(self._detect.toolTip())
        self._merge.setAccessibleName(self.tr('Merge regions'))
        self._merge.setAccessibleDescription(self._merge.toolTip())
        self._seedWarning.setAccessibleName(self.tr('Region warning'))
        self._note.setAccessibleName(self.tr('Region note'))
        # Focus follows reading order: the table, its buttons, Detect,
        # Merge, then the editor when it is open.
        order = [self.table, self._add, self._edit, self._remove,
                 self._detect, self._merge]
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)

    def note(self) -> str:
        """What Merge or Detect last said under the table, or ``''``."""
        return '' if self._note.isHidden() else self._note.text()

    def _say(self, text: str, status=None) -> None:
        self._note.setText(text)
        set_status(self._note, status)
        self._note.setVisible(bool(text))

    def seedPoint(self):
        """The point the editor holds now, or ``None`` if it is not a number."""
        values = []
        for key in _POINT_KEYS:
            editor = self.editor(key)
            if editor is None:
                return None
            try:
                values.append(float(editor.value()))
            except (TypeError, ValueError):
                return None
        return tuple(values)

    def _judgeSeed(self, *_args) -> None:
        point = self.seedPoint()
        if point is not None:
            # RP10: typing is announced as it is judged; a drag only when
            # it ends (`_placementFinished`).
            self.seedFeedback.judge(point)
            self._linkGizmo()
            if self._section is not None:
                self._section.follow(point)

    # -- Plan 36 RP3: the seed is dragged in the viewport -------------------- #

    def setSeedBoundsProvider(self, provider) -> None:
        """*provider* answers the ``(xmin, xmax, ..., zmax)`` a seed is kept in."""
        self._seedBounds = provider
        # Plan 36 RP6: the same box is the domain the region volumes fill.
        self.seedFeedback.setVolumeDomain(provider)

    def seedGizmo(self):
        """The handle the open editor's seed is dragged by, or ``None``."""
        return self._gizmo

    def _linkGizmo(self) -> None:
        """Follow the viewport's handle: one handle per placement, but the
        manager makes a new one if the point was cleared in between."""
        manager = _geometryManager()
        preview = getattr(manager, 'seedPreview', None)
        gizmo = preview() if preview is not None else None
        if not hasattr(gizmo, 'pointMoved'):
            gizmo = None
        if gizmo is self._gizmo:
            return
        self._unlinkGizmo()
        if gizmo is None:
            return
        self._gizmo = gizmo
        gizmo.pointMoved.connect(self._gizmoMoved)
        finished = getattr(gizmo, 'placementFinished', None)
        if finished is not None:
            finished.connect(self._placementFinished)
        bounds = None
        if self._seedBounds is not None:
            try:
                bounds = self._seedBounds()
            except Exception:                                 # noqa: BLE001
                bounds = None
        gizmo.setBounds(bounds)
        step = None
        cellSize = getattr(manager, 'getCellSize', None)
        if cellSize is not None:
            try:
                step = cellSize()
            except Exception:                                 # noqa: BLE001
                step = None           # no base grid yet: a hundredth of the box
        gizmo.setStep(step)
        # Plan 36 RP13 #2. Snap from where blockMesh starts, and keep the
        # seed off every face the mesh will have at any refinement level.
        grid, level = self._backgroundFaces()
        gizmo.setSnapOrigin(grid.origin if grid is not None else None)
        gizmo.setFaceGuard(grid, level)

    @staticmethod
    def _backgroundFaces():
        """``(grid, max_level)`` of the open case, or ``(None, 0)``."""
        from foammesh.core.mesh.face_clearance import (
            background_grid, max_refinement_level,
        )

        try:
            db = app.facadeClient.checkout()
        except Exception:  # noqa: BLE001 - no case open yet
            return None, 0
        surfaces = getattr(_geometryManager(), 'getSurfaceBounds', None)
        try:
            extent = surfaces() if surfaces is not None else None
            extent = extent.toTuple() if extent is not None else None
        except Exception:  # noqa: BLE001 - no surfaces drawn yet
            extent = None
        return background_grid(db, extent), max_refinement_level(db)

    # -- Plan 36 RP9: the seed placed on a section plane --------------------- #

    def sectionButton(self):
        """The editor's Section toggle, once the editor has been built."""
        return self._sectionButton

    def seedSection(self):
        """Section mode over the open editor's handle, or ``None``."""
        return self._section

    def _addSectionButton(self, dialog) -> None:
        if self._sectionButton is not None:
            return
        button = QToolButton(dialog)
        button.setObjectName('regionSeedSection')
        button.setText(self.tr('Section'))
        button.setCheckable(True)
        button.setShortcut(QKeySequence(self.SECTION_SHORTCUT))
        adoptShortcut('regions.section', button)
        button.setToolTip(self.tr(
            'Cut the model on a plane through the seed, across the view. '
            'The seed stays on the plane; push the plane to move it deeper, '
            'or double-click on the plane to drop it there (%s).')
            % self.SECTION_SHORTCUT)
        button.setAccessibleName(self.tr('Section through the seed'))
        button.setAccessibleDescription(button.toolTip())
        button.toggled.connect(self._setSection)
        layout = dialog.layout()
        layout.insertWidget(max(0, layout.count() - 1), button)
        self._sectionButton = button

    def _setSection(self, on: bool) -> None:
        if not on:
            section, self._section = self._section, None
            if section is not None:
                section.stop()
            return
        display = getattr(app.window, 'displayControl', None) \
            if app.window else None
        getter = getattr(display, 'cutTool', None)
        cutTool = getter() if callable(getter) else None
        started = False
        if self._gizmo is not None and cutTool is not None:
            from .seed_section import SeedSection
            self._section = SeedSection(self._gizmo, cutTool, self)
            started = self._section.start()
            if not started:
                self._section = None
            else:
                # Plan 36 RP6: the region volumes are cut on the same plane.
                self.seedFeedback.followSection(self._section)
        if not started:
            self._sectionButton.blockSignals(True)
            self._sectionButton.setChecked(False)
            self._sectionButton.blockSignals(False)

    def _endSection(self) -> None:
        if self._sectionButton is not None and self._sectionButton.isChecked():
            self._sectionButton.setChecked(False)
        elif self._section is not None:
            self._setSection(False)

    def _unlinkGizmo(self) -> None:
        self._endSection()
        gizmo, self._gizmo = self._gizmo, None
        if gizmo is None:
            return
        try:
            gizmo.pointMoved.disconnect(self._gizmoMoved)
        except (RuntimeError, TypeError):
            pass
        finished = getattr(gizmo, 'placementFinished', None)
        if finished is not None:
            try:
                finished.disconnect(self._placementFinished)
            except (RuntimeError, TypeError):
                pass

    def _gizmoMoved(self, point) -> None:
        """The handle was dragged: the fields say where, and it is judged.

        RP10: judged but not announced -- a screen reader told every
        position of a drag says nothing useful; it hears where the drag
        ended (`_placementFinished`).
        """
        for key, value in zip(_POINT_KEYS, point):
            editor = self.editor(key)
            if editor is not None:
                editor.set_value(value)
        self.seedFeedback.judge(point, announce=False)

    # -- Plan 36 RP10: placements undo and redo ------------------------------ #

    def _placementFinished(self, *_args) -> None:
        """A drag or a nudge ended: one placement, and it is announced."""
        self._recordPlacement()
        self.seedFeedback.announce()

    def _typedPlacement(self) -> None:
        self._recordPlacement()

    def _recordPlacement(self) -> None:
        if self._openDialog is None:
            return
        point = self.seedPoint()
        if point is None:
            return
        if (0 <= self._placementIndex < len(self._placements)
                and self._placements[self._placementIndex] == point):
            return
        del self._placements[self._placementIndex + 1:]
        self._placements.append(point)
        if len(self._placements) > self.PLACEMENT_LIMIT:
            del self._placements[0]
        self._placementIndex = len(self._placements) - 1

    def placements(self) -> list:
        """The placements this editor session has made, oldest first."""
        return list(self._placements)

    def canUndoPlacement(self) -> bool:
        return self._placementIndex > 0

    def canRedoPlacement(self) -> bool:
        return 0 <= self._placementIndex < len(self._placements) - 1

    def undoPlacement(self) -> bool:
        """Put the seed back where the last placement found it."""
        if not self.canUndoPlacement():
            return False
        self._placementIndex -= 1
        self._restorePlacement()
        return True

    def redoPlacement(self) -> bool:
        if not self.canRedoPlacement():
            return False
        self._placementIndex += 1
        self._restorePlacement()
        return True

    def _restorePlacement(self) -> None:
        point = self._placements[self._placementIndex]
        for key, value in zip(_POINT_KEYS, point):
            editor = self.editor(key)
            if editor is not None:
                editor.set_value(value)
        # `set_value` is silent, so the handle is moved and judged here.
        self._judgeSeed()
        if self._gizmo is not None and hasattr(self._gizmo, 'setPosition'):
            try:
                self._gizmo.setPosition(point)
            except Exception:  # noqa: BLE001 - the fields are the record
                pass

    def nudgeSeed(self, axis: int, steps: int) -> None:
        """Move the seed *steps* steps along *axis* (0, 1, 2 = X, Y, Z)."""
        if self._openDialog is None:
            return
        gizmo = self._gizmo
        nudge = getattr(gizmo, 'nudge', None)
        if nudge is not None and gizmo.step() is not None:
            # The handle clamps, snaps and says where it went; its
            # `placementFinished` records the placement.
            nudge(axis, 1 if steps > 0 else -1, float(abs(steps)))
            return
        point = self.seedPoint()
        if point is None:
            return
        step = self._fallbackStep()
        moved = list(point)
        moved[axis] += steps * step
        editor = self.editor(_POINT_KEYS[axis])
        if editor is not None:
            editor.set_value(moved[axis])
        self._judgeSeed()
        self._recordPlacement()
        self.seedFeedback.announce()

    def _fallbackStep(self) -> float:
        """A hundredth of the box the seed is kept in, else a millimetre."""
        bounds = None
        if self._seedBounds is not None:
            try:
                bounds = self._seedBounds()
            except Exception:  # noqa: BLE001 - no domain yet
                bounds = None
        try:
            spans = [float(bounds[2 * index + 1]) - float(bounds[2 * index])
                     for index in range(3)]
            largest = max(spans)
        except (TypeError, ValueError, IndexError):
            return 0.001
        return largest / 100.0 if largest > 0 else 0.001

    def _watchTyping(self, watch: bool) -> None:
        for key in _POINT_KEYS:
            editor = self.editor(key)
            finished = getattr(getattr(editor, 'editor', None),
                               'editingFinished', None)
            if finished is None:
                continue
            try:
                if watch:
                    finished.connect(self._typedPlacement)
                else:
                    finished.disconnect(self._typedPlacement)
            except (RuntimeError, TypeError):
                pass

    def _watchKeys(self, dialog, watch: bool) -> None:
        application = QApplication.instance()
        if application is None:
            return
        if watch:
            application.installEventFilter(self._placementKeys)
        else:
            application.removeEventFilter(self._placementKeys)

    def _watchPoint(self) -> None:
        # Connected per open, not once: an editor can be rebuilt between opens.
        self._unwatchPoint()
        for key in _POINT_KEYS:
            editor = self.editor(key)
            if editor is not None:
                editor.valueChanged.connect(self._judgeSeed)
                self._watched.append(editor)

    def _unwatchPoint(self) -> None:
        for editor in self._watched:
            try:
                editor.valueChanged.disconnect(self._judgeSeed)
            except (RuntimeError, TypeError):
                pass
        self._watched = []

    def editor_dialog(self):
        dialog = super().editor_dialog()
        dialog.acceptGuard = self._acceptProblem
        self._addSectionButton(dialog)
        self._sayBounds()
        # Both open paths call this after the values are set and before the
        # dialog is shown, so this is where the first verdict is given.
        if self.seedFeedback.isActive():
            self._judgeSeed()
        return dialog

    def open_add_dialog(self) -> None:
        if self.isEditing() or self.isDetecting():
            return
        self._placingRegion = None
        self._freshPoint = None
        super().open_add_dialog()

    # -- M5/M6/M7: what the editor says about the box and the name -------- #

    def _box(self):
        """``(xmin, xmax, ymin, ymax, zmin, zmax)`` of the background box, or None."""
        if self._seedBounds is None:
            return None
        try:
            bounds = self._seedBounds()
            box = tuple(float(value) for value in bounds)
        except Exception:  # noqa: BLE001 - no domain yet
            return None
        return box if len(box) == 6 else None

    def _sayBounds(self) -> None:
        """M6. The geometry's extent, in the unit the fields are shown in."""
        box = self._box()
        if box is None:
            self.boundsHint.setText('')
            self.boundsHint.setVisible(False)
            return
        shown = [display_unit.format_length(value, unit=False) for value in box]
        self.boundsHint.setText(self.tr(
            'The geometry spans X {0} to {1}, Y {2} to {3}, Z {4} to {5} '
            '{6}.').format(*shown, display_unit.current()))
        self.boundsHint.setVisible(True)

    def _acceptProblem(self) -> str:
        """The sentence that stops OK, or ``''`` (M5, M7)."""
        name = self.editor('name')
        if name is not None and not str(name.value() or '').strip():
            return self.tr('A region needs a name.')
        point = self.seedPoint()
        box = self._box()
        if point is not None and box is not None and not self._inBox(point, box):
            return self.tr('The point is outside the bounding box.')
        return ''

    @staticmethod
    def _inBox(point, box) -> bool:
        return all(box[2 * axis] <= point[axis] <= box[2 * axis + 1]
                   for axis in range(3))

    def fresh_value(self, key: str, descriptor):
        """Plan 37 #1(d): a new region's seed starts inside the fluid.

        Add opened the editor on the schema's (0, 0, 0), which for an elbow,
        an annulus or a part away from the origin is outside the geometry --
        the line under X, Y and Z said so, and OK wrote a seed snappy meshes
        the wrong side from. The point now comes from
        `geometry.fluid_seed.suggest` (the deepest point of the largest
        enclosed space, judged as launch judges it), asked once per Add.
        When nothing inside is found the schema default stands, as before.
        """
        if key == 'name':
            # M7. Region_N, the next one nobody has taken.
            taken = {str(row.get('name', '')) for row in self._rows}
            number = len(self._rows) + 1
            while f'Region_{number}' in taken:
                number += 1
            return f'Region_{number}'
        if key in self.POINT_KEYS:
            point = self._suggestedPoint()
            if point is not None:
                return point[self.POINT_KEYS.index(key)]
        return super().fresh_value(key, descriptor)

    def _suggestedPoint(self):
        if getattr(self, '_freshPoint', None) is None:
            self._freshPoint = ()
            try:
                payload = query(self._client, 'geometry.fluid_seed.suggest',
                                {}).payload or {}
            except Exception:                               # noqa: BLE001
                payload = {}
            point = payload.get('point')
            if payload.get('inside') and point and len(point) == 3:
                self._freshPoint = tuple(float(value) for value in point)
        return self._freshPoint or None

    def open_edit_dialog(self, *args) -> None:
        if (self.isEditing() or self.isDetecting()
                or self.selected_key() is None):
            return
        self._placingRegion = self.selected_key()
        super().open_edit_dialog(*args)

    # -- Plan 36 RP2: the editor stays open beside a live viewport ---------- #

    def isEditing(self) -> bool:
        return self._openDialog is not None

    def editorDock(self):
        return self._dock

    def _run_editor(self, dialog, accepted) -> None:
        """Open the form without blocking, and finish it on OK or Cancel.

        DP-818's `begin`/`end` bracket used to wrap `exec()`; it now wraps the
        time the form is open. `end` is guaranteed by `finished` and, should
        the dialog be dropped under an open form, by `destroyed`.
        """
        self._editorAccepted = accepted
        self._openDialog = dialog
        # RP13 #8: what the form opened on, and in which case.
        self._editCase = self._caseId()
        self._openValues = self._formValues()
        dialog.finished.connect(self._finishEditor)
        dialog.destroyed.connect(self._editorDestroyed)
        self._setEditing(True)
        try:
            self.seedFeedback.begin(self._placingRegion)
            self._watchPoint()
            self._judgeSeed()
            # Plan 36 RP10. The first placement is where the editor opened.
            start = self.seedPoint()
            self._placements = [start] if start is not None else []
            self._placementIndex = len(self._placements) - 1
            self._watchTyping(True)
            self._watchKeys(dialog, True)
            if self.width() >= self.DOCK_MIN_WIDTH:
                dialog.showDocked(self._dock)
            else:
                self._dock.setVisible(False)
                dialog.showFloating(self.window())
        except Exception:
            self._closeEditor()
            raise

    def _finishEditor(self, result) -> None:
        accepted = self._editorAccepted
        self._closeEditor()
        if (result == QDialog.DialogCode.Accepted
                and accepted is not None):
            accepted()

    def _editorDestroyed(self, *_args) -> None:
        self._openDialog = None
        self._closeEditor()

    def _closeEditor(self) -> None:
        dialog, self._openDialog = self._openDialog, None
        self._editorAccepted = None
        if dialog is not None:
            for signal, slot in ((dialog.finished, self._finishEditor),
                                 (dialog.destroyed, self._editorDestroyed)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._unwatchPoint()
        self._watchTyping(False)
        self._watchKeys(dialog, False)
        self._editCase = self._openValues = self._keptRows = None
        try:
            self._elsewhere.setVisible(False)
        except RuntimeError:
            pass
        self._placements = []
        self._placementIndex = -1
        self._unlinkGizmo()
        self.seedFeedback.end()
        self._placingRegion = None
        adjusting, self._adjusting = self._adjusting, None
        if adjusting is not None:
            try:
                adjusting.setEnabled(True)
            except RuntimeError:
                pass
        try:
            self._dock.setVisible(False)
        except RuntimeError:
            return
        self._setEditing(False)

    def cancelEditor(self) -> None:
        """Close an open editor as Cancel would, if one is open."""
        dialog = self._openDialog
        if dialog is not None:
            dialog.reject()

    # -- Plan 36 RP13 #8: the editor follows the case and the stored rows -- #

    def _caseId(self):
        try:
            return str(self._client.case_id)
        except Exception:  # noqa: BLE001 - no case open
            return None

    def _formValues(self) -> dict:
        values = {}
        for key, editor in self._editors.items():
            try:
                values[key] = editor.value()
            except Exception:  # noqa: BLE001 - an unreadable field is a value
                values[key] = None
        return values

    def hasUnsavedEdit(self) -> bool:
        """The open editor holds something its OK has not written yet."""
        return (self.isEditing()
                and self._formValues() != (self._openValues or {}))

    def _subscribeToCase(self) -> None:
        """Hear every write to the stored rows, and every case switch."""
        from foammesh.core.project import Event

        subscribe = getattr(getattr(app, 'events', None), 'subscribe', None)
        if not callable(subscribe):
            return

        def emitter(signal):
            def emit(**_payload):
                try:
                    signal.emit()
                except RuntimeError:              # the panel has gone
                    pass
            return emit

        changed = emitter(self._changedElsewhere)
        switched = emitter(self._caseSwitched)
        unsubscribes = [subscribe(event, changed) for event in (
            Event.TRANSACTION_APPLIED, Event.UNDONE, Event.REDONE,
            Event.ARTIFACT_RESTORED)]
        unsubscribes += [subscribe(event, switched) for event in (
            Event.PROJECT_CLOSING, Event.PROJECT_OPENED)]
        self.destroyed.connect(
            lambda *_args, gone=unsubscribes: [undo() for undo in gone])

    def _onCaseSwitched(self) -> None:
        """Another case: the edit belonged to the one closing, so it goes."""
        self.cancelEditor()

    def _onChangedElsewhere(self) -> None:
        if self.isEditing() and self._caseId() != self._editCase:
            self.cancelEditor()                   # the case was switched
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - no case to read rows from
            pass

    def _buildElsewhereBar(self):
        bar = QFrame(self)
        bar.setObjectName('regionsChangedElsewhere')
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(bar)
        row.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT,
                               MARGIN_TIGHT, MARGIN_TIGHT)
        label = QLabel(self.tr('Regions changed elsewhere'), bar)
        label.setWordWrap(True)
        set_status(label, 'warning')
        row.addWidget(label, 1)
        reload_ = QPushButton(self.tr('Reload'), bar)
        reload_.setObjectName('regionsReload')
        reload_.setToolTip(self.tr(
            'Close the editor without saving and show the regions as they '
            'are stored now'))
        reload_.clicked.connect(self.reloadElsewhere)
        keep = QPushButton(self.tr('Keep mine'), bar)
        keep.setObjectName('regionsKeepMine')
        keep.setToolTip(self.tr(
            'Go on editing; OK writes this region over what changed'))
        keep.clicked.connect(self.keepMine)
        row.addWidget(reload_)
        row.addWidget(keep)
        bar.setAccessibleName(label.text())
        bar.setVisible(False)
        self._elsewhereLabel = label
        self._elsewhereButtons = (reload_, keep)
        return bar

    def changedElsewhere(self) -> str:
        """The conflict bar's text while it is shown, or ``''``."""
        return ('' if self._elsewhere.isHidden()
                else self._elsewhereLabel.text())

    def elsewhereButtons(self) -> tuple:
        """``(Reload, Keep mine)`` on the conflict bar."""
        return self._elsewhereButtons

    def reloadElsewhere(self) -> None:
        """Reload: the unsaved edit is dropped and the stored rows shown."""
        self._elsewhere.setVisible(False)
        self._keptRows = None
        self.cancelEditor()
        self.refresh()

    def keepMine(self) -> None:
        """Keep mine: go on editing over the rows the form opened on."""
        self._keptRows = self._storedRows()
        self._elsewhere.setVisible(False)

    def _storedRows(self):
        try:
            return self._read_rows()
        except Exception:  # noqa: BLE001 - no case open
            return None

    def _refreshUnderEditor(self) -> bool:
        """A refresh while the editor is open. True when it is handled.

        The table is not reloaded under a form holding an unsaved edit: the
        edit is of the rows the form opened on, and a reload would select
        another row and write the form over it. The bar asks instead. With
        nothing unsaved the rows are reloaded and an edited region's form
        follows its row -- or closes, the row being gone.
        """
        stored = self._storedRows()
        if stored is None or stored == self._rows:
            return True
        if self.hasUnsavedEdit():
            if stored != self._keptRows:
                self._elsewhere.setVisible(True)
            return True
        editing = self._placingRegion
        blocked = self.table.blockSignals(True)
        try:
            ChildControlPanel.refresh(self)
        finally:
            self.table.blockSignals(blocked)
        if editing is None:
            return False
        keys = [str(row.get('__key__')) for row in self._rows]
        if str(editing) not in keys:
            self.cancelEditor()
            return False
        blocked = self.table.blockSignals(True)
        try:
            self.table.selectRow(keys.index(str(editing)))
        finally:
            self.table.blockSignals(blocked)
        self._load_selected()
        self._openValues = self._formValues()
        point = self.seedPoint()
        self._placements = [point] if point is not None else []
        self._placementIndex = len(self._placements) - 1
        if self._placements:
            self._restorePlacement()
        return False

    def revertDrag(self) -> None:
        """The page was left: a drag not yet placed goes back (RP13 #8).

        The editor stays open. What a finished drag, a nudge or typing
        placed is kept; the seed returns to the last of those.
        """
        if not self.isEditing():
            return
        cancel = getattr(self._gizmo, 'cancelDrag', None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001 - the fields are the record
                pass
        if (0 <= self._placementIndex < len(self._placements)
                and self.seedPoint() != self._placements[self._placementIndex]):
            self._restorePlacement()

    def _setEditing(self, editing: bool) -> None:
        # RP10: an Adjust closes the editor while detection is still open,
        # and the table stays locked until that closes too.
        locked = bool(editing) or self._detection is not None
        for widget in (self.table, self._add, self._edit, self._remove,
                       self._detect, self._merge):
            widget.setEnabled(not locked)
        self.editingChanged.emit(locked)

    # -- Plan 36 RP7: how many fluid regions? -------------------------------- #

    def detectButton(self):
        return self._detect

    def detectionPanel(self):
        """The open "how many fluid regions?" panel, or ``None``."""
        return self._detection

    def isDetecting(self) -> bool:
        return self._detection is not None

    def setDetectionHighlighter(self, highlighter) -> None:
        """What draws the candidate spaces in the viewport, or ``None``.

        Handed to every panel opened from here: it is called with the
        panel's `candidates()` list, and with ``[]`` when there is nothing
        to draw.
        """
        self._highlighter = highlighter
        if self._detection is not None:
            self._detection.setHighlighter(highlighter)

    def _buttonRow(self):
        layout = self.layout()
        for index in range(layout.count()):
            row = layout.itemAt(index).layout()
            if row is not None and row.indexOf(self._add) >= 0:
                return row
        return None

    def openDetection(self):
        """Open the panel under the table, at its first question."""
        if self.isEditing() or self._detection is not None:
            return self._detection
        names = [str(row.get('name') or '') for row in self._rows]
        panel = RegionDetectionPanel(self._client, self,
                                     existing_names=names,
                                     existing_count=len(self._rows))
        panel.setHighlighter(self._highlighter or self._highlightCandidates)
        panel.externalBox().toggled.connect(self._externalFlowChanged)
        panel.accepted.connect(self._detectionAccepted)
        panel.finished.connect(self._detectionFinished)
        panel.showOpenEdgesRequested.connect(self._showOpenEdges)
        # Plan 36 RP10. Adjust opens a row in the editor; Merge asks the
        # viewport's labelling which space each seed is in.
        panel.adjustRequested.connect(self.adjustCandidate)
        panel.setSpaceLookup(self._spaceAt)
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self._dock), panel)
        panel.show()
        self._detection = panel
        self._setEditing(True)
        panel.start()
        return panel

    def cancelDetection(self) -> None:
        """Close an open panel as Cancel would, if one is open."""
        if self._detection is not None:
            self._detection.cancel()

    def _detectionAccepted(self, written) -> None:
        self.refresh()
        self.childrenChanged.emit()
        # Plan 36 RP10. The accept is one saved edit: the Edit menu names
        # it, and the note says the one Undo takes every region back.
        update = getattr(app.window, '_updateMenuStates', None) \
            if app.window else None
        if callable(update):
            try:
                update()
            except Exception:  # noqa: BLE001 - the note still says it
                pass
        count = len(written or ())
        # Plan 37 #1(b): Accept all replaced the regions already defined
        # unless the panel was told to keep them; the note says which.
        panel = self._detection
        if panel is not None and panel.replacesExisting():
            self._say(self.tr(
                'Replaced the regions with %s. Ctrl+Z '
                '(Edit → Undo create fluid '
                'regions) brings the old ones back.')
                % count_text(count, 'region', 'regions'))
            return
        # DP-135/DP-217: the menu path is spelled with the one arrow, bound
        # to its names by no-break spaces so it never wraps on the arrow.
        self._say(self.tr(
            'Added %s. Ctrl+Z (Edit → Undo create fluid '
            'regions) takes them all back.')
            % count_text(count, 'region', 'regions'))

    # -- Plan 36 RP10: Adjust and Merge ------------------------------------- #

    @staticmethod
    def _spaceAt(point):
        """The label of the space *point* is in, 0 outside, None unknown."""
        volumes = getattr(_geometryManager(), 'regionVolumes', None)
        try:
            space = volumes().spaceAt(point) if volumes is not None else None
        except Exception:  # noqa: BLE001 - no labelling yet
            return None
        label = getattr(space, 'label', None)
        return int(label) if label is not None else None

    def adjustCandidate(self, index: int) -> None:
        """Open detection row *index* in the editor; OK keeps it there."""
        panel = self._detection
        row = panel.candidateAt(index) if panel is not None else None
        if row is None or self.isEditing():
            return
        values = {'name': row['name'], 'type': row['type']}
        values.update(zip(_POINT_KEYS, row['point']))
        for key, value in values.items():
            editor = self.editor(key)
            if editor is not None:
                editor.set_value(value)
        dialog = self.editor_dialog()
        dialog.applyRelevance()
        self._placingRegion = None

        def accepted() -> None:
            kind = self.editor('type')
            kind = getattr(kind.value(), 'value', kind.value()) \
                if kind is not None else None
            name = self.editor('name')
            panel.setCandidate(
                index, name=name.value() if name is not None else None,
                kind=kind, point=self.seedPoint())

        # Nothing is written until Accept all; the review waits meanwhile.
        panel.setEnabled(False)
        self._run_editor(dialog, accepted)
        self._adjusting = panel

    def mergeSelected(self) -> bool:
        """Drop the selected region if another's seed is in its space.

        RP13 #8: a seed is dropped only when the fluid-space field
        (`geometry.fluid_regions.seeds`) confirms the two share a space. A
        seed alone in its space, or one whose space is not known yet, is
        kept, and the note says why.
        """
        key = self.selected_key()
        if key is None:
            self._say(self.tr('Select the region to merge away.'), 'warning')
            return False
        key = str(key)
        spaces = self._readSeedSpaces() or self.seedSpaces()
        mine = (spaces.get(key) or {}).get('space')
        name = (spaces.get(key) or {}).get('name') or key
        if mine is None:
            self._say(self.tr(
                "Cannot tell %s's space yet (the domain is not labelled), "
                'so nothing was merged.') % name, 'warning')
            return False
        others = [str(row.get('name') or other)
                  for other, row in spaces.items()
                  if other != key and row.get('space') == mine]
        if not others:
            self._say(self.tr(
                '%s is the only seed in its space, so nothing was merged.')
                % name, 'warning')
            return False
        self._run(f'{self.collection_id}.remove', {'entity_id': key})
        self._say(self.tr('Merged %s into %s: they are one space.')
                  % (name, others[0]))
        return True

    def _detectionFinished(self, _accepted) -> None:
        panel, self._detection = self._detection, None
        if panel is None:
            return
        panel.hide()
        self.layout().removeWidget(panel)
        panel.deleteLater()
        self._setEditing(False)
        self.refresh()

    def _highlightCandidates(self, candidates) -> None:
        """RP6's region volumes draw the spaces the panel lists."""
        if candidates:
            self._showLabels(candidates)
        else:
            # DP-925. No review rows: the saved regions keep their labels.
            self._showLabels(self._savedLabels())
        show = getattr(_geometryManager(), 'showRegionCandidates', None)
        if show is None:
            return
        box = None
        if candidates and self._seedBounds is not None:
            try:
                box = self._seedBounds()
            except Exception:  # noqa: BLE001 - no domain yet: nothing drawn
                box = None
        show(candidates, box)

    def labelActors(self) -> list:
        """RP10. The viewport labels drawn over the candidates now."""
        return list(self._labelActors)

    def setRegionLabelsShown(self, shown: bool) -> None:
        """DP-925. Label the saved regions in the view while the page is up."""
        self._regionLabelsShown = bool(shown)
        self._showSavedLabels()

    def _savedLabels(self) -> list:
        """The saved regions as label rows: name, its space's volume, seed.

        DP-925 (Plan 36 RP12 live pass). After Accept all the view had no
        label at all: RP10 labelled only the review's candidates, and the
        review publishes ``[]`` as it closes, which took every label down
        with nothing put up in their place. The saved regions are labelled
        the same way, "fluid_1 · 0.0094 m³", while the page is up.
        """
        if not getattr(self, '_regionLabelsShown', False):
            return []
        spaces = getattr(self, '_seedSpaces', None) or {}
        listed = []
        for row in self._rows:
            key = str(row.get('__key__'))
            try:
                seed = [float(row[axis]) for axis in _POINT_KEYS]
            except (KeyError, TypeError, ValueError):
                continue
            space = spaces.get(key) or {}
            listed.append({'name': str(row.get('name') or key),
                           'volume': space.get('volume'), 'seed': seed,
                           'ticked': True})
        return listed

    def _showSavedLabels(self) -> None:
        """DP-925. The saved regions' labels, unless a review lists its own."""
        detection = getattr(self, '_detection', None)
        if detection is not None and detection.candidates():
            return
        self._showLabels(self._savedLabels())

    def _showLabels(self, candidates) -> None:
        """RP10. "Fluid 1 · 0.0100 m³" over each kept candidate."""
        display = getattr(app.window, 'displayControl', None) \
            if app.window else None
        for actor in self._labelActors:
            if display is not None:
                try:
                    display.removeOverlay(actor)
                except Exception:  # noqa: BLE001 - the view has gone
                    pass
        self._labelActors = []
        if not candidates:
            return
        self._labelActors = regionLabelActors(candidates)
        if display is None:
            return
        for actor in self._labelActors:
            display.addOverlay(actor)

    @staticmethod
    def _externalFlowChanged(external) -> None:
        """External flow: the space round the body is drawn as a region."""
        setExternal = getattr(_geometryManager(), 'setExternalFlow', None)
        if setExternal is not None:
            setExternal(bool(external))

    @staticmethod
    def _showOpenEdges() -> None:
        """Open edges are found and closed in Preparation."""
        navigation = (getattr(app.window, '_navigationView', None)
                      if app.window else None)
        if navigation is not None:
            navigation.setCurrentStep(Step.GEOMETRY_REPAIR)

    # -- Plan 36 RP8: each region its colour, and its space ------------------ #

    def refresh(self) -> None:
        # RP13 #8: not under an open editor unless nothing is unsaved.
        if (getattr(self, '_elsewhere', None) is not None
                and self.isEditing()):
            if self._refreshUnderEditor():
                return
        else:
            super().refresh()
        # `ChildControlPanel.__init__` refreshes before this panel's own
        # parts exist; `__init__` decorates once they do.
        if getattr(self, '_seedWarning', None) is not None:
            self._describe()
            self._decorateRows()

    def seedSpaces(self) -> dict:
        """Per region key, the space its seed is in (`fluid_regions.seeds`)."""
        return dict(self._seedSpaces)

    def seedWarning(self) -> str:
        """What is said under the table about shared spaces, or ``''``."""
        return '' if self._seedWarning.isHidden() else self._seedWarning.text()

    def _readSeedSpaces(self) -> dict:
        if not self._rows:
            return {}
        try:
            result = query(self._client, 'geometry.fluid_regions.seeds', {})
        except Exception:  # noqa: BLE001 - no case open, nothing to say
            return {}
        if getattr(result, 'status', '') != 'accepted':
            return {}
        payload = getattr(result, 'payload', None) or {}
        # Plan 37 #1 / #2: the flush-box, both-sides and duct warnings the
        # launch will carry, read with the same answer.
        self._placementWarnings = [
            str(one.get('message')) for one in payload.get('warnings') or ()
            if isinstance(one, dict) and one.get('message')]
        regions = payload.get('regions')
        return {str(key): dict(row)
                for key, row in dict(regions or {}).items()
                if isinstance(row, dict)}

    def _seedProblem(self, row, seed) -> str:
        """M4. Why a saved seed is not a material point, or ``''``."""
        try:
            point = tuple(float(row[key]) for key in self.POINT_KEYS)
        except (KeyError, TypeError, ValueError):
            return ''
        box = self._box()
        if box is not None and not self._inBox(point, box):
            return self.tr('Invalid point — outside bounding box.')
        if seed.get('space') is not None:
            if seed.get('outside'):
                return self.tr(SEED_MESSAGES['outside'])
            return ''
        try:
            payload = query(self._client, 'geometry.fluid_seed.check',
                            {'point': list(point)}).payload or {}
        except Exception:  # noqa: BLE001 - what cannot be judged is not wrong
            return ''
        if not payload.get('known'):
            return ''
        if payload.get('on_surface'):
            return self.tr('Invalid point — on the geometry surface.')
        if not (payload.get('inside') or payload.get('external')):
            return self.tr('Invalid point — outside the geometry.')
        return ''

    def _clashMessage(self, seed) -> str:
        """DP-2638. Why a Fluid and a Solid seed in *seed*'s space refuse
        the launch: the facade's `fluid_regions.clash_message`, or the same
        sentence built from the rows sharing the space."""
        from foammesh.core.mesh.fluid_regions import clash_message

        if seed.get('clash_message'):
            return str(seed['clash_message'])
        space = seed.get('space')
        members = [row for row in (self._seedSpaces or {}).values()
                   if row.get('clash') and row.get('space') == space]
        return clash_message({
            'regions': [str(row.get('name')) for row in members],
            'types': [str(row.get('type')) for row in members],
            'outside': bool(seed.get('outside'))})

    def _decorateRows(self) -> None:
        """The colour chip, the space's volume, and a shared space."""
        self._placementWarnings = []
        self._seedSpaces = self._readSeedSpaces()
        colours = region_zone_colours(
            [str(row.get('__key__')) for row in self._rows])
        lines, clashed = [], set()
        for index, row in enumerate(self._rows):
            key = str(row.get('__key__'))
            item = self.table.item(index, 0)
            if item is None:
                continue
            item.setIcon(chip_icon(colours.get(key)))
            seed = self._seedSpaces.get(key) or {}
            notes = [item.toolTip()] if item.toolTip() else []
            if seed.get('volume') is not None:
                notes.append(self.tr('Its space: %s')
                             % format_volume(seed['volume']))
            warning = ''
            if seed.get('clash'):
                # DP-2638: the launch gate's own sentence, so the page says
                # exactly what Run to end refuses.
                warning = self._clashMessage(seed)
                if seed.get('space') not in clashed:
                    clashed.add(seed.get('space'))
                    lines.append(warning)
            elif seed.get('same_as'):
                warning = (self.tr('Same space as %s: this seed adds '
                                   'nothing.') % seed['same_as'])
                name = seed.get('name') or row.get('name') or key
                lines.append('%s — %s' % (name, warning))
            if not warning:
                # M4. The saved seed keeps its own warning, as the legacy
                # card did: outside the box, on a surface, outside the body.
                warning = self._seedProblem(row, seed)
                if warning:
                    name = seed.get('name') or row.get('name') or key
                    lines.append('%s — %s' % (name, warning))
            if warning:
                notes.append(warning)
                item.setStatusTip(warning)
            if notes:
                item.setToolTip('\n'.join(notes))
        lines.extend(self.tr('Warning: %s') % text
                     for text in getattr(self, '_placementWarnings', ()))
        self._seedWarning.setText('\n'.join(lines))
        self._seedWarning.setVisible(bool(lines))
        self._showSavedLabels()


class ExcludePointPanel(ChildControlPanel):
    """Plan 37 UF16. The exclude points: spaces snappyHexMesh removes.

    Each row is one ``outsidePoints`` entry, in metres. The editor is docked
    under the table and not modal, like a region's, because the point is
    placed in the viewport: while it is open the point is drawn as the
    exclude handle (`SeedGizmo` role ``exclude`` -- a cube in the warning
    colour whose readout says "Exclude"), typing X, Y or Z moves it, and
    dragging it writes them.

    Under the table the preflight says what v13 would do with the points
    held (``core/mesh/exclude_points``): one off the domain, on its wall or
    on a region seed is an error, because v13 skips it silently and keeps
    everything; with region seeds present it warns that every space nobody
    seeded is kept too (live S1).
    """

    #: True while the editor is open, False when it closes.
    editingChanged = Signal(bool)
    DOCK_MIN_WIDTH = RegionSeedPanel.DOCK_MIN_WIDTH
    POINT_KEYS = RegionSeedPanel.POINT_KEYS
    POINT_DIGITS = RegionSeedPanel.POINT_DIGITS

    def _cell_text(self, key: str, value) -> tuple[str, str]:
        """Coordinates to six significant figures, as a region's (DP-920)."""
        shown = _point_cell_text(self, key, value)
        return shown if shown is not None else super()._cell_text(key, value)

    def __init__(self, facade_client, collection_id: str, title: str,
                 *args, **kwargs):
        self._openDialog = None
        self._editorAccepted = None
        self._gizmo = None
        self._watched = []
        self._bounds = None
        self._domain = None
        self._report = None
        self._findings = None
        super().__init__(facade_client, collection_id, title, *args,
                         **kwargs)
        layout = self.layout()
        self._findings = QLabel(self)
        self._findings.setObjectName('excludePointFindings')
        self._findings.setWordWrap(True)
        self._findings.setVisible(False)
        self._findings.setAccessibleName(self.tr('Exclude point findings'))
        layout.insertWidget(layout.indexOf(self.table) + 1, self._findings)
        # DP-1083. A write refused because the step is locked is said here,
        # under the table, instead of in a bare "Operation failed" box.
        self._lockNotice = QLabel(self)
        self._lockNotice.setObjectName('excludePointLockNotice')
        self._lockNotice.setWordWrap(True)
        self._lockNotice.setVisible(False)
        set_status(self._lockNotice, 'error')
        layout.insertWidget(layout.indexOf(self._findings) + 1, self._lockNotice)
        self._dock = QFrame(self)
        self._dock.setObjectName('excludeEditorDock')
        self._dock.setFrameShape(QFrame.Shape.StyledPanel)
        dockLayout = QVBoxLayout(self._dock)
        dockLayout.setContentsMargins(0, 0, 0, 0)
        self._dock.setVisible(False)
        layout.addWidget(self._dock)
        described = self.tr(
            'Points in spaces to remove from the mesh. Every space holding '
            'one is removed, unless a region seed is in the same space.')
        self.table.setToolTip(described)
        self._add.setAccessibleName(self.tr('Add exclude point'))
        self._add.setToolTip(self.tr(
            'Place a point in a space the mesh should remove — a closed '
            'pocket, or the outside of an internal-flow part'))
        self._edit.setAccessibleName(self.tr('Edit exclude point'))
        self._remove.setAccessibleName(self.tr('Remove exclude point'))
        self._showFindings()

    # -- what the preflight says -------------------------------------------- #

    def setBoundsProvider(self, provider) -> None:
        """*provider* answers the box an exclude point is dragged in."""
        self._bounds = provider

    def setDomainProvider(self, provider) -> None:
        """*provider* answers the `DomainBox` the points are judged against."""
        self._domain = provider
        self._showFindings()

    def _points(self) -> list:
        points = []
        for index, row in enumerate(self._rows):
            try:
                point = tuple(float(row[key]) for key in self.POINT_KEYS)
            except (KeyError, TypeError, ValueError):
                continue
            name = str(row.get('name') or '').strip()
            points.append((name or self.tr('exclude %s') % (index + 1), point))
        return points

    def _seeds(self) -> list:
        try:
            regions = dict(self._client.configuration().get('region') or {})
        except Exception:  # noqa: BLE001 - no case open
            return []
        seeds = []
        for key, row in regions.items():
            if not isinstance(row, dict) or row.get('gType') is not None:
                continue
            point = row.get('point')
            try:
                xyz = tuple(float(point[axis]) for axis in 'xyz')
            except (KeyError, TypeError, ValueError):
                continue
            seeds.append((str(row.get('name') or key), 'fluid', xyz))
        return seeds

    def findings(self):
        """The preflight `Report` for the points the table holds."""
        from foammesh.core.mesh.exclude_points import preflight

        domain = None
        if self._domain is not None:
            try:
                domain = self._domain()
            except Exception:  # noqa: BLE001 - no case, no domain check
                domain = None
        return preflight(self._points(), seeds=self._seeds(), domain=domain)

    def findingsLabel(self):
        return self._findings

    # -- a write refused because the step is locked (DP-1083) --------------- #

    def lockNotice(self) -> QLabel:
        return self._lockNotice

    @staticmethod
    def _lockRefusal(result):
        """The lock refusal's details when *result* is one, else ``None``."""
        from foammesh.core.facade.domain_operations import TaskLockedError

        error = getattr(result, 'error', None)
        payload = getattr(result, 'payload', None)
        if isinstance(error, TaskLockedError):
            return dict(getattr(error, 'details', None) or {})
        if isinstance(payload, dict) and payload.get('unlock_operation'):
            return dict(payload)
        return None

    def _close_write(self, write, ok: bool, result=None) -> None:
        details = (self._lockRefusal(result)
                   if not ok and result is not None else None)
        if details is not None and write is not None and not write['closed']:
            titles = [str(title) for title in (details.get('titles') or ())]
            what = ', '.join(titles) or self.tr('A meshing step')
            self._lockNotice.setText(self.tr(
                'Not saved: {0} {1} locked, because the mesh on disk was made '
                'from these points. Unlock the step (right-click it in the '
                'workflow) to change them; the results after it are '
                'discarded.').format(
                    what, self.tr('is') if len(titles) <= 1 else self.tr('are')))
            self._lockNotice.setAccessibleDescription(self._lockNotice.text())
            self._lockNotice.setVisible(True)
            super()._close_write(write, False, None)
            return
        if ok:
            self._lockNotice.setVisible(False)
        super()._close_write(write, ok, result)

    def findingsText(self) -> str:
        """What is said under the table, or ``''``."""
        if self._findings is None or self._findings.isHidden():
            return ''
        return self._findings.text()

    def _showFindings(self) -> None:
        if self._findings is None:
            return
        report = self.findings() if self._rows else None
        self._report = report
        if report is None or not report.findings:
            self._findings.setText('')
            self._findings.setVisible(False)
            return
        lines = [finding.message for finding in report.errors]
        lines += [finding.message for finding in report.warnings]
        self._findings.setText('\n'.join(lines))
        set_status(self._findings, 'error' if report.errors else 'warning')
        self._findings.setAccessibleDescription(self._findings.text())
        self._findings.setVisible(True)

    def refresh(self) -> None:
        super().refresh()
        self._showFindings()

    # -- the editor stays open beside a live viewport ----------------------- #

    def isEditing(self) -> bool:
        return self._openDialog is not None

    def excludeGizmo(self):
        """The handle the open editor's point is dragged by, or ``None``."""
        return self._gizmo

    def open_add_dialog(self) -> None:
        if not self.isEditing():
            super().open_add_dialog()

    def open_edit_dialog(self, *args) -> None:
        if not self.isEditing():
            super().open_edit_dialog(*args)

    def _run_editor(self, dialog, accepted) -> None:
        self._editorAccepted = accepted
        self._openDialog = dialog
        dialog.finished.connect(self._finishEditor)
        self._setEditing(True)
        try:
            for key in self.POINT_KEYS:
                editor = self.editor(key)
                if editor is not None:
                    editor.valueChanged.connect(self._placePoint)
                    self._watched.append(editor)
            self._placePoint()
            if self.width() >= self.DOCK_MIN_WIDTH:
                dialog.showDocked(self._dock)
            else:
                self._dock.setVisible(False)
                dialog.showFloating(self.window())
        except Exception:
            self._closeEditor()
            raise

    def _finishEditor(self, result) -> None:
        accepted = self._editorAccepted
        self._closeEditor()
        if result == QDialog.DialogCode.Accepted and accepted is not None:
            accepted()

    def cancelEditor(self) -> None:
        dialog = self._openDialog
        if dialog is not None:
            dialog.reject()

    def _closeEditor(self) -> None:
        dialog, self._openDialog = self._openDialog, None
        self._editorAccepted = None
        if dialog is not None:
            try:
                dialog.finished.disconnect(self._finishEditor)
            except (RuntimeError, TypeError):
                pass
        for editor in self._watched:
            try:
                editor.valueChanged.disconnect(self._placePoint)
            except (RuntimeError, TypeError):
                pass
        self._watched = []
        self._unlinkGizmo()
        preview = getattr(_geometryManager(), 'previewExclude', None)
        if preview is not None:
            preview(None)
        try:
            self._dock.setVisible(False)
        except RuntimeError:
            return
        self._setEditing(False)

    def _setEditing(self, editing: bool) -> None:
        for widget in (self.table, self._add, self._edit, self._remove):
            widget.setEnabled(not editing)
        if not editing:
            self._edit.setEnabled(bool(self._rows))
            self._remove.setEnabled(bool(self._rows))
        self.editingChanged.emit(bool(editing))

    def _point(self):
        values = []
        for key in self.POINT_KEYS:
            editor = self.editor(key)
            if editor is None:
                return None
            try:
                values.append(float(editor.value()))
            except (TypeError, ValueError):
                return None
        return tuple(values)

    def _placePoint(self, *_args) -> None:
        point = self._point()
        manager = _geometryManager()
        preview = getattr(manager, 'previewExclude', None)
        if point is None or preview is None:
            return
        preview(point)
        handle = getattr(manager, 'excludePreview', None)
        gizmo = handle() if handle is not None else None
        if gizmo is self._gizmo or not hasattr(gizmo, 'pointMoved'):
            return
        self._unlinkGizmo()
        self._gizmo = gizmo
        gizmo.pointMoved.connect(self._gizmoMoved)
        bounds = None
        if self._bounds is not None:
            try:
                bounds = self._bounds()
            except Exception:  # noqa: BLE001 - no case, no box
                bounds = None
        gizmo.setBounds(bounds)
        step = None
        cellSize = getattr(manager, 'getCellSize', None)
        if cellSize is not None:
            try:
                step = cellSize()
            except Exception:  # noqa: BLE001 - no base grid yet
                step = None
        gizmo.setStep(step)
        # Like a seed, an exclude point on a mesh face is perturbed into
        # whichever cell v13 finds (live S11): kept off every face.
        grid, level = RegionSeedPanel._backgroundFaces()
        gizmo.setSnapOrigin(grid.origin if grid is not None else None)
        gizmo.setFaceGuard(grid, level)

    def _unlinkGizmo(self) -> None:
        gizmo, self._gizmo = self._gizmo, None
        if gizmo is None:
            return
        try:
            gizmo.pointMoved.disconnect(self._gizmoMoved)
        except (RuntimeError, TypeError):
            pass

    def _gizmoMoved(self, point) -> None:
        for key, value in zip(self.POINT_KEYS, point):
            editor = self.editor(key)
            if editor is not None:
                editor.set_value(value)


class SnappyDomainRegionsPage(SnappyTaskPage):
    """The material points that say which side of the surface is meshed."""

    task_id_default = 'snappy.domain_regions'

    #: ``regions.items`` element fields, in the order they are read in.
    #: DP-570 (0924 rerun follow-up). MEASURED at the 360 px settings
    #: column: 325 px of columns in a 314 px table, 11 px of scroll. The
    #: stretched `Z` was a number, so every column -- the name and the type
    #: too -- was floored at a coordinate's width (DP-535). The name
    #: stretches instead, the three coordinates keep their floor, and all
    #: five fit.
    COLUMNS = ('name', 'type', 'point.x', 'point.y', 'point.z')

    #: Plan 31. Written into every ``geometry`` entry of snappyHexMeshDict,
    #: absent from the task descriptor. They are not ``castellatedMeshControls``
    #: keys -- they describe the surfaces themselves -- so they are named here
    #: rather than declared on a stage that does not own them.
    #:
    #: Plan 40 UXD9 (#38). Only the two that say what the surfaces *are* stay
    #: here: whether they are closed, which is what a region seed's inside and
    #: outside depend on, and their scale. How the surfaces are searched and
    #: how narrow a gap counts as closed act when castellation intersects the
    #: base grid with them, so they are edited on Castellation.
    extra_field_ids = (
        'meshing.geometry.tri_surface_declaration',
        'meshing.geometry.scale',
    )

    #: Plan 32 check 12. MEASURED on the page widget before this moved: 134
    #: words over four labels beside a single input. Two paragraphs of them
    #: were why a material point matters and how OpenFOAM decides a surface
    #: is closed -- reasoning, which §4.5 puts behind the help control.
    HELP_DETAIL = (
        'A region names a point inside the volume to keep. snappyHexMesh '
        'keeps the cells reachable from it, so a point on the wrong side '
        'of a surface produces the complement of the mesh you wanted. '
        'Inside and outside only exist for a surface OpenFOAM considers '
        'closed. It decides that by looking for open edges, so a surface '
        'meant to be watertight that has a pinhole or arrives in several '
        'parts answers "neither" — and refinement regions and cell zones '
        'built on it are then dropped with a note in the log. Declare the '
        'surfaces closed to override that. Narrow gaps and the surface '
        'search are set on Castellation. '
        'Detect (Alt+T) finds the spaces the surfaces close off and places '
        'a region in each one you keep; one Undo takes them all back. A '
        'seed can be dragged in the view, nudged with Alt and an arrow key '
        'and cut to on a Section plane (Alt+O); Ctrl+Z undoes a placement '
        'while the editor is open. Exclude points name spaces to remove: '
        'every space holding one is taken out of the mesh, unless a region '
        'seed is in the same space.')

    def build_sections(self, layout) -> None:
        # Plan 33 OF-02. The table opens the column. The two sentences that
        # used to stand above it were a third copy of `HELP_DETAIL`, which
        # `_moveProseBehindHelp` already sends to the help control and to the
        # page tooltip, so what they said is still one press away and the
        # reader no longer scrolls past it to reach the only editor here.
        self.panel = RegionSeedPanel(
            self._client, 'regions.items', self.tr('Regions'),
            columns=self.COLUMNS, parent=self, stretch='name')
        self.panel.childrenChanged.connect(self.refresh_keeping_edits)
        # DP-818. The glyphs follow the table: added, moved or deleted.
        self.panel.childrenChanged.connect(self._reloadSeedMarkers)
        self.panel.childrenChanged.connect(self._sayInterface)
        # Plan 36 RP2. The open editor is the only thing that edits here.
        self.panel.editingChanged.connect(self._lockWhileEditing)
        # Plan 36 RP3. A dragged seed stays in the box drawn around it (RP1).
        self.panel.setSeedBoundsProvider(self._seedBounds)
        layout.insertWidget(0, self.panel)
        # Plan 37 UF16. The spaces to remove, under the seeds that keep.
        self.excludePanel = ExcludePointPanel(
            self._client, 'meshing.castellation.exclude_points',
            self.tr('Exclude points'), columns=self.COLUMNS[:1] + self.COLUMNS[2:],
            parent=self, stretch='name')
        self.excludePanel.setBoundsProvider(self._seedBounds)
        self.excludePanel.setDomainProvider(lambda: self.domainBox())
        self.excludePanel.childrenChanged.connect(self._reloadSeedMarkers)
        self.excludePanel.editingChanged.connect(self._lockForExclude)
        # A region added or moved can put a seed on an exclude point.
        self.panel.childrenChanged.connect(self.excludePanel.refresh)
        layout.insertWidget(1, self.excludePanel)
        # Plan 37 UF14. With a farfield the seeds live inside it, not in the
        # block: say so, and which patch it becomes. Hidden without one.
        self.farfieldNote = QLabel(self)
        self.farfieldNote.setObjectName('farfieldNote')
        self.farfieldNote.setWordWrap(True)
        self.farfieldNote.hide()
        layout.insertWidget(2, self.farfieldNote)
        # DP-2265. The legacy Region page's interface warning, in place.
        self.interfaceNote = QLabel(self)
        self.interfaceNote.setObjectName('regionInterfaceWarning')
        self.interfaceNote.setWordWrap(True)
        set_status(self.interfaceNote, 'warning')
        self.interfaceNote.hide()
        layout.insertWidget(3, self.interfaceNote)
        # DP-2636. Imported bodies that share walls: offer the cut, in view.
        self.splitPrompt = QFrame(self)
        self.splitPrompt.setObjectName('regionSplitPrompt')
        prompt = QVBoxLayout(self.splitPrompt)
        prompt.setContentsMargins(0, 0, 0, 0)
        self.splitPromptText = QLabel(self.splitPrompt)
        self.splitPromptText.setObjectName('regionSplitPromptText')
        self.splitPromptText.setWordWrap(True)
        set_status(self.splitPromptText, 'warning')
        prompt.addWidget(self.splitPromptText)
        self.splitButton = QPushButton(
            self.tr('Split interfaces…'), self.splitPrompt)
        self.splitButton.setObjectName('regionSplitInterfaces')
        self.splitButton.setToolTip(self.tr(
            'Open the Geometry step and cut each wall two bodies share into '
            'an interface that couples their regions'))
        self.splitButton.clicked.connect(self._openSplitInterfaces)
        prompt.addWidget(self.splitButton, 0, Qt.AlignmentFlag.AlignLeft)
        self.splitPrompt.hide()
        layout.insertWidget(4, self.splitPrompt)
        # Item 7 (CHT2). A cell zone written under another name says so.
        self.writtenNamesNote = QLabel(self)
        self.writtenNamesNote.setObjectName('regionWrittenNames')
        self.writtenNamesNote.setWordWrap(True)
        set_status(self.writtenNamesNote, 'info')
        self.writtenNamesNote.hide()
        layout.insertWidget(5, self.writtenNamesNote)
        self._sharedWallsFound.connect(self._onSharedWalls)

    #: Plan 36 RP2. What each control's enabled state was before an editor
    #: opened, so closing it gives back exactly that -- a Run button shut for
    #: a reason stays shut.
    _lockedControls = None

    def _pageControls(self, editing=None):
        editing = self.panel if editing is None else editing
        layout = self._body_layout
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is not None and widget is not editing:
                yield widget
        yield from (self._preview, self._update, self._revert, self._runStage)

    def _lockForExclude(self, editing: bool) -> None:
        """Plan 37 UF16. The exclude editor locks the page, regions too."""
        self._lockWhileEditing(editing, self.excludePanel)

    def _lockWhileEditing(self, editing: bool, panel=None) -> None:
        """Disable the rest of the page while a region is being placed."""
        if editing:
            if self._lockedControls is not None:
                return
            self._lockedControls = [(widget, widget.isEnabled())
                                    for widget in self._pageControls(panel)]
            for widget, _enabled in self._lockedControls:
                widget.setEnabled(False)
            return
        locked, self._lockedControls = self._lockedControls, None
        for widget, enabled in locked or ():
            try:
                widget.setEnabled(enabled)
            except RuntimeError:
                continue

    @staticmethod
    def _reloadSeedMarkers() -> None:
        reload_ = getattr(_geometryManager(), 'reloadRegions', None)
        if reload_ is not None:
            reload_()

    @staticmethod
    def _showSeedMarkers(shown: bool) -> None:
        setShown = getattr(_geometryManager(), 'setRegionMarkersShown', None)
        if setShown is not None:
            setShown(shown)

    #: Plan 36 RP1. The background mesh's box while this page is up.
    _domainBoxActor = None

    @staticmethod
    def domainBox():
        """The box the background mesh will span, or ``None`` with no case.

        Asked of `domain_box`, the rule the writer uses, with the surfaces'
        extent from the viewport -- the same extent a run hands the case
        builder (DP-576).
        """
        client = app.facadeClient
        try:
            db = client.checkout()
        except Exception:  # noqa: BLE001 - no case open yet
            db = None
        try:
            case_path = client.case_root
        except Exception:  # noqa: BLE001 - an untitled case has no folder
            case_path = None
        surfaces = getattr(_geometryManager(), 'getSurfaceBounds', None)
        extent = surfaces() if surfaces is not None else None
        return domain_box(db, case_path,
                          extent.toTuple() if extent is not None else None)

    @classmethod
    def _seedBounds(cls):
        box = cls.domainBox()
        if box is None:
            return None
        # RP13 #5/#7: a domain that is not a box travels with its bounds, so
        # the region volumes label an L as an L.
        if not getattr(box, 'cuboid', True) and getattr(box, 'blocks', ()):
            from foammesh.core.mesh.fluid_spaces import DomainExtent

            return DomainExtent(box.bounds, box)
        return box.bounds

    def _showDomainBox(self, shown: bool) -> None:
        """Plan 36 RP1 (F4). A seed only counts inside the box, so draw it."""
        display = getattr(app.window, 'displayControl', None)             if app.window else None
        if display is None:
            return
        if self._domainBoxActor is not None:
            display.removeOverlay(self._domainBoxActor)
            self._domainBoxActor = None
        self._showFarfield(display, False)
        if not shown:
            return
        box = self.domainBox()
        if box is None:
            return
        # RP13 #5. A domain that is not a box is drawn block by block.
        self._domainBoxActor = domainBoxActor(
            box.bounds, blocks=None if box.cuboid else box.outlines())
        display.addOverlay(self._domainBoxActor)
        self._showFarfield(display, True)

    #: Plan 37 UF14. The farfield's skin and its label while this page is up.
    _farfieldActors = ()

    @staticmethod
    def farfield():
        """The resolved farfield, or ``None`` (none, no case, or unbuildable).

        Resolved around the surfaces' extent from the viewport, the extent a
        run hands the case builder, so what is drawn is what is written.
        """
        from foammesh.core.mesh import snappy_farfield

        try:
            db = app.facadeClient.checkout()
        except Exception:  # noqa: BLE001 - no case open yet
            return None
        spec = snappy_farfield.active(db)
        if spec is None:
            return None
        surfaces = getattr(_geometryManager(), 'getSurfaceBounds', None)
        extent = surfaces() if surfaces is not None else None
        if extent is None:
            return None
        try:
            return snappy_farfield.resolve(spec, extent.toTuple())
        except ValueError:
            return None

    def _showFarfield(self, display, shown: bool) -> None:
        """The farfield drawn apart from the block, and labelled (UF14)."""
        for actor in self._farfieldActors:
            try:
                display.removeOverlay(actor)
            except Exception:  # noqa: BLE001 - the view has gone
                pass
        self._farfieldActors = ()
        if not shown:
            return
        farfield = self.farfield()
        if farfield is None:
            return
        self._farfieldActors = (farfieldActor(farfield.primitive),
                                farfieldLabelActor(farfield.primitive))
        for actor in self._farfieldActors:
            display.addOverlay(actor)

    def _sayFarfield(self) -> None:
        """Plan 37 UF14. The outer boundary, its patch and what a seed means."""
        note = getattr(self, 'farfieldNote', None)
        if note is None:
            return
        from foammesh.core.mesh import snappy_farfield

        try:
            text = snappy_farfield.support_text(self._client.checkout())
        except Exception:  # noqa: BLE001 - no case open yet
            text = ''
        if text:
            # Plan 37. Where the farfield is changed, now that it has an
            # entry of its own on the Geometry page.
            text = ' '.join((text, self.tr(
                'It is changed from Edit farfield… on the Geometry page.')))
        note.setText(text)
        note.setVisible(bool(text))

    def _regionPoints(self) -> list:
        points = []
        for row in self.panel.rows():
            try:
                points.append(tuple(float(row[key]) for key in
                                    ('point.x', 'point.y', 'point.z')))
            except (KeyError, TypeError, ValueError):
                continue
        return points

    def interfaceWarning(self) -> str:
        """DP-2265. Do the regions and the inter-region interfaces agree?

        Ported from the removed Region page. More than one region with no
        inter-region interface on the Geometry page means the regions may
        share a boundary nothing couples; one region with such an interface
        means the interface has nothing to join. DP-543: regions whose seeds
        provably sit in shells that never touch have nothing to couple, so
        the first case stays quiet for them (anything unjudgeable keeps it).
        """
        from foammesh.db.configurations_schema import CFDType

        try:
            db = self._client.checkout()
            interfaces = db.elementCount(
                'geometry', lambda i, e: e['cfdType'] == CFDType.INTERFACE.value
                and e['interRegion'])
            # DP-2636. Split interfaces makes conformal interfaces (internal
            # faces, ``interRegion`` off) that couple the regions too:
            # MEASURED live (CHT2 walk SR) the note said nothing coupled
            # three regions the cut had just joined.
            conformal = db.elementCount(
                'geometry', lambda i, e: e['cfdType'] == CFDType.INTERFACE.value
                and not e['interRegion'])
        except Exception:  # noqa: BLE001 - no case open yet
            return ''
        regions = len(self.panel.rows())
        multiRegion = regions > 1
        if multiRegion and conformal > 0:
            return ''
        if multiRegion == (interfaces > 0):
            return ''
        if multiRegion:
            if self._regionsSeparated():
                return ''
            # DP-2636: the step has no "add an interface" control; the
            # interface is made by cutting the shared walls.
            return self.tr(
                'No inter-region interface is configured, and these regions '
                'may share a boundary that nothing couples. Split interfaces '
                'on the Geometry step cuts each wall two bodies share into an '
                'interface that couples them.')
        return self.tr(
            'Only one region point is configured while an inter-region '
            'interface is configured on the Geometry step.')

    def _regionsSeparated(self) -> bool:
        """DP-543. Do the seeds sit in closed shells that never touch?"""
        project = getattr(app, 'project', None)
        if project is None or getattr(project, 'path', None) is None:
            return False
        try:
            from foammesh.core.geometry import GeometryArtifactStore
            from foammesh.core.geometry.region_contact import regions_separated
            return regions_separated(
                GeometryArtifactStore(project.path).entries(),
                self._regionPoints())
        except Exception:  # noqa: BLE001 - what cannot be judged keeps the warning
            return False

    def _sayInterface(self) -> None:
        note = getattr(self, 'interfaceNote', None)
        if note is None:
            return
        text = self.interfaceWarning()
        note.setText(text)
        # DP-2636: the Split prompt below says it with the walls named.
        prompt = getattr(self, 'splitPrompt', None)
        note.setVisible(bool(text) and not (
            prompt is not None and not prompt.isHidden()))
        self._sayWrittenNames()

    def writtenNames(self) -> list[tuple[str, str]]:
        """(volume name, cell zone written) where the two differ.

        CHT2 item 7. A volume typed cell zone whose name starts with a digit
        (the issue's ``02_pipe_solid_r10to12mm_L500mm``, named after its file)
        is written ``zone_02_pipe_...``: OpenFOAM reads a leading digit as a
        number. That is the name the mesh, checkMesh and the solver show, so
        the page names it beside the one the user sees on the Geometry step.
        """
        from foammesh.db.configurations_schema import CFDType
        from foammesh.core.mesh.zone_names import cell_zone_word

        try:
            volumes = self._client.checkout().getElements(
                'geometry', lambda _key, item: item.get('gType') == 'volume'
                and item.get('cfdType') == CFDType.CELL_ZONE.value)
        except Exception:  # noqa: BLE001 - no case open yet
            return []
        pairs = []
        for _key, volume in sorted(volumes.items()):
            try:
                name = str(volume.value('name') or '')
            except Exception:  # noqa: BLE001 - a row without a name
                continue
            written = cell_zone_word(name)
            if name and written != name:
                pairs.append((name, written))
        return pairs

    def _sayWrittenNames(self) -> None:
        note = getattr(self, 'writtenNamesNote', None)
        if note is None:
            return
        pairs = self.writtenNames()
        if pairs:
            note.setText(self.tr(
                'Written to the mesh as {0}. An OpenFOAM name cannot start '
                'with a digit, so checkMesh and the solver show these '
                'names.').format('; '.join(
                    f'{name} \u2192 {written}' for name, written in pairs)))
        note.setVisible(bool(pairs))

    # -- DP-2636: bodies that share walls ------------------------------------ #

    #: From the worker that measured the shared walls: (key, names).
    _sharedWallsFound = Signal(object, object)
    _sharedWallsKey = None
    _sharedWallsNames: list = []
    _sharedWallsBusy = None

    @staticmethod
    def _geometryStore():
        project = getattr(app, 'project', None)
        path = getattr(project, 'path', None) if project is not None else None
        if path is None:
            return None
        from foammesh.core.geometry import GeometryArtifactStore

        return GeometryArtifactStore(path)

    def _checkSharedWalls(self) -> None:
        """Do the imported bodies share walls? Measured off the GUI thread.

        DP-2636. Three bodies drawn conformally -- a hot core, a pipe, a cold
        annulus -- share two walls. Seeded as they are, each region meshes on
        its own and nothing couples them; the cut that makes the interfaces
        (`Split interfaces`) sat on the Geometry step with nothing here
        pointing at it. The same preview that button runs answers it, once
        per set of geometry revisions; it writes nothing.
        """
        try:
            store = self._geometryStore()
            entries = store.entries() if store is not None else []
        except Exception:  # noqa: BLE001 - no case, nothing to offer
            entries = []
        if len(entries) < 2:
            self._showSplitPrompt([])
            return
        key = tuple(str(entry.get('artifact')) for entry in entries)
        if key == self._sharedWallsKey:
            self._showSplitPrompt(self._sharedWallsNames)
            return
        if key == self._sharedWallsBusy:
            return
        self._sharedWallsBusy = key
        import threading

        threading.Thread(target=self._measureSharedWalls, args=(store, key),
                         name='shared-walls', daemon=True).start()

    def _measureSharedWalls(self, store, key) -> None:
        """Worker: the interfaces `split_interfaces` would cut."""
        try:
            found = store.split_interfaces(None, preview=True)
            names = [str(item.get('name'))
                     for item in found.get('interfaces') or ()]
        except Exception:  # noqa: BLE001 - what cannot be cut offers nothing
            names = []
        try:
            self._sharedWallsFound.emit(key, names)
        except RuntimeError:      # the page went while the walls were read
            pass

    def _onSharedWalls(self, key, names) -> None:
        self._sharedWallsKey = key
        self._sharedWallsNames = list(names)
        self._sharedWallsBusy = None
        self._showSplitPrompt(self._sharedWallsNames)

    def _showSplitPrompt(self, names) -> None:
        prompt = getattr(self, 'splitPrompt', None)
        if prompt is None:
            return
        names = list(names or ())
        if names and self._interfaceCount() > 0:
            names = []          # already split: the interfaces couple them
        if names:
            self.splitPromptText.setText(self.tr(
                'The imported bodies share {0}: {1}. Seeded as they are, '
                'each region is meshed on its own and nothing couples them '
                'across those walls. Split interfaces cuts each shared wall '
                'into an interface between the two regions. Nothing is '
                'split until you do.').format(
                    count_text(len(names), 'wall'), ', '.join(names)))
        prompt.setVisible(bool(names))
        self._sayInterface()

    def _interfaceCount(self) -> int:
        from foammesh.db.configurations_schema import CFDType

        try:
            return int(self._client.checkout().elementCount(
                'geometry', lambda i, e: e['cfdType']
                == CFDType.INTERFACE.value and e['interRegion']))
        except Exception:  # noqa: BLE001 - no case open yet
            return 0

    def splitPromptText_(self) -> str:
        """The prompt as shown, or '' while it is hidden (for tests)."""
        prompt = getattr(self, 'splitPrompt', None)
        if prompt is None or prompt.isHidden():
            return ''
        return self.splitPromptText.text()

    def _openSplitInterfaces(self) -> None:
        """Go to the Geometry step and start its Split interfaces.

        The cut is the Geometry step's own action, with its own preview and
        confirmation; nothing is split from here. A Geometry step locked by
        Proceed is unlocked through its own question first (editing the
        geometry discards what was made after it), and the cut starts once
        the step is open again.
        """
        window = app.window
        navigation = getattr(window, '_navigationView', None)
        manager = getattr(window, '_stepManager', None)
        page = (getattr(manager, '_pages', {}) or {}).get(Step.GEOMETRY)
        if navigation is None or page is None:
            return
        navigation.setCurrentStep(Step.GEOMETRY)
        if getattr(page, '_locked', False):
            page._editLockedGeometry()
            self._splitWhenUnlocked(page, attempts=40)
            return
        page._splitInterfacesClicked()

    def _splitWhenUnlocked(self, page, attempts: int) -> None:
        """Start the cut once the step manager has unlocked the page."""
        from PySide6.QtCore import QTimer

        if not getattr(page, '_locked', False):
            page._splitInterfacesClicked()
            return
        if attempts <= 0:
            return            # the user kept the geometry: nothing to cut
        QTimer.singleShot(
            50, lambda: self._splitWhenUnlocked(page, attempts - 1))

    def showEvent(self, event) -> None:
        """DP-818. The seed glyphs are drawn while this page is up."""
        super().showEvent(event)
        if not event.spontaneous():
            self._showSeedMarkers(True)
            self._showDomainBox(True)
            # DP-925. The saved regions carry their labels on this page.
            self.panel.setRegionLabelsShown(True)
            self._offerDetection()
            # DP-2636. Bodies sharing walls are offered the cut, here.
            self._checkSharedWalls()

    def _offerDetection(self) -> None:
        """Plan 36 RP7 (D7). Ask once per case, while it has no regions.

        The facade decides and remembers (`geometry.fluid_regions.offer`),
        so leaving the page and coming back does not ask again.
        """
        panel = self.panel
        if panel.rows() or panel.isEditing() or panel.isDetecting():
            return
        if offer_detection(self._client):
            panel.openDetection()

    def hideEvent(self, event) -> None:
        """...and put away when it is left, like every page-scoped actor."""
        super().hideEvent(event)
        if not event.spontaneous():
            # Plan 36 RP13 #8. The editor stays docked for the way back; a
            # drag it had not placed yet goes back to where it started.
            self.panel.revertDrag()
            self.panel.cancelDetection()
            self._showSeedMarkers(False)
            self._showDomainBox(False)
            self.panel.setRegionLabelsShown(False)

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()
        self._sayFarfield()
        self._sayInterface()

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the control DP-230 already built.

        `_description` is where a task page authors what the step is for, and
        `refresh()` rewrites it from the descriptor on every pass, so the
        page's own paragraphs are appended after that and the help is re-read
        from the label rather than set beside it. The two therefore stay the
        same two strings, which is what the DP-230 gate asserts.
        """
        described = self._description.text().strip()
        if self.HELP_DETAIL not in described:
            described = (described + ' ' + self.HELP_DETAIL).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())
