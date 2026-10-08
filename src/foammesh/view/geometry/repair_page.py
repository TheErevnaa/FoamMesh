"""Facade-only Geometry Repair workflow page."""
from __future__ import annotations

import json
from typing import NamedTuple

import qasync

from PySide6.QtCore import QCoreApplication, QSignalBlocker, Qt, Signal

from foammesh.core.geometry.diagnostics.repair import (
    REPAIR_BANDS, TESSELLATED_ACTIONS,
)
from foammesh.core.mesh.presentation import count_text
from foammesh.core.naming import humanise_option
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSizePolicy, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from widgets.fit_to_text import FlowLayout, fit_to_text

from foammesh.app import app
from foammesh.view.outside_task import modal
from foammesh.core.facade.errors import FacadeError
from foammesh.view.facade_client import FailedResult, query, submit
from foammesh.view.step_page import StepPage
from foammesh.view.theming.metrics import (
    FORM_MARGIN, MARGIN_NONE, CompactDoubleSpinBox, CompactSpinBox,
    align_unit_column, unit_cell)
from foammesh.view.widgets.folder_header import FolderHeader
from foammesh.view.widgets.justification_dialog import JustificationDialog
from foammesh.view.workflow_controls.field_group_page import GmshHealingPanel

#: Findings columns holding a number rather than a name (Count, Size).
_NUMERIC_FINDING_COLUMNS = (2, 3)

#: DP-515. Repair parameters that tune how a solid is re-triangulated rather
#: than what is repaired. They are kept, editable and sent, but under the
#: plan's `Advanced` fold instead of inside the card of the action that
#: carries them (MA-08).
_SPECIALIST_PARAMETERS = frozenset({'linear_deflection',
                                    'angular_deflection_deg'})

#: Readiness states the product only accepts against a written reason.
_JUSTIFIED_STATES = frozenset({'blocked', 'wrap_recommended'})

#: What each repair action does, said in words a reader who has never read
#: the repair catalogue can act on. DP-207: sentence case, not title case.
#: `humanise_option` cannot do this -- it spells `tess.fill_holes` as
#: `Tess.fill holes`, which names the module rather than the work.
_ACTION_NAMES = {
    'tess.weld': 'Weld coincident points',
    'tess.dedupe': 'Remove duplicate triangles',
    'tess.orient': 'Point every triangle the same way',
    'tess.drop_fragments': 'Drop stray shell fragments',
    'tess.fill_holes': 'Fill holes in the surface',
    'tess.fix_nonmanifold': 'Separate edges shared by too many triangles',
    'tess.collapse_slivers': 'Collapse needle-thin triangles',
    'tess.detect_intersections': 'Find triangles that pass through each other',
    'cad.analyze': 'Check the solid for defects',
    'cad.fix_shape': 'Repair the solid',
    'cad.sew': 'Sew faces into a closed shell',
    'cad.fix_wireframe': 'Repair edges and vertices',
    'cad.remove_small_faces': 'Remove faces too small to mesh',
    'cad.unify_same_domain': 'Merge faces that lie in one surface',
    'cad.orient_and_solidify': 'Close the shell into a solid',
    'cad.retessellate': 'Rebuild the triangles from the solid',
    'cad.rediagnose': 'Check the solid again',
}


def _actionName(action: str) -> str:
    """The words a repair action is offered under (DP-218)."""
    named = _ACTION_NAMES.get(str(action))
    if named:
        return named
    return humanise_option(str(action).split('.')[-1])


def repair_effect_text(preview: dict) -> tuple[bool, str]:
    """Whether a previewed repair would write anything, and what it did.

    DP-487. Audit 0923 MA-03, case S1: **Weld coincident points** merged 0
    points and touched nothing, the preview said only `1 repair action
    previewed`, and the confirm asked to create a new revision -- which was
    then presented as a repair. The preview has always recorded each action's
    ``status``; this says it, and keeps apart the one thing a revision can do
    when every action did nothing: re-write the file from the surface as it
    was read, which is not the action's work and is named separately.

    Returns ``(writes, text)``. ``writes`` is False when nothing would change,
    in which case the store refuses to write a revision at all.
    """
    effect = preview.get('effect') or {}
    applied = [_actionName(item) for item in effect.get('applied') or ()]
    idle = [_actionName(item) for item in effect.get('no_effect') or ()]
    normalization = effect.get('normalization') or {}
    sentences = []
    if applied:
        sentences.append('Changes the surface: {0}.'.format(', '.join(applied)))
    if idle:
        sentences.append('Had no effect: {0}.'.format(', '.join(idle)))
    dropped = int(normalization.get('dropped_facets') or 0)
    if not applied and dropped:
        sentences.append(
            'No selected repair changed the surface. The new revision would '
            'only be a re-written copy of the surface as it was read, without '
            'the {0} the reader already discarded.'.format(
                count_text(dropped, 'zero-area facet')))
    elif dropped:
        sentences.append(
            'The new revision also leaves out the {0} the reader already '
            'discarded.'.format(count_text(dropped, 'zero-area facet')))
    outcome = effect.get('outcome')
    if outcome == 'none':
        sentences.append('The surface is unchanged, so no new revision is '
                         'written.')
    return outcome != 'none', ' '.join(sentences)


def _errorDetail(error) -> str:
    """The sentence a refused facade call carries, or the exception itself."""
    details = getattr(error, 'details', None) or {}
    return str(details.get('error') or error)


class PreparationVerdict(NamedTuple):
    """What Proceed on Preparation may do, and what to say about it.

    Plan 32 section 2: repair is optional. Three answers, and the page is
    the only thing that can give them, because it is the thing holding the
    readiness report and the recorded decision.

    ``can_proceed``   a prepared revision is already current; go.
    ``accept_as_is``  nothing here needs a written reason, so the press the
                      user already made is enough to record and freeze one.
    neither           refuse, and ``message`` says what must be fixed.

    ``stale`` is a decision recorded against a geometry that has changed
    since. It is not a decision about this geometry, so it is treated as
    none at all -- and said out loud, because the reader can see that
    something was recorded.
    """

    can_proceed: bool
    accept_as_is: bool
    stale: bool
    state: str
    message: str


class GeometryRepairPage(StepPage):
    #: DP-301. Emitted by the control on a choice that finishes preparation.
    #: The shell owns the route out of a step -- settling the task this page
    #: hosts, and opening whichever row the engine says comes next -- so the
    #: page asks for that route rather than growing a second copy of it.
    proceedRequested = Signal()

    def __init__(self, ui):
        widget = QWidget(ui.content)
        widget.setObjectName('geometryRepairPage')
        ui.content.insertWidget(1, widget)
        ui.geometryRepairPage = widget
        super().__init__(ui, widget)
        self._report = None
        self._repairPlan = None
        #: DP-300. What the page used to print into the column as indented
        #: JSON -- the readiness report, the plan the mesher receives, the
        #: last preview. Kept, and shown when the reader asks for it.
        self._details = {}

        layout = QVBoxLayout(widget)
        # Plan 32 section 4.1. The outline row is `3. Preparation` and the
        # page says the same word at its head. It is everything that has to
        # be true of the geometry before the chosen method can mesh it --
        # repair, wrap, a recorded decision to use it as it stands, and for
        # Gmsh the import and healing settings that used to be a task row of
        # their own.
        title = QLabel(self.tr('Preparation'), widget)
        title.setObjectName('preparationTitle')
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        # DP-78. Every button on this page used to act on `geometries[0]`,
        # while the banner above aggregated the readiness of all of them. On
        # a part-plus-farfield import that is how the page came to advertise
        # `Repairable` and then suggest nothing: the readiness belonged to the
        # part and the question was put to the farfield. The buttons now act
        # on whichever geometry this names, and it opens on the one in the
        # worst state, because that is the one the banner is talking about.
        # DP-80. True only once the user has picked from the list themselves.
        self._geometryPinned = False
        # DP-300. The row is a control, not a report, so it may stand above
        # the choices -- but only when there is a choice to make. On the one
        # geometry a case usually holds it would be a line that never
        # changes, so the whole row goes, label and all, rather than the
        # label staying behind to head an empty space.
        self._chooserRow = QWidget(widget)
        self._chooserRow.setObjectName('repairGeometryChooser')
        chooser = QHBoxLayout(self._chooserRow)
        chooser.setContentsMargins(0, 0, 0, 0)
        self._geometryLabel = QLabel(self.tr('Prepare geometry:'),
                                     self._chooserRow)
        self._geometrySelect = QComboBox(self._chooserRow)
        self._geometrySelect.setObjectName('repairGeometrySelect')
        # DP-534. Sized to its longest geometry name, this combo held the
        # page wider than the settings column on a long-named case. It
        # now asks for twelve characters and elides past them.
        self._geometrySelect.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._geometrySelect.setMinimumContentsLength(12)
        self._geometrySelect.currentIndexChanged.connect(self._geometryChosen)
        chooser.addWidget(self._geometryLabel)
        chooser.addWidget(self._geometrySelect, 1)
        self._chooserRow.setVisible(False)
        layout.addWidget(self._chooserRow)

        # DP-300. The three choices are what this step is for, and they open
        # it. What used to stand above them -- a sentence restating the
        # readiness verdict and a six-column table holding a five-row floor
        # whether or not it had five rows -- pushed them 189 px down a column
        # the shell had already had to make scroll. The verdict and the
        # findings are what the Repair choice is decided from, so they are
        # inside it.
        tabs = QTabWidget(widget)
        tabs.setObjectName('geometryPreparationActions')
        self._choices = tabs
        tabs.addTab(self._buildRepairTab(tabs), self.tr('Repair'))
        tabs.addTab(self._buildWrapTab(tabs), self.tr('Wrap'))
        # R178. The Boundaries tab that used to sit here is on 1. Geometry
        # now. Naming a boundary is not a repair: `geometry.patches.*` reads
        # the artifact store directly and needs no repair, no wrap and no
        # prepared revision, and the boundaries are already rows in the
        # geometry tree (R169). A user whose geometry was clean never opened
        # this page and so never found the controls that name the inlet.
        tabs.addTab(self._buildUseTab(tabs), self.tr('Use as-is'))
        layout.addWidget(tabs, 1)
        # DP-514. `Cancel running preparation` stood in the Repair tab's
        # button row at all times, beside the three presses that start
        # something, and did nothing unless one of them was still running
        # -- the facade answers `idle` when there is no job to stop. It is
        # the page's now, because a wrap is as cancellable as a repair, and
        # it is shown only while this page is waiting on a preparation job.
        self._preparationJobs = 0
        self._cancelButton = QPushButton(
            self.tr('Cancel running preparation'), widget)
        self._cancelButton.setObjectName('cancelPreparation')
        self._cancelButton.setAccessibleDescription(
            self.tr('Stop a repair or wrap that is still running.'))
        self._cancelButton.clicked.connect(self._cancelPreparation)
        fit_to_text(self._cancelButton)
        self._cancelButton.setVisible(False)
        layout.addWidget(self._cancelButton, 0, Qt.AlignmentFlag.AlignLeft)

        # Plan 32 section 4.4 / DP-149. `Describe geometry` was the first
        # task row of the Gmsh branch, so its nineteen registry fields -- the
        # import tolerances, the sewing, the small-edge and small-face fixes,
        # the solid making and the far-field box -- were asked after the file
        # had been imported, after it had been repaired, and only once a
        # method had been chosen and applied. They are statements about how
        # the file is read, so they are asked where the user is already
        # looking at what the geometry is wrong with. snappy has no import
        # stage, so the section only exists for the engine that asks.
        #
        # The panel is built on the first showing rather than here: it is
        # nineteen editors, and a case meshed with snappy never needs them.
        self._engineId = ''
        self._healingPanel = None
        self._healingBody = QWidget(widget)
        self._healingBody.setObjectName('gmshHealingBody')
        self._healingLayout = QVBoxLayout(self._healingBody)
        self._healingLayout.setContentsMargins(0, 0, 0, 0)
        self._healingHeader = FolderHeader(self.tr('Advanced'), widget)
        self._healingHeader.setObjectName('gmshHealingHeader')
        # DP-186/187. Read aloud, `Advanced` on its own says nothing about
        # which advanced this is.
        self._healingHeader.setAccessibleName(
            self.tr('Advanced import and healing settings for Gmsh'))
        layout.addWidget(self._healingHeader)
        layout.addWidget(self._healingBody)
        self._healingHeader.setContents(self._healingBody)
        self._healingHeader.setVisible(False)

        # DP-534 (audit 0924, S5/S6). The strip, the combo and the button
        # share one row, and each used to be at least as wide as its own
        # text -- the strip pinned by `fit_to_text`, the combo sized to its
        # longest revision name -- so the row's floor grew with the
        # geometry's name. With `two_cubes_one_file` it passed the settings
        # column: this page was clipped on the right, and because the page
        # stack was as wide as its widest page, every snappy stage page was
        # pushed wider than the column too and scrolled its heading off the
        # left edge. Now the strip and the combo share what the button
        # leaves: the strip stops asking for its text's width (a name with
        # no spaces could not wrap anyway) and keeps the whole text in its
        # tool tip, and the combo elides past ten characters.
        revisions = QHBoxLayout()
        self._revisionStrip = QLabel(widget)
        self._revisionStrip.setObjectName('geometryRevisionStrip')
        strip_policy = self._revisionStrip.sizePolicy()
        strip_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self._revisionStrip.setSizePolicy(strip_policy)
        self._revisionSelect = QComboBox(widget)
        self._revisionSelect.currentIndexChanged.connect(self._inspectRevision)
        self._revisionRestore = QPushButton(self.tr('Restore selected revision'), widget)
        self._revisionRestore.clicked.connect(self._restoreRevision)
        self._revisionSelect.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._revisionSelect.setMinimumContentsLength(10)
        revisions.addWidget(self._revisionStrip, 1)
        revisions.addWidget(self._revisionSelect, 1)
        revisions.addWidget(self._revisionRestore)
        layout.addLayout(revisions)

    @staticmethod
    def _rowsHigh(view, rows: int) -> int:
        """The height a table needs for ``rows`` rows and its header."""
        line = view.fontMetrics().height() + 12
        header = view.horizontalHeader().sizeHint().height() or line
        return rows * line + header + 2 * view.frameWidth()

    #: Readiness states, least to most serious. Both the banner and the
    #: geometry chooser rank by this, so they agree about which geometry the
    #: page is talking about (DP-78).
    _STATE_ORDER = ('ready', 'repairable', 'wrap_recommended', 'blocked')

    #: Findings column that absorbs the slack: the finding name, which is
    #: elided in the middle before any count, size or severity is cut.
    _FINDINGS_SLACK_COLUMN = 1
    #: DP-515. The raw repair-action ids (`cad.sew, cad.fix_shape`). The
    #: plan below names the same actions in words, with a checkbox each; the
    #: ids stay on the row and in `Details…`.
    _FINDINGS_ACTION_COLUMN = 5

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
        # sized to their own header and contents, so no title clips. The
        # floor is a short word, not `Severity`: the floor applies to every
        # column, and a `Severity`-wide floor held `Count` at twice its
        # width and pushed Severity past the panel edge (DP-515).
        header.setMinimumSectionSize(
            header.fontMetrics().horizontalAdvance(self.tr('Count')) + 16)
        # DP-515, MA-08. MEASURED on G3 (`step_unsewn_gap.step`, 1920 x
        # 1024): six content-sized columns wider than the panel, so the
        # table scrolled sideways and the panel edge cut `Warning` and
        # `Not evaluated` in Severity. The action ids are hidden (the plan
        # names them) and the geometry column only shows when there is more
        # than one geometry to tell apart (`_renderReadiness`); what is
        # left fits, and the finding name takes whatever width remains.
        self._findings.setColumnHidden(self._FINDINGS_ACTION_COLUMN, True)
        self._findings.setTextElideMode(Qt.TextElideMode.ElideMiddle)

    def _planEmptyText(self, *, suggested: bool) -> str:
        """What the empty plan area says, before and after a search (R11).

        The standing note asked for the button that had just been pressed --
        `No repair plan yet. "Suggest plan" derives one from the findings
        above` -- and stayed there above the result line reading `0 action(s)
        suggested`. The two statements contradicted each other, and only one
        of them was still true.

        DP-300. The first of the two is gone as well: the search runs itself
        once the findings arrive, so a note asking the reader to press a
        button that has already been pressed for them describes nothing.
        """
        if suggested:
            # DP-521. Nothing found is two different answers. On geometry
            # the mesher can take, the footer's Proceed uses it as it is;
            # on geometry it cannot, no repair closes the gap and the
            # other two choices are the way on.
            fatal, _engine = self._engineFatal()
            state = self._worstState([
                item['diagnostics']['readiness']['state']
                for item in (self._report or {}).get('geometries', ())])
            if fatal or state in _JUSTIFIED_STATES:
                return self.tr('No repair applies. See Wrap or Use as-is.')
            return self.tr('No repair needed. Proceed uses it as it is.')
        # DP-514. The search does not run itself -- it re-reads the whole
        # readiness report -- so the note names the press that runs it, in
        # one line rather than two.
        #
        # DP-521. That line only repeated the press beside it, whose own
        # label and description say it, so before a search it is empty.
        return ''

    def _buildRepairTab(self, parent):
        """The findings, and what can be done about them, in that order.

        DP-300. This is where both moved to. The readiness verdict and the
        findings table opened the page, above the three choices they exist to
        inform, and the table held a floor of five rows whether or not it had
        five rows -- so a case with one finding drew four rows of nothing and
        pushed `Repair`, `Wrap` and `Use as-is` off the first screenful. They
        are inside the choice they belong to now, and nothing reserves height
        for rows it does not have.
        """
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)
        # DP-344. This page and its two neighbours took the house margin
        # above the first row and below the last, inside a tab pane that
        # already insets its pages and above controls that carry their own
        # vertical room -- 16 px of dead height on the tallest step of the
        # column, which is 12 px more than the small screen had to spare
        # once the application is wearing the theme it ships with. The side
        # inset stays: that one keeps the controls off the pane border.
        layout.setContentsMargins(FORM_MARGIN, MARGIN_NONE,
                                  FORM_MARGIN, MARGIN_NONE)
        # DP-300. What the banner used to say, said where the reader is
        # already deciding what to do about it rather than above the choice.
        self._readinessNote = QLabel(tab)
        self._readinessNote.setObjectName('readinessNote')
        self._readinessNote.setWordWrap(True)
        verdict = QHBoxLayout()
        verdict.setContentsMargins(0, 0, 0, 0)
        verdict.addWidget(self._readinessNote, 1)
        # DP-513/514. `Details…` was a fifth button in the row of presses
        # that change the geometry, and it changes nothing: it reads the
        # records behind the verdict. It sits beside the verdict it
        # explains, and it is where the identity tree went.
        self._detailsButton = QPushButton(self.tr('Details…'), tab)
        self._detailsButton.setObjectName('preparationDetails')
        self._detailsButton.setAccessibleDescription(self.tr(
            'Show the readiness report, the repair plan as the mesher '
            'receives it, and the stable identity of every body and patch.'))
        self._detailsButton.clicked.connect(self._showDetails)
        fit_to_text(self._detailsButton)
        verdict.addWidget(self._detailsButton, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(verdict)

        self._findings = QTableWidget(0, 6, tab)
        self._findings.setObjectName('readinessFindings')
        self._findings.setHorizontalHeaderLabels([
            self.tr('Geometry'), self.tr('Finding'), self.tr('Count'),
            self.tr('Size'), self.tr('Severity'), self.tr('Repair actions')])
        self._findings.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._findings.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._findings.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._findings.itemSelectionChanged.connect(self._highlightFinding)
        self._findings.verticalHeader().setVisible(False)
        self._fitFindingColumns()
        layout.addWidget(self._findings, 1)
        # C4. An empty table is a header over a blank rectangle: it says
        # neither "nothing is wrong" nor "nothing has been checked yet".
        self._findingsEmpty = QLabel(self.tr(
            'No readiness findings. Nothing here needs repairing.'), tab)
        self._findingsEmpty.setObjectName('readinessFindingsEmpty')
        self._findingsEmpty.setWordWrap(True)
        self._findingsEmpty.setVisible(False)
        layout.addWidget(self._findingsEmpty)

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
        # DP-513. The tree took a standing band of the Repair tab for a
        # column of truncated uuids -- `3f2a9c1e…` beside `Body 1` -- that
        # nobody decides a repair from (the user marked it `Not
        # Required`). It is kept, filled on every report, and shown on
        # `Details…`, where a reader diagnosing a patch goes.
        self._bodyTree.setVisible(False)
        # Plan 26 WP7.4 adds the risk band; WP7.2 made the Action cell
        # read-only, because a user could overtype a suggested row's Action
        # with any registered id and Preview would run it.
        #
        # DP-300. The table is gone, and with it a four-row floor and a cell
        # of raw JSON the reader was expected to edit by hand. One card per
        # action: the readable name on its own checkbox, the surfaces it acts
        # on beneath, and one typed editor per parameter. The operation id
        # and the plan as the facade will receive it are on `Details`.
        self._planActions = []
        self._planArea = QWidget(tab)
        self._planArea.setObjectName('geometryRepairPlan')
        self._planLayout = QVBoxLayout(self._planArea)
        self._planLayout.setContentsMargins(0, 0, 0, 0)
        self._planArea.setVisible(False)
        layout.addWidget(self._planArea)
        self._planEmpty = QLabel(self._planEmptyText(suggested=False), tab)
        self._planEmpty.setObjectName('geometryRepairPlanEmpty')
        self._planEmpty.setWordWrap(True)
        layout.addWidget(self._planEmpty)
        # B1/B2/B3. Four buttons sharing a narrow panel were each squeezed
        # below their own label: `uggest pla`, `pply as new revisio`,
        # `ancel running preparatio`. No button is drawn narrower than its text
        # now, and the row wraps to a second line rather than clipping.
        #
        # DP-514. Five presses shared this row. `Details…` reads and moved
        # beside the verdict; `Cancel` stops a job and shows only while one
        # runs; `Find repair actions` fills the plan and goes once the plan
        # is on screen, since Preview and Apply act on that plan. What is
        # left is the search, the dry run and the press that writes.
        #
        # DP-521. Even three was one too many at a time. Before a search,
        # Preview and Apply ran the search themselves (`_editedPlan`) and so
        # were a second and third `Find`; after a search that found nothing
        # they stood under `nothing to preview or apply`. Each state now
        # shows only the presses that act in it (`_syncPlanButtons`).
        buttons = FlowLayout()
        pressed = []
        for text, slot, description in (
                # DP-301. `Apply as new revision…` did the work and stopped;
                # the press that finishes this step says so and moves on.
                (self.tr('Apply repairs and proceed'), self._applyRepairPlan,
                 self.tr('Write the checked repairs as a new geometry '
                         'revision and open the next step.')),
                (self.tr('Preview'), self._previewRepair,
                 self.tr('Run the checked repairs without writing them.')),
                (self.tr('Find repair actions'), self._suggestRepair,
                 self.tr('Search the findings above for repairs this '
                         'geometry can run.'))):
            button = QPushButton(text, tab)
            # DP-186. The button is spoken by the words painted on it; the
            # sentence explaining it is a description, not a second name.
            button.setAccessibleDescription(description)
            button.clicked.connect(slot)
            fit_to_text(button)
            buttons.addWidget(button)
            pressed.append(button)
        self._applyButton, self._previewButton, self._findButton = pressed
        layout.addLayout(buttons)
        self._syncPlanButtons(searched=False, actions=False)
        # DP-300/PREP-03. One line, not a pane of JSON: what the preview or
        # the apply came to. The document itself is behind `Details`.
        self._repairReport = QLabel(tab)
        self._repairReport.setObjectName('geometryRepairResult')
        self._repairReport.setWordWrap(True)
        layout.addWidget(self._repairReport)
        self._repairMessage = self._refusalLabel(tab)
        layout.addWidget(self._repairMessage)
        return tab

    def _refusalLabel(self, tab):
        """Where a refused proceed is said, on the choice that was pressed.

        DP-301. The footer says it in the status bar because that is where the
        footer lives. A control on the page is somewhere else entirely, and a
        sentence ten seconds long at the bottom of the window is not an
        answer to a press halfway up it.
        """
        label = QLabel(tab)
        label.setObjectName('preparationRefusal')
        label.setWordWrap(True)
        label.setProperty('foammeshStatus', 'error')
        label.setVisible(False)
        return label

    async def _runPreparation(self, operation, parameters):
        """Run one preparation job, with its cancel shown while it runs."""
        self._preparationJobs += 1
        self._cancelButton.setVisible(True)
        try:
            return await app.facadeClient.run(operation, parameters)
        finally:
            self._preparationJobs -= 1
            self._cancelButton.setVisible(self._preparationJobs > 0)

    @qasync.asyncSlot()
    async def _cancelPreparation(self):
        result = (await app.facadeClient.run('geometry.prepare.cancel', {})).payload
        self._repairReport.setText(self.tr('Cancellation: {0}').format(
            humanise_option(result.get('state', 'idle'))))

    def _buildWrapTab(self, parent):
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)
        # DP-344, as on the Repair tab: no dead margin above the first
        # row or below the last.
        layout.setContentsMargins(FORM_MARGIN, MARGIN_NONE,
                                  FORM_MARGIN, MARGIN_NONE)
        limitation = QLabel(self.tr(
            'Uniform-grid wrap changes geometry. Memory grows cubically and sharp edges may round.'), tab)
        limitation.setWordWrap(True)
        layout.addWidget(limitation)
        controls = QFormLayout()
        self._wrapResolution = CompactSpinBox(tab)
        self._wrapResolution.setRange(16, 256)
        self._wrapResolution.setValue(64)
        self._wrapGap = self._optionalDistance(tab)
        self._wrapDeviation = self._optionalDistance(tab)
        self._wrapSmoothing = CompactDoubleSpinBox(tab)
        self._wrapSmoothing.setRange(0, 1)
        self._wrapSmoothing.setSingleStep(.1)
        self._wrapSmoothing.setValue(1.0)
        self._wrapMinComponent = CompactSpinBox(tab)
        self._wrapMinComponent.setRange(0, 10_000_000)
        self._wrapMinComponent.setValue(27)
        self._wrapMode = QComboBox(tab)
        self._wrapMode.addItem(self.tr('External flow'), 'external')
        self._wrapMode.addItem(self.tr('Internal flow'), 'internal')
        self._wrapSeeds = QLineEdit(tab)
        self._wrapSeeds.setPlaceholderText('x,y,z; x,y,z')
        # DP-164. Every row ends with a unit column, whether or not the row
        # has a unit, so the boxes all end at one x -- the rule the
        # registry-built forms have followed since DP-156.
        # DP-300/PREP-05. What decides the grid, together: which side of the
        # surface is being wrapped, how fine the grid is, and how far the
        # wrap may stray from the geometry. Smoothing and the component
        # filter are tuning, and they used to sit between the tolerances and
        # the flow mode -- so the two settings that answer the same question
        # were three rows apart with a strength and a cell count between.
        for label, control, unit in (
                (self.tr('Flow mode'), self._wrapMode, ''),
                (self.tr('Base resolution'), self._wrapResolution, 'cells'),
                (self.tr('Maximum gap'), self._wrapGap, 'm'),
                (self.tr('Maximum deviation'), self._wrapDeviation, 'm'),
                (self.tr('Fluid seeds'), self._wrapSeeds, '')):
            controls.addRow(label, unit_cell(control, unit))
        layout.addLayout(controls)

        self._wrapAdvanced = QWidget(tab)
        self._wrapAdvanced.setObjectName('wrapAdvancedOptions')
        advanced = QFormLayout(self._wrapAdvanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self._wrapSmallestFeature = self._optionalDistance(self._wrapAdvanced)
        self._wrapSmallestFeature.setObjectName('wrapSmallestFeature')
        self._wrapSmallestFeature.setToolTip(self.tr(
            'Grid spacing is half this value and overrides Base resolution'))
        advanced.addRow(self.tr('Smoothing strength'),
                        unit_cell(self._wrapSmoothing, 'fraction'))
        advanced.addRow(self.tr('Minimum component cells'),
                        unit_cell(self._wrapMinComponent, 'cells'))
        advanced.addRow(self.tr('Smallest feature to preserve'),
                        unit_cell(self._wrapSmallestFeature, 'm'))
        precedence = QLabel(self.tr(
            'Automatic keeps Base resolution active. A positive value derives '
            'the grid from the smallest feature and takes precedence.'),
            self._wrapAdvanced)
        precedence.setWordWrap(True)
        advanced.addRow('', precedence)
        align_unit_column((controls, advanced))
        # DP-300/DP-287. A fold that hides settings is a fold the reader has
        # to open to find out whether it matters. The band opens with the
        # page and can be closed; the toggle it replaces was a bare
        # `Advanced options` arrow that started shut over one row.
        self._wrapAdvancedHeader = FolderHeader(self.tr('Advanced'), tab)
        self._wrapAdvancedHeader.setObjectName('wrapAdvancedOptionsHeader')
        self._wrapAdvancedHeader.setAccessibleName(
            self.tr('Advanced wrap options'))
        layout.addWidget(self._wrapAdvancedHeader)
        layout.addWidget(self._wrapAdvanced)
        # W-O2: the `setChecked(True)` that used to sit here is now the
        # default every `FolderHeader` carries.
        self._wrapAdvancedHeader.setContents(self._wrapAdvanced)
        buttons = FlowLayout()
        for text, slot, description in (
                # DP-301. The press that finishes this step says it does.
                (self.tr('Apply wrap and proceed'), self._applyWrap,
                 self.tr('Write the wrap as a new geometry revision and open '
                         'the next step.')),
                (self.tr('Build coarse preview'), self._estimateWrap,
                 self.tr('Wrap at a coarse grid and report what it comes '
                         'to, without writing anything.'))):
            button = QPushButton(text, tab)
            button.setAccessibleDescription(description)
            button.clicked.connect(slot)
            fit_to_text(button)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._wrapPreview = QLabel(tab)
        self._wrapPreview.setObjectName('wrapPreviewSummary')
        self._wrapPreview.setWordWrap(True)
        layout.addWidget(self._wrapPreview)
        self._wrapMessage = self._refusalLabel(tab)
        layout.addWidget(self._wrapMessage)
        return tab

    def _wrapSizingParameters(self):
        parameters = {'resolution': self._wrapResolution.value()}
        if self._wrapSmallestFeature.value() > 0:
            parameters['smallest_feature'] = self._wrapSmallestFeature.value()
        return parameters

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
        # DP-344, as on the Repair tab: no dead margin above the first
        # row or below the last.
        layout.setContentsMargins(FORM_MARGIN, MARGIN_NONE,
                                  FORM_MARGIN, MARGIN_NONE)
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
        note.setObjectName('geometryAsIsSemantics')
        note.setWordWrap(True)
        # DP-515. The paragraph is what the press commits to, read once;
        # it stood open above the state line and the button on every visit.
        # It holds no setting, so the fold may open closed.
        self._asIsDetails = FolderHeader(self.tr('Details'), tab)
        self._asIsDetails.setObjectName('geometryAsIsDetails')
        self._asIsDetails.setAccessibleName(
            self.tr('What using the geometry as-is records'))
        layout.addWidget(self._asIsDetails)
        layout.addWidget(note)
        self._asIsDetails.setContents(note)
        self._asIsDetails.setChecked(False)
        self._asIsState = QLabel(tab)
        self._asIsState.setObjectName('geometryAsIsState')
        self._asIsState.setWordWrap(True)
        layout.addWidget(self._asIsState)
        self._asIsMessage = self._refusalLabel(tab)
        layout.addWidget(self._asIsMessage)
        buttons = QHBoxLayout()
        # DP-515. One sentence beside the press when it is switched off; the
        # full reason is the state line above.
        self._asIsBlockedNote = QLabel(self.tr(
            'The surface must be closed or repaired before meshing.'), tab)
        self._asIsBlockedNote.setObjectName('geometryAsIsBlockedNote')
        self._asIsBlockedNote.setWordWrap(True)
        self._asIsBlockedNote.setVisible(False)
        buttons.addWidget(self._asIsBlockedNote, 1)
        # DP-301. MEASURED: `Use geometry as-is` recorded the decision, froze
        # a revision and refreshed this tab -- and the reader, having pressed
        # the control that finishes preparation, was still on preparation and
        # had to find the footer and press Proceed as well. The label says
        # where the press goes, and the press goes there.
        button = QPushButton(self.tr('Use as-is and proceed'), tab)
        button.setAccessibleDescription(self.tr(
            'Record the decision, freeze the current revision and open the '
            'next step.'))
        button.clicked.connect(self._recordAsIs)
        # DP-126. Held so the state line below can switch it off when the
        # chosen mesher cannot read this geometry at all.
        self._asIsButton = button
        fit_to_text(button)
        buttons.addStretch(1)
        buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return tab

    def _engineFatal(self):
        """The findings the chosen mesher cannot read past, and its name.

        DP-126. `blocked` is overridable on a written reason, which is right
        where the block is a judgement and wrong where it is a fact about the
        engine: no sentence closes a hole in a surface. DP-125 taught the
        Prepare command to refuse those, so this page has to stop offering
        them -- otherwise the user writes a justification and is told
        afterwards that it did not count.
        """
        kinds, engines = set(), set()
        for item in (self._report or {}).get('geometries', ()):
            readiness = (item.get('diagnostics', {}).get('readiness') or {})
            fatal = readiness.get('engine_fatal') or ()
            if fatal:
                kinds.update(fatal)
                engines.add(readiness.get('engine'))
        engine = engines.pop() if len(engines) == 1 else None
        return sorted(kinds), engine

    def _engineFatalSentence(self, fatal, engine):
        """The one sentence said about a mesher that cannot read this at all.

        DP-246. The wizard refuses the same geometry the tab refuses, so it
        says the same thing. Two wordings for one fact is two things to keep
        true, and the reader who meets one and then the other has to work out
        whether they mean the same.
        """
        return self.tr(
            'This geometry cannot be meshed by {0} as it is: {1}. No '
            'acknowledgement changes that. Repair or wrap it on the tabs '
            'to the left, or choose the other mesher.').format(
                humanise_option(engine) if engine
                else self.tr('the chosen mesher'),
                ', '.join(kind.replace('_', ' ') for kind in fatal))

    def _updateAsIsState(self):
        """Report the preparation decision on the tab that records it (F2)."""
        state = getattr(self, '_asIsState', None)
        if state is None:
            return
        fatal, engine = self._engineFatal()
        button = getattr(self, '_asIsButton', None)
        if button is not None:
            button.setEnabled(not fatal)
        blocked = getattr(self, '_asIsBlockedNote', None)
        if blocked is not None:
            blocked.setVisible(bool(fatal))
        if fatal:
            state.setText(self._engineFatalSentence(fatal, engine))
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
        control = CompactDoubleSpinBox(parent)
        control.setDecimals(8)
        control.setRange(0, 1e12)
        control.setSpecialValueText(self.tr('Automatic'))
        return control

    # -- the Gmsh import and healing section ------------------------------- #

    def setEngine(self, engine_id) -> None:
        """Show the import and healing section for the engine that asks.

        Plan 32 section 4.4. Only Gmsh has an import stage; snappy is handed
        a surface and starts from it. The page asks the facade which engine
        is selected on every refresh, so `StepManager` gains nothing to
        remember.
        """
        engine_id = str(engine_id or '')
        if engine_id == self._engineId:
            return
        self._engineId = engine_id
        wanted = engine_id == 'gmsh'
        if wanted and self._healingPanel is None:
            panel = GmshHealingPanel(app.facadeClient, self._healingBody)
            # DP-155/157. A section of a page does not scroll itself and
            # carries no commit pair of its own.
            panel.setEmbedded(True)
            self._healingLayout.addWidget(panel)
            self._healingPanel = panel
        elif wanted:
            # DP-339. A refresh keeps an uncommitted edit.
            self._healingPanel.reload(discard_pending=False)
        self._healingHeader.setVisible(wanted)
        self._healingBody.setVisible(wanted and self._healingHeader.isChecked())

    def healingPanel(self):
        """The import and healing editors, or None while none are needed."""
        return self._healingPanel

    def healingHeader(self):
        """The disclosure that opens the import and healing section."""
        return self._healingHeader

    def hasPendingHealingEdits(self) -> bool:
        """True when the section holds an edit nobody has committed."""
        return self._healingPanel is not None and self._healingPanel.is_dirty

    def applyPendingHealing(self):
        """Commit whatever the import and healing section is holding."""
        if self._healingPanel is None:
            return None
        return self._healingPanel.apply()

    async def savePendingHealing(self) -> bool:
        """Commit that section and say whether the case took it.

        DP-251, the same rule as the Advanced band on `2. Mesh setup`: a
        section with no Apply of its own (DP-157) is committed by the press
        that leaves the page, and the press has to know the answer before it
        moves. These settings describe how the CAD is read, so a press that
        walked past them unsaved would prepare the geometry with the
        tolerances the reader had just replaced. True when there is nothing
        to write -- no Gmsh, no section, no pending edit.
        """
        if self._healingPanel is None:
            return True
        return await self._healingPanel.save()

    def _syncEngine(self) -> None:
        """Ask the facade which engine is selected and follow it.

        A page that has no session, or a client that does not answer, leaves
        the section as it is rather than guessing.
        """
        try:
            payload = query(app.facadeClient, 'mesh.engine.list').payload
        except Exception:  # noqa: BLE001 - the page must still draw
            return
        engine_id = (payload or {}).get('current_engine')
        if engine_id:
            self.setEngine(str(engine_id))

    def load(self):
        self._loaded = True
        self.refresh()

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        self.refresh()
        await super().show(isWorkingStep, batchRunning)

    def isNextStepAvailable(self):
        if app.facadeClient is None or not app.facadeClient.has_case():
            return False
        status = query(app.facadeClient, 'workflow.status').payload
        return bool(status['geometry_preparation']['current'])

    # -- what Proceed may do (Plan 32 W3, DP-246/DP-247) -------------------- #

    def _blockingFindingKinds(self):
        """The findings worth naming in a refusal, in the table's own words.

        DP-247. `_renderReadiness` paints `humanise_option(kind)` in the
        Finding column, so a refusal that named the kind any other way would
        send the reader looking for a row that is not there. Errors are what
        a refusal is about; when a state is reached without one, every
        counted finding is named rather than none.
        """
        errors, counted = [], []
        for item in (self._report or {}).get('geometries', ()):
            for finding in item.get('diagnostics', {}).get('findings', ()):
                if not finding.get('count'):
                    continue
                name = humanise_option(finding['kind'])
                if name not in counted:
                    counted.append(name)
                if finding.get('severity') == 'error' and name not in errors:
                    errors.append(name)
        return errors or counted

    def _preparationRefusal(self, state, stale):
        """Why Proceed stayed here, what is wrong, and where it is fixed."""
        kinds = self._blockingFindingKinds()
        named = (', '.join(kinds) if kinds
                 else self.tr('The findings listed above'))
        if state == 'wrap_recommended':
            sentence = self.tr(
                '{0}: a wrap is recommended before meshing. Build one on '
                'the Wrap tab, or accept the geometry on the Use as-is tab '
                'with a written reason.').format(named)
        else:
            sentence = self.tr(
                '{0}: this must be fixed before meshing. Apply a plan on '
                'the Repair tab, wrap it on the Wrap tab, or accept the '
                'geometry on the Use as-is tab with a written reason.'
            ).format(named)
        if stale:
            return self.tr(
                'The recorded decision no longer applies: the geometry has '
                'changed since it was taken. ') + sentence
        return sentence

    def _acceptedAsIsSentence(self, stale, state='ready'):
        """What was decided on the reader's behalf, said where they can see.

        Two states reach here. `ready` found nothing at all, and `repairable`
        found something a plan could tidy but nothing that has to be tidied
        before a mesher will read the surface -- which is why neither needs a
        written reason. Saying `nothing that needs repairing` on the second
        of those would contradict the findings table standing above it, so
        the sentence says which of the two it was.
        """
        if stale:
            return self.tr(
                'The geometry has changed since the last decision, so it '
                'was accepted as it is again and a prepared revision was '
                'recorded.')
        if state == 'repairable':
            return self.tr(
                'The geometry was accepted as it is: the checks found '
                'nothing that must be fixed before meshing, so a prepared '
                'revision was recorded. The repairs offered above are still '
                'available if you want them.')
        return self.tr(
            'The geometry was accepted as it is: the checks found nothing '
            'that needs repairing, so a prepared revision was recorded.')

    def preparationVerdict(self):
        """What Proceed may do about preparation, or None if this cannot say.

        None is not a refusal and not a permission: it means this page holds
        no readiness report or has no case to ask about, and the caller
        should fall back to whatever it did before. Every other answer is
        decided from the report on screen, so the wizard and the page cannot
        disagree about the same geometry.
        """
        if app.facadeClient is None or not app.facadeClient.has_case():
            return None
        if not self._report:
            return None
        try:
            status = query(app.facadeClient, 'workflow.status').payload
            preparation = status['geometry_preparation']
        except (FacadeError, KeyError, OSError, RuntimeError, ValueError):
            return None
        decision = str(preparation.get('decision') or 'undecided')
        recorded = decision != 'undecided'
        if recorded and preparation.get('current'):
            return PreparationVerdict(True, False, False, 'ready', '')
        # A decision taken about a revision that is no longer loaded is not
        # a decision about this one (Plan 32 W3, point 3).
        stale = recorded
        fatal, engine = self._engineFatal()
        if fatal:
            return PreparationVerdict(
                False, False, stale, 'blocked',
                self._engineFatalSentence(fatal, engine))
        state = self._worstState([
            item['diagnostics']['readiness']['state']
            for item in self._report.get('geometries', [])])
        if state in _JUSTIFIED_STATES:
            return PreparationVerdict(False, False, stale, state,
                                      self._preparationRefusal(state, stale))
        return PreparationVerdict(False, True, stale, state,
                                  self._acceptedAsIsSentence(stale, state))

    async def acceptGeometryAsIs(self):
        """Record the as-is decision and freeze the revision. One press.

        The same two operations `_recordAsIs` submits, in the same order,
        with the same parameters -- there is no third way to prepare a
        geometry, and nothing downstream can tell which control asked. The
        tab chains them through callbacks because it is driven from a
        synchronous Qt slot; the wizard is already async, so it awaits them.

        Answers None when both landed, or the reason the first one did not.
        """
        parameters = {'decision': 'as_is'}
        try:
            await app.facadeClient.run(
                'geometry.preparation.decide', dict(parameters))
            await app.facadeClient.run(
                'geometry.prepared.create', {'preparation': dict(parameters)})
        except (FacadeError, OSError, RuntimeError, ValueError) as error:
            detail = (getattr(error, 'details', None) or {}).get(
                'error', str(error))
            return str(detail)
        return None

    async def runInBatchMode(self):
        return self.isNextStepAvailable()

    # -- what the page holds but does not print (DP-300) --------------------- #

    def _detail(self, key: str, payload) -> bool:
        """Keep a record the reader can open, and say whether it is whole.

        DP-89. Some payloads carry a live CAD shape that no serialiser can
        write down. That used to raise out of the slot that was previewing a
        repair, which killed the repair; the record is kept either way, the
        dialog renders what it can, and the caller is told so it can say so.
        """
        self._details[str(key)] = payload
        try:
            json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError):
            return False
        return True

    def _showDetails(self):
        """Show the records behind this step, as the mesher receives them."""
        records = dict(self._details)
        if self._report is not None:
            records.setdefault('readiness', self._report)
        plan = self._editedPlan()
        if plan:
            records['plan'] = plan
        dialog = QDialog(self._widget)
        dialog.setObjectName('preparationDetailsDialog')
        dialog.setWindowTitle(self.tr('Preparation details'))
        dialog.setAccessibleName(self.tr('Preparation details'))
        layout = QVBoxLayout(dialog)
        pages = QTabWidget(dialog)
        pages.setObjectName('preparationDetailsTabs')
        layout.addWidget(pages)
        view = QTextEdit(dialog)
        view.setObjectName('preparationDetailsText')
        view.setReadOnly(True)
        view.setAccessibleName(self.tr('Preparation details'))
        view.setPlainText(json.dumps(records, indent=2, sort_keys=True,
                                     default=str)
                          if records else
                          self.tr('Nothing has been checked yet.'))
        pages.addTab(view, self.tr('Records'))
        # DP-513. The identity tree is lent to the dialog and taken back,
        # so one tree is filled per report whether or not it is on screen.
        home = self._bodyTree.parentWidget()
        pages.addTab(self._bodyTree, self.tr('Bodies and patches'))
        self._bodyTree.setVisible(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                   parent=dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.resize(640, 480)
        try:
            dialog.exec()
        finally:
            self._bodyTree.setParent(home)
            self._bodyTree.setVisible(False)

    def clearResult(self):
        """Throw away the geometry that was prepared from the old one.

        GEO-08/DP-302. This was `return`. Every other page in the shell
        answers an unlock by dropping what it computed, and this one kept
        `foammesh/geometry/prepared/current.json`, the recorded decision and
        the settled hosted task -- so a reader who unlocked the geometry to
        replace it went on meshing the geometry they had just replaced, with
        a task tree that agreed nothing had changed.
        """
        self._repairPlan = None
        self._details = {}
        self._clearPlanCards()
        self._planArea.setVisible(False)
        self._planEmpty.setText(self._planEmptyText(suggested=False))
        self._syncPlanButtons(searched=False, actions=False)
        self._repairReport.setText('')
        self._wrapPreview.setText('')
        self.clearRefusal()
        self._findings.setRowCount(0)
        self._report = None

        def discarded(_result):
            self._updateAsIsState()
            self._updateNextStepAvailable()

        submit(app.facadeClient, 'geometry.prepared.discard', {},
               then=discarded)
        self.stepReset.emit()

    def forgetRemovedGeometry(self):
        """Drop what this page drew from a geometry set that has changed.

        DP-1213. Removing a geometry on `1. Geometry` left this page holding
        the readiness report, the body tree and the repair plan of the set
        before it, the removed body still in them, until something else
        redrew it. Nothing here asks the facade for anything: the next
        `show()` fetches the report of the set as it now is, and the prepared
        revision of the old set is no longer current once its fingerprint
        stops matching (`PreparedGeometryStore.current`).
        """
        self._repairPlan = None
        self._details = {}
        self._clearPlanCards()
        self._planArea.setVisible(False)
        self._planEmpty.setText(self._planEmptyText(suggested=False))
        self._syncPlanButtons(searched=False, actions=False)
        self._repairReport.setText('')
        self._wrapPreview.setText('')
        self._findings.setRowCount(0)
        self._bodyTree.clear()
        self._report = None
        self._offerGeometries([])

    def refresh(self):
        # C31-12. `geometry.readiness` is a READ that declares an artifact
        # (`readiness_report`), so the registry classes it as a mutation and
        # `query()` refuses it -- it has to stay on the write path. What it
        # does not have to do is hold the GUI thread while a CAD readiness
        # report is computed and written, which is the slowest thing this
        # page asks for. The report is fetched, and the redraw below happens
        # when it arrives, in the same order as before.
        self._syncEngine()

        def rendered(result):
            if isinstance(result, FailedResult):
                self._readinessNote.setText(
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
        # DP-114. One verdict used to answer for two engines that disagree
        # about what is fatal, and the banner did not say which one it meant.
        # MEASURED on `cyclone`: 54 free edges, graded `repairable`, meshed by
        # snappy and refused by Gmsh at the end of its run. The grading is
        # engine-aware now, so the banner has to name the engine it graded
        # for -- and say so when no engine has been chosen yet.
        engines = {(item['diagnostics']['readiness'] or {}).get('engine')
                   for item in geometries}
        engine = engines.pop() if len(engines) == 1 else None
        # DP-300/DP-220. The verdict belongs beside the findings that earned
        # it, on the Repair choice, rather than above the three choices where
        # it stood between the reader and the decision. It no longer counts
        # the geometries either: the list below is the count.
        if engine:
            note = self.tr('Readiness for {0}: {1}.').format(
                humanise_option(engine), humanise_option(state))
        else:
            note = self.tr(
                'Readiness: {0}. No mesher is chosen yet, so this verdict '
                'is advisory for both.').format(humanise_option(state))
        self._readinessNote.setText(note)
        # DP-78. A finding used to arrive with no geometry attached, so on a
        # two-geometry case the row carrying `dropped_facets` and `tess.weld`
        # sat beside the farfield's rows looking like a property of the case.
        findings = [(item, finding) for item in geometries
                    for finding in item['diagnostics']['findings']
                    if finding['count'] or not finding.get('evaluated', True)]
        self._findings.setRowCount(len(findings))
        # DP-515. The owner column tells two geometries' rows apart; on one
        # geometry it repeats one name down the table.
        self._findings.setColumnHidden(0, len(geometries) < 2)
        self._findings.setVisible(bool(findings))
        self._findingsEmpty.setVisible(not findings)
        for row, (owner, finding) in enumerate(findings):
            evaluated = bool(finding.get('evaluated', True))
            size = finding['characteristic_size']
            # R7. A row that says `Not evaluated` also printed `0` in Count,
            # so it asserted a measurement and denied taking one in the same
            # line. A check that did not run has no count and no size; the
            # dash says that, where `0` claimed a clean result.
            values = (owner.get('name') or self.tr('geometry'),
                      humanise_option(finding['kind']),
                      '{0:,}'.format(finding['count']) if evaluated
                      else '—',
                      # DP-223. The unit travels with the number: this
                      # column holds a length in one row and a count of
                      # cells in the next, so no heading speaks for both.
                      ' '.join(part for part in (
                          f'{size:.6g}',
                          finding.get('characteristic_unit') or '')
                          if part)
                      if evaluated and size is not None
                      else '—',
                      humanise_option(finding['severity'])
                      if evaluated
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
                # DP-175. Count and Size are numbers, and numbers in a
                # column are read by their last digit, not their first.
                if column in _NUMERIC_FINDING_COLUMNS:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self._findings.setItem(row, column, item)
        self._offerGeometries(geometries)
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
        # had taken the whole row for itself. DP-534: the two now share the
        # row by stretch, so neither is pinned to its text's width.
        self._revisionStrip.setText(self.tr('Current: {0}').format(' — '.join(labels) or 'none'))
        self._revisionStrip.setToolTip(self._revisionStrip.text())
        self._updateAsIsState()
        # DP-300. The wrap estimate is a coarse voxelisation of the whole
        # surface; running it on every redraw of a page the reader may not
        # even open the Wrap choice on spent that time for nothing. It runs
        # when the reader asks for it.
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
            self._detail('revision', report if report is not None else {
                'revision': revision_number,
                'kind': revision.get('kind', 'imported'),
                'provenance': revision.get('provenance', {}),
                'report': None,
            })
            # DP-300. The page used to print the whole revision record here as
            # indented JSON, which is a file, not a setting. The record is
            # kept and shown on request; the page says which revision it is
            # holding.
            self._repairReport.setText(self.tr(
                'Revision {0} ({1}) selected. Details… shows what it '
                'recorded.').format(revision_number,
                                    revision.get('kind', 'imported')))
            return

    def _offerGeometries(self, geometries):
        """List the geometries the buttons can act on, worst state first.

        DP-78. The list opens on the geometry in the worst readiness state
        rather than the first imported, because the banner above is already
        reporting that state and the user reads the two together. A selection
        the user has picked from this list survives a refresh; one the page
        made for itself does not, and neither does a geometry that has gone
        away.
        """
        # DP-80, MEASURED. Reading the combo back to find "the choice" keeps
        # a choice nobody made: the page refreshes while the import is still
        # running, so on `drone_quadcopter` the first report holds only the
        # farfield, the combo lands on it for want of anything else, and when
        # the drone body arrives `repairable` the farfield is still current
        # and looks chosen. Only a selection that came through
        # `_geometryChosen` -- which the signal blocker below reserves for the
        # user -- outranks the worst readiness state.
        chosen = self._geometrySelect.currentData() if self._geometryPinned else None
        order = {state: rank for rank, state in enumerate(self._STATE_ORDER)}

        def severity(item):
            state = (item.get('diagnostics', {}).get('readiness') or {}).get(
                'state', 'ready')
            return -order.get(state, 0)

        with QSignalBlocker(self._geometrySelect):
            self._geometrySelect.clear()
            for item in geometries:
                readiness = (item.get('diagnostics', {}).get('readiness')
                             or {}).get('state', 'ready')
                self._geometrySelect.addItem(
                    '{0} — {1}'.format(
                        item.get('name') or self.tr('geometry'),
                        readiness.replace('_', ' ')),
                    item['geometry_id'])
            index = self._geometrySelect.findData(chosen)
            if index < 0:
                # The geometry the user pinned is no longer imported, so the
                # pin goes with it rather than transferring to whatever the
                # page picks next.
                self._geometryPinned = False
                worst = min(geometries, key=severity, default=None)
                index = (self._geometrySelect.findData(worst['geometry_id'])
                         if worst else -1)
            self._geometrySelect.setCurrentIndex(max(index, 0))
        # More than one geometry is the whole reason this control exists; on
        # a single-geometry case it would only be a line that never changes.
        visible = len(geometries) > 1
        self._geometrySelect.setVisible(visible)
        self._geometryLabel.setVisible(visible)
        # DP-300. The row itself carries the spacing, so hiding only the two
        # controls inside it would leave a band of empty page above the
        # choices. It starts hidden and appears with its contents.
        self._chooserRow.setVisible(visible)

    def _geometryChosen(self):
        """Drop a plan built for a geometry the buttons no longer act on."""
        self._geometryPinned = True
        self._repairPlan = None
        self._clearPlanCards()
        self._planEmpty.setText(self._planEmptyText(suggested=False))
        self._syncPlanButtons(searched=False, actions=False)
        self._suggestRepair()

    def _activeGeometryId(self):
        """The geometry every button on this page acts on (DP-78)."""
        chosen = self._geometrySelect.currentData()
        if chosen:
            return chosen
        geometries = (self._report or {}).get('geometries', [])
        return geometries[0]['geometry_id'] if geometries else None

    def _clearPlanCards(self):
        """Take down the cards a previous search put up."""
        self._planActions = []
        while self._planLayout.count():
            item = self._planLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._planArea.setVisible(False)
        self._syncPlanButtons(searched=False, actions=False)

    def _syncPlanButtons(self, *, searched: bool, actions: bool):
        """Show the presses that act in the plan's state, and no others.

        DP-521. Before a search only `Find repair actions` does anything new
        (Preview and Apply would run the same search first). With a plan on
        screen, Preview is the dry run and Apply writes it; Find has done.
        After a search that found nothing, none of the three has work, and
        the one-line note says where the way on is.
        """
        self._findButton.setVisible(not searched)
        self._previewButton.setVisible(actions)
        self._applyButton.setVisible(actions)
        self._planEmpty.setVisible(
            bool(self._planEmpty.text()) and not actions)

    def _parameterEditor(self, parent, value):
        """A typed control for one repair parameter, and how to read it back.

        PREP-03. Every parameter used to live in one cell of raw JSON that
        the reader was expected to retype without a schema, a range or a
        unit, and a stray brace failed the whole plan. The suggestion says
        what each value is; the editor follows the value.
        """
        if isinstance(value, bool):
            control = QCheckBox(parent)
            control.setChecked(value)
            return control, control.isChecked
        if isinstance(value, int):
            control = CompactSpinBox(parent)
            control.setRange(-1_000_000_000, 1_000_000_000)
            control.setValue(value)
            return control, control.value
        if isinstance(value, float):
            control = CompactDoubleSpinBox(parent)
            control.setDecimals(8)
            control.setRange(-1e12, 1e12)
            control.setValue(value)
            return control, control.value
        if isinstance(value, str):
            control = QLineEdit(value, parent)
            return control, control.text
        # Lists and mappings have no editor of their own; they keep the
        # spelling the suggestion gave them and are read back as written.
        control = QLineEdit(json.dumps(value, sort_keys=True), parent)
        return control, lambda: json.loads(control.text() or 'null')

    def _scopeText(self, action):
        targets = [str(name) for name in (action.get('targets') or ())]
        return (self.tr('Acts on {0}.').format(', '.join(targets)) if targets
                else self.tr('Acts on the whole geometry.'))

    def _planCommon(self, actions):
        """What every card would say alike, said once above them (DP-515).

        MA-08, MEASURED on G3: the CAD route suggests seven actions and
        every card repeated `Acts on the whole geometry.` and
        `4 — CAD healing`, and three of them carried the same tolerance in
        three editors. What all the cards share is a heading; a value two
        or more cards share is one editor that sets all of them; the
        re-triangulation settings go under `Advanced`.
        """
        if len(actions) < 2:
            return None, None, {}
        scopes = {self._scopeText(action) for action in actions}
        bands = {_riskLabel(action['action']) for action in actions}
        scope = scopes.pop() if len(scopes) == 1 else None
        band = bands.pop() if len(bands) == 1 else None
        seen = {}
        for action in actions:
            for name, value in (action.get('params') or {}).items():
                if name in _SPECIALIST_PARAMETERS:
                    continue
                seen.setdefault(name, []).append((action['action'], value))
        shared = {}
        for name, uses in seen.items():
            values = {json.dumps(value, sort_keys=True) for _a, value in uses}
            if len(uses) > 1 and len(values) == 1:
                shared[name] = (uses[0][1], [a for a, _v in uses])
        return scope, band, shared

    def _planHeading(self, actions, scope, band, shared):
        """The one heading above the cards, and the shared editors.

        DP-219. The block is a container, not a label: its words are the
        sentence in `geometryRepairPlanScope`, which the plan computes, so it
        is named a header and stays in body type, not the page-heading type.
        """
        heading = QWidget(self._planArea)
        heading.setObjectName('geometryRepairPlanHeader')
        box = QVBoxLayout(heading)
        box.setContentsMargins(0, 0, 0, 0)
        words = ' '.join(part for part in (
            scope, self.tr('Risk band {0}.').format(band) if band else '')
            if part)
        readers = {}
        if words:
            label = QLabel(words, heading)
            label.setObjectName('geometryRepairPlanScope')
            label.setWordWrap(True)
            if band:
                label.setToolTip(_riskDetail(actions[0]['action']))
            box.addWidget(label)
        if shared:
            form = QFormLayout()
            form.setContentsMargins(0, 0, 0, 0)
            for name in sorted(shared):
                value, users = shared[name]
                control, read = self._parameterEditor(heading, value)
                control.setToolTip(self.tr('Used by: {0}').format(
                    ', '.join(_actionName(user) for user in users)))
                form.addRow(_parameterLabel(name), control)
                readers[name] = read
            box.addLayout(form)
        return heading, readers

    def _planAdvanced(self, specialist):
        """The re-triangulation settings, under one `Advanced` fold."""
        header = FolderHeader(self.tr('Advanced'), self._planArea)
        header.setObjectName('geometryRepairPlanAdvanced')
        header.setAccessibleName(self.tr('Advanced repair settings'))
        body = QWidget(self._planArea)
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        readers = {}
        for action, name, value in specialist:
            control, read = self._parameterEditor(body, value)
            control.setToolTip(_actionName(action))
            form.addRow(_parameterLabel(name), control)
            readers[(action, name)] = read
        header.setContents(body)
        return header, body, readers

    def _planCard(self, action, *, scope=True, band=True, shared=None,
                  specialist=None):
        """One suggested repair: what it does, where, and on what settings."""
        card = QWidget(self._planArea)
        card.setObjectName('geometryRepairAction')
        box = QVBoxLayout(card)
        # DP-195. One card is held off the next by the spacing the plan
        # area already carries, not by a distance typed in here.
        box.setContentsMargins(0, 0, 0, 0)
        heading = QHBoxLayout()
        use = QCheckBox(_actionName(action['action']), card)
        use.setChecked(bool(action.get('enabled', True)))
        # DP-186. The checkbox is spoken by the words beside it. What the
        # band means, and which operation this is, are said in the
        # description and the tooltip rather than in a second name.
        use.setAccessibleDescription(self.tr(
            'Include this repair when the plan is previewed or applied.'))
        use.setToolTip(_riskDetail(action['action']))
        heading.addWidget(use, 1)
        if band:
            label = QLabel(_riskLabel(action['action']), card)
            label.setToolTip(_riskDetail(action['action']))
            heading.addWidget(label)
        box.addLayout(heading)
        if scope:
            surfaces = QLabel(self._scopeText(action), card)
            surfaces.setWordWrap(True)
            box.addWidget(surfaces)
        editors = {}
        shared = shared or {}
        parameters = dict(action.get('params') or {})
        own = []
        for name in sorted(parameters):
            if name in shared:
                editors[name] = shared[name]
            elif name in _SPECIALIST_PARAMETERS and specialist is not None:
                specialist.append((action['action'], name, parameters[name]))
            else:
                own.append(name)
        if own:
            form = QFormLayout()
            form.setContentsMargins(0, 0, 0, 0)
            for name in own:
                control, read = self._parameterEditor(card, parameters[name])
                form.addRow(_parameterLabel(name), control)
                editors[name] = read
            box.addLayout(form)
        return card, {'action': action['action'], 'use': use,
                      'editors': editors, 'params': parameters}

    def _suggestRepair(self):
        """Search the findings for repairs this geometry can run (PREP-03)."""
        geometry_id = self._activeGeometryId()
        if not geometry_id:
            return
        self._repairPlan = query(
            app.facadeClient, 'geometry.repair.suggest',
            {'geometry_id': geometry_id}).payload
        actions = list(self._repairPlan.get('actions', ()))
        self._clearPlanCards()
        scope, band, shared = self._planCommon(actions)
        readers = {}
        if scope or band or shared:
            heading, readers = self._planHeading(actions, scope, band, shared)
            self._planLayout.addWidget(heading)
        specialist = []
        for action in actions:
            card, entry = self._planCard(
                action, scope=scope is None, band=band is None,
                shared=readers, specialist=specialist)
            self._planLayout.addWidget(card)
            self._planActions.append(entry)
        if specialist:
            header, body, advanced = self._planAdvanced(specialist)
            self._planLayout.addWidget(header)
            self._planLayout.addWidget(body)
            for entry in self._planActions:
                for (owner, name), read in advanced.items():
                    if owner == entry['action']:
                        entry['editors'][name] = read
        self._planArea.setVisible(bool(actions))
        # R11. The note is about what the page is waiting for, so it has to
        # change once the wait is over -- otherwise it still asks for the
        # search directly above that search's own answer.
        self._planEmpty.setText(self._planEmptyText(suggested=True))
        # DP-514/DP-521. The plan is on screen, and Preview and Apply act
        # on it; with no plan, the note is the whole answer, said once (the
        # result line used to repeat it as `No repair needed.`).
        self._syncPlanButtons(searched=True, actions=bool(actions))
        self._repairReport.setText(
            self.tr('{0} found.').format(
                count_text(len(actions), 'repair action'))
            if actions else '')

    def _editedPlan(self):
        if not self._repairPlan:
            self._suggestRepair()
        plan = dict(self._repairPlan or {})
        actions = []
        for entry in self._planActions:
            params = dict(entry['params'])
            for name, read in entry['editors'].items():
                params[name] = read()
            actions.append({'action': entry['action'], 'params': params,
                            'enabled': entry['use'].isChecked()})
        plan['actions'] = actions
        return plan

    @qasync.asyncSlot()
    async def _previewRepair(self):
        return await self._previewRepairCore()

    async def _previewRepairCore(self):
        try:
            plan = self._editedPlan()
            preview = (await self._runPreparation(
                'geometry.repair.preview', {'plan': plan})).payload
        except (TypeError, ValueError) as error:
            await modal(QMessageBox.warning,
                        self._widget, self.tr('Repair plan'), str(error))
            return None
        self._repairPlan = plan
        self._repairPlan['expected_preview_digest'] = preview.get('preview_digest')
        # DP-89. A preview that will not render is still a preview: the
        # apply path downstream reads its entries, not this line. Report the
        # render failure here instead of raising out of the slot, which
        # killed the whole repair action and left the page looking idle.
        #
        # DP-300/PREP-03. One line of it is on the page; the document itself
        # is on `Details`, which is where a reader who wants the digest and
        # the per-action entries goes.
        whole = self._detail('preview', preview)
        _writes, effect = repair_effect_text(preview)
        self._repairReport.setText(' '.join(filter(None, (self.tr(
            '{0} previewed.').format(
            count_text(len(preview.get('entries') or ()), 'repair action')),
            effect)))
            if whole else self.tr(
                '{0} previewed. Part of what it reported could not be shown; '
                'Details… has the rest. The repair itself is '
                'unaffected.').format(count_text(
                    len(preview.get('entries') or ()), 'repair action')))
        return preview

    @qasync.asyncSlot()
    async def _applyRepairPlan(self):
        """Preview, write the plan as a new revision, and move on (DP-301)."""
        self.clearRefusal()
        preview = await self._previewRepairCore()
        if not preview:
            return
        if not preview.get('entries'):
            self.showProceedRefusal(self.tr(
                'No repair ran, so nothing was written. Nothing in the '
                'findings maps to a repair this geometry can run.'))
            return
        # DP-487. An action that changed nothing is said to have changed
        # nothing, and a plan in which every action did is not offered as a
        # revision to create.
        writes, effect = repair_effect_text(preview)
        if not writes:
            self.showProceedRefusal(effect)
            return
        answer = await modal(
            QMessageBox.question,
            self._widget, self.tr('Apply repair preview'),
            ' '.join(filter(None, (
                self.tr('{0} previewed.').format(
                    count_text(len(preview['entries']), 'repair action')),
                effect, self.tr('Create a new geometry revision?')))),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            await self._runPreparation('geometry.repair.apply',
                                       {'plan': self._repairPlan})
        except (FacadeError, OSError, RuntimeError, ValueError) as error:
            self.showProceedRefusal(self.tr(
                'The repairs were not written: {0}').format(
                    _errorDetail(error)))
            return
        self.refresh()
        # DP-301. The same route the footer takes: preparing the geometry is
        # what this step is for, so the press that prepares it finishes it.
        self.proceedRequested.emit()

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
            await modal(QMessageBox.warning,
                        self._widget, self.tr('Wrap controls'), str(error))
            return None
        preview = (await self._runPreparation(
            'geometry.wrap.preview', {'geometry_id': geometry_id, **parameters})).payload
        self._wrapPreview.setText(self._wrapPreviewText(preview))
        return preview

    def _wrapPreviewText(self, preview):
        coarse = preview.get('coarse_preview', {})
        text = self.tr(
            'Grid {0} × {1} × {2}; memory {3:.1f} MiB; '
            'coarse preview {4:,} cells').format(
                *preview['dimensions'], preview['memory_bytes'] / 1024 ** 2,
                coarse.get('cells', 0))
        if coarse.get('same_as_apply') is False:
            # DP-640. The preview grid is capped; say so rather than let a
            # coarse result stand for the wrap that will be written.
            text += self.tr(
                ' (previewed on a {0}-cell grid; applied on {1})').format(
                    coarse.get('resolution'), coarse.get('applied_resolution'))
        return text

    @qasync.asyncSlot()
    async def _applyWrap(self):
        """Wrap the geometry, and move on once it is wrapped (DP-301)."""
        self.clearRefusal()
        geometry_id = self._activeGeometryId()
        preview = await self._estimateWrapCore()
        if not geometry_id or preview is None:
            return
        answer = await modal(
            QMessageBox.warning,
            self._widget, self.tr('Apply wrap'),
            self._wrapAcceptanceSummary(preview),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            await self._runPreparation(
                'geometry.wrap.apply', {'geometry_id': geometry_id,
                                        **self._wrapParameters()})
        except (FacadeError, OSError, RuntimeError, ValueError) as error:
            self.showProceedRefusal(self.tr(
                'The wrap was not written: {0}').format(_errorDetail(error)))
            return
        self.refresh()
        self.proceedRequested.emit()

    def _wrapAcceptanceSummary(self, preview):
        coarse = preview['coarse_preview']
        transfer = coarse['patch_transfer']
        recovery = transfer.get('per_patch_recovery_fraction', {})
        patch_rows = '\n'.join(
            self.tr('  Patch {0}: {1:.1%} area recovered').format(patch, fraction)
            for patch, fraction in sorted(recovery.items()))
        if not patch_rows:
            patch_rows = self.tr('  No named source patches were available.')
        grid = ''
        if coarse.get('same_as_apply') is False:
            # DP-640. The acceptance numbers come from the capped grid.
            grid = self.tr(
                '  This preview was meshed on a {0}-cell grid; the wrap is '
                'applied on a {1}-cell grid, so its triangles, deviation and '
                'patch recovery will differ.\n').format(
                    coarse.get('resolution'), coarse.get('applied_resolution'))
        return self.tr(
            'Wrapping changes geometry.\n\n'
            'Coarse acceptance preview:\n'
            '{7}'
            '  Watertight: {0}\n'
            '  Triangles: {1:,}\n'
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
                patch_rows, grid)

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
        # DP-126. Defence in depth for the disabled button: the facade
        # refuses this outright (DP-125), and asking for a justification it
        # will throw away is worse than saying so here.
        fatal, engine = self._engineFatal()
        if fatal:
            QMessageBox.warning(
                self._widget, self.tr('Geometry preparation'),
                self.tr('This geometry cannot be meshed by {0} as it is: '
                        '{1}. Repair or wrap it, or choose the other '
                        'mesher.').format(
                            humanise_option(engine) if engine
                            else self.tr('the chosen mesher'),
                            ', '.join(kind.replace('_', ' ')
                                      for kind in fatal)))
            return
        state = self._worstState([
            item['diagnostics']['readiness']['state']
            for item in (self._report or {}).get('geometries', [])])
        if state not in _JUSTIFIED_STATES:
            # DP-301. Nothing here needs a written reason, so this press is
            # the whole decision -- and the shell already knows how to turn a
            # decision into a prepared revision, a settled task and the next
            # open row. Asking for that route is how the page control and the
            # footer stay one route rather than two.
            self.proceedRequested.emit()
            return
        # R197. This asks for a justification a reviewer reads later
        # and offered a single-line box to write it in. Same prompt,
        # same dialog as the quality gate's `Accept anyway`.
        reason = JustificationDialog.ask(
            self._widget, self.tr('Acknowledge geometry risk'),
            self.tr('Explain why this geometry should be used without '
                    'preparation. This is recorded against the case.'))
        if not reason:
            return
        parameters = {'decision': 'as_is', 'ack_reason': reason}

        # C31-12. Decide, then freeze, then redraw -- the same three steps in
        # the same order, each one starting when the one before it answered.
        # A refusal used to leave the exception to whatever called the slot,
        # which showed the user nothing; it is now named where it happened.
        def decided(result):
            if isinstance(result, FailedResult):
                self.showProceedRefusal(self.tr(
                    'The decision was not recorded: {0}').format(
                        result.message))
                return
            self._freezePreparedGeometry({'decision': parameters['decision'],
                                          'ack_reason': reason},
                                         then=self._asIsRecorded)

        submit(app.facadeClient, 'geometry.preparation.decide', parameters,
               then=decided)

    def _asIsRecorded(self):
        """Redraw, then take the route the footer takes (DP-301)."""
        self.refresh()
        self.proceedRequested.emit()

    # -- saying why the page did not move (DP-301) --------------------------- #

    def _messageLabels(self):
        """The refusal line of each choice, in the order the tabs stand."""
        return (self._repairMessage, self._wrapMessage, self._asIsMessage)

    def clearRefusal(self):
        """Take down whatever the last refused press left on screen."""
        for label in self._messageLabels():
            label.setText('')
            label.setVisible(False)

    def showProceedRefusal(self, text):
        """Say why nothing moved, beside the control that was pressed.

        DP-301. The footer says it in the status bar, which is where the
        footer is. A control halfway up the page is not, and a sentence that
        appears for ten seconds at the bottom of the window is not an answer
        to a press that happened somewhere else.
        """
        self.clearRefusal()
        labels = self._messageLabels()
        index = self._choices.currentIndex()
        label = labels[index] if 0 <= index < len(labels) else labels[0]
        label.setText(str(text))
        label.setVisible(True)

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
        order = GeometryRepairPage._STATE_ORDER
        return max(states, key=order.index) if states else 'blocked'


#: DP-522. The re-triangulation settings, named with their units. The
#: generic spelling gave `Linear deflection` with no unit at all -- on a
#: panel whose own number had been applied in millimetres (DP-520) -- and
#: `Angular deflection deg`, the parameter's suffix read out as a word.
_PARAMETER_LABELS = {
    'linear_deflection': 'Linear deflection (m)',
    'angular_deflection_deg': 'Angular deflection (°)',
    # DP-530. The sewing and fixing tolerance, in the metres the plan
    # suggests it in and the healing backend now converts from. It stood
    # unlabelled, and was applied in whatever unit the CAD reader handed
    # back -- millimetres for a STEP, metres for a repaired revision.
    'tolerance': 'Tolerance (m)',
}


def _parameterLabel(name: str) -> str:
    """The label a repair parameter's editor stands beside."""
    label = _PARAMETER_LABELS.get(name)
    if label is None:
        return humanise_option(name)
    return QCoreApplication.translate('GeometryRepairPage', label)


def _riskLabel(action: str) -> str:
    """Which risk band an action belongs to, in the words the page shows.

    Plan 26 WP7.4. Grouping repair actions by risk lets a user tell what is
    safe to apply blindly from what moves geometry. CAD healing ids are not in
    the tessellated catalogue at all -- they apply before tessellation, which
    is band 4 -- so they are named here rather than looked up.
    """
    band = _riskBand(action)
    return f'{band} — {REPAIR_BANDS[band][0]}' if band in REPAIR_BANDS else '-'


def _riskDetail(action: str) -> str:
    band = _riskBand(action)
    return REPAIR_BANDS[band][1] if band in REPAIR_BANDS else ''


def _riskBand(action: str) -> int:
    if str(action).startswith('cad.'):
        return 4
    spec = TESSELLATED_ACTIONS.get(action)
    return spec.band if spec is not None else -1
