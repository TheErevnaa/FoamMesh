#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction, QColor, QIcon, QKeySequence, QLinearGradient, QPainter, QPixmap)
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QFileDialog, QFrame, QLabel, QMenu, QSizePolicy,
    QToolButton)

from widgets.rendering.rendering_widget import RenderingWidget
from widgets.rendering.rotation_center_widget import RotationCenterWidget
from widgets.rendering.ruler_widget import RulerWidget

from foammesh.app import app
from foammesh.view.main_window.main_window_ui import Ui_MainWindow
from foammesh.view.theming.icons import load_themed_icon
from foammesh.view.theming.status_colors import (
    apply_color_swatch, hide_menu_indicator)
from foammesh.view.display_control import view_modes
from foammesh.view.main_window.mesh_lines_control import MeshLinesButton
from foammesh.view.length_readout import format_extent


#: Matches the buttons the viewport toolbar already carries.
# DP-811. 28 px buttons with 20 px icons: at a 1280-px window the row has
# about 660 px, and every four pixels a button gives back is one more tool
# out of the overflow menu.
ICON_SIZE = QSize(20, 20)
BUTTON_SIZE = QSize(28, 28)


#: DP-106. Kept as a module name because the tests and the callers below
#: already say it; the ladder itself is shared with the base-grid readout.
_formatExtent = format_extent


class ElidingLabel(QLabel):
    """A readout that shortens itself rather than being cut off mid-number.

    DP-353. A plain `QLabel` asks the layout for the width of its whole text
    and has no answer when it does not get it: the layout hands it whatever
    is left and the text is clipped where it happens to run out, which on a
    number is indistinguishable from a different, smaller number. Measured on
    a meshed tee, the extent readout asked for 113 px and was given 44 at the
    standard window size and 16 at the narrow one, and the cell counter asked
    for 208 and was given 45 and 16. Both showed a fragment with no sign that
    it was a fragment.

    So it elides, keeps the full value in the tooltip, and -- the part that
    makes the row fit at all -- stops claiming the whole text as its minimum.
    A readout is the item in a toolbar that can most afford to be short; it
    was the item demanding the most.
    """

    #: Raised when the value changes, because the width the row has to find
    #: for this readout changes with it. Without it the strip reflowed on a
    #: resize and never on a number: a window sized while the counter was
    #: empty stayed laid out for an empty counter, and the first mesh to
    #: arrive was reported as `Section: ...`.
    demandChanged = Signal()

    #: Below this a readout is a shape, not a number; the row must find the
    #: space somewhere else.
    FLOOR_SAMPLE = '00000…'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ''
        self._note = ''
        self._floorText = ''

    # -- what the caller sets -------------------------------------------- #

    def setFloorText(self, text):
        """The part of the value that must never be shortened.

        DP-703 (viewport audit 0925 F14). The extent readout was elided to
        "200 mm acr": the floor was five digits, so the row could cut into
        the words that say what the number is. A caller that knows which
        leading part carries the meaning names it here, and the label claims
        that much as its minimum; only what follows it is ever elided.
        """
        value = str(text or '')
        if value == self._floorText:
            return
        self._floorText = value
        self.updateGeometry()
        self.demandChanged.emit()

    def setText(self, text):
        value = str(text or '')
        changed = value != self._full
        self._full = value
        self._applyElision()
        self.updateGeometry()
        if changed:
            self.demandChanged.emit()

    def setToolTip(self, text):
        """Remembered, so eliding can put the full value above it."""
        self._note = str(text or '')
        self._applyTip()

    # -- what a reader (or a harness) can ask ---------------------------- #

    def fullText(self) -> str:
        return self._full

    def isElided(self) -> bool:
        return bool(self._full) and self.text() != self._full

    # -- geometry --------------------------------------------------------- #

    def sizeHint(self):
        return QSize(self._advance(self._full) + 2, super().sizeHint().height())

    def minimumSizeHint(self):
        full = self._advance(self._full)
        # Room for the floor text and the ellipsis after it, or eliding the
        # rest would take the floor's last letters with it.
        kept = self._floorText
        if kept and self._full != kept:
            kept += '…'
        floor = min(full, max(self._advance(self.FLOOR_SAMPLE),
                              self._advance(kept)))
        return QSize(floor + 2, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._applyElision()

    # -- internals -------------------------------------------------------- #

    def _advance(self, text) -> int:
        return self.fontMetrics().horizontalAdvance(text) if text else 0

    def _applyElision(self):
        room = max(self.width() - 2, 0)
        if not self._full:
            super().setText('')
        elif room <= 0 or self._advance(self._full) <= room:
            super().setText(self._full)
        elif (self._floorText and self._full.startswith(self._floorText)
              and self._advance(self._floorText + '…') <= room):
            # DP-703. Keep the floor whole and elide only what follows it;
            # eliding the whole string can shave a letter off the floor.
            rest = self._full[len(self._floorText):]
            tail = self.fontMetrics().elidedText(
                rest, Qt.TextElideMode.ElideRight,
                room - self._advance(self._floorText))
            super().setText(self._floorText + (tail or '…'))
        else:
            super().setText(self.fontMetrics().elidedText(
                self._full, Qt.TextElideMode.ElideRight, room))
        self._applyTip()

    def _applyTip(self):
        if self.isElided():
            parts = [self._full] + ([self._note] if self._note else [])
            super().setToolTip('\n'.join(parts))
        else:
            super().setToolTip(self._note)


class RenderingTool(QObject):
    #: Asked for by the toolbar; the window owns what these actually do.
    sectionRequested = Signal()
    captureRequested = Signal()
    galleryRequested = Signal()
    isolateRequested = Signal()
    zoomSelectionRequested = Signal()
    #: DP-697. The bare `F` key: fit the selection, or everything when
    #: nothing is selected.
    fitSelectionOrAllRequested = Signal()
    showAllRequested = Signal()
    poorCellsRequested = Signal()
    fidelityRequested = Signal()
    featuresRequested = Signal()
    failedCellsRequested = Signal()
    explodeChanged = Signal(float)
    orbitRequested = Signal()
    sweepRequested = Signal()
    saveViewRequested = Signal()
    namedViewRequested = Signal(str)
    deleteViewRequested = Signal(str)
    viewModeRequested = Signal(str)
    layerCoverageRequested = Signal()

    def __init__(self, ui: Ui_MainWindow):
        super().__init__()
        self._ui = ui
        self._view: RenderingWidget = ui.renderingView

        self._ruler = None
        self._rotationCenter = None
        self._cheatSheet = None
        self._iconButtons = {}

        self._restoreBackgrounds()
        self._updateBGButtonStyle(self._ui.bg1, QColor.fromRgbF(*self._view.background1()))
        self._updateBGButtonStyle(self._ui.bg2, QColor.fromRgbF(*self._view.background2()))
        # DP-187. Two swatches that paint a colour and no words. The
        # label beside them reads `Background` now instead of `BG`, but a
        # screen reader lands on the buttons themselves, so each says
        # which end of the gradient it sets.
        # bg1 drives SetBackground, which VTK paints at the bottom of a
        # gradient background; bg2 drives SetBackground2 at the top.
        self._ui.bg1.setAccessibleName(self.tr('Background, bottom'))
        self._ui.bg1.setToolTip(self.tr('Colour at the bottom of the viewport gradient'))
        self._ui.bg2.setAccessibleName(self.tr('Background, top'))
        self._ui.bg2.setToolTip(self.tr('Colour at the top of the viewport gradient'))

        self._ui.alignAxis.clicked.connect(self._view.alignCamera)
        self._ui.axis.toggled.connect(self._view.setAxisVisible)
        self._ui.cubeAxis.toggled.connect(self._view.setCubeAxisVisible)
        self._ui.ruler.toggled.connect(self._setRulerVisible)
        self._ui.fit.clicked.connect(self._fitCamera)
        self._ui.perspective.toggled.connect(self._view.setParallelProjection)
        self._ui.rotate.clicked.connect(self._view.rollCamera)
        self._ui.rotationCenter.clicked.connect(self._toggleRotationCenter)
        self._ui.bg1.clicked.connect(self._pickBackground1)
        self._ui.bg2.clicked.connect(self._pickBackground2)
        self._backgroundResetButton = self._newBackgroundReset()
        self._compactBackgroundFrame()
        self._installViewActions()
        self._installToolbarControls()
        # DP-696. Back and Forward ask the view after every camera move,
        # not only after the few callers that remembered to refresh them.
        historyChanged = getattr(self._view, 'historyChanged', None)
        if hasattr(historyChanged, 'connect'):
            historyChanged.connect(self.updateHistoryButtons)

    #: Bindings owned by the viewport. Each one is surfaced on a control and in
    #: the `?` cheat sheet, because a key binding nobody can discover is a key
    #: binding that does not exist.
    VIEW_PRESETS = (
        ('View +X', '+x', 'Alt+1'), ('View -X', '-x', 'Alt+2'),
        ('View +Y', '+y', 'Alt+3'), ('View -Y', '-y', 'Alt+4'),
        ('View +Z', '+z', 'Alt+5'), ('View -Z', '-z', 'Alt+6'),
        ('Isometric View', 'isometric', 'Alt+7'),
    )
    #: `Ctrl+Shift+S` already belonged to Save Project As. Two window-context
    #: actions on one sequence is an ambiguous overload: Qt fires neither and
    #: logs the conflict, so the viewport capture had never once worked from
    #: the keyboard and Save Project As was collateral damage.
    CAPTURE_SHORTCUT = 'Ctrl+Shift+P'
    CHEAT_SHEET_SHORTCUT = 'F1'
    #: Fitting to what you selected matters most on the meshes that are
    #: too big to find anything in by dragging.
    ZOOM_SELECTION_SHORTCUT = 'Ctrl+Shift+F'
    #: DP-697. The key every CAD viewer uses to frame what you are looking
    #: at. Scoped to the viewport, so it never eats an F typed in a field.
    FIT_SHORTCUT = 'F'
    #: Wide enough for "Boundary mesh" at the toolbar font, so the mode the
    #: viewport is in is readable at the narrow window size item 7 asks about
    #: rather than elided to "Bound...".
    MODE_COMBO_MIN_WIDTH = 132
    #: The controls that paint something over the model, by the short name
    #: the window uses to ask about them. Every one of these is a switch: it
    #: reports what the viewport is showing and a second press takes it off.
    INSPECTION_BUTTONS = {
        'section': '_sectionButton',
        'poorCells': '_poorCellsButton',
        'failedCells': '_failedCellsButton',
        'fidelity': '_fidelityButton',
    }

    def _installViewActions(self):
        self._viewActions = []
        for title, preset, shortcut in self.VIEW_PRESETS:
            action = QAction(self.tr(title), self._view)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(
                lambda _checked=False, value=preset:
                self._view.setViewPreset(value))
            self._view.addAction(action)
            self._viewActions.append(action)

    def _installToolbarControls(self):
        """Put the operations people perform constantly onto the picture.

        A cross-section used to cost: open Display Control, choose the Clip
        radio, tick an axis, drag a value, press Apply. Highlighting bad cells
        cost a trip to the Mesh check dashboard. Neither is a once-a-session
        decision; both are things you do while looking at the mesh.
        """
        layout = self._ui.horizontalLayout
        index = layout.indexOf(self._ui.rotationCenter) + 1

        self._viewMenuButton = QToolButton()
        self._viewMenuButton.setIcon(
            load_themed_icon(':/graphicsIcons/viewPresets.svg'))
        self._viewMenuButton.setIconSize(ICON_SIZE)
        self._viewMenuButton.setMaximumSize(BUTTON_SIZE)
        self._viewMenuButton.setAutoRaise(True)
        self._viewMenuButton.setProperty('foammeshFlat', True)
        self._viewMenuButton.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._viewMenuButton.setToolTip(
            self.tr('Standard views. Every entry shows its shortcut.'))
        self._viewMenuButton.setAccessibleName(self.tr('Standard views'))
        menu = QMenu(self._viewMenuButton)
        for action in self._viewActions:
            menu.addAction(action)
        # WP5.2. Named views hang off the same button as the axis presets:
        # they answer the same question ("put the camera somewhere known") and
        # a second button for them would be a second thing to discover.
        menu.addSeparator()
        self._namedViewMenu = menu.addMenu(self.tr('Saved views'))
        self._namedViewMenu.setEnabled(False)
        menu.addAction(self.tr('Save current view…'),
                       self.saveViewRequested.emit)
        menu.addSeparator()
        self._sequenceMenu = menu.addMenu(self.tr('Record'))
        self._sequenceMenu.addAction(self.tr('Orbit'), self.orbitRequested.emit)
        self._sequenceMenu.addAction(self.tr('Section sweep'),
                                     self.sweepRequested.emit)
        # WP-13 / F-33. ``_saveScreenshotAs`` was written, tested by nothing and
        # connected to nothing: the capture button beside this menu files a shot
        # in the gallery, which is a different job from "write a PNG where I say".
        # Without this entry the only way to get a viewport image out of FoamMesh
        # was to screenshot the desktop, and the file dialog already existed.
        menu.addSeparator()
        self._saveImageAction = menu.addAction(self.tr('Save image as…'),
                                               self._saveScreenshotAs)
        # CP-09 item 8, layer-coverage views. `mesh.layer_coverage` has been a
        # facade operation since Plan 26 WP6.1, writing achieved per-patch
        # layers to `foammesh/quality/layer-coverage.json` on every layer run,
        # and no module under `src/foammesh/view` read it: the numbers existed
        # on disk and could not be seen from the picture they describe. Here
        # rather than as a tenth toolbar button because the toolbar is already
        # what item 7 asks about at the narrow window size.
        menu.addSeparator()
        self._layerCoverageAction = menu.addAction(
            self.tr('Layer coverage'), self.layerCoverageRequested.emit)
        self._layerCoverageAction.setToolTip(
            self.tr('Show the boundary mesh with the patches whose prism '
                    'layers fell short of the request highlighted'))
        self._layerCoverageAction.setEnabled(False)
        self._viewMenuButton.setMenu(menu)

        # CP-09 item 5. Five modes people ask for constantly -- what I gave
        # the mesher, what it put on the surface, what it filled the inside
        # with, that cut open, and how good it is -- each of which used to be
        # a hand-built combination of hiding parts and opening a second dock.
        # A combo rather than five buttons because they are exclusive: the
        # viewport is in exactly one of these at a time, and the control has
        # to say which.
        self._modeCombo = self._buildModeCombo()
        self._modeCombo.currentIndexChanged.connect(self._modeChosen)
        # DP-710 (F7). The grid's opacity, colour and width, beside the mode
        # that decides whether the grid is drawn at all.
        self._meshLinesButton = MeshLinesButton(
            size=BUTTON_SIZE, iconSize=ICON_SIZE)
        self._meshLinesButton.styleChanged.connect(
            lambda: self._view.refresh())

        # VIEW-05. These two walk the camera history and nothing else. Called
        # "Previous view" and sitting in a row of overlay toggles, they read
        # as the way back out of an overlay, which they have never been.
        self._backButton = self._toolButton(
            'viewBack.svg', self.tr('Previous camera position'), self._goBack,
            objectName='viewportBack')
        self._forwardButton = self._toolButton(
            'viewForward.svg', self.tr('Next camera position'),
            self._goForward, objectName='viewportForward')

        self._sectionButton = self._toolButton(
            'section.svg', self.tr('Cut a section along the current view'),
            self.sectionRequested.emit, objectName='viewportSection',
            checkable=True)
        self._isolateButton = self._toolButton(
            'isolate.svg', self.tr('Show only the selected parts'),
            self.isolateRequested.emit, objectName='viewportIsolate')
        self._zoomSelectionButton = self._toolButton(
            'zoomSelection.svg',
            self.tr('Fit the view to the selected parts  ({0})').format(
                self.ZOOM_SELECTION_SHORTCUT),
            self.zoomSelectionRequested.emit)
        self._showAllButton = self._toolButton(
            'showAll.svg', self.tr('Make every part visible again'),
            self.showAllRequested.emit, objectName='viewportShowAll')
        # VIEW-06. "The worst tenth of the metric" reads as the worst ten per
        # cent of the cells. It is a tenth of the interval between the
        # smallest and largest value on the mesh, which on a mesh with one
        # very bad cell is a completely different picture.
        self._poorCellsButton = self._toolButton(
            'poorCells.svg',
            self.tr('Colour the cells in the worst tenth of the metric range'),
            self._poorCellsPressed, objectName='viewportWorstCells',
            checkable=True)
        self._fidelityButton = self._toolButton(
            'fidelity.svg',
            self.tr('Colour the surface by how far it left the reference '
                    'geometry'),
            self.fidelityRequested.emit, objectName='viewportFidelity',
            checkable=True)
        self._featuresButton = self._toolButton(
            'features.svg',
            self.tr('Show the declared feature edges, coloured by whether the '
                    'mesh kept them'),
            self.featuresRequested.emit, objectName='viewportFeatures')
        self._failedCellsButton = self._toolButton(
            'failedCells.svg',
            self.tr('Show the cell sets the last mesh check wrote'),
            self._failedCellsPressed, objectName='viewportFailedCells',
            checkable=True)
        # VIEW-03. The button steps through the sets; the menu names them and
        # carries the way back out. Stepping was the only way to reach the
        # second set and the only way to leave the overlay was to keep
        # stepping until it wrapped.
        self._requestedFailedCellSet = None
        self._failedCellsMenu = QMenu(self._failedCellsButton)
        self._failedCellsMenu.aboutToShow.connect(self._rebuildFailedCellMenu)
        self._failedCellsButton.setMenu(self._failedCellsMenu)
        self._failedCellsButton.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        # VIEW-05. One control that reaches every overlay, because "show me
        # the model again" was otherwise a matter of remembering which of the
        # four were on and undoing each of them in turn.
        # DP-699. Its own icon: volume.svg is the geometry list's solid, so
        # the reset read as "a volume" rather than "back to the plain model".
        self._resetButton = self._toolButton(
            'resetOverlays.svg',
            self.tr('Put the plain model back, with no overlay and no cut'),
            self._resetInspection, objectName='viewportResetInspection')

        self._captureButton = self._toolButton(
            'capture.svg',
            self.tr('Save a picture of the viewport into the case  ({0})').format(
                self.CAPTURE_SHORTCUT),
            self.captureRequested.emit)
        self._galleryButton = self._toolButton(
            'gallery.svg', self.tr('Open the capture gallery'),
            self.galleryRequested.emit)

        self._adoptCellCount()

        self._normaliseOriginalButtons()
        self._installOverflowButton()

        self._scaleLabel = ElidingLabel()
        self._scaleLabel.setObjectName('viewportScale')
        # A note the current page can hang beside the model extent - the base
        # grid puts its cell size here, so the number and the box it describes
        # are read in one place instead of two.
        self._scaleNote = ''
        self._scaleLabel.setToolTip(
            self.tr('What the current page measures'))
        self._scaleLabel.hide()

        for widget in (self._modeCombo, self._meshLinesButton,
                       self._viewMenuButton, self._backButton,
                       self._forwardButton, self._sectionButton,
                       self._isolateButton, self._zoomSelectionButton,
                       self._showAllButton,
                       self._poorCellsButton, self._failedCellsButton,
                       self._fidelityButton, self._featuresButton,
                       self._resetButton,
                       self._captureButton, self._galleryButton,
                       self._overflowButton, self._scaleLabel):
            layout.insertWidget(index, widget)
            index += 1
        self._arrangeToolbar()

        for readout in (self._ui.cellCount, self._scaleLabel):
            readout.demandChanged.connect(self.reflowToolbar)

        capture = QAction(self.tr('Capture viewport'), self._view)
        capture.setShortcut(QKeySequence(self.CAPTURE_SHORTCUT))
        capture.triggered.connect(self.captureRequested)
        self._view.addAction(capture)

        zoomSelection = QAction(self.tr('Zoom to selection'), self._view)
        zoomSelection.setShortcut(QKeySequence(self.ZOOM_SELECTION_SHORTCUT))
        zoomSelection.triggered.connect(self.zoomSelectionRequested)
        self._view.addAction(zoomSelection)

        fitSelectionOrAll = QAction(
            self.tr('Fit the selection, or everything'), self._view)
        fitSelectionOrAll.setShortcut(QKeySequence(self.FIT_SHORTCUT))
        fitSelectionOrAll.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        fitSelectionOrAll.triggered.connect(self.fitSelectionOrAllRequested)
        self._view.addAction(fitSelectionOrAll)

        cheatSheet = QAction(self.tr('Viewport shortcuts'), self._view)
        cheatSheet.setShortcut(QKeySequence(self.CHEAT_SHEET_SHORTCUT))
        cheatSheet.triggered.connect(self.showCheatSheet)
        self._view.addAction(cheatSheet)
        self._extraActions = [capture, zoomSelection, fitSelectionOrAll,
                              cheatSheet]

        self.updateHistoryButtons()
        self.updateScaleReadout()

    def _buildModeCombo(self) -> QComboBox:
        """The five modes, behind a placeholder that claims nothing.

        The placeholder is the first entry and carries no mode id, so a
        freshly opened window does not assert it is "in Geometry mode" before
        anything is loaded -- and so a mode the window *refuses* has somewhere
        honest to fall back to, which `showViewMode('')` selects.
        """
        combo = QComboBox()
        combo.setObjectName('viewModeCombo')
        combo.setAccessibleName(self.tr('What the viewport shows'))
        combo.setMinimumWidth(self.MODE_COMBO_MIN_WIDTH)
        combo.addItem(self.tr('View mode…'), '')
        combo.setItemData(
            0, self.tr('Pick what the viewport should show.'),
            Qt.ItemDataRole.ToolTipRole)
        for mode in view_modes.MODES:
            combo.addItem(self.tr(mode.label), mode.id)
            combo.setItemData(combo.count() - 1, self.tr(mode.question),
                              Qt.ItemDataRole.ToolTipRole)
        combo.setToolTip(
            self.tr('What the viewport shows. Modes are exclusive: the one '
                    'named here is the one you are looking at.'))
        return combo

    def _modeChosen(self, index: int):
        mode_id = self._modeCombo.itemData(index)
        if mode_id:
            # DP-702 (viewport audit 0925 F17). Faces picked in the last mode
            # stayed selected in the service with nothing on screen showing
            # them, and Isolate and Fit selection then acted on them. A new
            # view starts with nothing selected.
            service = getattr(app, 'selectionService', None)
            if service is not None and hasattr(service, 'select'):
                service.select(())
            self.viewModeRequested.emit(str(mode_id))

    def currentViewMode(self) -> str:
        combo = getattr(self, '_modeCombo', None)
        return str(combo.currentData()) if combo is not None else ''

    def showViewMode(self, mode_id: str):
        """Reflect the mode the window actually entered, without re-asking.

        The window can refuse a mode (nothing to show) or enter one on its
        own -- a section drag is a Slice whether or not anyone chose it from
        here. Either way the combo must end up telling the truth, and must not
        bounce a second request back at the window while doing so.
        """
        combo = getattr(self, '_modeCombo', None)
        if combo is None:
            return
        index = combo.findData(mode_id)
        if index < 0 or index == combo.currentIndex():
            return
        blocked = combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(blocked)

    def _toolButton(self, iconName, tooltip, slot, *, objectName='',
                    checkable=False):
        """Icon-only, sized to match the buttons the toolbar already carries.

        The tooltip and the accessible name carry the label: an unnamed glyph
        is only half an affordance, and Rule 1 of this plan is that a
        capability a user cannot see does not exist.

        ``checkable`` is for the controls that paint something over the model.
        A press that paints and a press that reads identically to it are the
        same press, so the three inspection overlays used to have no visible
        off state and no way to tell whether they were on.
        """
        button = QToolButton()
        if objectName:
            button.setObjectName(objectName)
        button.setCheckable(bool(checkable))
        button.setIcon(load_themed_icon(f':/graphicsIcons/{iconName}'))
        button.setIconSize(ICON_SIZE)
        button.setMaximumSize(BUTTON_SIZE)
        # CP-09 item 7, minimum-size layouts. The row only had a *maximum*:
        # narrow the window and the layout takes the width back out of these
        # buttons, which have no text to stop them, until the icons are
        # unreadable slivers. A button that cannot be hit is not a control.
        button.setMinimumSize(BUTTON_SIZE)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setAutoRaise(True)
        # Borderless, like the buttons the toolbar already carried. Without
        # this the new controls sat in a visible box while the originals did
        # not, and one strip of icons read as two.
        button.setProperty('foammeshFlat', True)
        button.clicked.connect(slot)
        self._iconButtons[button] = iconName
        return button

    #: The eight buttons the toolbar was born with, which the layout had
    #: never been told to treat as icons: each asked for 54-56 px of the row
    #: for a 22 px icon, 190 px of pure padding across the strip.
    ORIGINAL_BUTTONS = ('axis', 'cubeAxis', 'ruler', 'perspective', 'fit',
                        'alignAxis', 'rotate', 'rotationCenter')

    def _normaliseOriginalButtons(self):
        for name in self.ORIGINAL_BUTTONS:
            button = getattr(self._ui, name)
            button.setMinimumSize(BUTTON_SIZE)
            button.setMaximumSize(BUTTON_SIZE)

    def _adoptCellCount(self):
        """Swap the cell counter for a readout that can be short.

        It is the widest single demand in the row (208 px for "12,004 /
        39,921 shown in the section") and the one the row was starving. The
        window keeps talking to `self._ui.cellCount`; only the class changes.
        """
        old = self._ui.cellCount
        label = ElidingLabel(old.parentWidget())
        label.setObjectName(old.objectName())
        label.setFont(old.font())
        label.setSizePolicy(old.sizePolicy())
        label.setFrameShape(old.frameShape())
        label.setText(old.text())
        label.setToolTip(old.toolTip())
        layout = old.parentWidget().layout()
        layout.replaceWidget(old, label)
        old.setParent(None)
        old.deleteLater()
        self._ui.cellCount = label

    def _installOverflowButton(self):
        """One button that holds whatever the row could not.

        DP-353. The row is a plain `QHBoxLayout`, so when the controls ask
        for more than the viewport is wide, Qt takes the shortfall out of
        every item in proportion to what it asked for -- the readouts lose
        most, the icons are ground down to slivers, and nothing tells anyone
        that the strip is not showing what it is meant to show. A toolbar
        that cannot fit has to have somewhere to put the rest.
        """
        self._overflowButton = QToolButton()
        self._overflowButton.setObjectName('viewportOverflow')
        self._overflowButton.setText('\u22ef')
        self._overflowButton.setMinimumSize(BUTTON_SIZE)
        self._overflowButton.setMaximumSize(BUTTON_SIZE)
        self._overflowButton.setAutoRaise(True)
        self._overflowButton.setProperty('foammeshFlat', True)
        self._overflowButton.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._overflowButton.setToolTip(
            self.tr('The viewport controls that do not fit at this width'))
        self._overflowButton.setAccessibleName(self.tr('More viewport tools'))
        menu = QMenu(self._overflowButton)
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(self._fillOverflowMenu)
        self._overflowButton.setMenu(menu)
        self._overflowButton.setVisible(False)
        self._hiddenTools = []
        self._reflowing = False
        self._ui.toolbar.installEventFilter(self)

    def _toolbarGroups(self):
        """The row as five groups, in the order they sit from the left.

        DP-698 (viewport audit 0925 F10). The strip had grown by insertion:
        the camera buttons the .ui was born with, then eighteen controls
        dropped in after Rotation centre, then the background swatches past
        the spacer. Fit and Fit selection were twelve buttons apart, and the
        axis toggles sat among the camera moves. Grouped by what the press
        is about -- where the camera is, what is visible, how it is drawn,
        what is measured on it, what leaves the viewport -- with a rule
        between groups, the row reads left to right.
        """
        ui = self._ui

        def part(name):
            return getattr(ui, name, None)

        return (
            (part('fit'), self._zoomSelectionButton, self._viewMenuButton,
             self._backButton, self._forwardButton, part('alignAxis'),
             part('rotate'), part('perspective'), part('rotationCenter')),
            (self._isolateButton, self._showAllButton),
            (self._modeCombo, self._meshLinesButton, part('axis'),
             part('cubeAxis'), part('ruler'), part('frame_2')),
            (self._sectionButton, self._poorCellsButton,
             self._failedCellsButton, self._fidelityButton,
             self._featuresButton, self._resetButton),
            (self._captureButton, self._galleryButton),
        )

    def _arrangeToolbar(self):
        """Lay the row out as `_toolbarGroups`, readouts and overflow last.

        Only what is already in the row is moved; anything else a caller put
        there keeps its place ahead of the spacer.
        """
        ui = self._ui
        layout = ui.horizontalLayout
        items = [layout.takeAt(0) for _ in range(layout.count())]
        present = {id(item.widget()) for item in items
                   if item.widget() is not None}
        groups = [[widget for widget in group
                   if widget is not None and id(widget) in present]
                  for group in self._toolbarGroups()]
        groups = [group for group in groups if group]
        readout = getattr(ui, 'widget_17', None)
        if readout is None or id(readout) not in present:
            readout = ui.cellCount
        right = [widget for widget in (readout, self._scaleLabel,
                                       self._overflowButton)
                 if id(widget) in present]
        placed = {id(widget) for group in groups for widget in group}
        placed |= {id(widget) for widget in right}
        others = [item.widget() for item in items
                  if item.widget() is not None
                  and id(item.widget()) not in placed]
        spacers = [item for item in items if item.widget() is None]

        self._toolGroups = groups
        self._groupSeparators = []
        for position, group in enumerate(groups):
            if position:
                separator = QFrame()
                separator.setObjectName('viewportGroupSeparator')
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Plain)
                separator.setFixedWidth(1)
                separator.setMaximumHeight(BUTTON_SIZE.height() - 8)
                layout.addWidget(separator)
                self._groupSeparators.append(separator)
            for widget in group:
                layout.addWidget(widget)
        for widget in others:
            layout.addWidget(widget)
        for spacer in spacers:
            # DP-811. The .ui spacer asked for 40 px of nothing, which the
            # reflow counted as demand -- one tool's worth of room spent on
            # a gap. It still takes whatever is left over.
            if spacer.spacerItem() is not None:
                spacer.spacerItem().changeSize(
                    0, 0, QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Minimum)
            layout.addItem(spacer)
        for widget in right:
            layout.addWidget(widget)
        self._updateSeparators()

    def _updateSeparators(self):
        """A rule stands before a group only when it and a group left of it
        both still show something, so hiding a whole group never leaves two
        rules side by side or one hanging at the end."""
        groups = getattr(self, '_toolGroups', ())
        shown = [any(not widget.isHidden() for widget in group)
                 for group in groups]
        for position, separator in enumerate(
                getattr(self, '_groupSeparators', ()), start=1):
            visible = shown[position] and any(shown[:position])
            if separator.isHidden() == visible:
                separator.setVisible(visible)

    def _overflowOrder(self):
        """What the row gives up first, and what stands in for it.

        Least-used first, and never the three things the strip exists to
        report: the view mode, the extent and the cell count. The second half
        of each pair is what the menu offers in its place -- for the
        background frame that is its two swatches, which is why the entry is
        a pair and not a widget.

        DP-698. The buttons the .ui was born with go first (Fit apart), then
        the output and analysis tools; the last to go are, from the end,
        Fit, Fit selection, Section, Isolate, Show all and Reset -- the ones
        a user reaches for while looking at the model. The mode combo, the
        mesh lines menu and the view presets never go.
        """
        ui = self._ui
        return (
            (ui.rotationCenter, (ui.rotationCenter,)),
            (ui.rotate, (ui.rotate,)),
            (ui.ruler, (ui.ruler,)),
            (ui.cubeAxis, (ui.cubeAxis,)),
            (ui.axis, (ui.axis,)),
            (ui.perspective, (ui.perspective,)),
            (ui.alignAxis, (ui.alignAxis,)),
            (ui.frame_2, (ui.bg1, ui.bg2, self._backgroundResetButton)),
            (self._galleryButton, (self._galleryButton,)),
            (self._featuresButton, (self._featuresButton,)),
            (self._fidelityButton, (self._fidelityButton,)),
            (self._failedCellsButton, (self._failedCellsButton,)),
            (self._poorCellsButton, (self._poorCellsButton,)),
            (self._captureButton, (self._captureButton,)),
            (self._forwardButton, (self._forwardButton,)),
            (self._backButton, (self._backButton,)),
            (self._resetButton, (self._resetButton,)),
            (self._showAllButton, (self._showAllButton,)),
            (self._isolateButton, (self._isolateButton,)),
            (self._sectionButton, (self._sectionButton,)),
            (self._zoomSelectionButton, (self._zoomSelectionButton,)),
            (ui.fit, (ui.fit,)),
        )

    #: A resize changes how much room there is; a layout request means one of
    #: the occupants changed how much it needs -- which is what a cell count
    #: arriving is. Watching only the first left the strip laid out for the
    #: empty counter it was sized with.
    REFLOW_EVENTS = (QEvent.Type.Resize, QEvent.Type.LayoutRequest)

    def eventFilter(self, watched, event):
        if watched is self._ui.toolbar and event.type() in self.REFLOW_EVENTS:
            self.reflowToolbar()
        return super().eventFilter(watched, event)

    def reflowToolbar(self):
        """Hide controls, least-used first, until the row fits; restore them
        when it grows again.

        Everything hidden is in the overflow menu, so the set of things the
        viewport can do never depends on how wide the window is -- only how
        many of them are one press away.

        It moves only the controls it has to. An earlier draft showed
        everything and re-hid from scratch on each pass, which is simpler to
        read and impossible to run: every pass changed a dozen widgets, each
        change posts the layout request that starts the next pass, and the
        strip flickered its way through an unbounded loop. A reflow that is
        already correct now touches nothing, which is what ends it.
        """
        if self._reflowing:
            return
        available = self._ui.toolbar.width()
        row = self._ui.horizontalLayout
        # The row is still being built until the overflow button is in it, and
        # a resize that arrives before then would hide controls into a menu
        # button that has nowhere to appear.
        if available <= 0 or row.indexOf(self._overflowButton) < 0:
            return
        order = self._overflowOrder()
        self._reflowing = True
        try:
            given = len([pair for pair in order if pair[0].isHidden()])
            # Give up the next one while the row wants more than it has. The
            # target is the row's *preferred* width, not its minimum: every
            # button in the strip wants exactly the size it needs, so the two
            # that want more than their floor are the readouts, and stopping
            # at the minimum would be stopping at the point where the numbers
            # are still unreadable -- which is the defect.
            while given < len(order):
                self._updateSeparators()
                row.invalidate()
                if row.sizeHint().width() <= available:
                    break
                order[given][0].setVisible(False)
                given += 1
            # And take the last one back the moment there is room for it.
            while given > 0:
                widget = order[given - 1][0]
                row.invalidate()
                if row.sizeHint().width() + self._demandOf(widget) > available:
                    break
                widget.setVisible(True)
                # DP-698. Showing it can bring back the rule before its
                # group, which the estimate above did not count.
                self._updateSeparators()
                row.invalidate()
                if row.sizeHint().width() > available:
                    widget.setVisible(False)
                    break
                given -= 1
            self._updateSeparators()
            self._hiddenTools = [pair for pair in order if pair[0].isHidden()]
            self._overflowButton.setVisible(bool(self._hiddenTools))
        finally:
            self._reflowing = False

    @staticmethod
    def _demandOf(widget) -> int:
        """What showing this again would cost the row."""
        width = widget.sizeHint().width()
        if widget.maximumWidth() > 0:
            width = min(width, widget.maximumWidth())
        return width

    def overflowedControls(self) -> list:
        """The accessible names of everything currently in the menu."""
        return [proxy.accessibleName() or proxy.toolTip()
                for _widget, proxies in self._hiddenTools
                for proxy in proxies]

    def _fillOverflowMenu(self):
        """Mirror the hidden buttons, state, reason and all.

        The entries are proxies, not copies: pressing one presses the button
        it stands for, so a control in the menu behaves exactly as it does in
        the row, including the checked state of the overlays and the reason a
        disabled one carries.
        """
        menu = self._overflowButton.menu()
        menu.clear()
        for _widget, proxies in self._hiddenTools:
            for proxy in proxies:
                action = menu.addAction(
                    proxy.accessibleName() or proxy.toolTip())
                action.setToolTip(proxy.toolTip())
                action.setEnabled(proxy.isEnabled())
                if proxy.isCheckable():
                    action.setCheckable(True)
                    action.setChecked(proxy.isChecked())
                action.triggered.connect(
                    lambda _checked=False, button=proxy: button.click())
        if menu.isEmpty():
            action = menu.addAction(self.tr('Everything fits at this width'))
            action.setEnabled(False)

    def _rethemeToolbarIcons(self):
        for button, iconName in self._iconButtons.items():
            button.setIcon(load_themed_icon(f':/graphicsIcons/{iconName}'))
        self._viewMenuButton.setIcon(
            load_themed_icon(':/graphicsIcons/viewPresets.svg'))

    def shortcutTable(self):
        """Every viewport binding, for the cheat sheet.

        A binding a user cannot discover is a binding that does not exist. The
        seven view presets and the capture existed for months as unlabelled key
        bindings, and from the user's chair they did not exist.
        """
        rows = [(title, shortcut) for title, _preset, shortcut
                in self.VIEW_PRESETS]
        rows.append((self.tr('Capture viewport'), self.CAPTURE_SHORTCUT))
        rows.append((self.tr('Zoom to selection'), self.ZOOM_SELECTION_SHORTCUT))
        rows.append((self.tr('Fit the selection, or everything'),
                     self.FIT_SHORTCUT))
        rows.append((self.tr('This list'), self.CHEAT_SHEET_SHORTCUT))
        return rows

    def showCheatSheet(self):
        from PySide6.QtWidgets import QMessageBox

        lines = '\n'.join(
            f'{shortcut:<12}{title}' for title, shortcut in self.shortcutTable())
        box = QMessageBox(app.window)
        box.setWindowTitle(self.tr('Viewport shortcuts'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(lines)
        self._cheatSheet = box
        box.open()
        return box

    def setNamedViews(self, names):
        """Rebuild the saved-view menu. Empty stays disabled, not absent."""
        self._namedViewMenu.clear()
        names = sorted(names)
        self._namedViewMenu.setEnabled(bool(names))
        if not names:
            self._namedViewMenu.setToolTip(
                self.tr('Save a view first and it appears here'))
            return
        for name in names:
            self._namedViewMenu.addAction(
                name, lambda _c=False, value=name:
                self.namedViewRequested.emit(value))
        self._namedViewMenu.addSeparator()
        forget = self._namedViewMenu.addMenu(self.tr('Forget'))
        for name in names:
            forget.addAction(
                name, lambda _c=False, value=name:
                self.deleteViewRequested.emit(value))

    def setSequencesEnabled(self, enabled: bool, reason: str = ''):
        # DP-700 (viewport audit 0925 F16). A late overlay rebuild during
        # close reached here after the Record menu was destroyed and raised
        # "Internal C++ object (QMenu) already deleted". A menu that is gone
        # has nothing left to gate.
        import shiboken6
        if not shiboken6.isValid(self._sequenceMenu):
            return
        self._sequenceMenu.setEnabled(enabled)
        self._sequenceMenu.setToolTip('' if enabled else reason)


    def _fitCamera(self):
        # DP-696. The Fit the user presses is undoable; see fitCameraFromUser.
        fit = getattr(self._view, 'fitCameraFromUser', None)
        if fit is None:
            self._view.fitCamera()
        else:
            fit()
        self.updateHistoryButtons()

    def _goBack(self):
        # Going back is what makes Forward possible, and going forward is what
        # exhausts it. Wiring the buttons straight to the view meant neither
        # ever re-asked, so Forward stayed disabled for the whole session and
        # Back stayed enabled at the end of the history.
        self._view.goBack()
        self.updateHistoryButtons()

    def _goForward(self):
        self._view.goForward()
        self.updateHistoryButtons()

    def updateHistoryButtons(self):
        self._backButton.setEnabled(bool(self._view.canGoBack()))
        self._forwardButton.setEnabled(bool(self._view.canGoForward()))

    def setScaleNote(self, note: str):
        """Show what the current page measures, or nothing when it has no note."""
        self._scaleNote = str(note or '')
        self.updateScaleReadout()

    def updateScaleReadout(self):
        # DP-811. The model extent left the toolbar: the overlay card in the
        # viewport already reads it, and the row needs the room for tools.
        # What stays is a page's own note -- the base grid's cell size --
        # and the label shows only while there is one.
        note = self._scaleNote
        self._scaleLabel.setFloorText('')
        self._scaleLabel.setText(note)
        if self._scaleLabel.isHidden() == bool(note):
            self._scaleLabel.setVisible(bool(note))
        # DP-353. The row can be narrower than this sentence; when it
        # is, the label elides and the tooltip is where the whole of it
        # lives. ``ElidingLabel`` does that itself on every ``setText``,
        # composing the full value above the description it was handed
        # once at construction -- so re-describing it here changed
        # nothing on screen and made this the one control in the window
        # described in two places, which DP-185 forbids.

    def setQualityActionsEnabled(self, poor: bool, poorReason: str,
                                 failed: bool, failedReason: str):
        """Disabled controls carry their reason; none of them fail silently.

        CP-09 item 7. The reason used to be written on the way down and never
        taken off: a mesh loaded after an empty viewport left "No mesh is
        loaded" hanging on a live, working button. A control says either what
        it does or why it cannot -- never last session's excuse.
        """
        self._gate(self._poorCellsButton, poor, poorReason)
        self._gate(self._failedCellsButton, failed, failedReason)

    def setInspectionActionsEnabled(self, fidelity: bool, fidelityReason: str,
                                    features: bool, featuresReason: str):
        """The two overlays that need a stored fidelity report to say anything.

        CP-09 acceptance: "every enabled toolbar action has an observable
        useful effect; unavailable actions explain why". Both of these were
        permanently enabled and answered a press with a status-bar line that
        vanished in nine seconds, on every case that had never run the
        geometry-fidelity task -- which is every case until it does.
        """
        self._gate(self._fidelityButton, fidelity, fidelityReason)
        self._gate(self._featuresButton, features, featuresReason)

    def setLayerCoverageEnabled(self, enabled: bool, reason: str):
        """The layer-coverage entry, and why it is shut when it is.

        A menu entry rather than a button, so it is gated by tooltip and
        status tip -- `QAction` has no accessible description to set.
        """
        action = self._layerCoverageAction
        default = action.property('foammeshDefaultTip')
        if default is None:
            default = action.toolTip()
            action.setProperty('foammeshDefaultTip', default)
        action.setEnabled(bool(enabled))
        action.setToolTip(default if enabled else str(reason or ''))
        action.setStatusTip('' if enabled else str(reason or ''))

    def _gate(self, button, enabled: bool, reason: str):
        """Enable or disable one button, and keep its tooltip truthful."""
        default = button.property('foammeshDefaultTip')
        if default is None:
            default = button.toolTip()
            button.setProperty('foammeshDefaultTip', default)
        button.setEnabled(bool(enabled))
        button.setToolTip(default if enabled else str(reason or ''))
        button.setAccessibleDescription('' if enabled else str(reason or ''))

    def setInspectionActive(self, name: str, active: bool):
        """Make the icon say what the viewport is actually showing.

        VIEW-04. The controls were pushes, so the only thing that ever said
        an overlay was on was the overlay. A request that failed, or found no
        data, left a user looking at an unchanged picture with nothing
        anywhere saying whether the press had taken.
        """
        button = getattr(self, self.INSPECTION_BUTTONS.get(name, ''), None)
        if button is None:
            return
        blocked = button.blockSignals(True)
        button.setChecked(bool(active))
        button.blockSignals(blocked)
        if name == 'failedCells' and not active:
            self.setFailedCellSetShown('')

    def isInspectionActive(self, name: str) -> bool:
        """Whether the named overlay is the one on screen."""
        button = getattr(self, self.INSPECTION_BUTTONS.get(name, ''), None)
        return bool(button is not None and button.isChecked())

    def setFailedCellSetShown(self, name: str):
        """Name the set on screen on the control that put it there.

        VIEW-03. checkMesh routinely writes several sets and the button drew
        one of them, so the tooltip described an operation rather than the
        thing a user was looking at.
        """
        button = self._failedCellsButton
        default = button.property('foammeshDefaultTip')
        if default is None:
            default = button.toolTip()
            button.setProperty('foammeshDefaultTip', default)
        button.setToolTip(
            self.tr('Showing the failed cells in {0}').format(name)
            if name else default)

    def _rebuildFailedCellMenu(self):
        """List the sets the last check wrote, and the way out of them."""
        menu = self._failedCellsMenu
        menu.clear()
        reader = getattr(app.window, 'failedCellSetNames', None)
        names = list(reader() or ()) if reader is not None else []
        for name in names:
            menu.addAction(
                name,
                lambda checked=False, value=name:
                self._chooseFailedCellSet(value))
        if names:
            menu.addSeparator()
        menu.addAction(self.tr('Off'),
                       lambda checked=False: self._chooseFailedCellSet(''))

    def _chooseFailedCellSet(self, name: str):
        self._requestedFailedCellSet = str(name)
        self.failedCellsRequested.emit()

    def _failedCellsPressed(self):
        self._requestedFailedCellSet = None
        self.failedCellsRequested.emit()

    def requestedFailedCellSet(self):
        """Which set the last request named, read once.

        ``None`` means the press asked for the next one rather than a named
        one, and ``''`` means it asked for none.
        """
        requested = getattr(self, '_requestedFailedCellSet', None)
        self._requestedFailedCellSet = None
        return requested

    def _poorCellsPressed(self):
        self.poorCellsRequested.emit()

    def _resetInspection(self):
        """Ask the window to put the plain model back.

        Straight to the window, the way the screenshot entry reaches it: the
        toolbar's signal wiring lives in a routine this package does not own,
        so a new signal here would arrive connected to nothing.
        """
        reset = getattr(app.window, '_resetInspectionView', None)
        if reset is not None:
            reset()

    def actionReason(self, name: str) -> str:
        """What a named toolbar action says while it is unavailable.

        ``''`` when the action is live. Named rather than exposed as widgets
        so a test can ask the question a user asks -- "why is this greyed" --
        without reaching into the toolbar's private attributes.
        """
        attribute = {'poorCells': '_poorCellsButton',
                     'failedCells': '_failedCellsButton',
                     'fidelity': '_fidelityButton',
                     'features': '_featuresButton',
                     'layerCoverage': '_layerCoverageAction'}.get(name)
        button = getattr(self, attribute, None) if attribute else None
        if button is None or button.isEnabled():
            return ''
        return button.toolTip()

    def _saveScreenshotAs(self):
        path, _selected = QFileDialog.getSaveFileName(
            app.window, self.tr('Save viewport screenshot'), '',
            self.tr('PNG image (*.png)'))
        if path:
            if not path.lower().endswith('.png'):
                path += '.png'
            if self._view.saveScreenshot(path, scale=2) is False:
                app.window.statusBar().showMessage(self.tr(
                    'The viewport is not drawing, so there is nothing to '
                    'save.'), 8000)

    def enable(self):
        self._ui.toolbar.setEnabled(True)
        self._ui.renderingView.setEnabled(True)

    def disable(self):
        self.clear()
        self._ui.toolbar.setEnabled(False)
        self._ui.renderingView.setEnabled(False)

    def clear(self):
        self._ui.axis.setChecked(False)
        self._ui.cubeAxis.setChecked(False)
        # The ruler and the rotation centre are actors in the renderer, not
        # properties of the case, so closing a case used to leave both drawn
        # over the empty viewport with their buttons still lit.
        self._ui.ruler.setChecked(False)
        self._ui.rotationCenter.setChecked(False)
        # R49. Blank the whole readout, not just the note: the extent half
        # is read back off the render view, which still holds the closing
        # case's actors at this point in the teardown.
        self._scaleNote = ''
        self._scaleLabel.setText('')
        if self._ruler is not None:
            self._ruler.off()
            self._ruler = None
        if self._rotationCenter is not None:
            self._rotationCenter.off()
            self._rotationCenter = None
        self.updateHistoryButtons()

    def applyTheme(self, tokens):
        self._updateBGButtonStyle(self._ui.bg1, QColor.fromRgbF(*self._view.background1()))
        self._updateBGButtonStyle(self._ui.bg2, QColor.fromRgbF(*self._view.background2()))
        self._rethemeToolbarIcons()
        if self._ruler is not None:
            self._ruler.applyTheme(tokens)
        if self._rotationCenter is not None:
            self._rotationCenter.applyTheme(tokens)

    def _setRulerVisible(self, checked):
        if checked:
            self._ruler = RulerWidget(self._view.interactor(), self._view.renderer())
            if app.themeManager is not None and app.themeManager.tokens is not None:
                self._ruler.applyTheme(app.themeManager.tokens)
            self._ruler.on()
        else:
            self._ruler.off()
            self._ruler = None

    def _toggleRotationCenter(self, checked):
        if checked:
            self._rotationCenter = self._rotationCenter or RotationCenterWidget(self._view)
            if app.themeManager is not None and app.themeManager.tokens is not None:
                self._rotationCenter.applyTheme(app.themeManager.tokens)
            self._rotationCenter.on()
        else:
            self._rotationCenter.off()

    def _pickBackground1(self):
        self._dialog = self._newBGColorDialog()
        self._dialog.colorSelected.connect(self._setBackground1)
        self._dialog.open()

    def _pickBackground2(self):
        self._dialog = self._newBGColorDialog()
        self._dialog.colorSelected.connect(self._setBackground2)
        self._dialog.open()

    def _newBGColorDialog(self):
        dialog = QColorDialog(app.window)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        tokens = app.themeManager.tokens if app.themeManager is not None else None
        dialog.setCustomColor(0, QColor(tokens.value('viewport.top') if tokens else '#383d54'))
        dialog.setCustomColor(1, QColor(tokens.value('viewport.bottom') if tokens else '#d1d1d1'))

        return dialog

    def _setBackground1(self, color):
        r, g, b, a = color.getRgbF()
        self._view.setBackground1(r, g, b)
        self._updateBGButtonStyle(self._ui.bg1, color)
        self._saveBackground(bottom=color.name())
        self._updateBackgroundReset()

    def _setBackground2(self, color):
        r, g, b, a = color.getRgbF()
        self._view.setBackground2(r, g, b)
        self._updateBGButtonStyle(self._ui.bg2, color)
        self._saveBackground(top=color.name())
        self._updateBackgroundReset()

    def _compactBackgroundFrame(self):
        """DP-811/DP-815. One toolbar button for the background, not three.

        The word, the box, the two swatches and the reset were a strip of
        their own, drawn smaller than and below the buttons around them. They
        stay in the frame, hidden, as the controls the menu presses; what the
        row shows is one button the size of the rest, painted with the
        gradient it sets, whose menu offers the top colour, the bottom colour
        and the way back to the theme.
        """
        ui = self._ui
        frame = getattr(ui, 'frame_2', None)
        if frame is None:
            return
        for name in ('label_64', 'bg2', 'bg1'):
            widget = getattr(ui, name, None)
            if widget is not None:
                widget.hide()
        self._backgroundResetButton.hide()
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setToolTip('')

        button = QToolButton()
        button.setObjectName('viewportBackground')
        tip = self.tr('Viewport background')
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setIconSize(ICON_SIZE)
        button.setMinimumSize(BUTTON_SIZE)
        button.setMaximumSize(BUTTON_SIZE)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        hide_menu_indicator(button)
        menu = QMenu(button)
        menu.addAction(self.tr('Top colour…'), ui.bg2.click)
        menu.addAction(self.tr('Bottom colour…'), ui.bg1.click)
        menu.addSeparator()
        self._backgroundResetAction = menu.addAction(
            self.tr('Reset to theme colours'),
            self._backgroundResetButton.click)
        button.setMenu(menu)
        layout = frame.layout()
        if layout is not None:
            layout.setSpacing(0)
            layout.addWidget(button)
        self._backgroundButton = button
        self._updateBackgroundButton()

    def _updateBackgroundButton(self):
        """Paint the button with the gradient on screen, top over bottom."""
        button = getattr(self, '_backgroundButton', None)
        if button is None:
            return
        colours = getattr(self, '_swatchColours', {})
        top = colours.get('bg2') or QColor.fromRgbF(*self._view.background2())
        bottom = colours.get('bg1') or QColor.fromRgbF(
            *self._view.background1())
        size = ICON_SIZE
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, 0, size.height())
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, bottom)
        painter.setBrush(gradient)
        painter.setPen(QColor(0, 0, 0, 90))
        painter.drawRoundedRect(1, 1, size.width() - 2, size.height() - 2,
                                3, 3)
        painter.end()
        button.setIcon(QIcon(pixmap))
        action = getattr(self, '_backgroundResetAction', None)
        if action is not None:
            action.setEnabled(self._backgroundResetButton.isEnabled())

    def _newBackgroundReset(self):
        """DP-739. The way back from a picked background to the theme's.

        It sits in the swatch frame, after the two swatches it undoes, and is
        enabled only while there is a pick to forget.
        """
        button = QToolButton()
        button.setObjectName('viewportBackgroundReset')
        button.setText('↺')
        tip = self.tr('Reset the background to the theme colours')
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setAutoRaise(True)
        button.setMaximumSize(QSize(20, 20))
        button.clicked.connect(self._resetBackground)
        frame = getattr(self._ui, 'frame_2', None)
        layout = frame.layout() if frame is not None else None
        if layout is not None:
            layout.addWidget(button)
        self._backgroundResetButton = button
        self._updateBackgroundReset()
        return button

    def _updateBackgroundReset(self):
        button = getattr(self, '_backgroundResetButton', None)
        if button is None:
            return
        picked = getattr(self._view, 'hasUserBackground', None)
        button.setEnabled(bool(picked()) if callable(picked) else False)
        self._updateBackgroundButton()

    def _resetBackground(self):
        reset = getattr(self._view, 'resetBackground', None)
        if callable(reset):
            reset()
        settings = self._backgroundSettings()
        if settings is not None and hasattr(settings, 'clearViewportBackground'):
            settings.clearViewportBackground()
        self._updateBGButtonStyle(
            self._ui.bg1, QColor.fromRgbF(*self._view.background1()))
        self._updateBGButtonStyle(
            self._ui.bg2, QColor.fromRgbF(*self._view.background2()))
        self._updateBackgroundReset()

    @staticmethod
    def _backgroundSettings():
        """The loaded settings store, or None where none is loaded.

        DP-701. Read off the application, never the module-level store, so a
        process that loaded no settings -- a test, a headless run -- neither
        reads nor writes the user's file.
        """
        settings = getattr(app, '_settings', None)
        if settings is None or not hasattr(settings, 'getViewportBackground'):
            return None
        return settings

    def _saveBackground(self, **ends):
        # DP-701 (viewport audit 0925 F15). A picked colour lasted until the
        # window closed; the next session opened on the theme's gradient.
        settings = self._backgroundSettings()
        if settings is not None:
            settings.updateViewportBackground(**ends)

    def _restoreBackgrounds(self):
        """Give the view the gradient ends saved by an earlier session."""
        settings = self._backgroundSettings()
        saved = settings.getViewportBackground() if settings is not None else {}
        for key, setter in (('bottom', 'setBackground1'),
                            ('top', 'setBackground2')):
            color = QColor(saved.get(key, ''))
            if key in saved and color.isValid() and hasattr(self._view, setter):
                r, g, b, _a = color.getRgbF()
                getattr(self._view, setter)(r, g, b)

    def _updateBGButtonStyle(self, button, color):
        apply_color_swatch(button, color)
        # The one row button is painted from the swatches' colours, which is
        # what every path that changes the background already reports here.
        self._swatchColours = dict(getattr(self, '_swatchColours', {}))
        self._swatchColours[button.objectName()] = QColor(color)
        self._updateBackgroundButton()
