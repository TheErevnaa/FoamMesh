"""The Automatic Sizing card: one widget, both routes (Plan 41 GN25c, 4.8.2).

It sits at the top of the Castellation page on the snappy route and at the top
of the Sizing row on the Gmsh route. It runs the shared field
(``sizing.field.compute``), shows what the field says before any mesh exists
(the colour map in the viewport through the overlay layout, the size
histogram, the estimate with its RAM), applies the proposals to the controls
that are empty or still the field's, and, on the snappy route, offers the
conformity map and the opt-in Refine to field pass. A proposal never replaces
a value the user typed.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget)

from foammesh.core import display_unit
from foammesh.view.engine_context import (
    GMSH, SNAPPY, WHEN_GMSH, WHEN_SNAPPY, engine_holds)
from foammesh.core.sizing.tool_setup import SETUP_ACTION
from foammesh.view.facade_client import query, submit
from foammesh.view.widgets.folder_header import FolderHeader
from foammesh.view.widgets.link_button import as_link, link_row
from foammesh.view.workflow_controls.field_widgets import FieldEditor

#: Field ids of the card's inputs, in the order they are drawn. The feature
#: angle is the project's own (shown as text); anisotropic is Gmsh-only.
INPUT_FIELDS = ('sizing.field.minimum_size', 'sizing.field.bulk_size',
                'sizing.field.maximum_size', 'sizing.field.gradation',
                'sizing.field.nodes_per_two_pi',
                'sizing.field.nodes_per_gap', 'sizing.field.smoothing')
GMSH_ONLY_FIELDS = ('sizing.field.anisotropic',
                    'sizing.field.use_as_background')


def length_text(metres: float) -> str:
    try:
        return display_unit.format_length(float(metres))
    except Exception:                                        # noqa: BLE001
        return f'{float(metres):.4g} m'


class SizeHistogram(QWidget):
    """The field's sizes at the surfaces, on a log axis (O10)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('sizeFieldHistogram')
        self.setMinimumSize(220, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        self.setAccessibleName('Size field histogram')
        self._edges: list = []
        self._counts: list = []

    def setDistribution(self, histogram: dict) -> None:
        self._edges = [float(e) for e in (histogram or {}).get('edges') or ()]
        self._counts = [int(c) for c in (histogram or {}).get('counts') or ()]
        low = length_text(self._edges[0]) if self._edges else ''
        high = length_text(self._edges[-1]) if self._edges else ''
        self.setToolTip('Field size at the surfaces, log scale: '
                        f'{low} to {high}' if self._edges else '')
        self.update()

    def edges(self) -> list:
        return list(self._edges)

    def counts(self) -> list:
        return list(self._counts)

    def paintEvent(self, event):                             # noqa: N802
        painter = QPainter(self)
        text = self.palette().windowText().color()
        painter.setPen(QPen(text))
        if not self._counts or len(self._edges) < 2:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self.tr('No field computed'))
            return
        label = float(self.fontMetrics().height() + 2)
        area = QRectF(4.0, 4.0, self.width() - 8.0,
                      self.height() - 8.0 - label)
        tallest = max(self._counts) or 1
        count = len(self._counts)
        width = area.width() / count
        bar = QColor(self.palette().highlight().color())
        for index, value in enumerate(self._counts):
            if value <= 0:
                continue
            height = max(1.0, value / tallest * area.height())
            painter.fillRect(QRectF(area.left() + index * width + 0.5,
                                    area.bottom() - height,
                                    max(1.0, width - 1.0), height), bar)
        painter.drawLine(int(area.left()), int(area.bottom()),
                         int(area.right()), int(area.bottom()))
        labels = QRectF(area.left(), area.bottom() + 2, area.width(), label)
        painter.drawText(labels, Qt.AlignmentFlag.AlignLeft,
                         length_text(self._edges[0]))
        painter.drawText(labels, Qt.AlignmentFlag.AlignRight,
                         length_text(self._edges[-1]))


def _label(name: str, parent, wrap=True) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


class AutomaticSizingCard(QFrame):
    """Compute, preview, apply: the shared card."""

    applied = Signal()
    computed = Signal()
    #: M-20. The Gmsh route's settings live in the Automatic mesh size row of
    #: the Size fields table; the card asks the page to show that row.
    rowOpenRequested = Signal(str)

    def __init__(self, client, engine: str = 'snappy', parent=None, *,
                 overlay_factory=None):
        super().__init__(parent)
        self._client = client
        self.engine = GMSH if str(engine).lower().startswith(GMSH) \
            else SNAPPY
        self._overlay_factory = overlay_factory
        self._overlay = None
        self._summary: dict = {}
        self._summary_key = None
        self._state = 'none'
        self.setObjectName('automaticSizingCard')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName('Automatic sizing')

        outer = QVBoxLayout(self)
        title = _label('automaticSizingTitle', self, wrap=False)
        title.setText(self.tr('Automatic sizing'))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        outer.addWidget(title)
        # Said once, to a reader who asks: on the title, not as a paragraph.
        title.setToolTip(self.tr(
            'Sizes the mesh from the shape: finer where the surface curves '
            'or two surfaces come close, coarser away from them. The same '
            'field serves both meshers. It proposes values; a value you '
            'typed is never replaced.'))

        # FIX-H, Plan 40 UXD4. The field's inputs are Common-tier settings
        # (each defaults from the model), so they sit behind the card's own
        # fold, closed, as every UXD4 page folds its Common tier;
        # Compute, Preview and Apply stay on the default screen. Nine inputs
        # on the default screen took Sizing past its census (26 against 17).
        # FIX-I: the fold is named for what its inputs are (the field's
        # own inputs), not for the structure around them.
        self._commonHeader = FolderHeader(self.tr('Field inputs'), self)
        self._commonHeader.setObjectName('automaticSizingAdvanced')
        self._commonHeader.setToolTip(self.tr(
            "The size field's inputs. Each one starts from the model; "
            'Compute works without opening this.'))
        outer.addWidget(self._commonHeader)
        self.inputs = QWidget(self)
        self.inputs.setObjectName('automaticSizingInputs')
        inputs = QVBoxLayout(self.inputs)
        inputs.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.editors: dict[str, FieldEditor] = {}
        # M-20. On the Gmsh route the settings are the row's (hMin, hBulk,
        # gradation ... and its Background box = use as background): the
        # card holds no copy. Only the anisotropic switch, which Gmsh's field
        # has no option for, stays here.
        ids = (('sizing.field.anisotropic',)
               if engine_holds(self.engine, *WHEN_GMSH) else INPUT_FIELDS)
        for field_id in ids:
            try:
                descriptor = client.descriptor(field_id)
            except Exception:                                # noqa: BLE001
                continue
            editor = FieldEditor(descriptor, self)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(editor.editor, 1)
            row.addWidget(editor.unit_label)
            holder = QWidget(self)
            holder.setLayout(row)
            form.addRow(editor.label, holder)
            self.editors[field_id] = editor
        inputs.addLayout(form)
        self.angle_label = _label('automaticSizingAngle', self.inputs)
        inputs.addWidget(self.angle_label)
        self.reuse_check = QCheckBox(self.tr('Reuse last field'), self.inputs)
        self.reuse_check.setObjectName('automaticSizingReuse')
        self.reuse_check.setChecked(True)
        inputs.addWidget(self.reuse_check)
        outer.addWidget(self.inputs)
        # M-20. The shortcut to the row that holds the settings.
        self.row_button = None
        self.row_note = _label('automaticSizingRowNote', self)
        if engine_holds(self.engine, *WHEN_GMSH):
            self.row_button = QPushButton(
                self.tr('Open the settings row'), self)
            self.row_button.setObjectName('automaticSizingOpenRow')
            self.row_button.setToolTip(self.tr(
                'The settings of the automatic size field are one row in '
                'the Gmsh size fields table, with every option Gmsh gives '
                'it. This opens that row, making it first if there is none.'))
            self.row_button.clicked.connect(self.openRow)
            outer.addWidget(link_row(
                self, 'automaticSizingOpenRowRow', self.row_button))
            outer.addWidget(self.row_note)
        else:
            self.row_note.setVisible(False)
        self._commonHeader.toggled.connect(self.inputs.setVisible)
        self._commonHeader.setChecked(False)
        self.inputs.setVisible(False)

        # FIX-H. Two rows, not three buttons on one: the row stood 296 px
        # and the card 314, wider than the 314 px compact column it sits in.
        buttons = QHBoxLayout()
        self.compute_button = QPushButton(self.tr('Compute'), self)
        self.compute_button.setObjectName('automaticSizingCompute')
        self.compute_button.clicked.connect(self.compute)
        self.preview_button = QPushButton(self.tr('Preview'), self)
        self.preview_button.setObjectName('automaticSizingPreview')
        self.preview_button.setCheckable(True)
        self.preview_button.toggled.connect(self.setPreviewShown)
        self.apply_button = QPushButton(self.tr('Apply proposals'), self)
        self.apply_button.setObjectName('automaticSizingApply')
        self.apply_button.clicked.connect(self.apply)
        for button in (self.compute_button, self.preview_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        outer.addLayout(buttons)
        apply_row = QHBoxLayout()
        apply_row.addWidget(self.apply_button)
        apply_row.addStretch(1)
        outer.addLayout(apply_row)

        # GN25e. Shown only when Compute found no size-field tool in the
        # runtime: one click copies the file FoamMesh ships (no download).
        self.setup_button = QPushButton(
            self.tr('Set up automatic sizing'), self)
        self.setup_button.setObjectName('automaticSizingSetup')
        self.setup_button.setToolTip(self.tr(
            'Copies the size-field tool that comes with FoamMesh into your '
            'OpenFOAM 13 runtime, under your home folder there. Nothing is '
            'downloaded and no administrator rights are needed.'))
        self.setup_button.clicked.connect(self.setUpTool)
        self.setup_button.setVisible(False)
        outer.addWidget(self.setup_button)

        self.status_label = _label('automaticSizingStatus', self)
        self.estimate_label = _label('automaticSizingEstimate', self)
        self.histogram = SizeHistogram(self)
        self.proposals_label = _label('automaticSizingProposals', self)
        self.thin_label = _label('automaticSizingThin', self)
        self.thin_label.setProperty('foammeshStatus', 'warning')
        self.split_label = _label('automaticSizingSplit', self)
        for widget in (self.status_label, self.estimate_label, self.histogram,
                       self.proposals_label, self.thin_label,
                       self.split_label):
            outer.addWidget(widget)

        # Snappy only: the conformity map and the opt-in Refine to field.
        self.after_mesh = QWidget(self)
        after = QVBoxLayout(self.after_mesh)
        after.setContentsMargins(0, 0, 0, 0)
        self.conformity_button = QPushButton(
            self.tr('Measure size conformity'), self.after_mesh)
        self.conformity_button.setObjectName('automaticSizingConformity')
        self.conformity_button.clicked.connect(self.measureConformity)
        self.conformity_label = _label('automaticSizingConformityResult',
                                       self.after_mesh)
        self.conformity_view = QCheckBox(self.tr('Show on the mesh'),
                                         self.after_mesh)
        self.conformity_view.setObjectName('automaticSizingConformityShow')
        self.conformity_view.toggled.connect(self._conformityShown)
        self.refine_check = QCheckBox(
            self.tr('Offer refining to the field'), self.after_mesh)
        self.refine_check.setObjectName('automaticSizingRefineOffer')
        self.refine_check.setToolTip(self.tr(
            'Off by default. When on, Refine to field can split the cells '
            'the field finds too coarse.'))
        self.refine_button = QPushButton(self.tr('Refine to field'),
                                         self.after_mesh)
        self.refine_button.setObjectName('automaticSizingRefine')
        self.refine_button.setEnabled(False)
        self.refine_button.setToolTip(self.tr(
            'Splits the cells larger than 1.5 times the field with '
            'refineMesh. For gaps the surface levels cannot reach.'))
        self.refine_check.toggled.connect(self.refine_button.setEnabled)
        self.refine_button.clicked.connect(self.refineToField)
        self.refine_label = _label('automaticSizingRefineResult',
                                   self.after_mesh)
        # Plan 44 MF20. The two actions are text links beside the box they
        # belong to, not two buttons the width of the card.
        after.addWidget(link_row(
            self.after_mesh, 'automaticSizingConformityRow',
            self.conformity_button))
        for widget in (self.conformity_label, self.conformity_view):
            after.addWidget(widget)
        after.addWidget(self.refine_check)
        after.addWidget(link_row(
            self.after_mesh, 'automaticSizingRefineRow', self.refine_button))
        after.addWidget(self.refine_label)
        outer.addWidget(self.after_mesh)
        self.after_mesh.setVisible(
            engine_holds(self.engine, *WHEN_SNAPPY))
        self._conformity: dict = {}
        self._set_idle_text()

    # -- text ---------------------------------------------------------------- #

    def _set_idle_text(self) -> None:
        for label in (self.estimate_label, self.proposals_label,
                      self.thin_label, self.split_label,
                      self.conformity_label, self.refine_label):
            label.setText('')
            label.setVisible(False)
        self._say_idle()

    def _say_idle(self) -> None:
        """No field yet: said on Compute, not as a line in the column.

        FIX-H. The status line is a result, shown once there is one (a field,
        a Compute, an Apply), like the estimate and conformity lines beside
        it; with no field it stood in the settings column as prose.
        """
        idle = self.tr('No field yet. Compute needs a prepared geometry.')
        self.status_label.setText(idle)
        self.status_label.setVisible(False)
        self.compute_button.setToolTip(idle)

    def _status(self, text: str) -> None:
        self.compute_button.setToolTip('')
        self._say(self.status_label, text)

    def _say(self, label: QLabel, text: str) -> None:
        label.setText(text)
        label.setVisible(bool(text))

    # -- reading ------------------------------------------------------------ #

    def _query(self, operation, parameters=None):
        try:
            result = query(self._client, operation, parameters or {})
        except Exception as error:                           # noqa: BLE001
            return {'available': False, 'reason': str(error)}
        payload = getattr(result, 'payload', None)
        return payload if isinstance(payload, dict) else {}

    def refresh(self) -> None:
        """Read the field's state, the stored inputs and the preview."""
        self._load_inputs()
        status = self._query('sizing.field.status')
        self._state = str(status.get('state') or 'none')
        if self._state == 'none':
            self._say_idle()
            self.reuse_check.setEnabled(False)
            self._summary = {}
            self._summary_key = None
            self.histogram.setDistribution({})
            for label in (self.estimate_label, self.thin_label,
                          self.split_label):
                self._say(label, '')
        else:
            stale = self._state == 'stale'
            self._status(
                self.tr('Field is stale: the geometry or the inputs changed '
                        'since it was computed.') if stale else
                self.tr('Field ready: {0} octree leaves.').format(
                    f"{int(status.get('leaves') or 0):,}"))
            self.reuse_check.setEnabled(not stale)
            if stale:
                self.reuse_check.setChecked(False)
            if status.get('key') != self._summary_key or not self._summary:
                self._read_preview()
                self._summary_key = status.get('key')
        self._show_proposal_count()
        self._refresh_enablement()

    # -- the row (M-20) ------------------------------------------------------ #

    def _stored_rows(self) -> list:
        from foammesh.core.gmsh import field_catalogue as catalogue
        try:
            raw = self._client.field_values((catalogue.FIELD_ID,)).get(
                catalogue.FIELD_ID)
            return catalogue.rows_from_text(getattr(raw, 'value', raw))
        except Exception:                                    # noqa: BLE001
            return []

    def openRow(self) -> str:
        """Make the Automatic mesh size row if there is none, then show it."""
        from foammesh.core.gmsh import field_catalogue as catalogue
        rows, row, created = catalogue.with_automatic_row(self._stored_rows())
        name = row['name']
        if not created:
            self.rowOpenRequested.emit(name)
            return name

        def stored(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                self._status(self._refusal(result))
                return
            self.rowOpenRequested.emit(name)

        submit(self._client, 'configuration.patch',
               {'patch': {catalogue.FIELD_ID: catalogue.rows_to_text(rows)}},
               then=stored)
        return name

    def _show_row_note(self) -> None:
        if self.row_button is None:
            return
        from foammesh.core.gmsh import field_catalogue as catalogue
        row = catalogue.automatic_row(self._stored_rows())
        if row is None:
            self._say(self.row_note, self.tr(
                'The settings are one row in the Gmsh size fields table. '
                'Open the row to make it.'))
            return
        text = self.tr('The settings are the row {0} in the Gmsh size '
                       'fields table.').format(row['name'])
        reason = catalogue.TYPES[catalogue.AUTOMATIC_TYPE].refusal
        if reason:
            text += ' ' + self.tr(
                'This Gmsh cannot run the field itself (it was built '
                'without p4est), so the row is switched off in the mesh '
                'run; Compute here still uses its settings.')
        self._say(self.row_note, text)

    def _load_inputs(self) -> None:
        self._show_row_note()
        try:
            values = self._client.field_values(tuple(self.editors))
        except Exception:                                    # noqa: BLE001
            values = {}
        for field_id, editor in self.editors.items():
            if field_id in values:
                editor.set_value(values[field_id])
        try:
            angle = self._client.field_value('mesh.feature_angle')
            angle = getattr(angle, 'value', angle)
        except Exception:                                    # noqa: BLE001
            angle = None
        self._say(self.angle_label, self.tr(
            'Feature angle {0} degrees, from the project.').format(angle)
            if angle not in (None, '') else '')

    def _read_preview(self) -> None:
        summary = self._query('sizing.field.preview')
        self._summary = summary
        if not summary.get('available'):
            return
        self.histogram.setDistribution(summary.get('histogram') or {})
        self._say(self.estimate_label, summary.get('estimate_text') or '')
        self.estimate_label.setProperty(
            'warning', bool((summary.get('estimate') or {}).get('over_ram')))
        thin = summary.get('thin_text') or ()
        self._say(self.thin_label, (self.tr('Thin regions: ')
                                    + '; '.join(thin)) if thin else '')
        offers = summary.get('split') or ()
        self._say(self.split_label, self._split_text(offers))
        if self.preview_button.isChecked():
            self._draw_field()

    def _split_text(self, offers) -> str:
        if not offers:
            return ''
        names = ', '.join(
            '{0} (levels {1} to {2})'.format(
                o['surface'], o['minimum_level'], o['maximum_level'])
            for o in offers)
        return self.tr(
            'Split proposal: these surfaces need levels two or more apart, '
            'so one refinement row cannot serve all of them: {0}. Split '
            'them by solid or by feature angle and give each part its own '
            'levels.').format(names)

    def _show_proposal_count(self) -> None:
        payload = self._query('sizing.proposals.get', {'badges_only': True})
        count = len(payload.get('badges') or {})
        text = self.tr(
            '{0} controls carry a field badge.').format(count)             if count else ''
        # GN25f. What the field could not propose (STL has no curves).
        notes = ' '.join(str(note) for note in payload.get('notes') or ())
        self._say(self.proposals_label, ' '.join(
            part for part in (text, notes) if part))

    def _refresh_enablement(self) -> None:
        have = self._state in ('ready', 'stale')
        self.preview_button.setEnabled(self._state == 'ready'
                                       or self._state == 'stale')
        self.apply_button.setEnabled(have)

    # -- acting -------------------------------------------------------------- #

    def _patch(self) -> dict:
        patch = {field_id: editor.value()
                 for field_id, editor in self.editors.items()}
        if self.row_button is not None:
            # M-20. The tool records the inputs it ran with; they are read
            # from the row, never typed in the card.
            from foammesh.core.gmsh import field_catalogue as catalogue
            patch.update(catalogue.automatic_patch(self._stored_rows()))
        return patch

    def compute(self) -> None:
        """Store the inputs, run the field (or reuse it), then show it."""
        self.compute_button.setEnabled(False)
        self._status(self.tr('Computing the size field…'))

        def finished(result) -> None:
            self.compute_button.setEnabled(True)
            if getattr(result, 'status', 'accepted') != 'accepted':
                text = self._refusal(result)
                self._status(text)
                self.setup_button.setVisible(SETUP_ACTION in text)
                return
            self.refresh()
            self.computed.emit()

        def stored(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                self.compute_button.setEnabled(True)
                self._status(self._refusal(result))
                return
            submit(self._client, 'sizing.field.compute',
                   {'reuse': self.reuse_check.isChecked()}, then=finished)

        submit(self._client, 'configuration.patch', {'patch': self._patch()},
               then=stored)

    def setUpTool(self) -> None:
        """Copy the shipped size-field tool into the runtime, then say so."""
        self.setup_button.setEnabled(False)
        self._status(self.tr('Copying the size-field tool into the runtime…'))

        def done(result) -> None:
            self.setup_button.setEnabled(True)
            if getattr(result, 'status', 'accepted') != 'accepted':
                self._status(self._refusal(result))
                return
            payload = getattr(result, 'payload', None) or {}
            self.setup_button.setVisible(False)
            self._status(str(payload.get('message') or self.tr(
                'The size-field tool is set up. Compute again.')))

        submit(self._client, 'sizing.tool.setup', {}, then=done)

    def _refusal(self, result) -> str:
        warnings = getattr(result, 'warnings', ()) or ()
        message = (warnings[0] if warnings else
                   getattr(result, 'message', '') or self.tr(
                       'The size field could not be computed.'))
        return str(message)

    def apply(self) -> None:
        def done(result) -> None:
            payload = getattr(result, 'payload', None) or {}
            # What the last Apply wrote and skipped, for a reader of the card.
            self.last_apply = dict(payload)
            skipped = payload.get('skipped') or {}
            typed = sum(1 for why in skipped.values() if why == 'typed')
            ran = sum(1 for why in skipped.values() if why == 'stage_ran')
            text = self.tr('{0} controls set from the field.').format(
                len(payload.get('applied') or ()))
            if typed:
                text += ' ' + self.tr(
                    '{0} typed values kept.').format(typed)
            if ran:
                # FIX-H. Base grid and surface features already ran: their
                # proposals stay as badges on those pages, not written here.
                text += ' ' + self.tr(
                    '{0} belong to a step already run (Base grid or Surface '
                    'features); change them there.').format(ran)
            self._status(text)
            self.applied.emit()
            self._show_proposal_count()

        submit(self._client, 'sizing.proposals.apply', {}, then=done)

    # -- the viewport -------------------------------------------------------- #

    def _display(self):
        try:
            from foammesh.app import app
            return app.window._displayControl
        except Exception:                                    # noqa: BLE001
            return None

    def overlay(self):
        if self._overlay is None:
            if self._overlay_factory is not None:
                self._overlay = self._overlay_factory()
            else:
                display = self._display()
                if display is None:
                    return None
                from foammesh.app import app
                from foammesh.rendering.size_field_overlay import (
                    SizeFieldOverlay)
                tokens = (app.themeManager.tokens
                          if app.themeManager is not None else None)
                self._overlay = SizeFieldOverlay(display, tokens)
        return self._overlay

    def setPreviewShown(self, shown: bool) -> None:
        overlay = self.overlay()
        if overlay is None:
            return
        if not shown:
            overlay.clear_field()
            return
        self._draw_field()

    def _draw_field(self) -> dict:
        import numpy as np
        path = self._summary.get('overlay')
        overlay = self.overlay()
        if not path or overlay is None:
            return {}
        try:
            with np.load(path) as data:
                arrays = {name: data[name] for name in data.files}
        except (OSError, ValueError):
            return {}
        return overlay.show_field(arrays)

    # -- snappy: conformity and Refine to field ------------------------------ #

    def measureConformity(self) -> None:
        self.conformity_button.setEnabled(False)

        def done(result) -> None:
            self.conformity_button.setEnabled(True)
            payload = getattr(result, 'payload', None) or {}
            self._conformity = payload
            if not payload.get('available'):
                self._say(self.conformity_label, str(payload.get('reason')
                          or self._refusal(result)))
                return
            cells = int(payload.get('cells') or 0)
            self._say(self.conformity_label, self.tr(
                'Size conformity of {0:,} cells: {1:,} above {2:g} '
                '(too coarse), {3:,} below {4:g} (too fine).').format(
                cells, int(payload.get('under') or 0),
                float(payload.get('under_limit', 2)),
                int(payload.get('over') or 0),
                float(payload.get('over_limit', 0.5))))
            if self.conformity_view.isChecked():
                self._conformityShown(True)

        submit(self._client, 'sizing.conformity', {}, then=done)

    def _conformityShown(self, shown: bool) -> None:
        overlay = self.overlay()
        if overlay is None:
            return
        if not shown:
            overlay.clear_conformity()
            return
        import numpy as np
        path = self._conformity.get('overlay')
        if not path:
            return
        try:
            with np.load(path) as data:
                overlay.show_conformity(data['centres'], data['ratio'])
        except (OSError, ValueError, KeyError):
            return

    def refineToField(self) -> None:
        self.refine_button.setEnabled(False)

        def done(result) -> None:
            self.refine_button.setEnabled(self.refine_check.isChecked())
            payload = getattr(result, 'payload', None) or {}
            if getattr(result, 'status', 'accepted') != 'accepted':
                self._say(self.refine_label, self._refusal(result))
                return
            self._say(self.refine_label, self.tr(
                'Refined {0:,} of {1:,} cells.').format(
                int(payload.get('to_refine') or 0),
                int(payload.get('cells') or 0)))

        submit(self._client, 'sizing.refine_to_field', {}, then=done)


class ThinRegionsNote(QLabel):
    """The Boundary layer page's warning: layers thicker than the gap."""

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self._client = client
        self.setObjectName('thinRegionsNote')
        self.setWordWrap(True)
        self.setProperty('foammeshStatus', 'warning')
        self.setVisible(False)

    def refresh(self) -> str:
        try:
            result = query(self._client, 'sizing.field.preview')
            payload = getattr(result, 'payload', None) or {}
        except Exception:                                    # noqa: BLE001
            payload = {}
        lines = payload.get('thin_text') or () \
            if payload.get('available') else ()
        text = (self.tr('Thin regions, before the layers are added: ')
                + '; '.join(lines)) if lines else ''
        self.setText(text)
        self.setVisible(bool(text))
        return text
