#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 43 LY6 and LY7: the viewport's three tool edges and the Parts panel.

``ViewportToolEdges`` is the one place that knows all of them:

* the **top bar** is the viewport toolbar, moved out of the row above the
  render window and into an overlay on it (so it no longer takes height from
  the model, folded or not);
* the **bottom bar** is Plan 43 section 1's "Parts count, Explode, Section,
  Units, Selection": the Parts count (opens the Parts panel), an Explode
  slider that moves with the panel's, Section (opens the Cut plane panel),
  the display unit and a summary of the selection; and it carries the
  readouts the corner used to hold -- the Measure answer with Clear, the
  resource usage, the cell count and the scale note. A result that appears
  while the bar is folded brings it out;
* the **side rail** is ``ViewportRail`` with its own ``ToolEdge``;
* the **Parts panel** is the rail's Parts popover, auto-hide or pinned; it
  opens beside the rail's Parts button (Plan 45, user 2026-10-17: the Parts
  chip is retired, the rail button is the one way in).

Every control of every edge is the widget it always was, moved. Nothing is
removed and no control has a second copy. See ``tool_edges`` for the rule
that none of this moves the model.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSlider, QToolButton, QWidget)

from foammesh.core import display_unit

from foammesh.view.main_window.tool_edges import (
    BOTTOM, EDGE_KEYS, EDGE_MARGIN, TOP, PartsPanelEdge, ToolEdge,
    centre_rect, centring_insets, make_edge_frame, make_pin_button,
    round_native_corners)
from foammesh.view.theming.metrics import GAP
from foammesh.view.theming.status_colors import style_floating_panel

EXPLODE_WIDTH = 90
#: Plan 44 MF3. The two pills: height, the side padding, and the glyph size
#: of a tool (16 px on a 24 to 26 px button, U2).
TOP_PILL_HEIGHT = 32
BOTTOM_PILL_HEIGHT = 30
PILL_PADDING = 12
SHOWN_SLACK = 12
PILL_GLYPH = 16
PILL_BUTTON = 26

#: The View menu state keys (``view_menu.STATE_SPEC``) these edges read, and
#: the edge each pin belongs to.
STATE_PIN = {'top': 'tools.pin_top', 'bottom': 'tools.pin_bottom',
             'rail': 'tools.pin_side'}
STATE_KEYS = (tuple(STATE_PIN.values())
              + ('tools.show_bottom', 'tools.reveal', 'parts.chip',
                 'parts.autohide', 'parts.show'))


class ViewportToolEdges(QObject):
    """The three edges and the Parts panel. What the user chose lives in the
    View menu's state (``bindViewState``), which is also what is saved."""

    def __init__(self, window, rail):
        view = window._ui.renderingView
        super().__init__(view)
        self._window = window
        self._ui = window._ui
        self._view = view
        self._rail = rail
        self._overlay = window._viewportOverlay
        self._tool = window._renderingTool
        self._state = None
        self._applyingState = False
        self._partsPinWanted = False

        self._buildTopBar()
        self._buildBottomBar()
        self._edges = {'top': self._top, 'bottom': self._bottom,
                       'rail': rail.edge()}
        self._buildPartsPanel()

    # -- the top bar -------------------------------------------------------- #

    def _buildTopBar(self) -> None:
        ui, view = self._ui, self._view
        toolbar = ui.toolbar
        # Out of the row above the render window: the model keeps the height.
        column = getattr(ui, 'verticalLayout', None)
        if column is not None:
            column.removeWidget(toolbar)
        content = toolbar
        # Measure (Inspect) and the "..." menu (Output) stood in an overlay of
        # their own; they join the top bar, the same two buttons.
        presetBar = getattr(view, 'presetBar', None)
        bar = presetBar() if callable(presetBar) else None
        self._topTools = ()
        if bar is not None:
            # Plan 45 J (P14). Born a child of the view, never a window of its
            # own: the toolbar beside the GL view is a native window, so the
            # row that takes it becomes one too. A parentless row became a
            # native *top-level* window, and once moved into the frame Qt
            # kept that window's screen position as its position in the frame
            # (measured: (2573, 151) for a row at (13, 1)). Every
            # mapToGlobal below it, and so where the View-mode list and the
            # Fit menu opened, was off by the view's screen position.
            content = QWidget(view)
            content.setObjectName('viewportTopBar')
            row = QHBoxLayout(content)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(GAP)
            row.addWidget(toolbar, 1)
            self._topTools = bar.takeTools()
            for button in self._topTools:
                row.addWidget(button)
            view.unregisterOverlay('camera-presets')
        pin = make_pin_button('top', self.tr('top bar'))
        frame = make_edge_frame(view, 'top', content, pin)
        self._shapePill(frame, TOP_PILL_HEIGHT)
        self._sizeGlyphs(content)
        self._topFrame = frame
        self._top = ToolEdge(view, 'top', TOP, frame, pin, self._topRect,
                             settled=self._tool.reflowToolbar)
        # Plan 44 MF3: a pinned top bar floats and reserves nothing. The
        # notice strip no longer stands below it (the clearance inset that
        # used to keep it clear of the bar is gone), so nothing the top bar
        # does moves anything else.
        getter = getattr(view, 'noticeStrip', None)
        strip = getter() if callable(getter) else None
        if strip is not None:
            strip.setClearance(lambda: 0)

    def _shapePill(self, frame, height: int) -> None:
        """A floating pill: a rounded plate, 12 px of padding at the ends,
        and a fixed minimum height (Plan 44 MF3)."""
        style_floating_panel(frame, radius=height // 2)
        row = frame.layout()
        row.setContentsMargins(PILL_PADDING, 0, PILL_PADDING, 0)
        frame.setFixedHeight(height)
        # Plan 45 P16: the pill is a native window over the GL view; cut it
        # to the sheet's radius or the square ends paint black.
        round_native_corners(frame, height // 2)

    def _sizeGlyphs(self, content: QWidget) -> None:
        """U2. The icon-only tools of the top pill draw a 16 px glyph on a
        26 px button (they were 20 px on 28 px). The glyph is tinted from the
        theme's foreground by the icon loader; hover, checked and ring come
        from the style sheet."""
        glyph = QSize(PILL_GLYPH, PILL_GLYPH)
        button_size = QSize(PILL_BUTTON, PILL_BUTTON)
        for button in content.findChildren(QToolButton):
            if button.text() or button.icon().isNull():
                continue
            if button.menu() is not None:
                # Split buttons keep the width their arrow needs.
                button.setIconSize(glyph)
                continue
            button.setIconSize(glyph)
            button.setMinimumSize(button_size)
            button.setMaximumSize(button_size)

    def clearRect(self) -> QRect:
        """The clear area in the view's pixels: what the sheet and the
        console leave. The pills are centred on it. Before the window has a
        clear-area controller it is the whole view."""
        controller = getattr(self._window, '_clearArea', None)
        if controller is not None:
            try:
                rect = controller.clearRect()
                if rect is not None and not rect.isEmpty():
                    return QRect(rect)
            except (RuntimeError, AttributeError, TypeError):
                pass
        return QRect(0, 0, self._view.width(), self._view.height())

    def attachClearArea(self, controller) -> None:
        """Re-place the pills whenever the clear area moves (a sheet or the
        console moved, the window was resized). Tools shown or hidden are
        followed by the frames' own layout requests."""
        controller.changed.connect(lambda _rect: self.reflow())
        # Plan 45 W1. The viewport overlay stands below the top pill when
        # the two share columns (``MainWindow.viewportOverlayRect``), but the
        # pill is not one of the clear area's floating items: when it grows
        # (a theme switch re-measures its tools), nothing told the overlay,
        # which stayed over the pill's end. A re-polish of the whole window
        # used to move the overlay by chance; now the pill says so itself.
        self._pillWatcher = _pill_watch(self, controller)
        self._topFrame.installEventFilter(self._pillWatcher)

    def reflow(self) -> None:
        for edge in (self._top, self._bottom):
            edge.apply()

    def topWantedWidth(self) -> int:
        """What the top pill asks for: its content with every tool in the
        row. The toolbar hides tools into its menu when the row is narrower
        than it wants, so what is hidden now still counts: the pill is as
        wide as all its tools together, and the toolbar only gives tools up
        when the clear area is narrower than that."""
        width = self._topFrame.sizeHint().width()
        tool = self._tool
        row = getattr(self._ui, 'horizontalLayout', None)
        spacing = max(row.spacing(), 0) if row is not None else 0
        for widget, _proxies in getattr(tool, '_hiddenTools', ()):
            width += tool._demandOf(widget) + spacing
            # A split button and the rule before its group cost a little
            # more once shown than the estimate says.
            width += SHOWN_SLACK
        return width

    def _topRect(self) -> QRect:
        clear = self.clearRect()
        return centre_rect(clear, QSize(self.topWantedWidth(), TOP_PILL_HEIGHT),
                           'x', clear.y() + EDGE_MARGIN)

    # -- the bottom bar ----------------------------------------------------- #

    def _openSection(self) -> None:
        window = self._window
        popover = getattr(window, 'sectionPopover', lambda: None)()
        if popover is not None and popover.isOpen():
            window.closeSectionPanel()
            return
        opener = getattr(window, 'openSectionPanel', None)
        if callable(opener):
            opener('cut')

    def _buildBottomBar(self) -> None:
        ui, view = self._ui, self._view
        # Plan 45 J: a child of the view from birth (see _buildTopBar).
        content = QWidget(view)
        content.setObjectName('viewportBottomBar')
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP)

        parts = QToolButton()
        parts.setObjectName('bottomParts')
        parts.setAutoRaise(True)
        parts.setText(self.tr('Parts'))
        parts.setToolTip(self.tr(
            'Parts: show, hide, isolate and explode (opens the Parts panel)'))
        parts.setAccessibleName(self.tr('Parts count'))
        parts.clicked.connect(lambda _c=False: self._rail.toggle('parts'))
        # DP-2719: its press is the panel's toggle, not a click away.
        self._rail.addOpener('parts', parts)

        # Explode: a twin of the Parts panel's slider. Either moves the other,
        # and the panel's is the one that tells the view.
        explode = QSlider(Qt.Orientation.Horizontal)
        explode.setObjectName('bottomExplode')
        explode.setRange(0, 100)
        explode.setFixedWidth(EXPLODE_WIDTH)
        explode.setToolTip(self.tr(
            'Explode: push the parts apart along the vector from the centre'))
        explode.setAccessibleName(self.tr('Explode'))
        explodeLabel = QLabel(self.tr('Explode'))
        explodeLabel.setBuddy(explode)
        explodeLabel.setObjectName('bottomExplodeLabel')

        section = QToolButton()
        section.setObjectName('bottomSection')
        section.setAutoRaise(True)
        section.setText(self.tr('Section ▾'))
        section.setToolTip(self.tr(
            'Section: aim the cut plane (opens the cut plane panel)'))
        section.setAccessibleName(self.tr('Section'))
        # Plan 45 N (Q1 a): the rail's Cut plane tool is gone; this opens the
        # one section panel (Plan 45 L) on its Cut plane tab, and a second
        # press folds it away again (DP-2719).
        section.clicked.connect(lambda _c=False: self._openSection())

        units = QLabel()
        units.setObjectName('bottomUnits')
        units.setToolTip(self.tr('The unit lengths are shown in'))
        units.setAccessibleName(self.tr('Units'))
        selection = QLabel()
        selection.setObjectName('bottomSelection')
        selection.setToolTip(self.tr('The parts selected in the view'))
        selection.setAccessibleName(self.tr('Selection'))

        for widget in (parts, explodeLabel, explode, section, units,
                       selection):
            row.addWidget(widget)
        row.addStretch(1)

        # What the corner held: the Measure answer with Clear (VP-163) and the
        # resource usage (VP-165). Same widgets, same names; they leave the
        # overlay slots they stood in.
        self._adopted = []
        measure = getattr(view, 'measureReadout', None)
        resource = getattr(view, 'resourceOverlay', None)
        for key, getter in (('measure-readout', measure),
                            ('resource-usage', resource)):
            widget = getter() if callable(getter) else None
            if widget is None:
                continue
            view.unregisterOverlay(key)
            # Over the GL view an overlay is its own native window; inside the
            # bar (itself a native window) it is a plain child. A native
            # readout makes its siblings, and so the whole row, native
            # windows, and a native child window of a frame that was hidden
            # and shown again lands off its layout rect.
            widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, False)
            row.addWidget(widget)
            self._adopted.append(widget)

        # The readouts the toolbar carried. Same widgets, same names.
        toolbar_row = getattr(ui, 'horizontalLayout', None)
        readout = getattr(ui, 'widget_17', None) or ui.cellCount
        for widget in (readout, getattr(self._tool, '_scaleLabel', None)):
            if widget is None:
                continue
            if toolbar_row is not None:
                toolbar_row.removeWidget(widget)
            row.addWidget(widget)

        pin = make_pin_button('bottom', self.tr('bottom bar'))
        frame = make_edge_frame(view, 'bottom', content, pin)
        self._shapePill(frame, BOTTOM_PILL_HEIGHT)
        self._bottomContent = content
        self._bottomFrame = frame
        self._partsButton = parts
        self._sectionButton = section
        self._explode = explode
        self._unitsLabel = units
        self._selectionLabel = selection
        self._bottom = ToolEdge(view, 'bottom', BOTTOM, frame, pin,
                                self._bottomRect, hold=self._bottomHeld)
        self._watcher = _ResultWatcher(self, self._bottom)
        for widget in self._adopted:
            widget.installEventFilter(self._watcher)

        # Explode both ways; Units and Selection from their sources.
        panelSlider = getattr(self._overlay, 'explodeSlider', None)
        self._panelExplode = panelSlider() if callable(panelSlider) else None
        if self._panelExplode is not None:
            explode.setValue(self._panelExplode.value())
            explode.valueChanged.connect(self._explodeFromBar)
            self._panelExplode.valueChanged.connect(self._explodeFromPanel)
        display_unit.subscribe(self._unitChanged)
        self._unitChanged()
        selected = getattr(self._overlay, 'selectionCountChanged', None)
        if selected is not None:
            selected.connect(self._selectionChanged)
        self._selectionChanged(0)
        self._overlay.sceneEmptyChanged.connect(self._sceneEmpty)
        self._sceneEmpty(self._overlay.isSceneEmpty())

    def _bottomHeld(self) -> bool:
        """A result on show (a measurement, the resource usage) keeps the bar
        out: it would otherwise fold away from what the user asked to see."""
        return any(not w.isHidden() for w in self._adopted)

    def _explodeFromBar(self, value: int) -> None:
        if self._panelExplode.value() != value:
            self._panelExplode.setValue(value)

    def _explodeFromPanel(self, value: int) -> None:
        if self._explode.value() != value:
            blocked = self._explode.blockSignals(True)
            self._explode.setValue(value)
            self._explode.blockSignals(blocked)

    def _unitChanged(self) -> None:
        self._unitsLabel.setText(
            self.tr('Units: {0}').format(display_unit.current()))

    def _selectionChanged(self, count: int) -> None:
        self._selectionLabel.setText(
            self.tr('Selection: none') if not count
            else self.tr('Selection: {0} selected').format(int(count)))

    def _bottomRect(self) -> QRect:
        """Content-sized, centred on the clear area's centre x, and floating
        ``EDGE_MARGIN`` above the console (the clear area's bottom)."""
        view = self._view
        frame = self._bottomFrame
        clear = self.clearRect()
        height = BOTTOM_PILL_HEIGHT
        top = clear.y() + clear.height() - height - EDGE_MARGIN
        # The side rail stands at the right of the view; the pill keeps off it
        # by shortening, not by sliding off the centre.
        right = view.width() - self._rail.extent()
        return centre_rect(clear, QSize(frame.sizeHint().width(), height),
                           'x', max(clear.y(), top),
                           hi=min(clear.x() + clear.width() - EDGE_MARGIN,
                                  right))

    def _sceneEmpty(self, empty: bool) -> None:
        for widget in (self._partsButton, self._sectionButton, self._explode):
            widget.setEnabled(not empty)

    # -- the Parts panel ---------------------------------------------------- #

    def _buildPartsPanel(self) -> None:
        # Plan 45 (user 2026-10-17): the Parts chip is retired; the rail's
        # Parts button is the one way in, and its list opens beside it.
        self._partsPanel = PartsPanelEdge(self._rail)
        self._overlay.chipCountsChanged.connect(self._countsChanged)
        self._overlay.sceneEmptyChanged.connect(self._panelSceneEmpty)
        self._partsPanel.pinnedChanged.connect(self._partsPinned)
        self._counts = (0, 0)

    def chip(self):
        """The Parts chip is retired (Plan 45); kept only so the window's
        float registration, which asks for it, finds none."""
        return None

    def partsPanel(self) -> PartsPanelEdge:
        return self._partsPanel

    def _countsChanged(self, shown: int, total: int) -> None:
        self._counts = (shown, total)
        # Plan 45 (user 2026-10-17): "2 of 2 parts" was said in three places.
        # The count is said here alone, and only when something is hidden --
        # then it is a warning, highlighted; with every part shown it is noise.
        hidden = max(0, total - shown) if total else 0
        self._partsButton.setText(
            self.tr('{0:,} of {1:,} hidden').format(hidden, total) if hidden
            else self.tr('Parts'))
        self._partsButton.setProperty('partial', bool(hidden))
        self._partsButton.style().unpolish(self._partsButton)
        self._partsButton.style().polish(self._partsButton)
        self._view.arrangeOverlays()

    def _panelSceneEmpty(self, empty: bool) -> None:
        if not empty and self._partsPinWanted and not self._partsPanel.isOpen():
            self._partsPanel.setPinned(True)

    def _partsPinned(self, on: bool) -> None:
        # A pin lost because the scene emptied is not the user letting go.
        if self._overlay.isSceneEmpty():
            return
        self._partsPinWanted = bool(on)
        self._tell('parts.autohide', not on)

    # -- the View menu state (LY8) ------------------------------------------ #

    def bindViewState(self, state) -> None:
        """Read the View menu's choices (LY8's ``ViewMenuState``) and keep
        them: the pins, the bottom bar, the Parts panel and
        how the edges are revealed. The state is the one place they live (and
        the one that is saved); the menu entries, Ctrl+H and Ctrl+0 only write
        to it, and this object applies what it holds."""
        self._state = state
        for key, edge in self._edges.items():
            edge.pinnedChanged.connect(
                lambda on, k=key: self._tell(STATE_PIN[k], bool(on)))
        self._partsPanel.openChanged.connect(
            lambda on: self._tell('parts.show', bool(on)))
        state.subscribe(self._stateChanged)
        for key in STATE_KEYS:
            self._apply(key)

    def _tell(self, key: str, value) -> None:
        if self._applyingState or self._state is None:
            return
        self._state.set(key, value)

    def _stateChanged(self, key: str) -> None:
        if key in STATE_KEYS:
            self._apply(key)

    def _apply(self, key: str) -> None:
        state = self._state
        value = state.get(key)
        self._applyingState = True
        try:
            for edge_key, state_key in STATE_PIN.items():
                if key == state_key:
                    self._edges[edge_key].setPinned(bool(value))
            if key == 'tools.show_bottom':
                self._bottom.setAvailable(bool(value))
            elif key == 'tools.reveal':
                for edge in self._edges.values():
                    edge.setRevealOnHover(value == 'hover')
                self._partsPanel.setRevealOnHover(value == 'hover')
            # 'parts.chip' (retired, Plan 45) is read and ignored.
            elif key == 'parts.autohide':
                self._partsPinWanted = not value
                self._partsPanel.setPinned(not value)
            elif key == 'parts.show':
                if value:
                    if not self._partsPanel.isOpen():
                        self._rail.open('parts')
                elif not value and self._partsPanel.isOpen():
                    self._rail.close('parts')
        finally:
            self._applyingState = False

    # -- the commands ------------------------------------------------------- #

    def edge(self, key: str) -> ToolEdge:
        return self._edges[key]

    def keys(self) -> tuple:
        return EDGE_KEYS

    def allPinned(self) -> bool:
        return all(self._edges[k].isPinned() for k in EDGE_KEYS)

    def setAllPinned(self, pinned: bool) -> None:
        for key in EDGE_KEYS:
            self._edges[key].setPinned(bool(pinned))

    def togglePinAll(self) -> None:
        """Ctrl+H: pin all three, or fold all three when all are pinned."""
        self.setAllPinned(not self.allPinned())

    # The three Show / Auto-hide choices are the View menu's: these only write
    # them to its state, which applies them back here (``_apply``).
    def setBottomBarShown(self, shown: bool) -> None:
        self._write('tools.show_bottom', bool(shown))

    def setPartsAutoHide(self, autoHide: bool) -> None:
        self._write('parts.autohide', bool(autoHide))

    def _write(self, key: str, value) -> None:
        if self._state is None:
            raise RuntimeError('the tool edges are not bound to the View menu')
        self._state.set(key, value)

    def insets(self) -> tuple:
        """What the edges take from the clear area (always nothing)."""
        return centring_insets(self._edges)

    def reachable(self) -> dict:
        """For each edge, whether its controls can be pressed right now, and
        the nub that brings them back. A test asks this of every state."""
        return {key: {'pinned': edge.isPinned(),
                      'revealed': edge.isRevealed(),
                      'nub': edge.nub().isVisibleTo(self._view)}
                for key, edge in self._edges.items()}


class _PillWatcher(QObject):
    """The top pill moved or was resized: place the viewport overlay again
    (and the floating items ranked after it)."""

    def __init__(self, owner: QObject, controller):
        super().__init__(owner)
        self._controller = controller

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            from foammesh.view.main_window.clear_area import FLOAT_RANK_OVERLAY
            try:
                self._controller.place_floats(after=FLOAT_RANK_OVERLAY - 1)
            except RuntimeError:                    # closing down
                pass
        return False


def _pill_watch(owner: QObject, controller) -> _PillWatcher:
    # A factory, so the facade inventory does not list the filter as a control.
    return _PillWatcher(owner, controller)


class _ResultWatcher(QObject):
    """A readout adopted by the bottom bar was shown: bring the bar out."""

    def __init__(self, owner: QObject, edge: ToolEdge):
        super().__init__(owner)
        self._edge = edge

    def eventFilter(self, watched, event):
        # ShowToParent: a readout shown inside a folded (hidden) bar still
        # brings the bar out; a Show event is not sent while the parent hides.
        if event.type() in (QEvent.Type.Show, QEvent.Type.ShowToParent):
            self._edge.reveal()
        return False
