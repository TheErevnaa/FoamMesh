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
from foammesh.core.mesh.domain_box import domain_box
from foammesh.db.configurations_schema import Step
from foammesh.rendering.domain_box_actor import domainBoxActor
from foammesh.rendering.region_labels import regionLabelActors
from foammesh.view.facade_client import query
from foammesh.view.region.seed_feedback import SeedFeedback
from foammesh.view.theming.patch_palette import region_zone_colours
from foammesh.view.theming.status_colors import set_status
from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import SnappyTaskPage
from .region_detection_panel import (
    RegionDetectionPanel, chip_icon, format_volume, offer_detection)


#: The coordinates a seed is typed into, in the order of a point.
_POINT_KEYS = ('point.x', 'point.y', 'point.z')


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
    (Alt+A) opens the editor, the coordinates are typed, Alt and an arrow
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
        if key in self.POINT_KEYS and not isinstance(value, bool):
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = None
            if number is not None:
                text = format(number, f'.{self.POINT_DIGITS}g')
                stored = str(value)
                return text, (stored if stored != text else '')
        return super()._cell_text(key, value)

    #: Plan 36 RP2. True while the editor is open, False when it closes.
    editingChanged = Signal(bool)
    #: Plan 36 RP13 #8. From the event bus, whatever thread it publishes on:
    #: the stored rows may have changed, or the case is being switched.
    _changedElsewhere = Signal()
    _caseSwitched = Signal()

    def __init__(self, *args, **kwargs):
        # Built with no parent (DP-312): the dialog it is shown in adopts it.
        self.seedFeedback = SeedFeedback()
        annotations = dict(kwargs.pop('annotations', None) or {})
        annotations.setdefault('point.z', self.seedFeedback)
        super().__init__(*args, annotations=annotations, **kwargs)
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
        self._detect.setToolTip(self.tr(
            'Find the spaces the surfaces close off, and place a region in '
            'each one you keep (%s).') % self.DETECT_SHORTCUT)
        self._detect.clicked.connect(self.openDetection)
        # Plan 36 RP10. Merge shares Detect's row: Add, Edit and Remove
        # already fill theirs at the narrowest column.
        self._merge = QPushButton(self.tr('Merge'), self)
        self._merge.setObjectName('regionMerge')
        self._merge.setShortcut(QKeySequence(self.MERGE_SHORTCUT))
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
        self.table.setAccessibleDescription(self.tr(
            'One row per region: its name, type and seed point. A chip in '
            "the region's colour marks each row; the tooltip says its "
            'space and volume.'))
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
        self._addSectionButton(dialog)
        # Both open paths call this after the values are set and before the
        # dialog is shown, so this is where the first verdict is given.
        if self.seedFeedback.isActive():
            self._judgeSeed()
        return dialog

    def open_add_dialog(self) -> None:
        if self.isEditing() or self.isDetecting():
            return
        self._placingRegion = None
        super().open_add_dialog()

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
        row.setContentsMargins(4, 2, 4, 2)
        label = QLabel(self.tr('Regions changed elsewhere'), bar)
        label.setWordWrap(True)
        set_status(label, 'warning')
        row.addWidget(label, 1)
        reload_ = QPushButton(self.tr('Reload'), bar)
        reload_.setObjectName('regionsReload')
        reload_.setToolTip(self.tr(
            'Close the editor without saving and show the regions as they '
            'are stored now.'))
        reload_.clicked.connect(self.reloadElsewhere)
        keep = QPushButton(self.tr('Keep mine'), bar)
        keep.setObjectName('regionsKeepMine')
        keep.setToolTip(self.tr(
            'Go on editing; OK writes this region over what changed.'))
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
        self._say(self.tr(
            'Added %d region(s). Ctrl+Z (Edit > Undo create fluid regions) '
            'takes them all back.') % count)

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
        regions = (getattr(result, 'payload', None) or {}).get('regions')
        return {str(key): dict(row)
                for key, row in dict(regions or {}).items()
                if isinstance(row, dict)}

    def _decorateRows(self) -> None:
        """The colour chip, the space's volume, and a shared space."""
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
                warning = self.tr('Fluid and Solid seeds in one space: '
                                  'snappy cannot keep both.')
                if seed.get('space') not in clashed:
                    clashed.add(seed.get('space'))
                    lines.append(warning)
            elif seed.get('same_as'):
                warning = (self.tr('Same space as %s: this seed adds '
                                   'nothing.') % seed['same_as'])
                name = seed.get('name') or row.get('name') or key
                lines.append('%s — %s' % (name, warning))
            if warning:
                notes.append(warning)
                item.setStatusTip(warning)
            if notes:
                item.setToolTip('\n'.join(notes))
        self._seedWarning.setText('\n'.join(lines))
        self._seedWarning.setVisible(bool(lines))
        self._showSavedLabels()


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
    extra_field_ids = (
        'meshing.geometry.tri_surface_declaration',
        'meshing.geometry.gap_detection',
        'meshing.geometry.gap_width',
        'meshing.geometry.tolerance',
        'meshing.geometry.max_tree_depth',
        'meshing.geometry.min_quality',
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
        'surfaces closed to override that, and close narrow gaps when the '
        'geometry has slots the base grid is too coarse to see. '
        'Detect (Alt+T) finds the spaces the surfaces close off and places '
        'a region in each one you keep; one Undo takes them all back. A '
        'seed can be dragged in the view, nudged with Alt and an arrow key '
        'and cut to on a Section plane (Alt+O); Ctrl+Z undoes a placement '
        'while the editor is open.')

    def build_sections(self, layout) -> None:
        # Plan 33 OF-02. The table opens the column. The two sentences that
        # used to stand above it were a third copy of `HELP_DETAIL`, which
        # `_moveProseBehindHelp` already sends to the help control and to the
        # page tooltip, so what they said is still one press away and the
        # reader no longer scrolls past it to reach the only editor here.
        self.panel = RegionSeedPanel(
            self._client, 'regions.items', self.tr('Regions'),
            columns=self.COLUMNS, parent=self, stretch='name')
        self.panel.childrenChanged.connect(self.refresh)
        # DP-818. The glyphs follow the table: added, moved or deleted.
        self.panel.childrenChanged.connect(self._reloadSeedMarkers)
        # Plan 36 RP2. The open editor is the only thing that edits here.
        self.panel.editingChanged.connect(self._lockWhileEditing)
        # Plan 36 RP3. A dragged seed stays in the box drawn around it (RP1).
        self.panel.setSeedBoundsProvider(self._seedBounds)
        layout.insertWidget(0, self.panel)

    #: Plan 36 RP2. What each control's enabled state was before an editor
    #: opened, so closing it gives back exactly that -- a Run button shut for
    #: a reason stays shut.
    _lockedControls = None

    def _pageControls(self):
        layout = self._body_layout
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is not None and widget is not self.panel:
                yield widget
        yield from (self._preview, self._update, self._revert, self._runStage)

    def _lockWhileEditing(self, editing: bool) -> None:
        """Disable the rest of the page while a region is being placed."""
        if editing:
            if self._lockedControls is not None:
                return
            self._lockedControls = [(widget, widget.isEnabled())
                                    for widget in self._pageControls()]
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
        if not shown:
            return
        box = self.domainBox()
        if box is None:
            return
        # RP13 #5. A domain that is not a box is drawn block by block.
        self._domainBoxActor = domainBoxActor(
            box.bounds, blocks=None if box.cuboid else box.outlines())
        display.addOverlay(self._domainBoxActor)

    def showEvent(self, event) -> None:
        """DP-818. The seed glyphs are drawn while this page is up."""
        super().showEvent(event)
        if not event.spontaneous():
            self._showSeedMarkers(True)
            self._showDomainBox(True)
            # DP-925. The saved regions carry their labels on this page.
            self.panel.setRegionLabelsShown(True)
            self._offerDetection()

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

    #: DP-517. `CaseBuilder._gap_width` reads the width only while the
    #: switch is on, so the box is live only then.
    _GAP_SWITCH = 'meshing.geometry.gap_detection'
    _GAP_WIDTH = 'meshing.geometry.gap_width'

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()

    def reload_values(self) -> None:
        super().reload_values()
        self._syncGapWidth()

    def _on_field_changed(self, field_id: str, value) -> None:
        super()._on_field_changed(field_id, value)
        if field_id == self._GAP_SWITCH:
            self._syncGapWidth()

    def _syncGapWidth(self) -> None:
        """Gap width is editable only while Close narrow gaps is on.

        DP-517 (audit 2026-09-23, the user's annotated Domain & regions >
        Advanced): the width box was live with the switch off, where the
        writer never reads it. The switch's own box is read, not the stored
        value, so ticking it enables the width before Apply. The row stays --
        a greyed width says what the switch will bring -- and applicability
        (`FieldEditor.setApplicability`) still owns whether it is shown.
        """
        switch = self._editors.get(self._GAP_SWITCH)
        width = self._editors.get(self._GAP_WIDTH)
        if switch is None or width is None:
            return
        live = (bool(switch.value()) and width.applies()
                and not width.descriptor.read_only)
        width.editor.setEnabled(live)
        width.label.setEnabled(live)
        width.unit_label.setEnabled(live)

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
