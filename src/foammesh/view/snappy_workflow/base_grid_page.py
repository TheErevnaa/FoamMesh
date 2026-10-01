"""Snappy workflow page: ``snappy.base_grid``.

Plan 30 WP-09 / F-17. The legacy Designer page wrote seventeen registry field
ids through ``apply_fields``; sixteen of them are declared on the
``snappy.base_grid`` task and render from the descriptor. ``standoff`` is not
declared there -- it is not a snappy control key, it widens the derived
bounding box -- so it is named here rather than being lost in the port.

Plan 31 FS-B adds the two halves of the background mesh that existed in the
writer and nowhere on screen:

* the *name* and the *category* of each of the six derived faces. Twelve
  registered fields that no task declared, which made the case builder's
  refusal -- "the background face xMin is declared a inlet but still carries
  the name this product generated for it" -- unreachable and unsatisfiable
  from the GUI. They are declared on the task now, and drawn face by face
  rather than split between a guided box and a collapsed advanced one,
  because it is the pairing of the two the writer refuses on.
* the authored multi-block topology -- vertices, blocks, curved edges, named
  patches, merge pairs. A complete validated writer with a passing test that
  a user of this product could not reach. It is opt-in: the five collections
  are empty on every existing project, ``background_mesh.from_records``
  returns ``None`` when no block is authored, and the derived single box is
  written byte for byte as before.

Plan 37 UF12 gives every graded direction a side as well as a ratio: the
ratio is the largest cell over the smallest and "Fine cells at" says where the
small ones go, so nobody types a reciprocal -- the arithmetic that put the
old "toward start" switch's cells at the wrong end (DP-1033).
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QVBoxLayout,
)

from foammesh.core.facade import applicability
from foammesh.core.mesh import grading as background_mesh
from foammesh.view.theming.metrics import ReflowGrid, apply_form_metrics
from foammesh.view.workflow_controls.child_controls import ChildControlPanel
from foammesh.view.workflow_controls.conditional_fields import (
    condition_context,
)

from .base import SnappyTaskPage


#: The six derived faces, in the order the dictionary writes them: the schema
#: key, the title of the group they are drawn in, and the storage label the
#: writer's refusal names when the face is left unnamed.
FACES = (
    ('x_min', 'xMin', 'Low X'),
    ('x_max', 'xMax', 'High X'),
    ('y_min', 'yMin', 'Low Y'),
    ('y_max', 'yMax', 'High Y'),
    ('z_min', 'zMin', 'Low Z'),
    ('z_max', 'zMax', 'High Z'),
)

#: Plan 37 UF12. Each axis of the one derived block: its ratio, then the side
#: its small cells go on, in the order the Grading group draws them.
GRADING_FIELDS = tuple(
    field_id for axis in 'xyz' for field_id in (
        f'meshing.base_grid.grading.{axis}',
        f'meshing.base_grid.grading_fine.{axis}'))

#: The words the derived block's side is offered under. The block is axis
#: aligned, so its start is the minus side of the world axis and the plan
#: names both, rather than the stored word alone.
FINE_LABELS = {
    'start': '− side (start)',
    'end': '+ side (end)',
    'centre': 'Centre',
    'both_edges': 'Both edges',
}

#: The note the grading migration leaves, said once until dismissed.
GRADING_NOTICE = 'meshing.base_grid.grading_notice'


class DismissedNotes:
    """Which grading notes this user has dismissed, kept on this machine.

    The note is read-only in the project -- it records what the migration
    did, and a field whose change stales nothing is not a field AF2 lets a
    page write -- so dismissing it is the viewer's, not the case's.
    """

    KEY = 'plan37/dismissedGradingNotes'

    def _settings(self):
        from PySide6.QtCore import QSettings
        return QSettings('FoamMesh', 'FoamMesh')

    def __contains__(self, token: str) -> bool:
        try:
            return token in (self._settings().value(self.KEY) or [])
        except Exception:                                    # noqa: BLE001
            return False

    def add(self, token: str) -> None:
        try:
            settings = self._settings()
            seen = list(settings.value(self.KEY) or [])
            if token not in seen:
                settings.setValue(self.KEY, (seen + [token])[-200:])
        except Exception:                                    # noqa: BLE001
            pass

#: Field id prefixes claimed by the per-face groups.
_FACE_PREFIXES = (
    'meshing.base_grid.boundary_types.',
    'meshing.base_grid.boundary_names.',
    'meshing.base_grid.boundary_categories.',
)


class SnappyBaseGridPage(SnappyTaskPage):
    """The background hex mesh every later snappy stage cuts out of."""

    task_id_default = 'snappy.base_grid'
    #: Runs blockMesh alone. The whole-pipeline run is the branch's
    #: "Run to end"; §7.1 allows this page exactly one Run.
    run_stage = 'blockMesh'
    #: Written by this task, absent from its descriptor. Without it the port
    #: would silently drop a field the legacy page could set. Plan 37 UF12:
    #: the three sides ride here too -- declaring them on the task would
    #: change the workflow digest and discard every saved case's progress.
    extra_field_ids = ('meshing.base_grid.standoff',
                       'meshing.base_grid.grading_fine.x',
                       'meshing.base_grid.grading_fine.y',
                       'meshing.base_grid.grading_fine.z')

    VERTEX_COLUMNS = ('x', 'y', 'z')
    #: Plan 33 OF-03. MEASURED at a 560 px settings column: nine columns
    #: scrolled the block table 374 px sideways. A block is its corners and
    #: how finely they are divided; the three gradings and the zone are read
    #: one block at a time, which is what the row editor is for.
    BLOCK_COLUMNS = ('name', 'vertices', 'num_cells_x', 'num_cells_y',
                     'num_cells_z')
    EDGE_COLUMNS = ('kind', 'start', 'end', 'points')
    #: DP-571 (0924 rerun follow-up). The five tables below are hidden until
    #: blocks written by hand are chosen, and no width gate ever chose them.
    #: MEASURED at the 360 px settings column a window under 1600 px wide
    #: gets: the patch table's stretched `Faces` fell to 50 px of the 60 its
    #: heading needs. A patch is read by its name, its type, what it is for
    #: and its faces; the optional `inGroups` entry is in the row editor.
    PATCH_COLUMNS = ('name', 'type', 'category', 'faces')
    MERGE_COLUMNS = ('master', 'slave')

    def build_sections(self, layout) -> None:
        # Plan 33 OF-03. The choice first, because it decides what the rest
        # of the page means. The paragraph that used to open the column said
        # what a base grid is; it is on the group that offers the two of them
        # now, where it is read by someone who wants it and skipped by
        # someone who does not.
        layout.insertWidget(0, self._build_mode_choice())
        self._grading_box = self._build_grading()
        layout.addWidget(self._grading_box)
        self._faces_box = self._build_faces()
        layout.addWidget(self._faces_box)
        self._build_authoring(layout)
        self.sync_authoring()

    # -- which of the two background meshes this case has ------------------- #

    def _build_mode_choice(self) -> QGroupBox:
        """Name the choice a checkable group box used to hide.

        A checkbox on a group is a disclosure: it says whether a section is
        shown. This one carried a choice between two mutually exclusive
        background meshes, and said so in the fifth sentence of a paragraph
        inside the section it was hiding -- "The settings above stop being
        used the moment a block exists here". Two radio buttons say it in the
        one place it has to be read, before either mesh is on screen.
        """
        box = QGroupBox(self.tr('Background mesh'), self)
        box.setObjectName('baseGridModeBox')
        box.setToolTip(self.tr(
            'The block the mesh is carved from. Its cell size sets the '
            'coarsest cell in the result, and every refinement level halves '
            'it.'))
        inner = QVBoxLayout(box)

        self._automatic = QRadioButton(
            self.tr('One block around the geometry'), box)
        self._automatic.setObjectName('baseGridModeAutomatic')
        self._automatic.setAccessibleDescription(self.tr(
            'A single box derived from the bounding box of the geometry, '
            'with the standoff and the faces below.'))
        inner.addWidget(self._automatic)

        self._custom = QRadioButton(
            self.tr('Blocks I write myself'), box)
        self._custom.setObjectName('baseGridModeCustom')
        self._custom.setAccessibleDescription(self.tr(
            'The block topology written out by hand: vertices first, then '
            'the blocks that use them, then a patch for every external '
            'face.'))
        inner.addWidget(self._custom)

        self._automatic.setChecked(True)
        self._automatic.toggled.connect(self._apply_mode)
        return box

    def authoring_is_chosen(self) -> bool:
        """Whether this case writes its own blocks rather than the box."""
        return bool(getattr(self, '_custom', None) is not None
                    and self._custom.isChecked())

    def _apply_mode(self, *_args) -> None:
        """Show the mesh that was chosen and only that one.

        FIELD-02 one level up: an input that is not read is not on the page.
        The derived faces are not read once a block exists, and the five
        collections are not read while the box is derived, so each set is
        here only while the run reads it. DP-580: so are the fields that size
        the derived box -- blocks written by hand carry their own counts and
        grading, and only Scale is read in both modes (``background_topology``).
        """
        custom = self.authoring_is_chosen()
        for name in ('_faces_box', '_grading_box'):
            box = getattr(self, name, None)
            if box is not None:
                box.setVisible(not custom)
        for panel in self.authoring_panels():
            panel.setVisible(custom)
        self._refresh_sizing_rows()

    # -- the six derived faces --------------------------------------------- #

    def _build_faces(self) -> QGroupBox:
        """One small form per face, holding that face's three coupled fields.

        Left to the default routing the category landed in Guided (it is a
        derivation) and the name and type behind the collapsed Advanced box
        (they are writer keys), which is the one arrangement in which the
        refusal that couples them cannot be read.
        """
        box = QGroupBox(self.tr('Background faces'), self)
        box.setObjectName('baseGridFacesBox')
        box.setToolTip(self.tr(
            'Each face carries a patch type, the name it is delivered under, '
            'and what it is for. A face given anything but "unclassified" '
            'must also be named: the mesh will not be written offering a '
            'generated label as your inlet.'))
        outer = QVBoxLayout(box)
        # Plan 33 section 6 check 4, W-O2. The faces are a level of nesting
        # deeper than the rest of the page -- this box, then the grid, then
        # one group per face -- and every level was adding its own left
        # margin. MEASURED at a 635 px settings column: the page put its
        # labels at x=26 and the faces put theirs at x=43, a 17 px jog down
        # one settings column that said nothing to the reader except that the
        # column had moved. The box's own frame inset and this layout's
        # margin are the 17, and they buy nothing here: the group title above
        # already says where the section starts, and the faces carry frames
        # of their own. Zeroing the two horizontally lands the face forms on
        # the page's one label column.
        margins = box.contentsMargins()
        box.setContentsMargins(0, margins.top(), 0, margins.bottom())
        outer.setContentsMargins(0, outer.contentsMargins().top(),
                                 0, outer.contentsMargins().bottom())

        # DP-160. Three across needs 822 px and the panel gives the page a
        # 615 px viewport that does not scroll sideways, so the third column
        # was clipped away entirely. `ReflowGrid` counts the columns that fit.
        self._face_grid = ReflowGrid(box)
        outer.addWidget(self._face_grid)
        self._face_forms: dict[str, QFormLayout] = {}
        for key, label, side in FACES:
            group = QGroupBox('%s (%s)' % (self.tr(side), label),
                              self._face_grid)
            group.setObjectName('baseGridFace_' + key)
            form = QFormLayout(group)
            # The same setters every other registry-backed form on the page
            # gets. Left to the `QFormLayout` default these six carried a
            # 9 px margin where the page carries 4, which was the other half
            # of the jog. `wrap=False` because a grid cell is already at its
            # narrowest and the width watcher would wrap every row of it.
            apply_form_metrics(form, wrap=False)
            self._face_forms[key] = form
            self._face_grid.addCell(group)
        return box

    # -- grading of the derived block (Plan 37 UF12) ------------------------ #

    def _build_grading(self) -> QGroupBox:
        """Each axis as a ratio and a side, with what they will write.

        Holds the migration's one-time note too: a project that stored a
        ratio below one opens showing its reciprocal with the side at End,
        and a changed number with no word about it reads as a lost setting.
        """
        box = QGroupBox(self.tr('Grading'), self)
        box.setObjectName('baseGridGradingBox')
        box.setToolTip(self.tr(
            'The ratio is the largest cell divided by the smallest, never '
            'below 1; 1 is uniform. Fine cells at says which side the small '
            'cells go on.'))
        outer = QVBoxLayout(box)
        # DP-1047. Left to the layout default the grading form's labels
        # started 9 px right of the page's one label column. The box keeps
        # its frame inset -- its form sits straight inside it, where the
        # faces above sit inside frames of their own -- and loses the
        # layout's margin.
        outer.setContentsMargins(0, outer.contentsMargins().top(),
                                 0, outer.contentsMargins().bottom())
        self._grading_notice = QFrame(box)
        self._grading_notice.setObjectName('baseGridGradingNotice')
        notice = QHBoxLayout(self._grading_notice)
        notice.setContentsMargins(0, 0, 0, 0)
        self._grading_notice_text = QLabel(self._grading_notice)
        self._grading_notice_text.setWordWrap(True)
        self._grading_notice_text.setObjectName('baseGridGradingNoticeText')
        notice.addWidget(self._grading_notice_text, 1)
        self._grading_dismiss = QPushButton(self._grading_notice)
        self._grading_dismiss.setText(self.tr('Dismiss'))
        self._grading_dismiss.setObjectName('baseGridGradingDismiss')
        self._grading_dismiss.setAccessibleName(self.tr(
            'Dismiss the grading note'))
        self._grading_dismiss.clicked.connect(self.dismiss_grading_notice)
        notice.addWidget(self._grading_dismiss)
        self._grading_notice.setVisible(False)
        outer.addWidget(self._grading_notice)
        container = QFrame(box)
        self._grading_form = QFormLayout(container)
        apply_form_metrics(self._grading_form)
        outer.addWidget(container)
        self._grading_report = QLabel(box)
        self._grading_report.setObjectName('baseGridGradingReport')
        self._grading_report.setWordWrap(True)
        outer.addWidget(self._grading_report)
        return box

    def _populate_fields(self, fields) -> None:
        """Put each side straight after its ratio, then name the sides.

        The sides arrive last, as extra fields; a Grading group reading
        ratio X, ratio Y, ratio Z and only then the three sides makes the
        reader pair them up by hand.
        """
        fields = list(fields)
        ordered = [field for field in fields
                   if field.get('field_id') not in GRADING_FIELDS]
        by_id = {field.get('field_id'): field for field in fields}
        ordered += [by_id[field_id] for field_id in GRADING_FIELDS
                    if field_id in by_id]
        super()._populate_fields(ordered)
        for axis in 'xyz':
            editor = self._editors.get(f'meshing.base_grid.grading_fine.{axis}')
            combo = getattr(editor, 'editor', None)
            if combo is None or not hasattr(combo, 'itemData'):
                continue
            for index in range(combo.count()):
                value = str(combo.itemData(index))
                if value in FINE_LABELS:
                    combo.setItemText(index, self.tr(FINE_LABELS[value]))
        self._refresh_grading_report()

    def _grading_value(self, field_id, default=None):
        if field_id in self._pending:
            return self._pending[field_id]
        editor = self._editors.get(field_id)
        if editor is not None:
            return editor.value()
        return default

    def grading_report(self) -> str:
        """What the three axes will write, one sentence per graded axis."""
        mode = self._grading_value(self.SIZING_MODE, 'counts')
        counted = str(getattr(mode, 'value', mode) or 'counts') == 'counts'
        lines = []
        for axis in 'xyz':
            ratio = self._grading_value(f'meshing.base_grid.grading.{axis}', 1)
            fine = self._grading_value(
                f'meshing.base_grid.grading_fine.{axis}', 'start')
            count = self._grading_value(f'meshing.base_grid.cells.{axis}')
            if not counted:
                # The count follows from the target size at write time, so
                # only the side is known here; the writer refuses a split
                # that cannot grade.
                count = None
            sentence = background_mesh.grading_summary(
                count, fine, ratio if ratio not in (None, '') else 1,
                axis.upper())
            if sentence:
                lines.append(sentence)
        return '\n'.join(lines)

    def _refresh_grading_report(self) -> None:
        label = getattr(self, '_grading_report', None)
        if label is None:
            return
        text = self.grading_report()
        label.setText(text)
        label.setVisible(bool(text))
        self._refresh_grading_notice()

    #: Where dismissals are kept; a test swaps in a plain set.
    dismissed_notes = DismissedNotes()

    def _notice_token(self, text: str) -> str:
        import hashlib
        case = str(getattr(self._client, 'case_root', '') or '')
        return hashlib.sha1(f'{case}|{text}'.encode()).hexdigest()

    def grading_notice(self) -> str:
        """The migration's note, unless this user has dismissed it here."""
        try:
            value = self._client.field_values((GRADING_NOTICE,))[GRADING_NOTICE]
        except Exception:                                    # noqa: BLE001
            return ''
        text = str(value or '').strip()
        if text and self._notice_token(text) in self.dismissed_notes:
            return ''
        return text

    def _refresh_grading_notice(self) -> None:
        frame = getattr(self, '_grading_notice', None)
        if frame is None:
            return
        text = self.grading_notice()
        self._grading_notice_text.setText(text)
        frame.setVisible(bool(text))

    def dismiss_grading_notice(self) -> None:
        """Put the note away: it is said once, and this is the once."""
        text = self._grading_notice_text.text().strip()
        if text:
            self.dismissed_notes.add(self._notice_token(text))
        self._grading_notice.setVisible(False)

    def field_form(self, field_id: str, classification):
        if field_id in GRADING_FIELDS:
            return getattr(self, '_grading_form', None)
        forms = getattr(self, '_face_forms', {})
        for prefix in _FACE_PREFIXES:
            if field_id.startswith(prefix):
                return forms.get(field_id[len(prefix):])
        return None

    def field_forms(self):
        forms = tuple(getattr(self, '_face_forms', {}).values())
        grading = getattr(self, '_grading_form', None)
        return forms + ((grading,) if grading is not None else ())

    # -- which sizing the run reads ------------------------------------------ #

    #: DP-579. The field whose value decides which of the two sizings apply.
    SIZING_MODE = 'meshing.base_grid.sizing_mode'
    #: DP-580 (field audit 0924 snappy-front D7). What sizes the one derived
    #: box, and is not read once the blocks are written by hand. Scale is
    #: read by both; the bounding Hex6 still keeps its volume out of
    #: ``geometry{}``.
    DERIVED_BOX_FIELDS = (
        'meshing.base_grid.sizing_mode', 'meshing.base_grid.target_cell_size',
        'meshing.base_grid.cells.x', 'meshing.base_grid.cells.y',
        'meshing.base_grid.cells.z', 'meshing.base_grid.grading.x',
        'meshing.base_grid.grading.y', 'meshing.base_grid.grading.z',
        'meshing.base_grid.grading_fine.x', 'meshing.base_grid.grading_fine.y',
        'meshing.base_grid.grading_fine.z',
        'meshing.base_grid.standoff')

    def reload_values(self) -> None:
        super().reload_values()
        # DP-580. The shared rule judged the clauses from the saved values
        # only; the hand-written-blocks choice is this page's, so it is
        # applied again over it.
        self._refresh_sizing_rows()

    def _on_field_changed(self, field_id: str, value) -> None:
        super()._on_field_changed(field_id, value)
        if field_id == self.SIZING_MODE:
            self._refresh_sizing_rows()
        if field_id in GRADING_FIELDS or field_id == self.SIZING_MODE \
                or field_id.startswith('meshing.base_grid.cells.'):
            self._refresh_grading_report()

    def _refresh_sizing_rows(self) -> None:
        """Judge the page's clauses against the mode as it is being edited.

        DP-579 (field audit 0924 snappy-front D6). The counts and the target
        size declare which sizing mode reads them, and the shared rule takes
        the unread set away -- but only from the saved value, so switching
        the mode left the old set on screen, and editable, until Update. The
        pending mode is what the next Update writes, so it is what decides
        here; a value typed into a set the mode has just put out of reach is
        dropped from the patch rather than written behind the user.
        """
        editors = getattr(self, '_editors', None) or {}
        if not editors:
            return
        values, titles = condition_context(self._client, editors)
        values.update(self._pending)
        custom = self.authoring_is_chosen()
        inactive: dict[str, str] = {}
        for field_id, editor in editors.items():
            verdict = applicability.evaluate(
                getattr(editor.descriptor, 'applies_when', ()) or (),
                values, titles=titles)
            applies, reason = verdict.applies, verdict.reason
            if custom and field_id in self.DERIVED_BOX_FIELDS:
                applies, reason = False, self.tr(
                    'This sizes the one block around the geometry. The '
                    'blocks written by hand carry their own cell counts and '
                    'grading, so the run will not read it.')
            editor.setApplicability(applies, reason)
            if not applies:
                inactive[field_id] = reason
                self._pending.pop(field_id, None)
        self._inactive_fields = inactive

    # -- the authored multi-block topology --------------------------------- #

    def _build_authoring(self, layout) -> None:
        """The five collections blockMesh reads, as five sections of the page.

        They used to live inside a checkable group box, under a five-sentence
        hint, with two more paragraphs between the tables. The group box is
        the mode choice above now, so each collection is a section in its own
        right and says what it is by its own title.
        """
        self.vertex_panel = ChildControlPanel(
            self._client, 'base_grid.vertices', self.tr('Vertices'),
            columns=self.VERTEX_COLUMNS, parent=self)
        layout.addWidget(self.vertex_panel)

        self.block_panel = BlockPanel(
            self._client, 'base_grid.blocks', self.tr('Blocks'),
            columns=self.BLOCK_COLUMNS, parent=self)
        layout.addWidget(self.block_panel)

        self.edge_panel = ChildControlPanel(
            self._client, 'base_grid.edges', self.tr('Curved edges'),
            columns=self.EDGE_COLUMNS, parent=self)
        self.edge_panel.setToolTip(self.tr(
            'Bends the straight line between two vertices, so the block '
            'follows a real curve instead of chording it. An arc takes one '
            'point it passes through; the spline kinds take a list.'))
        layout.addWidget(self.edge_panel)

        self.patch_panel = ChildControlPanel(
            self._client, 'base_grid.patches', self.tr('Patches'),
            columns=self.PATCH_COLUMNS, parent=self)
        layout.addWidget(self.patch_panel)

        self.merge_panel = ChildControlPanel(
            self._client, 'base_grid.merge_pairs', self.tr('Merge pairs'),
            columns=self.MERGE_COLUMNS, parent=self,
            choices={'master': self._patch_choices(),
                     'slave': self._patch_choices()})
        self.merge_panel.setToolTip(self.tr(
            'Fuses two patches into one internal interface, so blocks meshed '
            'separately become one region. Both patches leave the delivered '
            'boundary.'))
        layout.addWidget(self.merge_panel)

        for panel in self.authoring_panels():
            panel.childrenChanged.connect(self.refresh_keeping_edits)
        self.patch_panel.childrenChanged.connect(self.sync_merge_patches)

    def authoring_panels(self) -> tuple:
        """The five collection panels, in the order blockMesh reads them."""
        return tuple(
            panel for panel in
            (getattr(self, name, None) for name in
             ('vertex_panel', 'block_panel', 'edge_panel', 'patch_panel',
              'merge_panel'))
            if panel is not None)

    def sync_authoring(self) -> None:
        """Choose the authored mesh when a project already has rows in it.

        Opt-in must not mean hidden: a saved project that authored a topology
        would otherwise open on a page showing the derived-box controls that
        its own blocks have overridden. The choice is only ever moved towards
        what the case holds, so a reader who picked a mode keeps it.
        """
        if getattr(self, '_custom', None) is None:
            return
        if any(panel.rows() for panel in self.authoring_panels()):
            self._custom.setChecked(True)
        self._apply_mode()

    def _patch_choices(self) -> list:
        """The authored patch names a merge pair may name.

        ``mergePatchPairs`` takes patch names, and a name that matches no
        patch is a merge blockMesh never performs. The rows exist in the table
        above, so the field is a picker over them rather than the name typed
        a second time.
        """
        panel = getattr(self, 'patch_panel', None)
        if panel is None:
            return []
        return [(str(row.get('name') or ''), str(row.get('name') or ''),
                 self.tr('Authored background patch'), True)
                for row in panel.rows()
                if str(row.get('name') or '').strip()]

    def sync_merge_patches(self) -> None:
        """Re-offer the merge pickers after the patch rows change."""
        panel = getattr(self, 'merge_panel', None)
        if panel is None:
            return
        options = self._patch_choices()
        panel.set_choices('master', options)
        panel.set_choices('slave', options)

    def refresh(self) -> None:
        super().refresh()
        self.sync_authoring()


class BlockPanel(ChildControlPanel):
    """The block table, whose editor keeps each direction's two authorities apart.

    Plan 37 UF12. A direction is either a side and a ratio -- and then the
    text beside them is what they write, regenerated as they change -- or a
    Custom profile, and then the text is the grading, written as typed.
    Typing in the text while a side is chosen is a request for the text, so
    the side falls back to Custom profile rather than the text being thrown
    away on the next change of ratio.

    Start and End follow the block's own vertex order, which is not the
    world's: each side says which vertex the direction starts from and which
    it runs toward, read off the vertices typed above it.
    """

    #: blockMesh hex order: local X runs 0 -> 1, Y 0 -> 3, Z 0 -> 4.
    AXIS_ENDS = {'X': (0, 1), 'Y': (0, 3), 'Z': (0, 4)}

    def __init__(self, facade_client, collection_id: str, title: str,
                 *args, **kwargs):
        self._directions = {axis: QLabel() for axis in 'XYZ'}
        for axis, label in self._directions.items():
            label.setWordWrap(True)
            label.setObjectName(f'blockGradingDirection{axis}')
        annotations = dict(kwargs.pop('annotations', None) or {})
        annotations.update({f'grading_{axis.lower()}_fine': label
                            for axis, label in self._directions.items()})
        super().__init__(facade_client, collection_id, title, *args,
                         annotations=annotations, **kwargs)
        self._syncing = False
        for axis in 'xyz':
            for key in (f'grading_{axis}_fine', f'grading_{axis}_ratio'):
                editor = self.editor(key)
                if editor is not None:
                    editor.valueChanged.connect(
                        lambda *_a, a=axis: self.regenerate(a))
            text = self.editor(f'grading_{axis}')
            if text is not None:
                text.valueChanged.connect(
                    lambda *_a, a=axis: self._text_typed(a))
        vertices = self.editor('vertices')
        if vertices is not None:
            vertices.valueChanged.connect(lambda *_a: self.describe_directions())
        self.describe_directions()

    @staticmethod
    def _plain(value):
        return str(getattr(value, 'value', value) or '')

    def regenerate(self, axis: str) -> None:
        """Write the side and ratio of one direction into its text."""
        fine = self._plain(self.editor(f'grading_{axis}_fine').value())
        ratio_editor = self.editor(f'grading_{axis}_ratio')
        custom = fine not in background_mesh.FINE_PRESETS
        ratio_editor.editor.setEnabled(not custom)
        if custom:
            return
        try:
            # Shown the way a profile is typed, without the outer brackets
            # the dictionary puts round one direction's segments.
            text = background_mesh.preset_text(fine, ratio_editor.value())
        except (background_mesh.BackgroundMeshError, TypeError, ValueError):
            return
        self._syncing = True
        try:
            self.editor(f'grading_{axis}').set_value(text)
        finally:
            self._syncing = False

    def _text_typed(self, axis: str) -> None:
        if self._syncing:
            return
        fine = self.editor(f'grading_{axis}_fine')
        if self._plain(fine.value()) in background_mesh.FINE_PRESETS:
            fine.set_value(background_mesh.FINE_CUSTOM_PROFILE)
            self.editor(f'grading_{axis}_ratio').editor.setEnabled(False)

    def describe_directions(self) -> None:
        """Say where Start is for this block, from the vertices typed."""
        editor = self.editor('vertices')
        numbers = str(editor.value() if editor is not None else '').split()
        for axis, (first, last) in self.AXIS_ENDS.items():
            if len(numbers) == 8:
                text = self.tr(
                    'Local {0} runs from vertex {1} (Start) toward vertex {2} '
                    '(End), in this block\'s own vertex order.').format(
                        axis, numbers[first], numbers[last])
            else:
                text = self.tr(
                    'Local {0} runs from the first vertex listed (Start) '
                    'toward the {1} (End).').format(
                        axis, {'X': 'second', 'Y': 'fourth',
                               'Z': 'fifth'}[axis])
            self._directions[axis].setText(text)

    def _load_selected(self) -> None:
        super()._load_selected()
        self._after_load()

    def editor_dialog(self):
        # Reached by Add and by Edit after the values are set, both of which
        # set them with signals blocked.
        self._after_load()
        return super().editor_dialog()

    def _after_load(self) -> None:
        """Values are loaded with signals blocked; bring the extras along."""
        self.describe_directions()
        for axis in 'xyz':
            fine = self.editor(f'grading_{axis}_fine')
            ratio = self.editor(f'grading_{axis}_ratio')
            if fine is not None and ratio is not None:
                ratio.editor.setEnabled(
                    self._plain(fine.value()) in background_mesh.FINE_PRESETS)
