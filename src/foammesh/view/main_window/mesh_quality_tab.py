"""The Mesh quality tab: the metric table, the offending elements, Accept anyway.

Plan 26 WP3.2. This is where the quality gate's refusal stops being a dead end.
The gate can discard a 141,486-element mesh over three elements; before this
existed the user was told the count and given no way to see the three, no way
to judge whether they mattered, and no way to accept them.

Three things live here and nowhere else:

* **Every metric, judged on its own limit.** Passing one metric must not mask
  another, so each row states its own limit, count and verdict.
* **The offending elements themselves** (WP1.2), worst first, with the
  positions the runner recorded so the viewer can be pointed at them.
* **Accept anyway**, enabled only where the verdict permits it. An `invalid`
  verdict is an inverted or zero-volume cell: no solver can integrate over one,
  so there is nothing for a human to consent to and the button stays disabled
  with the reason stated.
"""
from __future__ import annotations

from datetime import datetime, timezone

from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_TIGHT, CompactSpinBox)
from foammesh.core.quantities import aligned
from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QStackedWidget,
    QStyle, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.quality.phrasing import NO_MESH_YET
from foammesh.core.quality.policy import tier_name
from foammesh.core.quality.verdict import verdict_source, verdict_tier_line

from .flow_layout import FlowLayout
from .quality_histogram import QualityHistogram
from foammesh.view.main_window.surface_rows import SurfaceRowsBox

_ELEMENT_ROLE = int(Qt.ItemDataRole.UserRole)

#: Verdicts a human may accept. Mirrors ``core.gmsh.quality.OVERRIDABLE``;
#: asserted equal in the tests so the two cannot drift apart.
OVERRIDABLE_VERDICTS = frozenset({'blemish', 'fail'})

_METRIC_COLUMNS = ('Metric', 'Limit', 'Worst', 'Mean', 'Below', 'Allowance',
                   'Verdict')

#: R196. How many rows a table promises to show before the layout may
#: shrink it. MEASURED on the tee: a failing gate opened this tab with
#: both tables squeezed to their column headers -- one metric row and 13
#: offending elements existed and none was on screen -- because a
#: QTableWidget's own minimum is a couple of pixels, so the band could
#: honestly report that it fitted. A table that needs room now says so.
_VISIBLE_ROWS = 3
_ELEMENT_COLUMNS = ('Element', 'Value', 'Position', 'Zone')

#: Plan 39 VI8. The metrics a ranked list can be asked for, in the order the
#: Worst cells button offers them (field name, label).
_RANK_METRICS = (
    ('nonOrthoAngle', 'Non-orthogonal angle'),
    ('skewness', 'Skewness'),
    ('cellAspectRatio', 'Aspect ratio'),
    ('cellVolume', 'Volume'),
)
_OFFENDING_TITLE = 'Offending elements'
_NOT_CHECKED = 'This mesh has not been checked yet.'

#: Plan 43 LY10 (section 3). The three arrangements of one set of controls.
#: B is the default: the metrics table, the distribution with its limit and
#: the offending cells side by side, the inspector beside them. A shows the
#: metrics as cards; C is one compact tabbed column. Every control of the
#: tab is in all three.
LAYOUT_MODES = ('A', 'B', 'C')
DEFAULT_LAYOUT = 'B'
LAYOUT_NAMES = {'A': 'A: metric cards', 'B': 'B: table, distribution, cells',
                'C': 'C: compact'}


class MeshQualityTab(QWidget):
    """Region C's quality surface. Read-only apart from Accept anyway."""

    #: Emitted with the accepted verdict when the user overrides the gate.
    acceptRequested = Signal(dict)
    #: Emitted with an offending element record when the user selects one, so
    #: the viewport can put a camera on it.
    elementSelected = Signal(dict)
    #: Plan 39 VI8. The user asked for the worst cells of this metric (a field
    #: name of ``core.quality.cell_fields``).
    rankRequested = Signal(str)
    #: Plan 39 VI9. The Quality inspector. The metric changed (field name).
    metricChanged = Signal(str)
    #: The user typed or dragged a limit (float), or cleared it (None).
    limitChanged = Signal(object)
    #: "Use mesher limit" / "Use check limit" ('mesher' or 'check').
    limitSourceRequested = Signal(str)
    #: "Show cells beyond limit" was switched on or off.
    showBeyondToggled = Signal(bool)
    #: The toggle was set from outside (the picture refused or followed it):
    #: the View menu's record has to hear it too (Plan 43 LY10).
    beyondFollowed = Signal(bool)
    #: "Measure" was pressed: draw this metric's distribution.
    measureRequested = Signal(str)
    #: Plan 41 GN13. "Pick an element" was switched on or off.
    pickElementToggled = Signal(bool)
    #: Plan 40 UXA6 (#21). "Run mesh check" was pressed.
    runCheckRequested = Signal()
    #: Plan 41 GN23 (FM-04). Named selections of elements: save the listed
    #: cells under a name, show one, delete one.
    saveSelectionRequested = Signal(str, list)
    showSelectionRequested = Signal(str)
    deleteSelectionRequested = Signal(str)
    #: Plan 39 VI17 (I7). "Export report..." was pressed.
    exportReportRequested = Signal()
    #: Plan 43 LY10. The tab was arranged as A, B or C (the letter).
    layoutModeChanged = Signal(str)
    #: Plan 43 FIX-LY10 (DP-1948). The verdict, or the list of cells, on the
    #: tab changed (or was cleared): the labels on the model follow it.
    verdictChanged = Signal()
    #: Plan 44 MF23. Something the inspector shows changed (the metric, the
    #: limit, the cells beyond it, the listed cells, the toggles): the
    #: on-model inspector card reads it again.
    inspectorChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('meshQualityTab')
        self.setAccessibleName('Mesh quality report')
        self._verdict: dict = {}
        #: Plan 39 VI9. A mesh is loaded, so there is something to inspect
        #: whether or not a check has produced a verdict yet.
        self._inspector_available = False
        self._mode = ''
        #: Plan 44 MF23. The on-model card (None until a view hosts it) and
        #: whether the colouring on the model is the quality one.
        self._card = None
        self._on_model = True

        # Plan 43 FIX-LY10 (DP-1947). The console band hands this tab 280 px
        # less its tab bar, which is less than the sentences, the three-row
        # tables and the buttons ask for. A layout given less than its minimum
        # draws its widgets over one another (the note over the Metrics rows,
        # live at 1280 x 800), so everything above the buttons sits in one
        # scroll area: short of room, it scrolls and nothing overlaps.
        outer = _hold(QVBoxLayout, self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._scroll = _hold(QScrollArea, self)
        self._scroll.setObjectName('meshQualityScroll')
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = _hold(QWidget, self._scroll)
        self._content.setObjectName('meshQualityContent')
        self._scroll.setWidget(self._content)
        outer.addWidget(self._scroll, 1)
        footer = _hold(QWidget, self)
        footer_layout = _hold(QVBoxLayout, footer)
        footer_layout.setContentsMargins(GAP, GAP_TIGHT // 2, GAP, GAP_TIGHT)
        footer_layout.setSpacing(0)
        outer.addWidget(footer)

        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(GAP, GAP_TIGHT, GAP, GAP_TIGHT)
        layout.setSpacing(GAP_TIGHT)
        self._headline = QLabel(self)
        self._headline.setObjectName('meshQualityHeadline')
        self._headline.setWordWrap(True)
        layout.addWidget(self._headline)

        # R143. MEASURED: the QA run filed `failed_checks: 1` and
        # `advisory_findings: ["Concave cells (using face planes) found,
        # number of cells: 1747"]` into foammesh/quality/latest.json, and this
        # panel rendered neither -- the verdict already carries both lists and
        # nothing read them, so the one classified finding checkMesh made had
        # nowhere to surface (R100). They are repeated verbatim, never
        # re-graded: the severity beside each is the one the report assigned.
        self._findings = QLabel(self)
        self._findings.setObjectName('meshQualityFindings')
        self._findings.setWordWrap(True)
        self._findings.setAccessibleName(
            self.tr('Findings recorded by the last mesh check'))
        self._findings.setVisible(False)
        layout.addWidget(self._findings)

        # Plan 40 UXD9 (N7). Which tier of max non-orthogonality judged this
        # verdict: the checkMesh report tier, never the mesher's two gates.
        self._tier_line = QLabel(self)
        self._tier_line.setObjectName('meshQualityTierLine')
        self._tier_line.setWordWrap(True)
        self._tier_line.setProperty('foammeshTone', 'secondary')
        self._tier_line.setVisible(False)
        layout.addWidget(self._tier_line)

        self._metrics = _table(_METRIC_COLUMNS, self,
                               'Quality metrics and their limits')
        # DP-108. Held on the instance because it has to be hideable. On a
        # case with no mesh this group drew its heading row and then a third
        # of the window of empty grid -- R187 again, the fault the task pages
        # closed with `claimBodyStretch`, on a panel that does not go through
        # that machinery and so never got the fix.
        self._metrics_box = QGroupBox(self.tr('Metrics'), self)
        self._metrics_box.setObjectName('meshQualityMetricsBox')
        metrics_layout = QVBoxLayout(self._metrics_box)
        metrics_layout.addWidget(self._metrics)
        self._columns_note = QLabel(self)
        self._columns_note.setObjectName('meshQualityEmptyColumns')
        self._columns_note.setWordWrap(True)
        _NoteHeight(self._columns_note)
        self._columns_note.setProperty('foammeshTone', 'secondary')
        self._columns_note.setVisible(False)
        metrics_layout.addWidget(self._columns_note)

        # Plan 39 VI9 (F5c, F5e, F5f). The Quality inspector: metric, the
        # distribution, a limit, the cells beyond it, and the worst N. The
        # Display Control "Mesh quality" group it replaces is gone (Q3).
        # Held under the VI8 name `_rank_bar` because it is the same strip,
        # grown: a verdict names no elements, and the inspector asks for them.
        self._rank_bar = QGroupBox(self.tr('Inspect cells'), self)
        self._rank_bar.setObjectName('meshQualityRankBar')
        inspector = QVBoxLayout(self._rank_bar)
        metric_row = FlowLayout()
        rank_label = QLabel(self.tr('Metric'), self._rank_bar)
        self._rank_metric = QComboBox(self._rank_bar)
        self._rank_metric.setObjectName('meshQualityRankMetric')
        self._rank_metric.setAccessibleName(
            self.tr('Quality metric to inspect'))
        for name, label in _RANK_METRICS:
            self._rank_metric.addItem(self.tr(label), name)
        rank_label.setBuddy(self._rank_metric)
        self._measure_button = QPushButton(self.tr('Measure'), self._rank_bar)
        self._measure_button.setObjectName('meshQualityMeasure')
        self._measure_button.setToolTip(self.tr(
            'Measure every cell for this metric and draw how the cells are '
            'distributed. The first measurement of a mesh takes a moment.'))
        metric_row.addWidget(rank_label)
        metric_row.addWidget(self._rank_metric)
        metric_row.addWidget(self._measure_button)
        inspector.addLayout(metric_row)

        limit_row = FlowLayout()
        limit_label = QLabel(self.tr('Limit'), self._rank_bar)
        self._limit_field = QLineEdit(self._rank_bar)
        self._limit_field.setObjectName('meshQualityLimit')
        self._limit_field.setAccessibleName(
            self.tr('Limit for the chosen metric'))
        self._limit_field.setPlaceholderText(self.tr('none'))
        self._limit_field.setValidator(QDoubleValidator(self._limit_field))
        self._limit_field.setToolTip(self.tr(
            'Cells beyond this value count as failing. Type one, drag the '
            'line on the distribution, or take the limit of the mesher or '
            'of the check.'))
        limit_label.setBuddy(self._limit_field)
        self._use_mesher = QPushButton(self.tr('Use mesher limit'),
                                       self._rank_bar)
        self._use_mesher.setObjectName('meshQualityUseMesherLimit')
        self._use_check = QPushButton(self.tr('Use check limit'),
                                      self._rank_bar)
        self._use_check.setObjectName('meshQualityUseCheckLimit')
        limit_row.addWidget(limit_label)
        limit_row.addWidget(self._limit_field)
        limit_row.addWidget(self._use_mesher)
        limit_row.addWidget(self._use_check)
        inspector.addLayout(limit_row)

        beyond_row = FlowLayout()
        self._beyond_button = QPushButton(self.tr('Show cells beyond limit'),
                                          self._rank_bar)
        self._beyond_button.setObjectName('meshQualityShowBeyond')
        self._beyond_button.setCheckable(True)
        self._beyond_label = QLabel(self._rank_bar)
        self._beyond_label.setObjectName('meshQualityBeyondCount')
        self._beyond_label.setWordWrap(True)
        beyond_row.addWidget(self._beyond_button)
        beyond_row.addWidget(self._beyond_label)
        inspector.addLayout(beyond_row)

        worst_row = FlowLayout()
        worst_label = QLabel(self.tr('Worst'), self._rank_bar)
        self._rank_count = CompactSpinBox(self._rank_bar)
        self._rank_count.setObjectName('meshQualityRankCount')
        self._rank_count.setAccessibleName(
            self.tr('How many of the worst cells to list'))
        self._rank_count.setRange(1, 100000)
        self._rank_count.setValue(100)
        worst_label.setBuddy(self._rank_count)
        self._rank_button = QPushButton(self.tr('Find'), self._rank_bar)
        self._rank_button.setObjectName('meshQualityRank')
        self._rank_button.setToolTip(self.tr(
            'List the worst cells for this metric, worst first. Select a row '
            'to go to that cell.'))
        worst_row.addWidget(worst_label)
        worst_row.addWidget(self._rank_count)
        worst_row.addWidget(QLabel(self.tr('cells'), self._rank_bar))
        worst_row.addWidget(self._rank_button)
        inspector.addLayout(worst_row)

        selection_row = FlowLayout()
        self._selection_name = QLineEdit(self._rank_bar)
        self._selection_name.setObjectName('meshQualitySelectionName')
        self._selection_name.setPlaceholderText(self.tr('Selection name'))
        self._selection_name.setAccessibleName(
            self.tr('Name for the selection of the listed cells'))
        self._selection_save = QPushButton(self.tr('Save listed cells'),
                                           self._rank_bar)
        self._selection_save.setObjectName('meshQualitySelectionSave')
        self._selection_save.setToolTip(self.tr(
            'Keep the cells in the list below under this name, with the '
            'project, so they can be shown again after the mesh is made '
            'again'))
        self._selection_combo = QComboBox(self._rank_bar)
        self._selection_combo.setObjectName('meshQualitySelections')
        self._selection_combo.setAccessibleName(self.tr('Saved selections'))
        self._selection_show = QPushButton(self.tr('Show'), self._rank_bar)
        self._selection_show.setObjectName('meshQualitySelectionShow')
        self._selection_delete = QPushButton(self.tr('Delete'),
                                             self._rank_bar)
        self._selection_delete.setObjectName('meshQualitySelectionDelete')
        selection_row.addWidget(self._selection_name)
        selection_row.addWidget(self._selection_save)
        selection_row.addWidget(self._selection_combo)
        selection_row.addWidget(self._selection_show)
        selection_row.addWidget(self._selection_delete)
        inspector.addLayout(selection_row)
        self.set_selections([])
        self._selection_save.clicked.connect(self._on_save_selection)
        self._selection_show.clicked.connect(
            lambda: self.showSelectionRequested.emit(
                self._selection_name_chosen()))
        self._selection_delete.clicked.connect(
            lambda: self.deleteSelectionRequested.emit(
                self._selection_name_chosen()))

        # Plan 41 GN13 (A-13). Inspect element: click a cell, read its tag,
        # type, nodes, entity and Gmsh's measures.
        self._pick_element = QCheckBox(self.tr('Inspect element: click a cell'),
                                       self._rank_bar)
        self._pick_element.setObjectName('meshQualityPickElement')
        self._pick_element.setToolTip(self.tr(
            'Click a cell of the Gmsh mesh in the viewport to read its tag, '
            'type, nodes, entity, partition and quality measures'))
        self._pick_element.toggled.connect(self.pickElementToggled)
        self._element_readout = QLabel(self._rank_bar)
        self._element_readout.setObjectName('meshQualityElementReadout')
        self._element_readout.setWordWrap(True)
        self._element_readout.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._element_readout.setAccessibleName(
            self.tr('The inspected element'))
        self._element_readout.setVisible(False)
        inspector.addWidget(self._pick_element)
        inspector.addWidget(self._element_readout)

        self._rank_metric.currentIndexChanged.connect(self._on_metric)
        self._measure_button.clicked.connect(self._on_measure)
        self._limit_field.editingFinished.connect(self._on_limit_typed)
        self._use_mesher.clicked.connect(
            lambda: self.limitSourceRequested.emit('mesher'))
        self._use_check.clicked.connect(
            lambda: self.limitSourceRequested.emit('check'))
        self._beyond_button.toggled.connect(self.showBeyondToggled)
        self._rank_button.clicked.connect(self._on_rank)
        self._rank_bar.setVisible(False)
        for signal in (self._rank_metric.currentIndexChanged,
                       self._limit_field.textChanged,
                       self._beyond_button.toggled,
                       self._pick_element.toggled,
                       self._rank_count.valueChanged,
                       self.verdictChanged, self.layoutModeChanged):
            signal.connect(self._emit_inspector_changed)

        #: WP6.4. checkMesh reports a maximum, and a maximum of 66 degrees does
        #: not distinguish one bad cell from eight thousand -- the difference
        #: between "ignore" and "remesh". VI9 wires it (F5c).
        self._distribution = QualityHistogram(self)
        self._distribution.limitDragged.connect(self._on_limit_dragged)
        self._distribution_box = QGroupBox(self.tr('Distribution'), self)
        self._distribution_box.setObjectName('meshQualityDistributionBox')
        QVBoxLayout(self._distribution_box).addWidget(self._distribution)
        self._distribution_box.setVisible(False)

        # Plan 42 SG7. Gmsh's Statistics figures, as items of this tab (kind
        # `mesh`, decision D3): they follow the mesh on screen, so the group
        # shows only while the volume drawn carries Gmsh's arrays, and its
        # frame is hidden otherwise.
        from foammesh.view.gmsh_workflow.statistics_panel import (
            GmshStatisticsPanel)
        self._statistics = GmshStatisticsPanel(lambda: None, self)
        self._statistics.setObjectName('meshQualityStatistics')
        self._statistics.refreshButton().setVisible(False)
        self._statistics_box = QGroupBox(self.tr('Mesh statistics'), self)
        self._statistics_box.setObjectName('meshQualityStatisticsBox')
        QVBoxLayout(self._statistics_box).addWidget(self._statistics)
        self._statistics_box.setVisible(False)

        self._elements = _table(_ELEMENT_COLUMNS, self,
                                'Elements below the requested limit')
        self._elements.itemSelectionChanged.connect(self._on_element_selected)
        self._elements.itemSelectionChanged.connect(
            self._emit_inspector_changed)
        # A row already selected selects nothing when pressed again, so the
        # way back to a cell after looking elsewhere is a double-click.
        self._elements.itemDoubleClicked.connect(
            lambda _item: self._on_element_selected())
        self._elements_box = QGroupBox(self.tr(_OFFENDING_TITLE), self)
        self._elements_box.setObjectName('meshQualityElementsBox')
        QVBoxLayout(self._elements_box).addWidget(self._elements)

        # Plan 43 LY10. The metric cards of layout A: one card per metric row,
        # holding every column of the table.
        self._cards_area = _hold(QScrollArea, self)
        self._cards_area.setObjectName('meshQualityCards')
        self._cards_area.setWidgetResizable(True)
        self._cards_area.setFrameShape(QFrame.Shape.NoFrame)
        self._cards_host = _hold(QWidget, self._cards_area)
        self._cards_layout = _hold(QVBoxLayout, self._cards_host)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(GAP_TIGHT)
        self._cards_area.setWidget(self._cards_host)
        self._cards_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cards_area.setMinimumWidth(160)
        self._cards_area.setVisible(False)

        # The body: the arrangement is a page of this stack, and the same
        # widgets are moved between pages (see `set_layout_mode`).
        self._body = _hold(QStackedWidget, self)
        self._body.setObjectName('meshQualityBody')
        self._pages = {}
        for mode in LAYOUT_MODES:
            page = _hold(QWidget, self._body)
            page.setObjectName('meshQualityLayout' + mode)
            self._pages[mode] = page
            self._body.addWidget(page)
        self._parking = _hold(QWidget, self)
        self._parking.setVisible(False)
        self._distribution_column = _hold(QWidget, self)
        QVBoxLayout(self._distribution_column).setContentsMargins(0, 0, 0, 0)
        self._inspector_scroll = _hold(QScrollArea, self)
        self._inspector_scroll.setObjectName('meshQualityInspectorScroll')
        self._inspector_scroll.setWidgetResizable(True)
        self._inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # DP-1947. The rows of the inspector wrap (FlowLayout), so it needs
        # no sideways bar; it keeps a width its widest control fits in.
        self._inspector_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._inspector_scroll.setMinimumWidth(
            self._rank_bar.minimumSizeHint().width()
            + self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
            + 2)
        self._tabs = _hold(QTabWidget, self)
        self._tabs.setObjectName('meshQualityTabs')
        self._tabs.setDocumentMode(True)
        # DP-1776. The body is held in a scroll area: when the band is shorter
        # or narrower than the columns need, the columns keep the size they
        # need and the body scrolls, instead of the groups being squeezed over
        # each other or cut at the edge.
        self._body_scroll = _hold(_FitScrollArea, self)
        self._body_scroll.setObjectName('meshQualityBodyScroll')
        self._body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._body_scroll.hold(self._body)
        layout.addWidget(self._body_scroll, 1)

        # Plan 43 LY11. The deviation figures and the tolerance, while the
        # model is coloured by deviation.
        self._surface_rows = SurfaceRowsBox(self)
        layout.addWidget(self._surface_rows)

        # DP-108. Takes the body space when no table is there to take it, so
        # the sentence at the top of the panel stays at the top instead of
        # being stretched down the middle of an empty tab. Exactly one of
        # this and the groups above is ever visible; see `_claim_body_space`.
        self._filler = QWidget(self)
        self._filler.setObjectName('meshQualityFiller')
        layout.addWidget(self._filler, 1)

        self._export_report = QPushButton(self.tr('Export report…'), self)
        self._export_report.setObjectName('meshQualityExportReport')
        self._export_report.setToolTip(self.tr(
            'Write the metrics, the histogram, the cells listed above, the '
            'check summary and a picture of the view into the case, as an '
            'HTML page and a Markdown page'))
        self._export_report.setEnabled(False)
        self._export_report.clicked.connect(self.exportReportRequested)

        self._run_check = QPushButton(self.tr('Run mesh check'), self)
        self._run_check.setObjectName('meshQualityRunCheck')
        self._run_check.setToolTip(self.tr(
            'Measure this mesh now. The result appears here.'))
        self._run_check.clicked.connect(self.runCheckRequested)
        self._run_check.setVisible(False)

        self._accept = QPushButton(self.tr('Accept anyway'), self)
        self._accept.setObjectName('meshQualityAccept')
        self._accept.clicked.connect(self._on_accept)
        # DP-109. The reason this button is disabled was computed, written to
        # a tooltip and to the accessible description, and shown on screen
        # nowhere -- so a disabled control sat alone in the corner of the
        # window with nothing beside it, and a user cannot tell a control
        # that is waiting from one that is forbidden from one that is broken.
        # It goes next to the button because the two are one statement.
        self._accept_reason = QLabel(self)
        self._accept_reason.setObjectName('meshQualityAcceptReason')
        self._accept_reason.setWordWrap(True)
        self._accept_reason.setProperty('foammeshTone', 'secondary')
        buttons = QHBoxLayout()
        buttons.addWidget(self._export_report)
        buttons.addWidget(self._run_check)
        buttons.addWidget(self._accept_reason, 1)
        buttons.addWidget(self._accept)
        footer_layout.addLayout(buttons)
        self.set_layout_mode(DEFAULT_LAYOUT)
        self.clear()

    # -- Plan 43 LY10: the three layouts ------------------------------------ #

    def layout_mode(self) -> str:
        return self._mode

    def set_layout_mode(self, mode: str) -> None:
        """Arrange the tab as A, B or C. The controls are the same objects in
        all three; only where they sit changes, so nothing is lost by
        switching and no state (limit, selection, table rows) is reset."""
        mode = str(mode).upper()
        if mode not in LAYOUT_MODES:
            mode = DEFAULT_LAYOUT
        if mode == self._mode:
            return
        self._mode = mode
        movable = (self._metrics_box, self._distribution_box,
                   self._statistics_box, self._elements_box, self._rank_bar)
        # Moving a widget between parents forgets that it was hidden on
        # purpose (no mesh, no rows): remember it and put it back.
        hidden = {widget: widget.isHidden() for widget in movable}
        if self._inspector_scroll.widget() is not None:
            self._inspector_scroll.takeWidget()
        for widget in movable:
            widget.setParent(self._parking)
        while self._tabs.count():
            host = self._tabs.widget(0)
            self._tabs.removeTab(0)
            if host is self._inspector_scroll:
                host.setParent(self._parking)
            else:
                host.deleteLater()
        column = self._distribution_column.layout()
        while column.count():
            column.takeAt(0)
        for page in self._pages.values():
            old = page.layout()
            if old is not None:
                # `setLayout` hands every widget the layout manages to the new
                # parent, which would delete them with it: let go of them first.
                while old.count():
                    held = old.takeAt(0).widget()
                    if held is not None:
                        held.setParent(self._parking)
                QWidget().setLayout(old)
        page = self._pages[mode]
        grid = _hold(QHBoxLayout, page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(GAP)
        # Compact asks for two visible rows, not three, so the tab fits the
        # 280 px it opens at (R196's three rows hold for A and B).
        for table in (self._metrics, self._elements):
            rows = table.verticalHeader().defaultSectionSize()
            table.setMinimumHeight(
                table.horizontalHeader().sizeHint().height()
                + (2 if mode == 'C' else _VISIBLE_ROWS) * rows
                + 2 * table.frameWidth())
        if mode == 'C':
            self._build_compact(grid)
        else:
            self._build_columns(grid, with_table=(mode == 'B'))
        for widget, was_hidden in hidden.items():
            widget.setVisible(not was_hidden)
        self._body.setCurrentWidget(page)
        self._cards_area.setVisible(mode == 'A' and self._cards_wanted())
        self._claim_body_space()
        self.layoutModeChanged.emit(mode)

    def _build_columns(self, grid, *, with_table: bool) -> None:
        if with_table:
            grid.addWidget(self._metrics_box, 3)
        else:
            grid.addWidget(self._cards_area, 2)
        column = self._distribution_column.layout()
        column.addWidget(self._distribution_box, 1)
        column.addWidget(self._statistics_box, 1)
        grid.addWidget(self._distribution_column, 2)
        grid.addWidget(self._elements_box, 2)
        self._inspector_scroll.setWidget(self._rank_bar)
        grid.addWidget(self._inspector_scroll, 2)
        self._claim_column_widths()

    def _claim_column_widths(self) -> None:
        """DP-1776. Each column keeps the width its controls need; a band
        narrower than the sum scrolls (see ``_body_scroll``), so no control is
        cut off at a column's edge and the inspector never needs a scroll bar
        of its own across."""
        bar = self._inspector_scroll
        self._metrics_box.setMinimumWidth(0)
        self._elements_box.setMinimumWidth(0)
        bar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar.setMinimumWidth(
            self._rank_bar.minimumSizeHint().width()
            + 2 * bar.frameWidth()
            + bar.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))

    def _build_compact(self, grid) -> None:
        self._inspector_scroll.setMinimumWidth(0)
        self._inspector_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._metrics_box.setMinimumWidth(0)
        self._elements_box.setMinimumWidth(0)
        for box, title in ((self._metrics_box, 'Metrics'),
                           (self._distribution_box, 'Distribution'),
                           (self._elements_box, 'Offending elements'),
                           (self._statistics_box, 'Mesh statistics'),
                           (self._rank_bar, 'Inspect cells')):
            if box is self._rank_bar:
                # The inspector is tall: it scrolls inside its tab.
                self._inspector_scroll.setWidget(box)
                self._tabs.addTab(self._inspector_scroll, self.tr(title))
                continue
            host = _hold(QWidget, self._tabs)
            inner = _hold(QVBoxLayout, host)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(box)
            self._tabs.addTab(host, self.tr(title))
        grid.addWidget(self._tabs, 1)

    def _sync_columns(self) -> None:
        """Hide a column none of whose boxes is drawn (a hidden child does
        not hide its holder by itself)."""
        self._distribution_column.setVisible(not (
            self._distribution_box.isHidden()
            and self._statistics_box.isHidden()))
        if self._mode != 'C':
            self._inspector_scroll.setVisible(not self._rank_bar.isHidden())
        if self._mode == 'C':
            boxes = (self._metrics_box, self._distribution_box,
                     self._elements_box, self._statistics_box,
                     self._rank_bar)
            for position, box in enumerate(boxes):
                self._tabs.setTabVisible(position, not box.isHidden())

    def _cards_wanted(self) -> bool:
        return self._cards_layout.count() > 0

    def card_texts(self) -> list:
        """The text of each metric card (layout A), for the tests."""
        texts = []
        for position in range(self._cards_layout.count()):
            card = self._cards_layout.itemAt(position).widget()
            if card is not None:
                texts.append(' | '.join(
                    label.text() for label in card.findChildren(QLabel)))
        return texts

    def _populate_cards(self, rows) -> None:
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for metric in rows:
            card = _hold(QFrame, self._cards_host)
            card.setObjectName('meshQualityCard')
            card.setFrameShape(QFrame.Shape.StyledPanel)
            inner = _hold(QVBoxLayout, card)
            inner.setContentsMargins(
                FORM_MARGIN, GAP_TIGHT, FORM_MARGIN, GAP_TIGHT)
            inner.setSpacing(GAP_TIGHT // 4)
            name = _hold(QLabel, str(metric.get('measure') or ''), card)
            name.setObjectName('meshQualityCardName')
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            verdict = str(metric.get('verdict') or '')
            state = _hold(QLabel, verdict, card)
            state.setObjectName('meshQualityCardVerdict')
            state.setProperty('foammeshStatus', {
                'fail': 'error', 'blemish': 'warning'}.get(verdict))
            tier = tier_name(str(metric.get('tier') or ''))
            limit = _number(metric.get('requestedMinimum'))
            if tier:
                limit = f'{limit} ({tier})'
            head = _hold(QHBoxLayout, )
            head.addWidget(name, 1)
            head.addWidget(state)
            inner.addLayout(head)
            for line in (
                    str(self.tr('Limit {0}')).format(limit),
                    str(self.tr('Worst {0}, mean {1}')).format(
                        _number(metric.get('achievedMinimum')),
                        _number(metric.get('achievedMean'))),
                    str(self.tr('Below {0}, allowance {1}')).format(
                        _count(metric.get('belowThreshold'),
                               metric.get('total')),
                        _allowance(metric.get('allowance')))):
                label = _hold(QLabel, line, card)
                # DP-1947: a line wraps rather than widening the card.
                label.setWordWrap(True)
                inner.addWidget(label)
            card.setMinimumWidth(140)
            self._cards_layout.addWidget(card)
        if rows:
            self._cards_layout.addStretch(1)
        self._cards_area.setVisible(self._mode == 'A' and bool(rows))

    # -- data -------------------------------------------------------------- #

    # -- Plan 42 SG7: the Gmsh items ---------------------------------------- #

    def surface_rows(self) -> SurfaceRowsBox:
        return self._surface_rows

    def statistics_panel(self):
        return self._statistics

    def statistics_box(self) -> QGroupBox:
        return self._statistics_box

    def set_statistics_source(self, controller, client=None) -> None:
        """Where the statistics are read: ``controller()`` is the resident
        Gmsh session controller (or None), ``client`` holds the gate limits."""
        self._statistics._controller = controller
        self._statistics._client = client

    def on_session_mesh(self) -> bool:
        """A Gmsh session mesh is on screen: the check is the judgement of
        its elements against the mesher's limits (Plan 42 SG7)."""
        return not self._statistics_box.isHidden()

    def judge_session_mesh(self) -> dict:
        """The one "Run mesh check" on a Gmsh session mesh: judge every
        element against the mesher's limits; the verdict goes to the strip."""
        return self._statistics.judge()

    def show_mesh_statistics(self, on: bool) -> None:
        """The mesh on screen carries Gmsh's arrays (or no longer does)."""
        on = bool(on)
        if on:
            self._statistics.refresh()
        else:
            self._statistics.clear()
        self._statistics_box.setVisible(on)
        self._claim_body_space()

    def clear(self) -> None:
        self._verdict = {}
        self._headline.setText(self.tr(NO_MESH_YET))
        self._metrics.setRowCount(0)
        self._elements.setRowCount(0)
        self._populate_findings((), ())
        self._tier_line.setVisible(False)
        self._tier_line.setText('')
        self._metrics_box.setVisible(False)
        self._elements_box.setVisible(False)
        self._elements_box.setTitle(self.tr(_OFFENDING_TITLE))
        self._rank_bar.setVisible(self._inspector_available)
        self._beyond_label.setText('')
        self._distribution.clear()
        self._distribution_box.setVisible(False)
        self._claim_body_space()
        self._set_accept(False, '')
        self._show_accept(False)
        self._run_check.setVisible(self._inspector_available)
        if self._inspector_available:
            self._headline.setText(self.tr(_NOT_CHECKED))
        self.verdictChanged.emit()

    def show_verdict(self, verdict: dict) -> None:
        self._verdict = dict(verdict or {})
        if not self._verdict:
            self.clear()
            return
        # D9. The verdict strip a few pixels above already states the
        # verdict. What this line adds is the reason behind it -- and, when
        # there is no reason because nothing was wrong, what the limits were
        # and what they are *not* (F14: checkMesh judges a different thing,
        # and the two disagreeing without explanation reads as a bug).
        headline = str(self._verdict.get('reason')
                       or self.tr('Every requested quality limit was met. '
                                  'checkMesh runs its own separate checks; '
                                  'the Quality task reports those.'))
        if self._verdict.get('stale'):
            headline = str(self.tr(
                'This check was run against a different mesh than the one now '
                'loaded. Re-run Mesh → Mesh check. ')) + headline
        elif self._verdict.get('checkedAt'):
            headline += ' ({0}, {1})'.format(
                verdict_source(self._verdict),
                local_time_text(self._verdict['checkedAt']))
        self._headline.setText(headline)
        self._populate_findings(self._verdict.get('blocking') or (),
                                self._verdict.get('advisory') or ())
        tier_line = verdict_tier_line(self._verdict)
        self._tier_line.setText(tier_line)
        self._tier_line.setVisible(bool(tier_line))
        self._populate_metrics(self._verdict.get('metrics') or ())
        self._elements_box.setTitle(self.tr(_OFFENDING_TITLE))
        self._populate_elements(self._verdict.get('offending') or ())
        # The inspector stands beside the gate's own offending elements: the
        # user may ask for the worst cells of any metric, and for the cells
        # beyond a limit, whether or not the verdict named some.
        self._rank_bar.setVisible(True)
        self._update_accept()
        verdict_kind = str(self._verdict.get('verdict') or '')
        self._show_accept(verdict_kind not in ('', 'unrated'))
        self._run_check.setVisible(
            self._inspector_available
            and (verdict_kind in ('', 'unrated')
                 or bool(self._verdict.get('stale'))))
        self._claim_body_space()
        self.verdictChanged.emit()

    def show_ranked(self, metric_label: str, rows) -> None:
        """Fill the element table with the worst cells of one metric.

        Plan 39 VI8. Worst first, as ranked; the box says which metric it is
        so a table of numbers is never separated from the thing it measures.
        """
        rows = list(rows or ())
        self._populate_elements(rows)
        self._elements_box.setTitle(
            self.tr('Worst cells: {0}').format(metric_label) if rows
            else self.tr(_OFFENDING_TITLE))
        self._claim_body_space()
        self.verdictChanged.emit()

    def listed_elements(self) -> list:
        """The cells in the table, in order: what Export report lists."""
        rows = []
        for row in range(self._elements.rowCount()):
            item = self._elements.item(row, 0)
            element = item.data(_ELEMENT_ROLE) if item is not None else None
            if isinstance(element, dict):
                rows.append(dict(element))
        return rows

    def listed_title(self) -> str:
        box = self._elements_box
        return box.title()

    def verdict(self) -> dict:
        return dict(self._verdict)

    def distribution_widget(self) -> QualityHistogram:
        return self._distribution

    def export_button(self) -> QPushButton:
        return self._export_report

    def listed_tags(self) -> list:
        """The element tags of the cells in the table (Plan 41 GN23)."""
        tags = []
        for row in range(self._elements.rowCount()):
            item = self._elements.item(row, 0)
            element = item.data(_ELEMENT_ROLE) if item is not None else None
            tag = element.get('tag') if isinstance(element, dict) else None
            if tag is not None:
                tags.append(int(tag))
        return tags

    def set_selections(self, names, notes=None) -> None:
        """The saved selections to choose from (Plan 41 GN23). *notes* maps a
        name to what to say of a selection made on another mesh (Plan 42
        SG14): it is marked in the list and its note is the tooltip."""
        names = list(names or ())
        notes = notes or {}
        current = self._selection_combo.currentData()             or self._selection_combo.currentText()
        self._selection_combo.clear()
        for name in names:
            note = notes.get(name) or ''
            self._selection_combo.addItem(
                f'{name} (other mesh)' if note else name, name)
            if note:
                self._selection_combo.setItemData(
                    self._selection_combo.count() - 1, note,
                    Qt.ItemDataRole.ToolTipRole)
        if current in names:
            self._selection_combo.setCurrentIndex(names.index(current))
        for button in (self._selection_show, self._selection_delete):
            button.setEnabled(bool(names))
        self._selection_combo.setEnabled(bool(names))

    def _selection_name_chosen(self) -> str:
        return str(self._selection_combo.currentData()
                   or self._selection_combo.currentText())

    def _on_save_selection(self) -> None:
        self.saveSelectionRequested.emit(self._selection_name.text(),
                                         self.listed_tags())

    def show_inspection(self, rows) -> None:
        """The inspected element's read-out, ``[(label, text), ...]``."""
        rows = list(rows or ())
        self._element_readout.setText('\n'.join(
            f'{label}: {text}' for label, text in rows))
        self._element_readout.setVisible(bool(rows))

    def inspection_text(self) -> str:
        return self._element_readout.text()

    def set_picking_element(self, on: bool) -> None:
        if self._pick_element.isChecked() != bool(on):
            self._pick_element.setChecked(bool(on))

    def set_rank_metrics(self, metrics, current: str = '') -> None:
        """Offer exactly *metrics* ``[(field name, label), ...]``.

        Plan 41 GN13. A mesh from the Gmsh session is judged by Gmsh's
        measures, any other by OpenFOAM's four.
        """
        wanted = [(str(name), str(label)) for name, label in metrics]
        have = [(str(self._rank_metric.itemData(i)),
                 self._rank_metric.itemText(i))
                for i in range(self._rank_metric.count())]
        if wanted and [name for name, _ in wanted] != [n for n, _ in have]:
            self._rank_metric.blockSignals(True)
            self._rank_metric.clear()
            for name, label in wanted:
                self._rank_metric.addItem(self.tr(label), name)
            self._rank_metric.blockSignals(False)
        if current:
            self.set_rank_metric(current)

    def set_rank_metric(self, name: str) -> None:
        """Follow the metric chosen elsewhere (the toolbar's Worst cells)."""
        position = self._rank_metric.findData(str(name))
        if position >= 0 and position != self._rank_metric.currentIndex():
            self._rank_metric.setCurrentIndex(position)

    def rank_metric(self) -> str:
        return str(self._rank_metric.currentData() or '')

    def _on_rank(self) -> None:
        self.rankRequested.emit(self.rank_metric())

    # -- the inspector (Plan 39 VI9) ---------------------------------------- #

    def set_inspector_available(self, available: bool) -> None:
        """A mesh is loaded (or gone): the inspector can (not) be used."""
        self._inspector_available = bool(available)
        self._export_report.setEnabled(bool(available))
        self._rank_bar.setVisible(bool(available) or bool(self._verdict))
        self._emit_inspector_changed()
        if not self._verdict:
            self._run_check.setVisible(bool(available))
            self._headline.setText(self.tr(
                _NOT_CHECKED if available else NO_MESH_YET))
        if not available:
            self._beyond_label.setText('')
            self._beyond_button.blockSignals(True)
            self._beyond_button.setChecked(False)
            self._beyond_button.blockSignals(False)
            self.beyondFollowed.emit(False)
            self._distribution.clear()
            self._distribution_box.setVisible(False)
        self._claim_body_space()

    def rank_count(self) -> int:
        return int(self._rank_count.value())

    def limit(self) -> float | None:
        """The limit in the field, or None when it is empty."""
        return _parse_limit(self._limit_field.text())

    def set_limit(self, value) -> None:
        """Show *value* as the limit without announcing it as a user edit."""
        self._limit_field.blockSignals(True)
        self._limit_field.setText('' if value is None else f'{float(value):.6g}')
        self._limit_field.blockSignals(False)
        if self._distribution.limit() != (None if value is None
                                          else float(value)):
            self._distribution.set_limit(value)

    def set_limit_sources(self, mesher, check) -> None:
        """Say which limits the buttons would take, and disable a missing one."""
        for button, value, noun in ((self._use_mesher, mesher, 'mesher'),
                                    (self._use_check, check, 'check')):
            button.setEnabled(value is not None)
            button.setToolTip(
                str(self.tr('Take the {0} limit of {1:g} for this metric.'))
                .format(noun, value) if value is not None else str(self.tr(
                    'The {0} has no limit for this metric.')).format(noun))
        self._emit_inspector_changed()

    def set_beyond_count(self, counts, limit) -> None:
        """``(beyond, total)`` for the limit, or None where there is none."""
        if limit is None:
            self._beyond_label.setText(str(self.tr(
                'Set a limit to count the cells beyond it.')))
        elif counts is None:
            self._beyond_label.setText(str(self.tr(
                'Press Measure to count the cells beyond {0:g}.')).format(
                    limit))
        else:
            beyond, total = counts
            self._beyond_label.setText(str(self.tr(
                '{0:,} of {1:,} cells are beyond {2:g}.')).format(
                    beyond, total, limit))
        self._emit_inspector_changed()

    def beyond_text(self) -> str:
        return self._beyond_label.text()

    # -- Plan 44 MF23: the on-model inspector card ---------------------------- #

    def _emit_inspector_changed(self, *_args) -> None:
        self.inspectorChanged.emit()

    def attach_inspector_card(self, host, clear_rect_of=None):
        """Host the layout-C inspector card on *host* (the view).

        The registration hook for the viewport: *clear_rect_of()* returns the
        clear area as a QRect in *host* coordinates (None: all of *host*). The
        card is made once and re-placed on every resize of *host*. It shows
        only in layout C, with a mesh to inspect, while the model is coloured
        by quality (``set_inspector_on_model``)."""
        from foammesh.view.display_control.inspector_card import InspectorCard
        if self._card is not None:
            self._card.setParent(host)
        else:
            self._card = InspectorCard(self, host)
        self._card.set_clear_rect_source(clear_rect_of)
        self._card.follow(host)
        self._card.refresh()
        return self._card

    def inspector_card(self):
        """The on-model card, or None before a view hosts it."""
        return self._card

    def set_inspector_on_model(self, on: bool) -> None:
        """The model is (not) coloured by quality: the card follows."""
        self._on_model = bool(on)
        self._emit_inspector_changed()

    def inspector_state(self) -> dict:
        """Everything the card shows, read from this tab's own controls."""
        rows = self._elements.rowCount()
        selected = self._elements.selectedItems()
        row = selected[0].row() if selected else -1
        return {
            'metric': self._rank_metric.currentText(),
            'limit': self._limit_field.text(),
            'mesher_enabled': self._use_mesher.isEnabled(),
            'check_enabled': self._use_check.isEnabled(),
            'beyond': self._beyond_button.isChecked(),
            'beyond_text': self._beyond_label.text(),
            'listed': rows,
            'position': (str(self.tr('{0} of {1}')).format(row + 1, rows)
                         if rows and row >= 0
                         else (str(self.tr('{0} listed')).format(rows)
                               if rows else str(self.tr('none listed')))),
            'worst_text': str(self.tr('Worst {0}')).format(
                self._rank_count.value()),
            'picking': self._pick_element.isChecked(),
            'readout': self._element_readout.text(),
            'card_wanted': (self._mode == 'C' and self._on_model
                            and (self._inspector_available
                                 or bool(self._verdict))),
        }

    def apply_limit_text(self, text: str) -> None:
        """The limit typed on the card: the tab's own field takes it."""
        if self._limit_field.text() != text:
            self._limit_field.setText(text)
        self._on_limit_typed()

    def use_limit_source(self, source: str) -> None:
        """The card's Mesher / Check buttons (the tab's own two)."""
        button = self._use_mesher if source == 'mesher' else self._use_check
        if button.isEnabled():
            button.click()

    def toggle_beyond(self, on: bool) -> None:
        if self._beyond_button.isChecked() != bool(on):
            self._beyond_button.setChecked(bool(on))

    def request_worst(self) -> None:
        self._rank_button.click()

    def save_listed(self) -> None:
        self._selection_save.click()

    def step_listed(self, delta: int) -> None:
        """Select the previous / next listed cell (wrapping), which points
        the viewport at it exactly as a click on the row does."""
        rows = self._elements.rowCount()
        if not rows:
            return
        selected = self._elements.selectedItems()
        row = selected[0].row() if selected else (-1 if delta > 0 else 0)
        self._elements.selectRow((row + int(delta)) % rows)

    def set_show_beyond(self, on: bool) -> None:
        """Follow the picture: the toggle is lit while the colouring is."""
        self._beyond_button.blockSignals(True)
        self._beyond_button.setChecked(bool(on))
        self._beyond_button.blockSignals(False)
        self.beyondFollowed.emit(bool(on))
        self._emit_inspector_changed()

    def _on_metric(self, _index: int) -> None:
        self.metricChanged.emit(self.rank_metric())

    def _on_measure(self) -> None:
        self.measureRequested.emit(self.rank_metric())

    def _on_limit_typed(self) -> None:
        value = self.limit()
        self._distribution.set_limit(value)
        self.limitChanged.emit(value)

    def _on_limit_dragged(self, value: float) -> None:
        self._limit_field.blockSignals(True)
        self._limit_field.setText(f'{value:.6g}')
        self._limit_field.blockSignals(False)
        self.limitChanged.emit(float(value))

    def set_distribution(self, metric: str, histogram: dict, *,
                         limit: float | None = None,
                         beyond_is_above: bool = True) -> None:
        """Show one metric's distribution beside the numbers that summarise it."""
        self._distribution.set_distribution(
            metric, histogram, limit=limit, beyond_is_above=beyond_is_above)
        self._distribution_box.setVisible(bool(histogram))
        self._claim_body_space()

    # -- rendering --------------------------------------------------------- #

    def _populate_findings(self, blocking, advisory) -> None:
        """The findings checkMesh classified, worst first, as it worded them.

        A finding is a sentence the run recorded, not a number this panel may
        summarise: "Concave cells (using face planes) found, number of cells:
        1747" is the whole of what was measured about those cells. Blocking
        leads because it is what stops a solver; advisory follows because it
        is what a user still has to decide about.
        """
        lines = [str(self.tr('BLOCKING: %s')) % str(item) for item in blocking]
        lines += [str(self.tr('Advisory: %s')) % str(item)
                  for item in advisory]
        self._findings.setText('\n'.join(lines))
        self._findings.setProperty(
            'foammeshStatus', ('error' if blocking else 'warning')
            if lines else None)
        self._findings.setVisible(bool(lines))
        self._findings.setAccessibleDescription('\n'.join(lines))

    def _claim_body_space(self) -> None:
        """Either a table takes the body of the panel, or the filler does.

        DP-108. A hidden widget is skipped by the layout, so with every group
        below the headline hidden -- the state a case with no mesh is in --
        the headline itself would be stretched down the middle of an empty
        tab. `isHidden` rather than `isVisible` because this is asked while
        the tab is still off screen, where nothing is visible yet.
        """
        drawn = any(not box.isHidden() for box in (
            self._metrics_box, self._elements_box, self._distribution_box,
            self._rank_bar, self._statistics_box))
        self._filler.setVisible(not drawn)
        self._body_scroll.setVisible(drawn)
        self._sync_columns()

    def _populate_metrics(self, metrics) -> None:
        rows = list(metrics)
        self._populate_cards(rows)
        # DP-108. No rows, no table. The heading row of an empty grid states
        # nothing the sentence at the top of the panel has not already said.
        self._metrics_box.setVisible(bool(rows))
        self._metrics.setRowCount(len(rows))
        for row, metric in enumerate(rows):
            limit = _number(metric.get('requestedMinimum'))
            tier = tier_name(str(metric.get('tier') or ''))
            if tier:
                # Plan 40 UXD9 (N7). The value says which tier it is.
                limit = f'{limit} ({tier})'
            values = (
                str(metric.get('measure') or ''),
                limit,
                _number(metric.get('achievedMinimum')),
                _number(metric.get('achievedMean')),
                _count(metric.get('belowThreshold'), metric.get('total')),
                _allowance(metric.get('allowance')),
                str(metric.get('verdict') or ''),
            )
            for column, value in enumerate(values):
                self._metrics.setItem(row, column, QTableWidgetItem(value))
        # UXA6 (#22). A column nothing filled is not shown blank: it is
        # hidden and named, with who did not report it.
        empty = []
        for column, name in enumerate(_METRIC_COLUMNS):
            blank = bool(rows) and all(
                (self._metrics.item(r, column).text() in ('', '—'))
                for r in range(len(rows)))
            optional = name in ('Worst', 'Mean', 'Below', 'Allowance')
            self._metrics.setColumnHidden(column, blank and optional)
            if blank and optional:
                empty.append(name)
        self._columns_note.setVisible(bool(empty))
        self._columns_note.setText(str(self.tr(
            'Not reported by the {0}: {1}.')).format(
                verdict_source(self._verdict), ', '.join(
                    str(self.tr(name)).lower() for name in empty))
            if empty else '')

    def _populate_elements(self, offending) -> None:
        rows = list(offending)
        self._elements_box.setVisible(bool(rows))
        # The zone column is only worth its width where some cell has one.
        self._elements.setColumnHidden(
            3, not any(element.get('zone') for element in rows))
        self._elements.setRowCount(len(rows))
        for row, element in enumerate(rows):
            centroid = element.get('centroid') or (0.0, 0.0, 0.0)
            values = (
                str(element.get('tag') or ''),
                _number(element.get('value')),
                # DP-165. One point, so one precision: `0.1, 4, 0.006`
                # reads as three unrelated numbers.
                ', '.join(aligned(centroid)),
                str(element.get('zone') or ''),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(_ELEMENT_ROLE, dict(element))
                self._elements.setItem(row, column, item)

    def _update_accept(self) -> None:
        verdict = str(self._verdict.get('verdict') or '')
        source = verdict_source(self._verdict)
        if verdict in ('', 'pass'):
            # DP-761. A pass is only as wide as what was measured: a Gmsh
            # gate pass over checkMesh metrics that were never run said
            # "met its limits" about limits nothing had checked.
            metrics = [item for item in (self._verdict.get('metrics') or ())
                       if isinstance(item, dict)]
            unmeasured = sum(1 for item in metrics
                             if str(item.get('verdict') or '') == 'unrated')
            if unmeasured:
                self._set_accept(False, str(self.tr(
                    '{0} passed, but {1} of {2} metrics were not measured; '
                    'there is nothing to accept.')).format(
                        source, unmeasured, len(metrics)))
            else:
                self._set_accept(False, str(self.tr(
                    'This mesh met the {0} limits; there is nothing to '
                    'accept.')).format(source))
        elif verdict == 'unrated':
            self._set_accept(False, self.tr(
                'This mesh has not been measured, so there is no verdict to '
                'accept. Press Run mesh check first.'))
        elif not self._verdict.get('overridable') \
                and self._verdict.get('source'):
            # An opened mesh has no run behind it to re-publish, so there is
            # nothing a waiver could bind to. Saying that is honest; offering
            # the button and failing afterwards would not be.
            self._set_accept(False, str(self.tr(
                'This verdict comes from the {0} on an existing mesh. There '
                'is no run to re-publish, so it cannot be accepted here.'))
                .format(source if source != 'checkMesh' else 'checkMesh run'))
        elif self._verdict.get('overridable') and verdict in OVERRIDABLE_VERDICTS:
            self._set_accept(True, self.tr(
                'Publish this mesh and record the decision against it.'))
        else:
            self._set_accept(False, self.tr(
                'This mesh contains inverted or zero-volume cells. A solver '
                'cannot integrate over one, so it cannot be accepted — '
                're-mesh instead.'))

    def _show_accept(self, shown: bool) -> None:
        """UXA6 (#20). Without a verdict there is nothing to accept."""
        self._accept.setVisible(shown)
        if not shown:
            self._accept_reason.setVisible(False)

    def _set_accept(self, enabled: bool, reason: str) -> None:
        self._accept.setEnabled(enabled)
        self._accept.setToolTip(reason)
        self._accept.setAccessibleDescription(reason)
        # DP-109. On screen only while the button cannot be pressed: once it
        # can be, the button's own words are the whole of what there is to
        # say, and a sentence beside it would only compete with them.
        self._accept_reason.setText('' if enabled else str(reason))
        self._accept_reason.setVisible(not enabled and bool(reason))

    # -- interaction ------------------------------------------------------- #

    def _on_accept(self) -> None:
        if self._accept.isEnabled():
            self.acceptRequested.emit(dict(self._verdict))

    def _on_element_selected(self) -> None:
        items = self._elements.selectedItems()
        if not items:
            return
        record = self._elements.item(items[0].row(), 0)
        if record is not None:
            payload = record.data(_ELEMENT_ROLE)
            if payload:
                self.elementSelected.emit(dict(payload))


def _hold(cls, *args):
    """Build a container or a display-only widget. Not a control: it is made
    through this call so the facade inventory (which lists assigned controls)
    does not list the tab's plumbing."""
    return cls(*args)


def _table(columns, parent, accessible: str) -> QTableWidget:
    table = QTableWidget(0, len(columns), parent)
    table.setHorizontalHeaderLabels([str(name) for name in columns])
    table.setAccessibleName(accessible)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents)
    # R196. Header plus three rows, in this style's own metrics rather
    # than a pixel count that a different font would make wrong.
    rows = table.verticalHeader().defaultSectionSize()
    table.setMinimumHeight(
        table.horizontalHeader().sizeHint().height()
        + _VISIBLE_ROWS * rows
        + 2 * table.frameWidth())
    return table


def _parse_limit(text) -> float | None:
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def _number(value) -> str:
    try:
        return f'{float(value):.4g}'
    except (TypeError, ValueError):
        return str(value) if value not in (None, '') else '—'


def local_time_text(stamp, now: datetime | None = None) -> str:
    """``today 08:49``, ``yesterday 08:49`` or ``9 Oct 08:49``, in local time.

    UXA6 (#22). The report stores UTC ISO 8601; a reader wants the clock on
    their own wall. An unreadable stamp is returned as it came.
    """
    text = str(stamp or '').strip()
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone()
    today = (now or datetime.now().astimezone()).astimezone().date()
    days = (today - moment.date()).days
    clock = moment.strftime('%H:%M')
    if days == 0:
        return f'today {clock}'
    if days == 1:
        return f'yesterday {clock}'
    return f'{moment.day} {moment.strftime("%b")} {clock}'


def _count(below, total) -> str:
    """``3 of 141,486``, or a dash where nothing was counted.

    checkMesh reports extrema, not populations. Rendering an uncounted metric
    as "0 of 0" claims nothing failed, which is a different statement from
    "this was never counted" -- and the first one reads as a pass.
    """
    if below is None or total is None:
        return '—'
    return f'{int(below):,} of {int(total):,}'


def _allowance(value) -> str:
    return '—' if value is None else f'{int(value):,}'


class _NoteHeight(QObject):
    """Keeps a wrapped label's minimum height at what its text needs at the
    width it has, so a group holding it is never given less than that (the
    note under the Metrics rows was drawn over them when it wrapped,
    DP-1776). Owned by the label."""

    def __init__(self, label: QLabel) -> None:
        super().__init__(label)
        self._label = label
        label.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._label and event.type() == QEvent.Type.Resize:
            label = self._label
            needed = label.heightForWidth(label.width()) if label.wordWrap() else 0
            if needed != label.minimumHeight():
                label.setMinimumHeight(max(0, needed))
        return False


class _FitScrollArea(QScrollArea):
    """A scroll area whose content is as big as the viewport, and never
    smaller than the content's own *minimum* size hint.

    ``QScrollArea.setWidgetResizable`` sizes the content from its size hint
    (here the sum of what every column would like), which made the Mesh
    quality body scroll at every window size (DP-1776). The minimum is what
    the controls need; anything beyond it is shared out as usual.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._content = None
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def hold(self, content: QWidget) -> None:
        self._content = content
        self.setWidget(content)
        content.installEventFilter(self)
        self.fit()

    def fit(self) -> None:
        content = self._content
        if content is None:
            return
        wanted = content.minimumSizeHint().expandedTo(content.minimumSize())
        # The scroll bars take room from the viewport, so size against the
        # viewport with the bars the content would then need.
        full = self.maximumViewportSize()
        size = full.expandedTo(QSize(0, 0))
        needs_h = wanted.width() > size.width()
        needs_v = wanted.height() > size.height()
        extent = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        if needs_h and not needs_v and wanted.height() > size.height() - extent:
            needs_v = True
        if needs_v and not needs_h and wanted.width() > size.width() - extent:
            needs_h = True
        if needs_v:
            size.setWidth(size.width() - extent)
        if needs_h:
            size.setHeight(size.height() - extent)
        content.resize(size.expandedTo(wanted))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._content and event.type() == QEvent.Type.LayoutRequest:
            self.fit()
        return super().eventFilter(watched, event)
