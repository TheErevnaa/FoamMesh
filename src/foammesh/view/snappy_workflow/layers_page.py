"""Snappy workflow page: ``snappy.layers``.

Plan 30 WP-09 / F-17. The legacy Designer page wrote nineteen ``addLayers/*``
keys, all declared on this task, and edited the per-patch layer groups through
a dialog. The groups are a registered collection (``addLayers/layers``) and are
edited here through the shared child-control table, which surfaces WP-11's
``layer_policy`` (F-42) directly: a patch can be told to grow layers, to freeze
at none, or to inherit, and ``surface_layers`` may legitimately be 0.

Plan 32 W4 adds the default. A layers step with no group at all wrote
``addLayersControls { layers { } }``, which OpenFOAM 13 reads as a request for
nothing: the stage ran, reported success and changed no cell (DP-112). The
boundary roles the Geometry step already authored say which boundary a layer
belongs on, so when this page opens on a case with no group the wall
boundaries become one, it is stored rather than proposed, and the page names
the boundaries it chose.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMessageBox

from foammesh.core.geometry.boundary_roles import (
    boundaries_from_manifest, default_layer_pattern, default_layer_targets,
    defaulted_targets_sentence, flat_boundaries)
from foammesh.view.facade_client import query, submit
from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import SnappyTaskPage
from .layer_membership import (
    BY_PATTERN, LayerMembership, bound_layer_groups, unbind_layer_group,
)
from .layer_pattern_preview import (
    LayerPatternPreview, candidate_patch_names,
)


class LayerGroupPanel(ChildControlPanel):
    """The layer group table, whose row editor also says what it covers.

    DP-524. A group on the default ``geometry`` selector is written only for
    the geometry rows bound to it, so the editor that sets its layers sets
    the binding too -- the same shape as DP-490's refinement groups. The
    membership list sits under ``Selects patches by`` and is written once the
    facade has accepted the row, when a new group has its id.
    """

    def __init__(self, facade_client, collection_id, title, **kwargs):
        self.membership = LayerMembership(
            facade_client, selector=self._selectorValue)
        annotations = dict(kwargs.pop('annotations', None) or {})
        annotations['patch_selector'] = self.membership
        super().__init__(facade_client, collection_id, title,
                         annotations=annotations, **kwargs)
        editor = self.editor('patch_selector')
        if editor is not None:
            editor.valueChanged.connect(self.membership.syncSelector)

    def _selectorValue(self):
        editor = self.editor('patch_selector')
        return editor.value() if editor is not None else None

    def open_add_dialog(self) -> None:
        self.membership.load(None)
        super().open_add_dialog()

    def fresh_value(self, key: str, descriptor):
        """DP-595: a new group starts on the setting every group shares.

        Foundation 13 reads ``relativeSizes`` once for the whole layer
        addition, so the facade copies it across the groups; a new group
        opening on the schema default would flip all the others.
        """
        if key == 'relative_sizes':
            for row in self.rows():
                if row.get(key) is not None:
                    return row.get(key)
        if key == 'surface_layers':
            # DP-601 (field audit 0924 snappy-back D8). The schema's 0 made
            # Add open on a "grow" group that grew nothing; a new group starts
            # on the count the page's first group gets. Saved rows are kept.
            return SnappyLayersPage.DEFAULT_SURFACE_LAYERS
        return super().fresh_value(key, descriptor)

    def open_edit_dialog(self, *args) -> None:
        if self.selected_key() is None:
            return
        self.membership.load(self.selected_key())
        super().open_edit_dialog(*args)

    def _run(self, operation: str, parameters: dict) -> None:
        """The inherited write, followed by the binding it implies."""
        kind = operation.rsplit('.', 1)[-1]

        def ran(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                QMessageBox.warning(
                    self, self.tr('Operation failed'),
                    str(getattr(result, 'message', '')
                        or self.tr('The facade rejected the change.')))
                return
            payload = getattr(result, 'payload', None) or {}
            group = (payload.get('entity_id') if kind == 'create'
                     else parameters.get('entity_id'))

            def finished() -> None:
                self.refresh()
                self.childrenChanged.emit()

            if kind == 'remove':
                unbind_layer_group(self._client, parameters.get('entity_id'),
                                   then=finished)
            else:
                self.membership.commit(group, then=finished)

        submit(self._client, operation, parameters, then=ran)


class SnappyLayersPage(SnappyTaskPage):
    """Prism layers on the patches that asked for them."""

    task_id_default = 'snappy.layers'
    run_stage = 'layers'

    #: Plan 33 OF-07. MEASURED at a 560 px settings column: twelve columns in
    #: 494 px of table scrolled 806 px sideways, so more of every row was
    #: behind the bar than in front of it, and the bar moved the header with
    #: it. These five are what one group has to say to be told apart from the
    #: next: which patches, what is being done to them, how many prisms and
    #: how thick. The rest of the row is edited in the row editor, which is
    #: built from every element field whatever the table shows.
    #: DP-570 (0924 rerun follow-up). MEASURED at the 360 px settings column
    #: a window under 1600 px wide gets: those five scrolled 100 px sideways.
    #: The pattern is what tells two groups over different patches apart, and
    #: the policy and count are what is done to them, so the thickness is in
    #: the row editor beside the thickness model that gives it its meaning,
    #: and the group name takes the room left over.
    COLUMNS = ('group_name', 'patch_pattern', 'layer_policy', 'surface_layers')

    #: The three things a group can ask for, said in words. The stored values
    #: are one word each -- grow, freeze, inherit -- and what they mean is the
    #: difference between a patch snappy leaves out of ``layers {}`` and one
    #: it writes with zero layers, which is not a difference a reader can
    #: infer from the word. It used to be explained in a paragraph above the
    #: table; it is in the picker now, where the choice is made.
    POLICY_CHOICES = (
        ('Grow layers on these patches', 'grow',
         'The group extrudes its layer count on every patch it covers'),
        ('Freeze these patches', 'freeze',
         'Written as zero layers, which stops the patch sliding as well'),
        ('Leave these patches out', 'inherit',
         'The patch is not listed, so it slides with its neighbours'),
    )

    #: What the defaulted group is called on screen. It is an ordinary group:
    #: a user can rename it, re-point it or delete it, and nothing here puts
    #: it back once the case holds a group of its own.
    DEFAULT_GROUP_NAME = 'Walls'

    #: How many layers the defaulted group asks for. The schema default for
    #: ``nSurfaceLayers`` is 0, which snappy reads as *freeze* -- a defaulted
    #: group carrying it would be DP-112 again with a row in front of it, so
    #: the count is written explicitly.
    DEFAULT_SURFACE_LAYERS = 3

    #: Task states in which a default must not be written. R185: a
    #: configuration contradicts a skip, so configuring the step on the user's
    #: behalf would un-skip the step the user had just declined.
    NOT_ENABLED_STATES = ('skipped',)

    def build_sections(self, layout) -> None:
        # C31-11. Built before the panel, because the panel wires it into the
        # editor form as it constructs the editors. DP-312: built with no
        # parent, because until that dialog adopts it a widget parented to
        # the page belongs to none of the page's layouts, and Qt draws an
        # unmanaged child at the default (0, 0, 100, 30) -- the top left
        # corner of the settings column, over whatever the page put there.
        self.pattern_preview = LayerPatternPreview(source=self.patchNames)

        self.panel = LayerGroupPanel(
            self._client, 'meshing.layers.groups',
            self.tr('Surfaces receiving layers'),
            columns=self.COLUMNS, parent=self, stretch='group_name',
            annotations={'patch_pattern': self.pattern_preview})
        # Offered before any editor reference is taken: re-offering a field's
        # choices rebuilds its editor and drops the cached dialog with it.
        self.panel.set_choices('layer_policy', [
            (self.tr(label), value, self.tr(explained), True)
            for label, value, explained in self.POLICY_CHOICES])
        self.panel.childrenChanged.connect(self.refresh)
        # Plan 33 OF-07. First in the column: the surfaces are what this step
        # is about, and the eighteen shrinking and smoothing controls the task
        # declares are the settings behind them.
        layout.insertWidget(0, self.panel)

        # Plan 32 W4. The mark on a defaulted target is a sentence naming it,
        # not a colour: a colour cannot be read aloud, cannot be searched for
        # and does not say why the boundary was chosen.
        self._defaultNote = QLabel(self)
        self._defaultNote.setObjectName('snappyLayerDefaultNote')
        self._defaultNote.setWordWrap(True)
        self._defaultNote.setVisible(False)
        layout.insertWidget(1, self._defaultNote)

        # DP-524. A group that covers no boundary is saved, listed with its
        # layer count, and never written: said here, where the table reads as
        # though it were configured. Hidden while every group covers
        # something, so an empty or complete page says nothing.
        self.unbound_label = QLabel(self)
        self.unbound_label.setObjectName('snappyLayerUnboundGroups')
        self.unbound_label.setWordWrap(True)
        self.unbound_label.setVisible(False)
        layout.insertWidget(2, self.unbound_label)

        editor = self.panel.editor('patch_pattern')
        if editor is not None:
            editor.valueChanged.connect(
                lambda _field, value: self.pattern_preview.setPattern(value))
        # Selecting a row loads it into the editors with signals blocked, so
        # the preview has to be told; without this, opening the dialog on a
        # second group shows the first group's matches.
        self.panel.table.itemSelectionChanged.connect(self.sync_preview)
        self.sync_preview()
        self.sync_unbound_groups()

    def unbound_groups(self) -> list:
        """The names of the layer groups that cover no patch.

        A group selecting by geometry covers the rows bound to it; one
        selecting by pattern covers what its pattern names, so an empty
        pattern is a group over nothing as well.
        """
        panel = getattr(self, 'panel', None)
        if panel is None:
            return []
        try:
            configuration = self._client.configuration() or {}
        except Exception:                        # noqa: BLE001 - advisory only
            configuration = {}
        bound = bound_layer_groups(configuration)
        names = []
        for row in panel.rows():
            selector = str(row.get('patch_selector') or 'geometry')
            if selector == BY_PATTERN:
                covered = bool(str(row.get('patch_pattern') or '').strip())
            else:
                covered = str(row.get('__key__')) in bound
            if not covered:
                names.append(str(row.get('group_name') or row.get('__key__')))
        return names

    def sync_unbound_groups(self) -> None:
        label = getattr(self, 'unbound_label', None)
        if label is None:
            return
        names = self.unbound_groups()
        label.setText(self.tr(
            'Covers no patch yet, so it is not written to the mesher: %s. '
            'Edit the row and choose its boundaries, or give it a patch name '
            'pattern.') % ', '.join(names))
        label.setVisible(bool(names))

    def sync_preview(self) -> None:
        """Re-read the case's patch names and re-run the current pattern."""
        preview = getattr(self, 'pattern_preview', None)
        if preview is None:
            return
        preview.refreshNames()
        editor = self.panel.editor('patch_pattern')
        preview.setPattern(editor.value() if editor is not None else '')

    # -- Plan 32 W4: which boundaries grow layers when nobody said --------- #

    def preparedBoundaries(self) -> tuple:
        """``(name, role)`` for every boundary the prepared geometry names.

        The same payload the Gmsh layers page reads, through the same facade
        query: the boundary roles are the geometry's, not an engine's, so an
        engine-specific second reading of them is a second answer waiting to
        disagree.
        """
        try:
            payload = query(self._client,
                            'geometry.prepared.current', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory only
            return ()
        prepared = payload.get('prepared')
        if not isinstance(prepared, dict):
            return ()
        manifest = prepared.get('group_manifest')
        if not isinstance(manifest, dict) or not manifest:
            manifest = prepared if 'groups' in prepared else {}
        return boundaries_from_manifest(manifest)

    def patchNames(self) -> tuple:
        """The patches the layer stage will be handed, named as the writer
        names them: the prepared groups first, the geometry rows when nothing
        was prepared (DP-491)."""
        prepared = [name for name, _role in self.preparedBoundaries()]
        try:
            configuration = self._client.configuration() or {}
        except Exception:                        # noqa: BLE001 - advisory only
            configuration = {}
        geometry = [str((row or {}).get('name') or '')
                    for row in (configuration.get('geometry') or {}).values()
                    if isinstance(row, dict)]
        return candidate_patch_names(prepared, geometry)

    def defaultTargets(self) -> tuple:
        """The boundaries a layer group would default to, by role."""
        return default_layer_targets(self.preparedBoundaries())

    def layersEnabled(self) -> bool:
        """Whether this step is one the case means to run.

        A step the user has skipped is not enabled, and configuring it behind
        them would contradict the skip (R185) -- Proceed would then run a
        stage nobody asked for.
        """
        try:
            return self.task_state()[0] not in self.NOT_ENABLED_STATES
        except Exception:                        # noqa: BLE001 - advisory only
            return True

    def defaultTargetGroup(self) -> None:
        """Store one layer group on the walls when the case holds none.

        DP-112: an empty ``layers { }`` is a stage that runs, succeeds and
        adds nothing. DP-228: the run must consume what the page shows, so
        the group is written through the facade at the moment it is chosen
        rather than left as an unsaved proposal.
        """
        if getattr(self, '_defaultingTargets', False):
            return
        if self.panel.rows() or not self.layersEnabled():
            return
        targets = self.defaultTargets()
        if not targets:
            return
        self._defaultingTargets = True
        try:
            submit(self._client, 'meshing.layers.groups.create', {'fields': {
                'group_name': self.DEFAULT_GROUP_NAME,
                'layer_policy': 'grow',
                'surface_layers': self.DEFAULT_SURFACE_LAYERS,
                'patch_selector': 'pattern',
                # One key selecting exactly these names. `layerParameters.C`
                # reads a quoted key as a `wordRe`, and the writer quotes it.
                'patch_pattern': default_layer_pattern(targets),
            }}, then=lambda _result: self.panel.refresh())
        finally:
            self._defaultingTargets = False
        self._defaultedTargets = tuple(targets)

    def updateDefaultNote(self) -> None:
        """Name the boundaries the role rule chose, and the ones it left."""
        if not hasattr(self, '_defaultNote'):
            return
        targets = tuple(getattr(self, '_defaultedTargets', ()) or ())
        if not targets:
            self._defaultNote.clear()
            self._defaultNote.setVisible(False)
            return
        boundaries = self.preparedBoundaries()
        self._defaultNote.setText(defaulted_targets_sentence(
            targets, flat_boundaries(boundaries)))
        self._defaultNote.setVisible(True)

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, 'panel'):
            self.defaultTargetGroup()
        self.updateDefaultNote()
        self.sync_preview()
        self.sync_unbound_groups()
