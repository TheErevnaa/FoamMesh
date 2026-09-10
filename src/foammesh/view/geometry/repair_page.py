"""Facade-only Geometry Repair workflow page."""
from __future__ import annotations

import json
import qasync

from PySide6.QtCore import QSignalBlocker, Qt

from foammesh.core.geometry.diagnostics.repair import (
    REPAIR_BANDS, TESSELLATED_ACTIONS,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from widgets.fit_to_text import FlowLayout, fit_headers, fit_to_text

from foammesh.app import app
from foammesh.core.facade.errors import FacadeError
from foammesh.view.facade_client import FailedResult, query, submit
from foammesh.view.step_page import StepPage
from foammesh.view.widgets.justification_dialog import JustificationDialog


class GeometryRepairPage(StepPage):
    def __init__(self, ui):
        widget = QWidget(ui.content)
        widget.setObjectName('geometryRepairPage')
        ui.content.insertWidget(1, widget)
        ui.geometryRepairPage = widget
        super().__init__(ui, widget)
        self._report = None
        self._repairPlan = None

        layout = QVBoxLayout(widget)
        title = QLabel(self.tr('Geometry preparation and repair'), widget)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self._banner = QLabel(widget)
        self._banner.setObjectName('readinessBanner')
        self._banner.setWordWrap(True)
        layout.addWidget(self._banner)

        self._findings = QTableWidget(0, 5, widget)
        self._findings.setObjectName('readinessFindings')
        self._findings.setHorizontalHeaderLabels([
            self.tr('Finding'), self.tr('Count'), self.tr('Size'),
            self.tr('Severity'), self.tr('Repair actions')])
        self._findings.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._findings.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._findings.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._findings.itemSelectionChanged.connect(self._highlightFinding)
        # B11/C1. `Severity` was clipped to `Severit` while the table was
        # fixed at roughly two rows tall, so the horizontal scrollbar it needed
        # was drawn straight over the single finding -- the one line of text
        # the page exists to show. The table asks for enough height that the
        # scrollbar has somewhere of its own to sit.
        self._findings.verticalHeader().setVisible(False)
        self._fitFindingColumns()
        self._findings.setMinimumHeight(self._rowsHigh(self._findings, 5))
        layout.addWidget(self._findings, 1)
        # C4. An empty table is a header over a blank rectangle: it says
        # neither "nothing is wrong" nor "nothing has been checked yet".
        self._findingsEmpty = QLabel(self.tr(
            'No readiness findings. Nothing here needs repairing.'), widget)
        self._findingsEmpty.setObjectName('readinessFindingsEmpty')
        self._findingsEmpty.setWordWrap(True)
        self._findingsEmpty.setVisible(False)
        layout.addWidget(self._findingsEmpty)

        tabs = QTabWidget(widget)
        tabs.setObjectName('geometryPreparationActions')
        tabs.addTab(self._buildRepairTab(tabs), self.tr('Repair'))
        tabs.addTab(self._buildWrapTab(tabs), self.tr('Wrap'))
        # R178. The Boundaries tab that used to sit here is on 1. Geometry
        # now. Naming a boundary is not a repair: `geometry.patches.*` reads
        # the artifact store directly and needs no repair, no wrap and no
        # prepared revision, and the boundaries are already rows in the
        # geometry tree (R169). A user whose geometry was clean never opened
        # this page and so never found the controls that name the inlet.
        tabs.addTab(self._buildUseTab(tabs), self.tr('Use as-is'))
        layout.addWidget(tabs)

        revisions = QHBoxLayout()
        self._revisionStrip = QLabel(widget)
        self._revisionStrip.setObjectName('geometryRevisionStrip')
        self._revisionSelect = QComboBox(widget)
        self._revisionSelect.currentIndexChanged.connect(self._inspectRevision)
        self._revisionRestore = QPushButton(self.tr('Restore selected revision'), widget)
        self._revisionRestore.clicked.connect(self._restoreRevision)
        self._revisionSelect.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        revisions.addWidget(self._revisionStrip)
        revisions.addWidget(self._revisionSelect, 1)
        revisions.addWidget(self._revisionRestore)
        layout.addLayout(revisions)

    @staticmethod
    def _rowsHigh(view, rows: int) -> int:
        """The height a table needs for ``rows`` rows and its header."""
        line = view.fontMetrics().height() + 12
        header = view.horizontalHeader().sizeHint().height() or line
        return rows * line + header + 2 * view.frameWidth()

    #: Findings column that absorbs the slack. The repair-action ids it lists
    #: are repeated verbatim in the plan table below, so eliding them costs
    #: nothing; eliding the finding name costs the whole point of the row.
    _FINDINGS_SLACK_COLUMN = 4

    def _fitFindingColumns(self):
        """Give the finding name the width its own text needs (R6/R77).

        ``fit_headers`` hands *every* column the widest header's width as a
        floor, so `Count`, `Size` and `Severity` -- three nearly empty columns
        -- each reserved as much room as `Repair actions` and starved the
        column that carries the meaning: row one rendered as `Self ...` (which
        could be self-intersection or self-proximity) and the panel still
        needed a horizontal scrollbar. Sizing each column to its own contents
        instead makes the name whole and the scrollbar unnecessary.
        """
        header = self._findings.horizontalHeader()
        for column in range(self._findings.columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Stretch
                if column == self._FINDINGS_SLACK_COLUMN
                else QHeaderView.ResizeMode.ResizeToContents)
        # Only the slack column can be squeezed to this floor; the others are
        # sized to their own header and contents, so no title clips.
        header.setMinimumSectionSize(
            header.fontMetrics().horizontalAdvance(self.tr('Severity')) + 24)

    def _planEmptyText(self, *, suggested: bool) -> str:
        """What the empty plan area says, before and after a suggestion (R11).

        The standing note asked for the button that had just been pressed --
        `No repair plan yet. "Suggest plan" derives one from the findings
        above` -- and stayed there above the result line reading `0 action(s)
        suggested`. The two statements contradicted each other, and only one
        of them was still true.
        """
        if suggested:
            return self.tr(
                'No repair actions were suggested. Nothing in the findings '
                'above maps to a repair this geometry can run, so there is '
                'nothing to preview or apply.')
        return self.tr(
            'No repair plan yet. "Suggest plan" derives one from the findings '
            'above; the parameters stay editable before you preview it.')

    def _buildRepairTab(self, parent):
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)
        description = QLabel(self.tr(
            'Review the deterministic suggested plan, edit parameters, preview, then apply.'), tab)
        description.setWordWrap(True)
        layout.addWidget(description)
        self._bodyTree = QTreeWidget(tab)
        self._bodyTree.setObjectName('geometryRepairBodyTree')
        self._bodyTree.setHeaderLabels([self.tr('Assembly / body / patch'),
                                        self.tr('Stable identity')])
        self._bodyTree.setAccessibleName(self.tr('CAD assembly repair body tree'))
        # E8. The identity column held a full uuid and took the width to show
        # it, while the part names beside it -- the only column anyone reads --
        # were elided to `pip...`. The name gets the room now; the identity is
        # shortened in place and keeps the whole value in its tooltip, where it
        # can still be read and copied.
        self._bodyTree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._bodyTree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._bodyTree)
        # Plan 26 WP7.4 adds the risk band; WP7.2 makes the Action cell
        # read-only. The table never set NoEditTriggers, so a user could
        # overtype a suggested row's Action with any registered id and Preview
        # would run it -- undiscoverable, and it destroyed the suggested action
        # it replaced. Parameters stay editable: tuning them is the point.
        self._planTable = QTableWidget(0, 4, tab)
        self._planTable.setHorizontalHeaderLabels([
            self.tr('Use'), self.tr('Action'), self.tr('Parameters (JSON)'),
            self.tr('Risk')])
        # B5. `Parameters (JSON)` was drawn as `meters (JS`, which is not an
        # elide -- the beginning is gone too, so the word cannot be recovered
        # from what is left on screen.
        self._planTable.verticalHeader().setVisible(False)
        fit_headers(self._planTable, stretch_column=2)
        self._planTable.setMinimumHeight(self._rowsHigh(self._planTable, 4))
        layout.addWidget(self._planTable)
        self._planEmpty = QLabel(self._planEmptyText(suggested=False), tab)
        self._planTable.setVisible(False)
        self._planEmpty.setObjectName('geometryRepairPlanEmpty')
        self._planEmpty.setWordWrap(True)
        layout.addWidget(self._planEmpty)
        # B1/B2/B3. Four buttons sharing a narrow panel were each squeezed
        # below their own label: `uggest pla`, `pply as new revisio`,
        # `ancel running preparatio`. No button is drawn narrower than its text
        # now, and the row wraps to a second line rather than clipping.
        buttons = FlowLayout()
        for text, slot in ((self.tr('Suggest plan'), self._suggestRepair),
                           (self.tr('Preview'), self._previewRepair),
                           (self.tr('Apply as new revision...'), self._applyRepairPlan),
                           (self.tr('Cancel running preparation'), self._cancelPreparation)):
            button = QPushButton(text, tab)
            button.clicked.connect(slot)
            fit_to_text(button)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._repairReport = QTextEdit(tab)
        self._repairReport.setReadOnly(True)
        self._repairReport.setAccessibleName(self.tr('Repair preview report'))
        self._repairReport.setPlaceholderText(self.tr(
            'Preview and apply results are reported here.'))
        layout.addWidget(self._repairReport)
        return tab

    @qasync.asyncSlot()
    async def _cancelPreparation(self):
        result = (await app.facadeClient.run('geometry.prepare.cancel', {})).payload
        self._repairReport.append(self.tr('\nCancellation: {0}').format(
            result.get('state', 'idle')))

    def _buildWrapTab(self, parent):
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)
        limitation = QLabel(self.tr(
            'Uniform-grid wrap changes geometry. Memory grows cubically and sharp edges may round.'), tab)
        limitation.setWordWrap(True)
        layout.addWidget(limitation)
        controls = QFormLayout()
        self._wrapResolution = QSpinBox(tab)
        self._wrapResolution.setRange(16, 256)
        self._wrapResolution.setValue(64)
        self._wrapResolution.valueChanged.connect(self._updateWrapEstimate)
        self._wrapGap = self._optionalDistance(tab)
        self._wrapDeviation = self._optionalDistance(tab)
        self._wrapSmoothing = QDoubleSpinBox(tab)
        self._wrapSmoothing.setRange(0, 1)
        self._wrapSmoothing.setSingleStep(.1)
        self._wrapSmoothing.setValue(1.0)
        self._wrapMinComponent = QSpinBox(tab)
        self._wrapMinComponent.setRange(0, 10_000_000)
        self._wrapMinComponent.setValue(27)
        self._wrapMode = QComboBox(tab)
        self._wrapMode.addItem(self.tr('External flow'), 'external')
        self._wrapMode.addItem(self.tr('Internal flow'), 'internal')
        self._wrapSeeds = QLineEdit(tab)
        self._wrapSeeds.setPlaceholderText('x,y,z; x,y,z')
        for label, control in (
                (self.tr('Base resolution'), self._wrapResolution),
                (self.tr('Maximum gap'), self._wrapGap),
                (self.tr('Maximum deviation'), self._wrapDeviation),
                (self.tr('Smoothing strength'), self._wrapSmoothing),
                (self.tr('Minimum component cells'), self._wrapMinComponent),
                (self.tr('Flow mode'), self._wrapMode),
                (self.tr('Fluid seeds'), self._wrapSeeds)):
            controls.addRow(label, control)
        layout.addLayout(controls)

        self._wrapAdvancedToggle = QToolButton(tab)
        self._wrapAdvancedToggle.setObjectName('wrapAdvancedOptionsToggle')
        self._wrapAdvancedToggle.setText(self.tr('Advanced options'))
        self._wrapAdvancedToggle.setAccessibleName(
            self.tr('Show advanced wrap options'))
        self._wrapAdvancedToggle.setCheckable(True)
        self._wrapAdvancedToggle.setArrowType(Qt.ArrowType.RightArrow)
        self._wrapAdvancedToggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(self._wrapAdvancedToggle)

        self._wrapAdvanced = QWidget(tab)
        self._wrapAdvanced.setObjectName('wrapAdvancedOptions')
        advanced = QFormLayout(self._wrapAdvanced)
        self._wrapSmallestFeature = self._optionalDistance(self._wrapAdvanced)
        self._wrapSmallestFeature.setObjectName('wrapSmallestFeature')
        self._wrapSmallestFeature.setAccessibleName(
            self.tr('Smallest feature to preserve'))
        self._wrapSmallestFeature.setToolTip(self.tr(
            'When set, grid spacing is half this value and overrides Base resolution.'))
        self._wrapSmallestFeature.valueChanged.connect(self._updateWrapEstimate)
        advanced.addRow(self.tr('Smallest feature to preserve'),
                        self._wrapSmallestFeature)
        precedence = QLabel(self.tr(
            'Automatic keeps Base resolution active. A positive value derives '
            'the grid from the smallest feature and takes precedence.'),
            self._wrapAdvanced)
        precedence.setWordWrap(True)
        advanced.addRow('', precedence)
        self._wrapAdvanced.setVisible(False)
        self._wrapAdvancedToggle.toggled.connect(
            self._setWrapAdvancedVisible)
        layout.addWidget(self._wrapAdvanced)
        buttons = QHBoxLayout()
        preview = QPushButton(self.tr('Build coarse preview'), tab)
        preview.clicked.connect(self._estimateWrap)
        apply = QPushButton(self.tr('Apply wrap...'), tab)
        apply.clicked.connect(self._applyWrap)
        fit_to_text(preview)
        fit_to_text(apply)
        buttons.addWidget(preview)
        buttons.addWidget(apply)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._wrapPreview = QLabel(tab)
        self._wrapPreview.setWordWrap(True)
        layout.addWidget(self._wrapPreview)
        return tab

    def _setWrapAdvancedVisible(self, visible):
        self._wrapAdvanced.setVisible(bool(visible))
        self._wrapAdvancedToggle.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def _wrapSizingParameters(self):
        parameters = {'resolution': self._wrapResolution.value()}
        if self._wrapSmallestFeature.value() > 0:
            parameters['smallest_feature'] = self._wrapSmallestFeature.value()
        return parameters

    def _updateWrapEstimate(self, _value=None):
        geometry_id = self._activeGeometryId()
        if not geometry_id or app.facadeClient.session() is None:
            return
        try:
            estimate = query(app.facadeClient, 'geometry.wrap.estimate', {
                'geometry_id': geometry_id,
                **self._wrapSizingParameters()}).payload
        except (OSError, RuntimeError, ValueError):
            return
        self._wrapPreview.setText(self.tr(
            'Estimated grid {0} x {1} x {2}; memory {3:.1f} MiB; voxel {4:.6g}.').format(
                *estimate['dimensions'], estimate['memory_bytes'] / 1024 ** 2,
                estimate['voxel_size']))

    def _buildUseTab(self, parent):
        """Say what accepting the geometry means, and show that it happened.

        F9. This was a tall empty tab with one right-aligned button floating in
        the middle of it -- no heading, and no statement anywhere of what
        pressing it commits to. F2. Pressing it then looked like nothing had
        happened: the decision was recorded, the page refreshed, and the tab it
        was pressed on said exactly what it had said before.
        """
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)
        heading = QLabel(self.tr('Use the geometry without preparing it'), tab)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        note = QLabel(self.tr(
            'This records a decision and freezes the current revision as the '
            'geometry the meshers read. The findings above are not repaired '
            'and no new revision is written. Geometry the checks call blocked '
            'or wrap-recommended asks for a written reason first, which is '
            'kept with the decision. Repairing or wrapping later replaces '
            'it.'), tab)
        note.setWordWrap(True)
        layout.addWidget(note)
        self._asIsState = QLabel(tab)
        self._asIsState.setObjectName('geometryAsIsState')
        self._asIsState.setWordWrap(True)
        layout.addWidget(self._asIsState)
        buttons = QHBoxLayout()
        button = QPushButton(self.tr('Use geometry as-is'), tab)
        button.setAccessibleName(self.tr('Use geometry as-is'))
        button.clicked.connect(self._recordAsIs)
        fit_to_text(button)
        buttons.addStretch(1)
        buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return tab

    def _updateAsIsState(self):
        """Report the preparation decision on the tab that records it (F2)."""
        state = getattr(self, '_asIsState', None)
        if state is None:
            return
        try:
            status = query(app.facadeClient, 'workflow.status').payload
            preparation = status['geometry_preparation']
        except (FacadeError, KeyError, OSError, RuntimeError, ValueError):
            state.setText('')
            return
        decision = str(preparation.get('decision') or 'undecided')
        if decision == 'undecided':
            state.setText(self.tr('No preparation decision recorded yet.'))
        elif preparation.get('current'):
            state.setText(self.tr(
                'Recorded: {0}. The prepared geometry matches what is loaded, '
                'so meshing can proceed.').format(decision.replace('_', ' ')))
        else:
            state.setText(self.tr(
                'Recorded: {0}, but the geometry has changed since. Record a '
                'decision again before meshing.').format(
                    decision.replace('_', ' ')))

    def _optionalDistance(self, parent):
        control = QDoubleSpinBox(parent)
        control.setDecimals(8)
        control.setRange(0, 1e12)
        control.setSpecialValueText(self.tr('Automatic'))
        return control

    def load(self):
        self._loaded = True
        self.refresh()

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        self.refresh()
        await super().show(isWorkingStep, batchRunning)

    def isNextStepAvailable(self):
        if app.facadeClient is None or app.facadeClient.session() is None:
            return False
        status = query(app.facadeClient, 'workflow.status').payload
        return bool(status['geometry_preparation']['current'])

    async def runInBatchMode(self):
        return self.isNextStepAvailable()

    def clearResult(self):
        return

    def refresh(self):
        # C31-12. `geometry.readiness` is a READ that declares an artifact
        # (`readiness_report`), so the registry classes it as a mutation and
        # `query()` refuses it -- it has to stay on the write path. What it
        # does not have to do is hold the GUI thread while a CAD readiness
        # report is computed and written, which is the slowest thing this
        # page asks for. The report is fetched, and the redraw below happens
        # when it arrives, in the same order as before.
        def rendered(result):
            if isinstance(result, FailedResult):
                self._banner.setText(
                    self.tr('Readiness unavailable: {0}').format(result.message))
                self._findings.setRowCount(0)
                self.stepReset.emit()
                return
            self._renderReadiness(result.payload)

        submit(app.facadeClient, 'geometry.readiness', {}, then=rendered)

    def _renderReadiness(self, report):
        """Draw the readiness report `refresh()` asked for (C31-12)."""
        self._report = report
        geometries = self._report.get('geometries', [])
        self._bodyTree.clear()
        for geometry in geometries:
            root = self._identifiedItem(
                self._bodyTree, geometry.get('name', 'geometry'),
                geometry['geometry_id'])
            bodies = {}
            for patch in geometry.get('patches', ()):
                body_index = int(patch.get('source_ref', {}).get('body_index', 0))
                body = bodies.get(body_index)
                if body is None:
                    body = QTreeWidgetItem(root, [self.tr('Body {0}').format(body_index + 1), ''])
                    bodies[body_index] = body
                self._identifiedItem(
                    body, patch.get('name') or self.tr('Unnamed patch'),
                    patch.get('patch_uuid', ''))
            if not bodies:
                self._identifiedItem(root, self.tr('Tessellated surface'),
                                     geometry.get('patch_uuid', ''))
        self._bodyTree.expandToDepth(1)
        state = self._worstState([
            item['diagnostics']['readiness']['state'] for item in geometries])
        self._banner.setText(self.tr('Readiness: {0} - {1} geometry item(s)').format(
            state.replace('_', ' ').title(), len(geometries)))
        findings = [finding for item in geometries
                    for finding in item['diagnostics']['findings']
                    if finding['count'] or not finding.get('evaluated', True)]
        self._findings.setRowCount(len(findings))
        self._findings.setVisible(bool(findings))
        self._findingsEmpty.setVisible(not findings)
        for row, finding in enumerate(findings):
            evaluated = bool(finding.get('evaluated', True))
            size = finding['characteristic_size']
            # R7. A row that says `Not evaluated` also printed `0` in Count,
            # so it asserted a measurement and denied taking one in the same
            # line. A check that did not run has no count and no size; the
            # dash says that, where `0` claimed a clean result.
            values = (finding['kind'].replace('_', ' ').title(),
                      str(finding['count']) if evaluated else '—',
                      f'{size:.6g}' if evaluated and size is not None
                      else '—',
                      finding['severity'].title() if evaluated
                      else self.tr('Not evaluated'),
                      ', '.join(finding.get('repairable_by', ())))
            # R77. `1728` small features on a 576-triangle surface is three
            # per triangle, and reads as nonsense until you know the check
            # counts *edges*. The check has always said so in its message --
            # "1728 surface edge(s) are smaller than twice target cell size"
            # -- and the table simply never showed it.
            explanation = str(finding.get('message') or '')
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, finding.get('locations', ()))
                if explanation:
                    item.setToolTip(explanation)
                self._findings.setItem(row, column, item)
        with QSignalBlocker(self._revisionSelect):
            self._revisionSelect.clear()
            labels = []
            for geometry in geometries:
                labels.append(
                    f"{geometry.get('name', 'geometry')} r{geometry.get('revision', 1)}")
                for revision in geometry.get('revisions', ()):
                    self._revisionSelect.addItem(
                        f"{geometry.get('name', 'geometry')} r{revision['revision']} "
                        f"({revision.get('kind', 'imported')})",
                        (geometry['geometry_id'], int(revision['revision'])))
        # B6. The strip was squeezed to `Current: pi` by a combo box that
        # had taken the whole row for itself.
        self._revisionStrip.setText(self.tr('Current: {0}').format(' - '.join(labels) or 'none'))
        fit_to_text(self._revisionStrip, padding=8)
        self._updateAsIsState()
        self._updateWrapEstimate()
        self._updateNextStepAvailable()

    def _identifiedItem(self, parent, name: str, identity: str):
        """A tree row whose identity is shown short and read in full (E8)."""
        identity = str(identity or '')
        shown = identity if len(identity) <= 12 else identity[:8] + '…'
        item = QTreeWidgetItem(parent, [name, shown])
        item.setToolTip(0, name)
        if identity:
            item.setToolTip(1, identity)
            item.setData(1, Qt.ItemDataRole.UserRole, identity)
        return item

    def _highlightFinding(self):
        manager = getattr(app.window, 'geometryManager', None) if app.window else None
        if manager is None:
            return
        row = self._findings.currentRow()
        item = self._findings.item(row, 0) if row >= 0 else None
        locations = item.data(Qt.ItemDataRole.UserRole) if item is not None else ()
        if locations:
            manager.highlightLocations(locations)
        else:
            manager.clearFindingHighlight()

    def _inspectRevision(self, _index=None):
        selection = self._revisionSelect.currentData()
        if not selection:
            return
        geometry_id, revision_number = selection
        for geometry in (self._report or {}).get('geometries', ()):
            if geometry.get('geometry_id') != geometry_id:
                continue
            revision = next((item for item in geometry.get('revisions', ())
                             if int(item['revision']) == int(revision_number)), None)
            if revision is None:
                return
            report = revision.get('report')
            if report is not None:
                self._repairReport.setPlainText(json.dumps(
                    report, indent=2, sort_keys=True))
            else:
                self._repairReport.setPlainText(json.dumps({
                    'revision': revision_number,
                    'kind': revision.get('kind', 'imported'),
                    'provenance': revision.get('provenance', {}),
                    'report': None,
                }, indent=2, sort_keys=True))
            return

    def _activeGeometryId(self):
        geometries = (self._report or {}).get('geometries', [])
        return geometries[0]['geometry_id'] if geometries else None

    def _suggestRepair(self):
        geometry_id = self._activeGeometryId()
        if not geometry_id:
            return
        self._repairPlan = query(
            app.facadeClient, 'geometry.repair.suggest',
            {'geometry_id': geometry_id}).payload
        actions = self._repairPlan.get('actions', ())
        self._planTable.setRowCount(len(actions))
        self._planEmpty.setVisible(not actions)
        # R11. The note is about what the page is waiting for, so it has to
        # change once the wait is over -- otherwise it still asks for
        # "Suggest plan" directly above that button's own answer.
        self._planEmpty.setText(self._planEmptyText(suggested=True))
        self._planTable.setVisible(bool(actions))
        for row, action in enumerate(actions):
            enabled = QCheckBox(self._planTable)
            enabled.setChecked(action.get('enabled', True))
            self._planTable.setCellWidget(row, 0, enabled)
            name = QTableWidgetItem(action['action'])
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._planTable.setItem(row, 1, name)
            self._planTable.setItem(row, 2, QTableWidgetItem(
                json.dumps(action.get('params', {}), sort_keys=True)))
            band = QTableWidgetItem(_riskLabel(action['action']))
            band.setFlags(band.flags() & ~Qt.ItemFlag.ItemIsEditable)
            band.setToolTip(_riskDetail(action['action']))
            self._planTable.setItem(row, 3, band)
        self._repairReport.setPlainText(self.tr('{0} action(s) suggested.').format(len(actions)))

    def _editedPlan(self):
        if not self._repairPlan:
            self._suggestRepair()
        plan = dict(self._repairPlan or {})
        actions = []
        for row in range(self._planTable.rowCount()):
            params = json.loads(self._planTable.item(row, 2).text() or '{}')
            actions.append({'action': self._planTable.item(row, 1).text(),
                            'params': params,
                            'enabled': self._planTable.cellWidget(row, 0).isChecked()})
        plan['actions'] = actions
        return plan

    @qasync.asyncSlot()
    async def _previewRepair(self):
        return await self._previewRepairCore()

    async def _previewRepairCore(self):
        try:
            plan = self._editedPlan()
            preview = (await app.facadeClient.run(
                'geometry.repair.preview', {'plan': plan})).payload
        except (TypeError, ValueError) as error:
            QMessageBox.warning(self._widget, self.tr('Repair plan'), str(error))
            return None
        self._repairPlan = plan
        self._repairPlan['expected_preview_digest'] = preview.get('preview_digest')
        self._repairReport.setPlainText(json.dumps(preview, indent=2, sort_keys=True))
        return preview

    @qasync.asyncSlot()
    async def _applyRepairPlan(self):
        preview = await self._previewRepairCore()
        if not preview:
            return
        if not preview.get('entries'):
            QMessageBox.information(self._widget, self.tr('Repair'),
                                    self.tr('No executable repair was suggested.'))
            return
        answer = QMessageBox.question(
            self._widget, self.tr('Apply repair preview'),
            self.tr('{0} action(s) previewed. Create a new geometry revision?').format(
                len(preview['entries'])),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            await app.facadeClient.run('geometry.repair.apply', {'plan': self._repairPlan})
            self.refresh()

    def _wrapParameters(self):
        parameters = {**self._wrapSizingParameters(),
                      'smoothing_strength': self._wrapSmoothing.value(),
                      'minimum_component_size': self._wrapMinComponent.value(),
                      'mode': self._wrapMode.currentData()}
        if self._wrapGap.value() > 0:
            parameters['max_gap'] = self._wrapGap.value()
        if self._wrapDeviation.value() > 0:
            parameters['max_deviation'] = self._wrapDeviation.value()
        seeds = []
        for value in self._wrapSeeds.text().split(';'):
            if not value.strip():
                continue
            coordinates = [float(item.strip()) for item in value.split(',')]
            if len(coordinates) != 3:
                raise ValueError(self.tr('Each fluid seed must contain x, y and z.'))
            seeds.append(coordinates)
        if seeds:
            parameters['fluid_seeds'] = seeds
        return parameters

    @qasync.asyncSlot()
    async def _estimateWrap(self):
        return await self._estimateWrapCore()

    async def _estimateWrapCore(self):
        geometry_id = self._activeGeometryId()
        if not geometry_id:
            return None
        try:
            parameters = self._wrapParameters()
        except ValueError as error:
            QMessageBox.warning(self._widget, self.tr('Wrap controls'), str(error))
            return None
        preview = (await app.facadeClient.run(
            'geometry.wrap.preview', {'geometry_id': geometry_id, **parameters})).payload
        self._wrapPreview.setText(self.tr(
            'Grid {0} x {1} x {2}; memory {3:.1f} MiB; coarse preview {4} cells').format(
                *preview['dimensions'], preview['memory_bytes'] / 1024 ** 2,
                preview.get('coarse_preview', {}).get('cells', 0)))
        return preview

    @qasync.asyncSlot()
    async def _applyWrap(self):
        geometry_id = self._activeGeometryId()
        preview = await self._estimateWrapCore()
        if not geometry_id or preview is None:
            return
        answer = QMessageBox.warning(
            self._widget, self.tr('Apply wrap'),
            self._wrapAcceptanceSummary(preview),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            await app.facadeClient.run(
                'geometry.wrap.apply', {'geometry_id': geometry_id,
                                        **self._wrapParameters()})
            self.refresh()

    def _wrapAcceptanceSummary(self, preview):
        coarse = preview['coarse_preview']
        transfer = coarse['patch_transfer']
        recovery = transfer.get('per_patch_recovery_fraction', {})
        patch_rows = '\n'.join(
            self.tr('  Patch {0}: {1:.1%} area recovered').format(patch, fraction)
            for patch, fraction in sorted(recovery.items()))
        if not patch_rows:
            patch_rows = self.tr('  No named source patches were available.')
        return self.tr(
            'Wrapping changes geometry.\n\n'
            'Coarse acceptance preview:\n'
            '  Watertight: {0}\n'
            '  Triangles: {1}\n'
            '  Maximum deviation: {2:.6g} (budget {3:.6g})\n'
            '  Unassigned patch area: {4:.1%}\n'
            '  New-skin fraction: {5:.1%}\n'
            'Patch transfer:\n{6}\n\n'
            'The imported revision remains immutable. Continue?').format(
                coarse['watertight'],
                coarse.get('triangle_count', coarse.get('cells', 0)),
                coarse['deviation']['max'],
                coarse.get('deviation_budget', 0.0),
                transfer.get('unassigned_area_fraction',
                             transfer.get('unassigned_fraction', 1.0)),
                coarse['new_skin_fraction'],
                patch_rows)

    def _restoreRevision(self):
        selection = self._revisionSelect.currentData()
        if not selection:
            return
        geometry_id, revision = selection
        answer = QMessageBox.question(
            self._widget, self.tr('Restore geometry revision'),
            self.tr('Make revision {0} current? Existing revisions are retained.').format(revision),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            # C31-12. The rollback is scheduled and the redraw is its
            # continuation, so the page is redrawn after the rollback, as
            # before -- not on the GUI thread while it runs.
            submit(app.facadeClient, 'geometry.repair.rollback', {
                'geometry_id': geometry_id, 'target_revision': revision},
                then=lambda _result: self.refresh())

    def _recordAsIs(self):
        state = self._worstState([
            item['diagnostics']['readiness']['state']
            for item in (self._report or {}).get('geometries', [])])
        parameters = {'decision': 'as_is'}
        if state in {'blocked', 'wrap_recommended'}:
            # R197. This asks for a justification a reviewer reads later
            # and offered a single-line box to write it in. Same prompt,
            # same dialog as the quality gate's `Accept anyway`.
            reason = JustificationDialog.ask(
                self._widget, self.tr('Acknowledge geometry risk'),
                self.tr('Explain why this geometry should be used without '
                        'preparation. This is recorded against the case.'))
            if not reason:
                return
            parameters['ack_reason'] = reason

        # C31-12. Decide, then freeze, then redraw -- the same three steps in
        # the same order, each one starting when the one before it answered.
        # A refusal used to leave the exception to whatever called the slot,
        # which showed the user nothing; it is now named where it happened.
        def decided(result):
            if isinstance(result, FailedResult):
                QMessageBox.warning(
                    self._widget, self.tr('Geometry preparation'),
                    self.tr('The decision was not recorded: {0}').format(
                        result.message))
                return
            self._freezePreparedGeometry({'decision': parameters['decision']},
                                         then=self.refresh)

        submit(app.facadeClient, 'geometry.preparation.decide', parameters,
               then=decided)

    def _freezePreparedGeometry(self, preparation, then=None):
        """Materialize the prepared geometry the meshers actually consume.

        Recording the decision only writes it into the configuration. The
        engines read ``PreparedGeometryStore.current()``, so without this the
        decision was accepted and the Gmsh run then refused with "prepare the
        geometry before running Gmsh" -- a step the GUI offered no way to take.

        C31-12: scheduled, not blocking. ``then`` runs afterwards either way,
        because the decision itself stands whether or not the freeze did.
        """
        def frozen(result):
            if isinstance(result, FailedResult):
                error = result.error
                detail = (getattr(error, 'details', None) or {}).get(
                    'error', str(error))
                QMessageBox.warning(
                    self._widget, self.tr('Geometry preparation'),
                    self.tr('The decision was recorded, but the prepared geometry '
                            'could not be frozen: {0}').format(detail))
            if then is not None:
                then()

        submit(app.facadeClient, 'geometry.prepared.create',
               {'preparation': preparation}, then=frozen)

    @staticmethod
    def _worstState(states):
        order = ('ready', 'repairable', 'wrap_recommended', 'blocked')
        return max(states, key=order.index) if states else 'blocked'


def _riskLabel(action: str) -> str:
    """Which risk band an action belongs to, in the words the page shows.

    Plan 26 WP7.4. Grouping repair actions by risk lets a user tell what is
    safe to apply blindly from what moves geometry. CAD healing ids are not in
    the tessellated catalogue at all -- they apply before tessellation, which
    is band 4 -- so they are named here rather than looked up.
    """
    band = _riskBand(action)
    return f'{band} - {REPAIR_BANDS[band][0]}' if band in REPAIR_BANDS else '-'


def _riskDetail(action: str) -> str:
    band = _riskBand(action)
    return REPAIR_BANDS[band][1] if band in REPAIR_BANDS else ''


def _riskBand(action: str) -> int:
    if str(action).startswith('cad.'):
        return 4
    spec = TESSELLATED_ACTIONS.get(action)
    return spec.band if spec is not None else -1
