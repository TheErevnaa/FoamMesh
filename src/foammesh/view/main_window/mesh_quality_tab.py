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

from foammesh.core.quantities import aligned
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.quality.phrasing import NO_MESH_YET
from foammesh.core.quality.verdict import verdict_source

from .quality_histogram import QualityHistogram

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
_ELEMENT_COLUMNS = ('Element', 'Value', 'Position')


class MeshQualityTab(QWidget):
    """Region C's quality surface. Read-only apart from Accept anyway."""

    #: Emitted with the accepted verdict when the user overrides the gate.
    acceptRequested = Signal(dict)
    #: Emitted with an offending element record when the user selects one, so
    #: the viewport can put a camera on it.
    elementSelected = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('meshQualityTab')
        self.setAccessibleName('Mesh quality report')
        self._verdict: dict = {}

        layout = QVBoxLayout(self)
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

        self._metrics = _table(_METRIC_COLUMNS, self,
                               'Quality metrics and their limits')
        # DP-108. Held on the instance because it has to be hideable. On a
        # case with no mesh this group drew its heading row and then a third
        # of the window of empty grid -- R187 again, the fault the task pages
        # closed with `claimBodyStretch`, on a panel that does not go through
        # that machinery and so never got the fix.
        self._metrics_box = QGroupBox(self.tr('Metrics'), self)
        QVBoxLayout(self._metrics_box).addWidget(self._metrics)
        layout.addWidget(self._metrics_box, 1)

        self._elements = _table(_ELEMENT_COLUMNS, self,
                                'Elements below the requested limit')
        self._elements.itemSelectionChanged.connect(self._on_element_selected)
        self._elements_box = QGroupBox(self.tr('Offending elements'), self)
        QVBoxLayout(self._elements_box).addWidget(self._elements)
        layout.addWidget(self._elements_box, 1)

        #: WP6.4. checkMesh reports a maximum, and a maximum of 66 degrees does
        #: not distinguish one bad cell from eight thousand -- the difference
        #: between "ignore" and "remesh".
        self._distribution = QualityHistogram(self)
        self._distribution_box = QGroupBox(self.tr('Distribution'), self)
        QVBoxLayout(self._distribution_box).addWidget(self._distribution)
        self._distribution_box.setVisible(False)
        layout.addWidget(self._distribution_box)

        # DP-108. Takes the body space when no table is there to take it, so
        # the sentence at the top of the panel stays at the top instead of
        # being stretched down the middle of an empty tab. Exactly one of
        # this and the groups above is ever visible; see `_claim_body_space`.
        self._filler = QWidget(self)
        self._filler.setObjectName('meshQualityFiller')
        layout.addWidget(self._filler, 1)

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
        buttons.addWidget(self._accept_reason, 1)
        buttons.addWidget(self._accept)
        layout.addLayout(buttons)
        self.clear()

    # -- data -------------------------------------------------------------- #

    def clear(self) -> None:
        self._verdict = {}
        self._headline.setText(self.tr(NO_MESH_YET))
        self._metrics.setRowCount(0)
        self._elements.setRowCount(0)
        self._populate_findings((), ())
        self._metrics_box.setVisible(False)
        self._elements_box.setVisible(False)
        self._distribution.clear()
        self._distribution_box.setVisible(False)
        self._claim_body_space()
        self._set_accept(False, self.tr('There is no verdict to accept.'))

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
            headline += ' ({0}, {1})'.format(verdict_source(self._verdict),
                                             self._verdict['checkedAt'])
        self._headline.setText(headline)
        self._populate_findings(self._verdict.get('blocking') or (),
                                self._verdict.get('advisory') or ())
        self._populate_metrics(self._verdict.get('metrics') or ())
        self._populate_elements(self._verdict.get('offending') or ())
        self._update_accept()
        self._claim_body_space()

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
            self._metrics_box, self._elements_box, self._distribution_box))
        self._filler.setVisible(not drawn)

    def _populate_metrics(self, metrics) -> None:
        rows = list(metrics)
        # DP-108. No rows, no table. The heading row of an empty grid states
        # nothing the sentence at the top of the panel has not already said.
        self._metrics_box.setVisible(bool(rows))
        self._metrics.setRowCount(len(rows))
        for row, metric in enumerate(rows):
            values = (
                str(metric.get('measure') or ''),
                _number(metric.get('requestedMinimum')),
                _number(metric.get('achievedMinimum')),
                _number(metric.get('achievedMean')),
                _count(metric.get('belowThreshold'), metric.get('total')),
                _allowance(metric.get('allowance')),
                str(metric.get('verdict') or ''),
            )
            for column, value in enumerate(values):
                self._metrics.setItem(row, column, QTableWidgetItem(value))

    def _populate_elements(self, offending) -> None:
        rows = list(offending)
        self._elements_box.setVisible(bool(rows))
        self._elements.setRowCount(len(rows))
        for row, element in enumerate(rows):
            centroid = element.get('centroid') or (0.0, 0.0, 0.0)
            values = (
                str(element.get('tag') or ''),
                _number(element.get('value')),
                # DP-165. One point, so one precision: `0.1, 4, 0.006`
                # reads as three unrelated numbers.
                ', '.join(aligned(centroid)),
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
                'accept. Run Mesh → Mesh check first.'))
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

    def _set_accept(self, enabled: bool, reason: str) -> None:
        self._accept.setEnabled(enabled)
        self._accept.setToolTip(reason)
        self._accept.setAccessibleDescription(reason)
        # DP-109. On screen only while the button cannot be pressed: once it
        # can be, the button's own words are the whole of what there is to
        # say, and a sentence beside it would only compete with them.
        self._accept_reason.setText('' if enabled else str(reason))
        self._accept_reason.setVisible(not enabled)

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


def _number(value) -> str:
    try:
        return f'{float(value):.4g}'
    except (TypeError, ValueError):
        return str(value if value is not None else '')


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
