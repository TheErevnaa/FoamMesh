#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QFileDialog, QLabel, QMenu, QToolButton)

from widgets.rendering.rendering_widget import RenderingWidget
from widgets.rendering.rotation_center_widget import RotationCenterWidget
from widgets.rendering.ruler_widget import RulerWidget

from foammesh.app import app
from foammesh.view.main_window.main_window_ui import Ui_MainWindow
from foammesh.view.theming.icons import load_themed_icon
from foammesh.view.theming.status_colors import apply_color_swatch
from foammesh.view.display_control import view_modes


#: Matches the buttons the viewport toolbar already carries.
ICON_SIZE = QSize(22, 22)
BUTTON_SIZE = QSize(32, 32)


def _formatExtent(extent: float) -> str:
    """A model size a user can sanity-check at a glance.

    Unit mistakes are the most common silent error in a meshing workflow and
    cost a whole run to find out about.
    """
    if extent <= 0:
        return ''
    if extent >= 1:
        return f'{extent:.4g} m across'
    if extent >= 1e-3:
        return f'{extent * 1e3:.4g} mm across'
    return f'{extent * 1e6:.4g} µm across'


class RenderingTool(QObject):
    #: Asked for by the toolbar; the window owns what these actually do.
    sectionRequested = Signal()
    captureRequested = Signal()
    galleryRequested = Signal()
    isolateRequested = Signal()
    zoomSelectionRequested = Signal()
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

        self._updateBGButtonStyle(self._ui.bg1, QColor.fromRgbF(*self._view.background1()))
        self._updateBGButtonStyle(self._ui.bg2, QColor.fromRgbF(*self._view.background2()))

        self._ui.alignAxis.clicked.connect(self._view.alignCamera)
        self._ui.axis.toggled.connect(self._view.setAxisVisible)
        self._ui.cubeAxis.toggled.connect(self._view.setCubeAxisVisible)
        self._ui.ruler.toggled.connect(self._setRulerVisible)
        self._ui.fit.clicked.connect(self._view.fitCamera)
        self._ui.perspective.toggled.connect(self._view.setParallelProjection)
        self._ui.rotate.clicked.connect(self._view.rollCamera)
        self._ui.rotationCenter.clicked.connect(self._toggleRotationCenter)
        self._ui.bg1.clicked.connect(self._pickBackground1)
        self._ui.bg2.clicked.connect(self._pickBackground2)
        self._installViewActions()
        self._installToolbarControls()

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
    #: Wide enough for "Boundary mesh" at the toolbar font, so the mode the
    #: viewport is in is readable at the narrow window size item 7 asks about
    #: rather than elided to "Bound...".
    MODE_COMBO_MIN_WIDTH = 132

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
        cost a trip to the Mesh Check dashboard. Neither is a once-a-session
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
        self._namedViewMenu = menu.addMenu(self.tr('Saved Views'))
        self._namedViewMenu.setEnabled(False)
        menu.addAction(self.tr('Save Current View…'),
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
        self._saveImageAction = menu.addAction(self.tr('Save Image As…'),
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
            self.tr('Layer Coverage'), self.layerCoverageRequested.emit)
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

        self._backButton = self._toolButton(
            'viewBack.svg', self.tr('Previous view'), self._goBack)
        self._forwardButton = self._toolButton(
            'viewForward.svg', self.tr('Next view'), self._goForward)

        self._sectionButton = self._toolButton(
            'section.svg', self.tr('Cut a section along the current view'),
            self.sectionRequested.emit)
        self._isolateButton = self._toolButton(
            'isolate.svg', self.tr('Show only the selected parts'),
            self.isolateRequested.emit)
        self._zoomSelectionButton = self._toolButton(
            'zoomSelection.svg',
            self.tr('Fit the view to the selected parts  ({0})').format(
                self.ZOOM_SELECTION_SHORTCUT),
            self.zoomSelectionRequested.emit)
        self._showAllButton = self._toolButton(
            'showAll.svg', self.tr('Make every part visible again'),
            self.showAllRequested.emit)
        self._poorCellsButton = self._toolButton(
            'poorCells.svg',
            self.tr('Colour the worst tenth of the active quality metric'),
            self.poorCellsRequested.emit)
        self._fidelityButton = self._toolButton(
            'fidelity.svg',
            self.tr('Colour the surface by how far it left the reference '
                    'geometry'),
            self.fidelityRequested.emit)
        self._featuresButton = self._toolButton(
            'features.svg',
            self.tr('Show the declared feature edges, coloured by whether the '
                    'mesh kept them'),
            self.featuresRequested.emit)
        self._failedCellsButton = self._toolButton(
            'failedCells.svg',
            self.tr('Show the cell sets the last mesh check wrote'),
            self.failedCellsRequested.emit)

        self._captureButton = self._toolButton(
            'capture.svg',
            self.tr('Save a picture of the viewport into the case  ({0})').format(
                self.CAPTURE_SHORTCUT),
            self.captureRequested.emit)
        self._galleryButton = self._toolButton(
            'gallery.svg', self.tr('Open the capture gallery'),
            self.galleryRequested.emit)

        self._scaleLabel = QLabel()
        self._scaleLabel.setObjectName('viewportScale')
        # A note the current page can hang beside the model extent - the base
        # grid puts its cell size here, so the number and the box it describes
        # are read in one place instead of two.
        self._scaleNote = ''
        self._scaleLabel.setToolTip(
            self.tr('The largest dimension of what is on screen'))

        for widget in (self._modeCombo, self._viewMenuButton, self._backButton,
                       self._forwardButton, self._sectionButton,
                       self._isolateButton, self._zoomSelectionButton,
                       self._showAllButton,
                       self._poorCellsButton, self._failedCellsButton,
                       self._fidelityButton, self._featuresButton,
                       self._captureButton, self._galleryButton,
                       self._scaleLabel):
            layout.insertWidget(index, widget)
            index += 1

        capture = QAction(self.tr('Capture Viewport'), self._view)
        capture.setShortcut(QKeySequence(self.CAPTURE_SHORTCUT))
        capture.triggered.connect(self.captureRequested)
        self._view.addAction(capture)

        zoomSelection = QAction(self.tr('Zoom to Selection'), self._view)
        zoomSelection.setShortcut(QKeySequence(self.ZOOM_SELECTION_SHORTCUT))
        zoomSelection.triggered.connect(self.zoomSelectionRequested)
        self._view.addAction(zoomSelection)

        cheatSheet = QAction(self.tr('Viewport Shortcuts'), self._view)
        cheatSheet.setShortcut(QKeySequence(self.CHEAT_SHEET_SHORTCUT))
        cheatSheet.triggered.connect(self.showCheatSheet)
        self._view.addAction(cheatSheet)
        self._extraActions = [capture, zoomSelection, cheatSheet]

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

    def _toolButton(self, iconName, tooltip, slot):
        """Icon-only, sized to match the buttons the toolbar already carries.

        The tooltip and the accessible name carry the label: an unnamed glyph
        is only half an affordance, and Rule 1 of this plan is that a
        capability a user cannot see does not exist.
        """
        button = QToolButton()
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
        rows.append((self.tr('Capture Viewport'), self.CAPTURE_SHORTCUT))
        rows.append((self.tr('Zoom to Selection'), self.ZOOM_SELECTION_SHORTCUT))
        rows.append((self.tr('This list'), self.CHEAT_SHEET_SHORTCUT))
        return rows

    def showCheatSheet(self):
        from PySide6.QtWidgets import QMessageBox

        lines = '\n'.join(
            f'{shortcut:<12}{title}' for title, shortcut in self.shortcutTable())
        box = QMessageBox(app.window)
        box.setWindowTitle(self.tr('Viewport Shortcuts'))
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
        self._sequenceMenu.setEnabled(enabled)
        self._sequenceMenu.setToolTip('' if enabled else reason)


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
        """Show, beside the model extent, what the current page measures."""
        self._scaleNote = str(note or '')
        self.updateScaleReadout()

    def updateScaleReadout(self):
        extent = _formatExtent(self._view.modelExtent() or 0)
        parts = [text for text in (extent, self._scaleNote) if text]
        self._scaleLabel.setText('  ·  '.join(parts))

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
            app.window, self.tr('Save Viewport Screenshot'), '',
            self.tr('PNG image (*.png)'))
        if path:
            if not path.lower().endswith('.png'):
                path += '.png'
            self._view.saveScreenshot(path, scale=2)

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

    def _setBackground2(self, color):
        r, g, b, a = color.getRgbF()
        self._view.setBackground2(r, g, b)
        self._updateBGButtonStyle(self._ui.bg2, color)

    def _updateBGButtonStyle(self, button, color):
        apply_color_swatch(button, color)
