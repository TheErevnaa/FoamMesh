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
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QFormLayout, QGridLayout, QGroupBox, QLabel, QVBoxLayout, QWidget,
)

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

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
    #: would silently drop a field the legacy page could set.
    extra_field_ids = ('meshing.base_grid.standoff',)

    VERTEX_COLUMNS = ('x', 'y', 'z')
    BLOCK_COLUMNS = ('name', 'vertices', 'num_cells_x', 'num_cells_y',
                     'num_cells_z', 'grading_x', 'grading_y', 'grading_z',
                     'zone')
    EDGE_COLUMNS = ('kind', 'start', 'end', 'points')
    PATCH_COLUMNS = ('name', 'type', 'category', 'group', 'faces')
    MERGE_COLUMNS = ('master', 'slave')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'The base grid is the block the mesh is carved from: its cell '
            'size sets the coarsest cell in the result, and every refinement '
            'level halves it. Standoff pads the derived bounding box so the '
            'block is not flush with the geometry.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addWidget(self._build_faces())
        layout.addWidget(self._build_authoring())
        self.sync_authoring()

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
        outer = QVBoxLayout(box)
        hint = QLabel(self.tr(
            'Each face of the derived block carries a patch type, the name it '
            'is delivered under, and what it is for. A face given anything '
            'but "unclassified" must also be named: the mesh will not be '
            'written offering a generated label as your inlet.'), box)
        hint.setWordWrap(True)
        outer.addWidget(hint)

        grid = QGridLayout()
        outer.addLayout(grid)
        self._face_forms: dict[str, QFormLayout] = {}
        for index, (key, label, side) in enumerate(FACES):
            group = QGroupBox('%s (%s)' % (self.tr(side), label), box)
            group.setObjectName('baseGridFace_' + key)
            self._face_forms[key] = QFormLayout(group)
            grid.addWidget(group, index // 3, index % 3)
        return box

    def field_form(self, field_id: str, classification):
        forms = getattr(self, '_face_forms', {})
        for prefix in _FACE_PREFIXES:
            if field_id.startswith(prefix):
                return forms.get(field_id[len(prefix):])
        return None

    def field_forms(self):
        return tuple(getattr(self, '_face_forms', {}).values())

    # -- the authored multi-block topology --------------------------------- #

    def _build_authoring(self) -> QGroupBox:
        box = QGroupBox(
            self.tr('Author the background mesh block by block'), self)
        box.setObjectName('baseGridAuthoringBox')
        box.setCheckable(True)
        outer = QVBoxLayout(box)

        self._authoring_body = QWidget(box)
        body = QVBoxLayout(self._authoring_body)
        body.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._authoring_body)

        hint = QLabel(self.tr(
            'Leave this off and the background mesh is the single box derived '
            'from the geometry, with the faces above. Turn it on to write the '
            'block topology yourself: vertices first, then the blocks that '
            'use them, then a patch for every external face. The settings '
            'above stop being used the moment a block exists here.'),
            self._authoring_body)
        hint.setWordWrap(True)
        body.addWidget(hint)

        self.vertex_panel = ChildControlPanel(
            self._client, 'base_grid.vertices', self.tr('Vertices'),
            columns=self.VERTEX_COLUMNS, parent=self._authoring_body)
        body.addWidget(self.vertex_panel)

        self.block_panel = ChildControlPanel(
            self._client, 'base_grid.blocks', self.tr('Blocks'),
            columns=self.BLOCK_COLUMNS, parent=self._authoring_body)
        body.addWidget(self.block_panel)

        edge_note = QLabel(self.tr(
            'A curved edge bends the straight line between two vertices, so '
            'the block follows a real curve instead of chording it. An arc '
            'takes one point it passes through; the spline kinds take a '
            'list.'), self._authoring_body)
        edge_note.setWordWrap(True)
        body.addWidget(edge_note)

        self.edge_panel = ChildControlPanel(
            self._client, 'base_grid.edges', self.tr('Curved edges'),
            columns=self.EDGE_COLUMNS, parent=self._authoring_body)
        body.addWidget(self.edge_panel)

        self.patch_panel = ChildControlPanel(
            self._client, 'base_grid.patches', self.tr('Patches'),
            columns=self.PATCH_COLUMNS, parent=self._authoring_body)
        body.addWidget(self.patch_panel)

        merge_note = QLabel(self.tr(
            'A merge pair fuses two patches into one internal interface, so '
            'blocks meshed separately become one region. Both patches leave '
            'the delivered boundary.'), self._authoring_body)
        merge_note.setWordWrap(True)
        body.addWidget(merge_note)

        self.merge_panel = ChildControlPanel(
            self._client, 'base_grid.merge_pairs', self.tr('Merge pairs'),
            columns=self.MERGE_COLUMNS, parent=self._authoring_body,
            choices={'master': self._patch_choices(),
                     'slave': self._patch_choices()})
        body.addWidget(self.merge_panel)

        for panel in self.authoring_panels():
            panel.childrenChanged.connect(self.refresh)
        self.patch_panel.childrenChanged.connect(self.sync_merge_patches)

        box.toggled.connect(self._authoring_body.setVisible)
        box.setChecked(False)
        self._authoring_body.setVisible(False)
        return box

    def authoring_panels(self) -> tuple:
        """The five collection panels, in the order blockMesh reads them."""
        return tuple(
            panel for panel in
            (getattr(self, name, None) for name in
             ('vertex_panel', 'block_panel', 'edge_panel', 'patch_panel',
              'merge_panel'))
            if panel is not None)

    def sync_authoring(self) -> None:
        """Open the authoring section when a project already has rows in it.

        Opt-in must not mean hidden: a saved project that authored a topology
        would otherwise open on a page showing the derived-box controls that
        its own blocks have overridden.
        """
        box = self.findChild(QGroupBox, 'baseGridAuthoringBox')
        if box is None:
            return
        if any(panel.rows() for panel in self.authoring_panels()):
            box.setChecked(True)

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
