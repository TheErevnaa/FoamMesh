"""How many fluid regions? The Domain & Regions page asks, and finds them.

Plan 36 RP7, the GUI half of `geometry.fluid_regions.detect` and
`geometry.fluid_regions.apply` (§4.4 of the plan). Three steps, in the
settings column under the regions table:

1. **Ask.** How many fluid regions there are, and whether the flow is
   external. A fluid region is a space the surfaces close off, which is
   the one thing a user can count without knowing how snappy finds it.
2. **Detect.** The facade labels the domain on its worker thread; this
   panel only listens to the job's progress events, so the viewport keeps
   answering and Cancel stops the run.
3. **Review.** Every space found, largest first, the proposed ones ticked.
   A row can be renamed, retyped, unticked, or ticked to swap it in. When
   the count does not fit, the panel says why, in the geometry's terms,
   and offers what can be done about it. **Accept all** writes the ticked
   rows through one `apply` call, so one Undo takes them all back.

Plan 36 RP10 adds the review's other two actions (section 4.4 step 4).
**Adjust...** opens the selected row in the regions editor, where its seed
can be dragged, renamed or retyped; what is accepted there is kept for that
row, and nothing is written until Accept all. **Merge** takes two selected
rows and drops the second seed -- but only when the viewport's labelling of
the domain confirms both seeds are in one space (RP13 #8). Two rows in
different spaces are two spaces: dropping a seed would drop a space from the
mesh, so the panel says so and merges nothing. Every button names its
shortcut in its tooltip, and each row says its space in words as well as in
its colour chip.

The viewport half -- each candidate drawn as a translucent volume -- belongs
to the region volumes (RP6). This panel publishes what it would draw through
:attr:`RegionDetectionPanel.candidatesChanged` and an optional highlighter
callable, and draws nothing itself.
"""
from __future__ import annotations

import uuid

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QProgressBar, QPushButton, QSpinBox, QStackedWidget, QStyle,
    QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from foammesh.core.mesh.presentation import count_text
from foammesh.view.facade_client import query, submit
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT, CompactSpinBox
from foammesh.view.theming.patch_palette import zone_colour
from foammesh.view.theming.status_colors import set_status

#: The operations this panel drives.
DETECT = 'geometry.fluid_regions.detect'
APPLY = 'geometry.fluid_regions.apply'
OFFER = 'geometry.fluid_regions.offer'

#: What a region can be. The facade's `RegionType` values, in user words.
REGION_TYPES = (('fluid', 'Fluid'), ('solid', 'Solid'))

#: A space under this share of the largest one is noise (plan §4.4).
NOISE_FRACTION = 0.01

#: The stage names the detector reports, in user words.
STAGES = {
    'distance': 'measuring the distance to the surfaces',
    'labelling': 'finding the closed-off spaces',
    'band': 'finding the closed-off spaces',
    'depth': 'finding the middle of each space',
    'surfaces': 'measuring each space',
}


#: Where each stage sits on the one bar, ``(start, end)``: the detector
#: reports each stage's own fraction, and the distance field is most of it.
STAGE_SPANS = {
    'distance': (0.0, 0.6),
    'labelling': (0.6, 0.7),
    'band': (0.7, 0.75),
    'depth': (0.75, 0.9),
    'surfaces': (0.9, 1.0),
}


# -- words ------------------------------------------------------------------ #

def format_volume(volume) -> str:
    """``0.0100 m³``: four decimals, the precision the plan's sentence uses."""
    value = float(volume or 0.0)
    if value and abs(value) < 1e-4:
        return f'{value:.2e} m³'
    return f'{value:.4f} m³'


def format_depth(depth) -> str:
    """``3.2 cm``: a distance in the unit a person would say it in."""
    value = float(depth or 0.0)
    if value >= 1.0:
        return f'{value:.2f} m'
    if value >= 0.01:
        return f'{value * 100:.1f} cm'
    return f'{value * 1000:.1f} mm'


def _spaces(count) -> str:
    return count_text(count, 'space', 'spaces')


def _regions(count) -> str:
    return count_text(count, 'fluid region', 'fluid regions')


def mismatch_text(payload) -> str:
    """The sentence the review step opens with, or ``''`` when all fits.

    The plan's wording (§4.4), keyed on the reason the facade names from the
    data. Numbers come from the payload, never from the reason alone, so the
    sentence cannot claim a space the detector did not find.
    """
    mismatch = (payload or {}).get('mismatch')
    if not mismatch:
        return ''
    reason = mismatch.get('reason')
    if reason == 'farfield_is_the_fluid':
        # DP-915: Gmsh's far field cuts the bodies out; the outside is the
        # one fluid region and nothing is proposed. Plan 37 UF14: the far
        # field is a box, sphere or cylinder, so the fallback names the
        # shape when the payload carries it and no shape when it does not.
        shape = str(payload.get('farfield_shape') or '').strip()
        field = f'far-field {shape}' if shape else 'far field'
        return str(payload.get('note') or
                   f'The {field} is on: the bodies are cut out of it, '
                   'and the fluid is the one space around them.')
    asked = int(mismatch.get('asked') or 0)
    found = int(mismatch.get('found') or 0)
    rows = {row['id']: row for row in payload.get('spaces') or ()}
    # RP13 #4: a thin space is counted, as the facade counts it.
    enclosed = [row for row in rows.values() if not row.get('outside')]
    total = sum(float(row.get('volume') or 0.0) for row in enclosed)
    if reason == 'more_spaces':
        proposed = [rows[key] for key in payload.get('proposed') or ()
                    if key in rows]
        others = [row for row in enclosed if row not in proposed]
        largest = max((float(row['volume']) for row in enclosed), default=0.0)
        noun = 'enclosed spaces' if not any(
            row.get('outside') for row in proposed) else 'spaces'
        text = (f'Found {found} {noun}. The {len(proposed)} largest are '
                f'proposed.' if len(proposed) != 1 else
                f'Found {found} {noun}. The largest is proposed.')
        if others:
            small = all(float(row['volume']) < NOISE_FRACTION * largest
                        for row in others)
            if small:
                text += (f' The other {len(others)} are smaller than 1% of '
                         'the largest (listed below); they may be gaps '
                         'between parts.')
            else:
                text += (f' The other {len(others)} are listed below; tick '
                         'one to use it instead.')
        return text
    if reason == 'no_closed_surface' or (
            reason == 'domain_cuts_geometry' and not enclosed):
        return ('No enclosed space: the surface has open edges, or the '
                'domain box cuts through the geometry.')
    if reason == 'domain_cuts_geometry':
        return ('The domain box cuts through the geometry, so any space it '
                'cuts open is part of the outside. Make the domain box '
                'larger than the geometry, then detect again.')
    encloses = (f'The geometry encloses {_spaces(len(enclosed))} '
                f'({format_volume(total)}).' if enclosed else
                'The geometry encloses no space.')
    text = f'You asked for {_regions(asked)}. {encloses}'
    if reason == 'core_open_to_outside':
        text += (" The pipe's core is open at both ends, so it is part of "
                 'the outside.')
    elif reason == 'surface_leaks':
        text += (' The surface has a gap, so a space that should be closed '
                 'leaks into the outside. Close the gap in Preparation.')
    return text


# -- the chip ---------------------------------------------------------------- #

def chip_icon(colour, size: int = 12) -> QIcon:
    """A small filled square of ``colour``; an empty icon for ``None``."""
    if not colour:
        return QIcon()
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(colour))
    return QIcon(pixmap)


# -- offered once per case (D7) -------------------------------------------- #

def offer_detection(client) -> bool:
    """D7: whether the panel opens by itself, noting the offer when it does.

    The facade answers (`geometry.fluid_regions.offer`): once per case, while
    it has no regions and a closed surface. The offer is noted there, in the
    case's cache folder, so the view writes nothing. A client without a case
    -- or one that cannot answer -- is not asked.
    """
    try:
        result = query(client, OFFER, {'record': True})
    except Exception:  # noqa: BLE001 - no case, no offer
        return False
    if getattr(result, 'status', '') != 'accepted':
        return False
    return bool((getattr(result, 'payload', None) or {}).get('offer'))


# -- the panel --------------------------------------------------------------- #

class RegionDetectionPanel(QFrame):
    """Ask how many fluid regions, find them, and write the ones accepted."""

    #: The panel has finished; True when regions were written.
    finished = Signal(bool)
    #: Accept all wrote these regions (the facade's ``regions`` rows).
    accepted = Signal(list)
    #: What the viewport should draw: one dict per listed space,
    #: ``{space, name, colour, seed, bounds, ticked, outside, volume}``,
    #: ``[]`` when there is nothing to show.
    candidatesChanged = Signal(list)
    #: The user asked to see the surface's open edges.
    showOpenEdgesRequested = Signal()
    #: Plan 36 RP10. Adjust... on this table row: open it in the editor.
    adjustRequested = Signal(int)

    #: Plan 36 RP10. The review's shortcuts; each tooltip names its own.
    ADJUST_SHORTCUT = 'Alt+J'
    MERGE_SHORTCUT = 'Alt+G'
    ACCEPT_SHORTCUT = 'Ctrl+Return'

    ASK, DETECTING, REVIEW = range(3)

    def __init__(self, client, parent=None, *, existing_names=(),
                 existing_count: int = 0):
        super().__init__(parent)
        self.setObjectName('regionDetectionPanel')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._client = client
        self._existingNames = set(existing_names)
        self._existingCount = int(existing_count)
        # Plan 37 #1(b): Accept all replaces the regions already defined.
        self._keepExisting = False
        self._jobId = None
        #: RP13 #7: the request in flight and the case it was asked of; an
        #: answer for any other is late and dropped.
        self._requestId = None
        self._requestCase = None
        self._unsubscribe = []
        self._payload = None
        self._external = False
        self._highlighter = None
        self._filling = False
        self._edited = set()
        #: Plan 36 RP10. Per space id, the seed an Adjust moved it to.
        self._moved = {}
        #: ``lookup(point) -> space label or None``: what Merge asks.
        self._spaceLookup = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT,
                                  MARGIN_TIGHT, MARGIN_TIGHT)
        layout.setSpacing(GAP_TIGHT)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)
        self._stack.addWidget(self._buildAsk())
        self._stack.addWidget(self._buildDetecting())
        self._stack.addWidget(self._buildReview())

    # -- building ------------------------------------------------------------ #

    def _buildAsk(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP_TIGHT)
        row = QHBoxLayout()
        question = QLabel(self.tr('How many fluid regions are there?'), page)
        question.setWordWrap(True)
        self._count = CompactSpinBox(page)
        self._count.setObjectName('regionDetectionCount')
        self._count.setRange(1, 64)
        self._count.setValue(1)
        question.setBuddy(self._count)
        row.addWidget(question, 1)
        row.addWidget(self._count)
        layout.addLayout(row)
        self._externalBox = QCheckBox(
            self.tr('The flow is external (around the body)'), page)
        self._externalBox.setObjectName('regionDetectionExternal')
        layout.addWidget(self._externalBox)
        help_line = QLabel(self.tr(
            'A space closed off by the surfaces. A jacketed pipe has two: '
            'the pipe and the jacket.'), page)
        help_line.setWordWrap(True)
        layout.addWidget(help_line)
        self._askError = QLabel(page)
        self._askError.setWordWrap(True)
        self._askError.hide()
        layout.addWidget(self._askError)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._detect = QPushButton(self.tr('Detect'), page)
        self._detect.setObjectName('regionDetectionDetect')
        self._detect.setDefault(True)
        self._close = QPushButton(self.tr('Close'), page)
        buttons.addWidget(self._detect)
        buttons.addWidget(self._close)
        layout.addLayout(buttons)
        self._detect.clicked.connect(self.detect)
        self._close.clicked.connect(lambda: self._finish(False))
        return page

    def _buildDetecting(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP_TIGHT)
        layout.addWidget(QLabel(self.tr('Detecting fluid spaces…'), page))
        self._progress = QProgressBar(page)
        self._progress.setRange(0, 0)
        self._progress.setObjectName('regionDetectionProgress')
        layout.addWidget(self._progress)
        self._stage = QLabel(page)
        self._stage.setWordWrap(True)
        layout.addWidget(self._stage)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel = QPushButton(self.tr('Cancel'), page)
        self._cancel.setObjectName('regionDetectionCancel')
        buttons.addWidget(self._cancel)
        layout.addLayout(buttons)
        self._cancel.clicked.connect(self.cancelDetection)
        return page

    def _buildReview(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP_TIGHT)
        self._message = QLabel(page)
        self._message.setObjectName('regionDetectionMessage')
        self._message.setWordWrap(True)
        layout.addWidget(self._message)
        self._choices = QHBoxLayout()
        self._choices.setSpacing(GAP_TIGHT)
        self._use = QPushButton(page)
        self._useExternal = QPushButton(
            self.tr('Include the outside as external flow'), page)
        self._treatExternal = QPushButton(
            self.tr('Treat as external flow'), page)
        self._openEdges = QPushButton(self.tr('Show open edges'), page)
        choices = QWidget(page)
        choices.setLayout(self._choices)
        # Two per line at most: the settings column is narrow.
        for button in (self._use, self._useExternal, self._treatExternal,
                       self._openEdges):
            button.hide()
            self._choices.addWidget(button)
        self._choices.addStretch(1)
        layout.addWidget(choices)

        self._table = QTableWidget(0, 4, page)
        self._table.setObjectName('regionDetectionTable')
        self._table.setHorizontalHeaderLabels([
            self.tr('Region'), self.tr('Type'), self.tr('Volume'),
            self.tr('To wall')])
        self._table.verticalHeader().hide()
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # DP-921: the Type column holds a combo box, which ResizeToContents
        # sizes to the combo's own size hint -- but the cell hands the combo
        # its text rect, which the theme's `::item { padding }` shrinks, so
        # under the theme "Fluid" read "Fluic". `_fitTypeColumn` sets it.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        for column in (2, 3):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemChanged.connect(self._itemChanged)
        self._table.setAccessibleName(self.tr('Spaces found'))
        self._table.setAccessibleDescription(self.tr(
            'One row per space. Tick a row to keep it; each kept row is '
            'written as a region. Select two rows to merge them.'))
        layout.addWidget(self._table)

        self._reviewError = QLabel(page)
        self._reviewError.setObjectName('regionDetectionNote')
        self._reviewError.setWordWrap(True)
        self._reviewError.hide()
        layout.addWidget(self._reviewError)
        # Plan 36 RP10. Adjust and Merge on a row of their own: beside
        # Accept all, Back and Cancel they would not fit the 280 px column.
        tools = QHBoxLayout()
        tools.setSpacing(GAP_TIGHT)
        self._adjust = QPushButton(self.tr('Adjust…'), page)
        self._adjust.setObjectName('regionDetectionAdjust')
        self._adjust.setShortcut(QKeySequence(self.ADJUST_SHORTCUT))
        self._adjust.setToolTip(self.tr(
            'Open the selected row in the regions editor, to drag its seed, '
            'rename or retype it (%s).') % self.ADJUST_SHORTCUT)
        self._merge = QPushButton(self.tr('Merge'), page)
        self._merge.setObjectName('regionDetectionMerge')
        self._merge.setShortcut(QKeySequence(self.MERGE_SHORTCUT))
        self._merge.setToolTip(self.tr(
            'Make two selected rows one region: the second seed is dropped, '
            'but only when both seeds are in the same space (%s).')
            % self.MERGE_SHORTCUT)
        tools.addWidget(self._adjust)
        tools.addWidget(self._merge)
        tools.addStretch(1)
        layout.addLayout(tools)
        self._adjust.clicked.connect(self._adjustSelected)
        self._merge.clicked.connect(self.mergeSelected)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._accept = QPushButton(self.tr('Accept all'), page)
        self._accept.setObjectName('regionDetectionAccept')
        self._accept.setShortcut(QKeySequence(self.ACCEPT_SHORTCUT))
        self._accept.setToolTip(self.tr(
            'Write every ticked row as a region, in one step: one Undo '
            '(Ctrl+Z) takes them all back (%s).') % self.ACCEPT_SHORTCUT)
        self._back = QPushButton(self.tr('Back'), page)
        self._reviewCancel = QPushButton(self.tr('Cancel'), page)
        for button in (self._accept, self._back, self._reviewCancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._accept.clicked.connect(self.acceptAll)
        self._back.clicked.connect(self.back)
        self._reviewCancel.clicked.connect(lambda: self._finish(False))
        self._use.clicked.connect(self._useFound)
        self._useExternal.clicked.connect(lambda: self._redetect(True))
        self._treatExternal.clicked.connect(lambda: self._redetect(True))
        self._openEdges.clicked.connect(self.showOpenEdgesRequested)
        return page

    # -- state ---------------------------------------------------------------- #

    def step(self) -> int:
        return self._stack.currentIndex()

    def isDetecting(self) -> bool:
        return self._jobId is not None

    def payload(self):
        return self._payload

    def countBox(self) -> QSpinBox:
        return self._count

    def externalBox(self) -> QCheckBox:
        return self._externalBox

    def table(self) -> QTableWidget:
        return self._table

    def messageText(self) -> str:
        return self._message.text()

    def choiceButtons(self) -> dict:
        """The mismatch buttons on show, by their text."""
        return {button.text(): button
                for button in (self._use, self._useExternal,
                               self._treatExternal, self._openEdges)
                if not button.isHidden()}

    def setHighlighter(self, highlighter) -> None:
        """``highlighter(candidates)`` is told what to draw; ``None`` stops it.

        The hook RP6's region volumes take: it is called with the same list
        :attr:`candidatesChanged` carries, and with ``[]`` when the panel
        closes, so whatever it drew goes with it.
        """
        self._highlighter = highlighter
        self._publishCandidates()

    # -- ask ----------------------------------------------------------------- #

    def start(self, count=None, external=None) -> None:
        """Show the ask step (the Detect… button, and the automatic open)."""
        if count is not None:
            self._count.setValue(int(count))
        if external is not None:
            self._externalBox.setChecked(bool(external))
        self._askError.hide()
        self._stack.setCurrentIndex(self.ASK)
        self._publishCandidates()

    def detect(self) -> None:
        """Run the detector off the GUI thread; progress arrives as events."""
        if self._jobId is not None:
            return
        self._external = self._externalBox.isChecked()
        self._jobId = f'fluid-regions-gui-{uuid.uuid4().hex[:12]}'
        request = self._requestId = uuid.uuid4().hex
        case = self._requestCase = self._clientCase()
        self._progress.setRange(0, 0)
        self._stage.setText('')
        self._stack.setCurrentIndex(self.DETECTING)
        self._subscribe()
        parameters = {'count': int(self._count.value()),
                      'external': bool(self._external),
                      'job_id': self._jobId, 'request_id': request}
        submit(self._client, DETECT, parameters,
               then=lambda result: self._detected(result, request, case))

    def _clientCase(self):
        """The client's case id now, or ``None`` when it has none."""
        try:
            case = getattr(self._client, 'case_id', None)
            case = case() if callable(case) else case
        except Exception:  # noqa: BLE001 - no case open
            return None
        return None if case is None else str(case)

    def _late(self, result, request, case) -> bool:
        """RP13 #7: is *result* for a request no longer asked?

        It is when the panel closed or asked again since (*request* is not
        the one in flight), when the client moved to another case, or when
        the answer names another case or request than the one it was sent
        for.
        """
        if request is None:
            return False            # not from `detect`: a direct delivery
        if request != self._requestId:
            return True
        # A case asked of and now closed (no id) or replaced is gone.
        if case is not None and self._clientCase() != case:
            return True
        payload = getattr(result, 'payload', None) or {}
        named = payload.get('case_id')
        if case is not None and named is not None and str(named) != case:
            return True
        asked = payload.get('request_id')
        return asked is not None and str(asked) != request

    def _subscribe(self) -> None:
        subscribe = getattr(self._client, 'subscribe', None)
        if subscribe is None:
            return
        from foammesh.core.project import Event

        try:
            self._unsubscribe = [
                subscribe(Event.JOB_PROGRESS, self._onProgress)]
        except Exception:  # noqa: BLE001 - no case, no progress; still runs
            self._unsubscribe = []

    def _unsubscribeAll(self) -> None:
        for unsubscribe in self._unsubscribe:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribe = []

    def _onProgress(self, *, job_id=None, stage=None, fraction=None,
                    **_kwargs) -> None:
        if job_id != self._jobId:
            return
        if fraction is not None:
            start, end = STAGE_SPANS.get(str(stage), (0.0, 1.0))
            overall = start + (end - start) * min(max(float(fraction), 0.0), 1.0)
            if self._progress.maximum() == 0:
                self._progress.setRange(0, 100)
                self._progress.setValue(0)
            # A refining run starts the distance field again; the bar does
            # not go backwards for it.
            self._progress.setValue(max(self._progress.value(),
                                        int(round(overall * 100))))
        if stage:
            words = STAGES.get(str(stage), str(stage))
            self._stage.setText(self.tr('Now %s.') % words)

    def cancelDetection(self) -> None:
        """Stop the run. The facade answers ``detection_cancelled``."""
        job_id = self._jobId
        if job_id is None:
            return
        from foammesh.core.geometry.diagnostics import budget as budget_module

        self._cancel.setEnabled(False)
        self._stage.setText(self.tr('Stopping…'))
        budget_module.cancel(job_id)

    def _detected(self, result, request=None, case=None) -> None:
        if self._late(result, request, case):
            return
        self._requestId = self._requestCase = None
        self._unsubscribeAll()
        self._jobId = None
        self._cancel.setEnabled(True)
        if getattr(result, 'status', None) == 'failed':
            error = (result.payload or {}).get('error')
            self._stack.setCurrentIndex(self.ASK)
            if error == 'detection_cancelled':
                self._askError.setText(self.tr('Detection was cancelled.'))
                set_status(self._askError, None)
            else:
                self._askError.setText(
                    self.tr('The spaces could not be found: %s')
                    % (result.message or self.tr('the detector stopped')))
                set_status(self._askError, 'error')
            self._askError.show()
            return
        self._payload = dict(getattr(result, 'payload', None) or {})
        self._edited = set()
        self._moved = {}
        self._stack.setCurrentIndex(self.REVIEW)
        self._fillReview()

    # -- review --------------------------------------------------------------- #

    def _fillReview(self) -> None:
        payload = self._payload or {}
        spaces = list(payload.get('spaces') or ())
        proposed = [int(key) for key in payload.get('proposed') or ()]
        # Proposed first, in the order they were proposed; then the rest.
        order = proposed + [row['id'] for row in spaces
                            if row['id'] not in proposed]
        rows = {row['id']: row for row in spaces}
        self._filling = True
        try:
            self._table.setRowCount(0)
            for space_id in order:
                row = rows.get(space_id)
                if row is None:
                    continue
                self._appendRow(row, space_id in proposed)
        finally:
            self._filling = False
        self._renumber()
        self._fitTypeColumn()
        self._showMessage()

    def _typeColumnInset(self) -> int:
        """What the cell takes off the width it gives its combo box.

        The item delegate places a cell widget in the style's item text
        rect, so the theme's item padding (and the grid line) come off it.
        """
        probe = QStyleOptionViewItem()
        probe.rect = QRect(0, 0, 200, 40)
        text = self._table.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, probe, self._table)
        inset = max(0, probe.rect.width() - text.width())
        return inset + (1 if self._table.showGrid() else 0)

    def _fitTypeColumn(self) -> None:
        """DP-921: make the Type column wide enough for its widest combo."""
        widest = self._table.horizontalHeader().sectionSizeFromContents(
            1).width()
        for index in range(self._table.rowCount()):
            kind = self._table.cellWidget(index, 1)
            if kind is None:
                continue
            kind.ensurePolished()
            widest = max(widest,
                         kind.sizeHint().width() + self._typeColumnInset())
        self._table.setColumnWidth(1, widest)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (event.type() in (QEvent.Type.StyleChange, QEvent.Type.FontChange)
                and getattr(self, '_table', None) is not None):
            self._fitTypeColumn()

    def _appendRow(self, row, ticked) -> None:
        index = self._table.rowCount()
        self._table.insertRow(index)
        name = QTableWidgetItem('')
        name.setData(Qt.ItemDataRole.UserRole, int(row['id']))
        name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                      | Qt.ItemFlag.ItemIsEditable
                      | Qt.ItemFlag.ItemIsUserCheckable)
        name.setCheckState(Qt.CheckState.Checked if ticked
                           else Qt.CheckState.Unchecked)
        self._table.setItem(index, 0, name)

        kind = QComboBox(self._table)
        for value, label in REGION_TYPES:
            kind.addItem(self.tr(label), value)
        kind.currentIndexChanged.connect(lambda _index: self._publishCandidates())
        self._table.setCellWidget(index, 1, kind)

        volume = QTableWidgetItem(format_volume(row.get('volume')))
        depth_text = format_depth(row.get('depth'))
        depth = QTableWidgetItem(depth_text)
        tip = self.tr('%s from the nearest wall') % depth_text
        if row.get('too_thin'):
            depth.setText(self.tr('%s, too thin') % depth_text)
            tip += self.tr('. Too thin for the finest cell the case refines '
                           'to: snappy cannot fit a cell into it.')
        elif row.get('resolution_warning'):
            # RP13 #4 / DP-863: kept and counted, but not assured.
            depth.setText(self.tr('%s, thin') % depth_text)
            tip += self.tr('. Thinner than the base cell or than the '
                           'detection could resolve: only refinement '
                           'around it fits cells into it. Check it meshes.')
        if row.get('outside'):
            volume.setToolTip(self.tr(
                'The outside: the space around the body, open to the '
                'domain box'))
        depth.setToolTip(tip)
        for item in (volume, depth):
            item.setFlags(Qt.ItemFlag.ItemIsEnabled
                          | Qt.ItemFlag.ItemIsSelectable)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(index, 2, volume)
        self._table.setItem(index, 3, depth)

    def _space(self, space_id):
        for row in (self._payload or {}).get('spaces') or ():
            if row['id'] == space_id:
                return row
        return None

    def _rows(self):
        """``(table_row, space_row, name_item, ticked)`` in table order."""
        for index in range(self._table.rowCount()):
            item = self._table.item(index, 0)
            if item is None:
                continue
            space = self._space(item.data(Qt.ItemDataRole.UserRole))
            yield (index, space, item,
                   item.checkState() == Qt.CheckState.Checked)

    def _renumber(self) -> None:
        """Give ticked rows the names `apply` would, and each its colour.

        A name the user typed is kept. Unticked rows say which space they
        are; colours follow the order the regions will be written in, after
        the case's existing regions (RP8's numeric order).
        """
        taken = set(self._existingNames) if self.keepsExisting() else set()
        for _index, _space, item, ticked in self._rows():
            if ticked and id(item) in self._edited:
                taken.add(item.text().strip())
        slot = self._existingSlot()
        self._filling = True
        try:
            for _index, space, item, ticked in self._rows():
                label = self._spaceLabel(space)
                if ticked:
                    if id(item) not in self._edited or not item.text().strip():
                        self._edited.discard(id(item))
                        number = 1
                        while f'fluid_{number}' in taken:
                            number += 1
                        item.setText(f'fluid_{number}')
                        taken.add(item.text())
                    item.setIcon(chip_icon(zone_colour(slot)))
                    item.setToolTip(label)
                    # Plan 36 RP10: not colour alone -- the row says which
                    # space it is, as its viewport label does.
                    item.setData(Qt.ItemDataRole.AccessibleDescriptionRole,
                                 self.tr('Kept, %s, %s') % (
                                     label, format_volume(space.get('volume')
                                                          if space else None)))
                    slot += 1
                else:
                    if id(item) not in self._edited:
                        item.setText(label)
                    item.setIcon(QIcon())
                    item.setToolTip(self.tr('Tick to use this space'))
                    item.setData(Qt.ItemDataRole.AccessibleDescriptionRole,
                                 self.tr('Not kept, %s') % label)
        finally:
            self._filling = False
        ticked = any(True for *_rest, on in self._rows() if on)
        self._accept.setEnabled(ticked)
        self._publishCandidates()

    def _spaceLabel(self, space) -> str:
        if space is None:
            return ''
        if space.get('outside'):
            return self.tr('Outside (space %d)') % space['id']
        return self.tr('Space %d') % space['id']

    def _itemChanged(self, item) -> None:
        if self._filling or item.column() != 0:
            return
        space = self._space(item.data(Qt.ItemDataRole.UserRole))
        if item.text().strip() and item.text() != self._spaceLabel(space):
            self._edited.add(id(item))
        self._renumber()

    def _showMessage(self) -> None:
        payload = self._payload or {}
        text = mismatch_text(payload)
        mismatch = payload.get('mismatch') or {}
        reason = mismatch.get('reason')
        found = int(mismatch.get('found') or 0)
        if not text and payload.get('note'):
            # DP-915: the answer came with its own sentence (Gmsh's
            # far-field box is the fluid, or it is off in external flow).
            text = str(payload['note'])
        if not text:
            count = len(payload.get('proposed') or ())
            text = (self.tr('Found %s. Tick the ones to keep, rename them if '
                            'you like, then Accept all.')
                    % (_spaces(count)))
        # Plan 37 #1 / #2: External on a box flush with the geometry, on both
        # sides of one closed surface, or around a duct -- said before Accept.
        placement = [str(one.get('message'))
                     for one in payload.get('placement_warnings') or ()
                     if isinstance(one, dict) and one.get('message')]
        if placement:
            text = '\n\n'.join([text] + [self.tr('Warning: %s') % line
                                         for line in placement])
        self._message.setText(text)
        set_status(self._message, 'warning' if (mismatch and
                   reason != 'more_spaces') or placement else None)
        for button in (self._use, self._useExternal, self._treatExternal,
                       self._openEdges):
            button.hide()
        none_found = reason == 'no_closed_surface' or (
            reason == 'domain_cuts_geometry' and not payload.get('proposed'))
        if reason in ('core_open_to_outside', 'fewer_spaces',
                      'surface_leaks') and found:
            self._use.setText(self.tr('Use %d') % found)
            self._use.show()
        if reason in ('core_open_to_outside', 'fewer_spaces') \
                and not self._external:
            self._useExternal.show()
        if none_found:
            self._openEdges.show()
            if not self._external:
                self._treatExternal.show()
        elif reason == 'surface_leaks':
            self._openEdges.show()
        self._reviewError.hide()

    def _useFound(self) -> None:
        """[Use N]: take what was found as the answer, and ask for no more."""
        found = int(((self._payload or {}).get('mismatch') or {}).get(
            'found') or 0)
        if found:
            self._count.setValue(found)
        self._message.setText(self.tr(
            'Using %s. Tick the ones to keep, then Accept all.')
            % _spaces(found))
        set_status(self._message, None)
        for button in (self._use, self._useExternal, self._treatExternal):
            button.hide()

    def _redetect(self, external: bool) -> None:
        self._externalBox.setChecked(bool(external))
        self.detect()

    def back(self) -> None:
        self.start()

    def ticked(self) -> list:
        """The rows Accept all writes, as `apply` takes them."""
        regions = []
        for index, space, item, ticked in self._rows():
            if not ticked or space is None:
                continue
            kind = self._table.cellWidget(index, 1)
            regions.append({'name': item.text().strip(),
                            'type': kind.currentData() if kind else 'fluid',
                            'point': list(self._seedOf(space))})
        return regions

    # -- Plan 36 RP10: Adjust and Merge ---------------------------------------- #

    def _seedOf(self, space):
        """The row's seed: where Adjust put it, else where it was found."""
        moved = self._moved.get(space['id'])
        return tuple(moved if moved is not None else space['seed'])

    def setSpaceLookup(self, lookup) -> None:
        """``lookup(point)`` answers the label of the space *point* is in
        (0 outside), or ``None`` when that is not known yet. Merge asks it
        of both seeds and merges only on the same non-zero answer."""
        self._spaceLookup = lookup

    def adjustButton(self) -> QPushButton:
        return self._adjust

    def mergeButton(self) -> QPushButton:
        return self._merge

    def acceptButton(self) -> QPushButton:
        return self._accept

    def note(self) -> str:
        """What Adjust or Merge last said, or ``''``."""
        return '' if self._reviewError.isHidden() else self._reviewError.text()

    def _say(self, text: str, status=None) -> None:
        self._reviewError.setText(text)
        set_status(self._reviewError, status)
        self._reviewError.setVisible(bool(text))

    def selectedRows(self) -> list:
        """The selected table rows, top to bottom."""
        model = self._table.selectionModel()
        rows = ({index.row() for index in model.selectedRows()}
                if model is not None else set())
        if not rows and self._table.currentRow() >= 0:
            rows = {self._table.currentRow()}
        return sorted(rows)

    def candidateAt(self, index: int):
        """``{name, type, point, space}`` of table row *index*, or None."""
        item = self._table.item(index, 0)
        if item is None:
            return None
        space = self._space(item.data(Qt.ItemDataRole.UserRole))
        if space is None:
            return None
        kind = self._table.cellWidget(index, 1)
        return {'name': item.text().strip(),
                'type': kind.currentData() if kind else 'fluid',
                'point': self._seedOf(space), 'space': space['id']}

    def setCandidate(self, index: int, *, name=None, kind=None,
                     point=None) -> None:
        """Keep what the editor accepted for row *index* (Adjust). Nothing
        is written: Accept all writes it with the rest."""
        item = self._table.item(index, 0)
        if item is None:
            return
        space = self._space(item.data(Qt.ItemDataRole.UserRole))
        if space is None:
            return
        if point is not None:
            self._moved[space['id']] = tuple(float(value) for value in point)
        combo = self._table.cellWidget(index, 1)
        if kind is not None and combo is not None:
            found = combo.findData(kind)
            if found >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(found)
                combo.blockSignals(False)
        self._filling = True
        try:
            if name is not None and str(name).strip():
                item.setText(str(name).strip())
                self._edited.add(id(item))
            item.setCheckState(Qt.CheckState.Checked)
        finally:
            self._filling = False
        self._renumber()
        self._say(self.tr('%s adjusted. Accept all writes it.')
                  % item.text().strip())

    def _adjustSelected(self) -> None:
        rows = self.selectedRows()
        if len(rows) != 1:
            self._say(self.tr('Select one row to adjust.'), 'warning')
            return
        self._say('')
        self.adjustRequested.emit(rows[0])

    def mergeSelected(self) -> bool:
        """Drop the second of two selected rows' seeds when one space holds
        both; otherwise say why not and change nothing (RP13 #8)."""
        rows = self.selectedRows()
        if len(rows) != 2:
            self._say(self.tr('Select two rows to merge (Ctrl+click, or '
                              'Shift with an arrow key).'), 'warning')
            return False
        first, second = (self.candidateAt(index) for index in rows)
        if first is None or second is None:
            return False
        labels = [None, None]
        if self._spaceLookup is not None:
            for slot, row in enumerate((first, second)):
                try:
                    labels[slot] = self._spaceLookup(row['point'])
                except Exception:  # noqa: BLE001 - unknown, not the same
                    labels[slot] = None
        if None in labels or 0 in labels:
            self._say(self.tr(
                'Cannot confirm that %s and %s are one space yet, so '
                'nothing was merged.') % (first['name'], second['name']),
                'warning')
            return False
        if labels[0] != labels[1]:
            self._say(self.tr(
                '%s and %s are different spaces: each needs its own seed, '
                'so nothing was merged.') % (first['name'], second['name']),
                'warning')
            return False
        item = self._table.item(rows[1], 0)
        self._filling = True
        try:
            item.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self._filling = False
        self._renumber()
        self._say(self.tr('Merged: %s and %s are one space; %s keeps the '
                          'seed.') % (first['name'], second['name'],
                                      first['name']))
        return True

    def setKeepsExisting(self, keep: bool) -> None:
        """Add the rows beside the regions already defined, not over them."""
        self._keepExisting = bool(keep)
        self._renumber()

    def keepsExisting(self) -> bool:
        """True when Accept all adds to the regions already defined."""
        return self._existingCount > 0 and self._keepExisting

    def replacesExisting(self) -> bool:
        """True when Accept all replaces regions the case already has."""
        return self._existingCount > 0 and not self._keepExisting

    def _existingSlot(self) -> int:
        """The colour slot the first written row takes."""
        return self._existingCount if self.keepsExisting() else 0

    def acceptAll(self) -> None:
        """Write the ticked rows in one `apply`: one Undo takes them back.

        Plan 37 #1(b): the rows replace the regions already defined (of the
        types written) -- detecting again is asking again; one Ctrl+Z brings
        the old ones back, and Add in the regions table still adds by hand. Before, detecting
        a second time added beside the first answer, so the stale seeds
        stayed -- or the new ones were refused as holding a taken space.
        """
        regions = self.ticked()
        if not regions:
            return
        self._accept.setEnabled(False)
        parameters = {'regions': regions}
        if not self.keepsExisting():
            parameters['replace'] = True
        detection = (self._payload or {}).get('detection_id')
        if detection and (self._payload or {}).get('source') != 'solids':
            # RP13 #3: the rows came from this detection; the facade refuses
            # them if the geometry or the box changed while they were shown.
            parameters['detection_id'] = detection
        submit(self._client, APPLY, parameters, then=self._applied)

    def _applied(self, result) -> None:
        self._accept.setEnabled(True)
        if getattr(result, 'status', None) == 'failed':
            self._reviewError.setText(
                self.tr('The regions were not written: %s') % result.message)
            set_status(self._reviewError, 'error')
            self._reviewError.show()
            return
        written = list((getattr(result, 'payload', None) or {}).get(
            'regions') or ())
        self.accepted.emit(written)
        self._finish(True)

    # -- the viewport ---------------------------------------------------------- #

    def candidates(self) -> list:
        if self._stack.currentIndex() != self.REVIEW or not self._payload:
            return []
        listed = []
        slot = self._existingSlot()
        for _index, space, item, ticked in self._rows():
            if space is None:
                continue
            colour = zone_colour(slot) if ticked else None
            if ticked:
                slot += 1
            listed.append({'space': space['id'], 'name': item.text(),
                           'colour': colour,
                           'seed': list(self._seedOf(space)),
                           'bounds': list(space.get('bounds') or ()),
                           'ticked': ticked,
                           'outside': bool(space.get('outside')),
                           'volume': float(space.get('volume') or 0.0)})
        return listed

    def _publishCandidates(self) -> None:
        listed = self.candidates()
        self.candidatesChanged.emit(listed)
        if self._highlighter is not None:
            try:
                self._highlighter(listed)
            except Exception:  # noqa: BLE001 - a drawing hook never blocks
                pass

    # -- closing ---------------------------------------------------------------- #

    def _finish(self, accepted: bool) -> None:
        if self._jobId is not None:
            self.cancelDetection()
        # RP13 #7: whatever the run still answers is for a closed panel.
        self._jobId = self._requestId = self._requestCase = None
        self._cancel.setEnabled(True)
        self._unsubscribeAll()
        self._payload = None
        self._stack.setCurrentIndex(self.ASK)
        self._publishCandidates()
        self.finished.emit(bool(accepted))

    def cancel(self) -> None:
        """Leave without writing anything; a running detection is stopped."""
        self._finish(False)
