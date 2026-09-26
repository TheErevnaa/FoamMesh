"""Gmsh workflow page: gmsh.boundary_layers."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout,
    QWidget)

from foammesh.app import app
from foammesh.core.geometry.boundary_roles import (
    LAYER_ROLES, defaulted_targets_sentence)
from foammesh.core.gmsh.layer_targets import (
    MODE_ALL_WALLS, boundary_category, eligible_wall_names, normalise_mode)
from foammesh.view.theming.metrics import apply_form_metrics
from foammesh.view.theming.status_colors import apply_color_swatch

from .base import GmshTaskPage


class GmshBoundaryLayersPage(GmshTaskPage):
    """Prism layers grown into the volume, on the patches that need them.

    R118. The page used to say Gmsh could not restrict layers to chosen
    patches, because every closure tried until now reused the *original*
    un-extruded surface -- which double-covers the plane the extrusion's own
    lateral faces already sit in, leaving the core hollow. The runner deletes
    each un-extruded surface instead and rebuilds it from the extrusion's
    inner rim. MEASURED live (gmsh 4.15.2) on a 0.1 x 0.1 x 0.6 m duct: with
    layers on the wall only, 0 prisms on either end plane against 388 with the
    whole boundary extruded, 0.005999988 m^3 against an analytic 0.006, and
    every boundary face still named by a patch (inlet 308, outlet 310,
    wall_duct 4178).
    """

    task_id_default = 'gmsh.boundary_layers'

    #: LAYER-01. Announces which prepared boundary the page is now about, so
    #: that anything showing a second selection can let go of it. The payload
    #: is the prepared boundary's own stable id, which is what the selection
    #: service and every other page key a surface by. W-O2, Plan 33 section 6
    #: check 9: the highlight a reader sees is not done by anyone listening to
    #: this -- `highlightSurface` calls `app.selectionService.select` itself,
    #: on the line after the emit, and that is what
    #: `test_the_layer_selector_is_first.py` measures.
    selectedSurfaceChanged = Signal(str)

    #: The field the selection is stored in. It stays reachable as a text
    #: field under Advanced: it is what the job file records, and a patch the
    #: prepared geometry cannot name has to remain typeable.
    PATCH_FIELD = 'gmsh.boundary_layers.patches'

    #: Plan 33 section 1.1. Whether the list above is the answer, or whether
    #: the run reads every eligible wall off the geometry it imports.
    PATCH_MODE_FIELD = 'gmsh.boundary_layers.patch_mode'

    #: Categories whose patches are pre-selected. MEASURED on the venturi:
    #: layers on the inlet and the outlet put the worst elements of the mesh
    #: on the two flow faces (z=0.004999 and z=0.5937), which is wrong for
    #: every flow case.
    #:
    #: Plan 32 W4: the answer is the core rule, not a copy of it. This page
    #: and the snappy layers page were each deciding which boundary grows
    #: layers, so the same question had two homes; it now has one, in
    #: ``core/geometry/boundary_roles``, and this is the name it goes by here.
    LAYER_CATEGORIES = LAYER_ROLES

    SCOPE_NOTE = (
        'Layers grow on the surfaces ticked here and on no others. A layer '
        'on an inlet or an outlet puts prism cells across the flow face, so '
        'the walls are ticked to begin with.')

    #: W-O1. What becomes of the requested first-layer height once a run has
    #: happened. A constant rather than a literal at the call site because
    #: two controls are told it and DP-185 refuses the same sentence written
    #: twice in one file.
    MEASURED_NOTE = (
        'After a run, the achieved first-layer height is measured from the '
        'mesh and reported beside the requested one.')

    #: Storage keys :func:`derive_boundary_layers` reads, against the semantic
    #: field ids this page edits.
    _STACK_FIELDS = {
        'mode': 'gmsh.boundary_layers.mode',
        'layerCount': 'gmsh.boundary_layers.layer_count',
        'ratio': 'gmsh.boundary_layers.ratio',
        'firstHeight': 'gmsh.boundary_layers.first_height',
        'totalThickness': 'gmsh.boundary_layers.total_thickness',
    }
    #: Which field each mode derives rather than reads. R61/R117/R154: the
    #: page presented all four as independent controls, so two contradictory
    #: definitions of one stack sat four rows apart with nothing saying which
    #: the mesher would use.
    _DERIVED_FIELD = {
        'first_and_ratio': 'gmsh.boundary_layers.total_thickness',
        'total_and_count': 'gmsh.boundary_layers.first_height',
    }

    def build_sections(self, layout) -> None:
        # LAYER-01. Everything about *where* a layer stands, in one box that
        # sits directly under `Enabled` and above every thickness. The
        # thicknesses are claimed into a box of this page's own below it,
        # because the shared form draws its guided fields before
        # `build_sections` is ever called and would otherwise put them first.
        self._surfaceBox = QGroupBox(self.tr('Surfaces receiving layers'),
                                     self)
        self._surfaceBox.setObjectName('gmshLayerSurfaceBox')
        surfaces = QVBoxLayout(self._surfaceBox)
        self._surfaceForm = QFormLayout()
        apply_form_metrics(self._surfaceForm)
        # DP-154. This form is nested inside the box layout rather than being
        # the box layout, so it paid the box layout's default margin on top
        # of its own and its editors began 9 px to the right of every other
        # editor on the page. MEASURED before this line, in the product font:
        # the editors of `gmshLayerSurfaceBox` began at x=134 and those of
        # `gmshLayerStackBox`, Guided and Advanced at x=125. The margin that
        # keeps the form off the border is carried by the box layout, which
        # is what puts the notes and the tick list on the same edge, and the
        # form itself adds nothing further.
        surfaces.setContentsMargins(self._surfaceForm.contentsMargins())
        self._surfaceForm.setContentsMargins(0, 0, 0, 0)
        surfaces.addLayout(self._surfaceForm)

        scope = QLabel(self.tr(self.SCOPE_NOTE), self._surfaceBox)
        scope.setObjectName('gmshLayerScopeNote')
        scope.setWordWrap(True)
        # W-O1. What the tick list means is an explanation of the control
        # below it, so it is that control's tooltip and accessible
        # description rather than a paragraph above it. The label carries the
        # words; `describeSurfaceBox` draws them, and adds the catalogue
        # sentence when there is nothing to tick.
        scope.setVisible(False)
        self._scopeNote = scope
        surfaces.addWidget(scope)

        # R118. The selection the runner honours, offered as the patch names
        # the user gave the boundaries two steps earlier rather than as tags,
        # each beside the colour its boundary is drawn in.
        self._patchLayout = QVBoxLayout()
        self._patchChecks = {}
        self._patchSwatches = {}
        surfaces.addLayout(self._patchLayout)

        self._selectedCount = QLabel(self._surfaceBox)
        self._selectedCount.setObjectName('gmshLayerSelectedCount')
        self._selectedCount.setWordWrap(True)
        surfaces.addWidget(self._selectedCount)

        # LAYER-02. When the catalogue cannot be read the box says so in one
        # sentence and stays where it is. Hiding it left the free-text field
        # under Advanced as the only way to name a surface, which is the one
        # arrangement in which a user cannot tell that a list exists.
        self._catalogueNote = QLabel(self._surfaceBox)
        self._catalogueNote.setObjectName('gmshLayerCatalogueNote')
        self._catalogueNote.setWordWrap(True)
        # W-O1. LAYER-02's sentence is the state of the case -- no prepared
        # geometry has been read -- and it is said on the box that would have
        # listed the boundaries, where a reader who wonders why the list is
        # empty is already looking. The box stays exactly where it was.
        self._catalogueNote.setVisible(False)
        surfaces.addWidget(self._catalogueNote)

        self._patchNote = QLabel(self._surfaceBox)
        self._patchNote.setObjectName('gmshLayerPatchNote')
        self._patchNote.setWordWrap(True)
        surfaces.addWidget(self._patchNote)
        # W-O1. A tooltip from the first paint, before any refresh has run.
        self.describeSurfaceBox(True)

        self._stackBox = QGroupBox(self.tr('How thick the layers are'), self)
        self._stackBox.setObjectName('gmshLayerStackBox')
        self._stackForm = QFormLayout(self._stackBox)
        apply_form_metrics(self._stackForm)

        # W-O1. This stood under the stack box as a paragraph promising a
        # measurement that is made somewhere else. It explains one field --
        # what happens to the first-layer height after a run -- so it is that
        # field's tooltip and the stack box's description, and the promise is
        # kept by the Boundary layers panel on Qualification summary, which
        # is where the achieved numbers are actually shown.
        self._stackBox.setToolTip(self.tr(self.MEASURED_NOTE))
        self._stackBox.setAccessibleDescription(self.tr(self.MEASURED_NOTE))
        first = self._editors.get('gmsh.boundary_layers.first_height')
        if first is not None:
            first.editor.setToolTip(self.tr(self.MEASURED_NOTE))

        # R117. `Total Thickness` stayed at its shipped 0.002 while first
        # height / ratio / count implied 0.001444, and nothing on the page
        # marked it inactive or recomputed it.
        self._stackNote = QLabel(self)
        self._stackNote.setObjectName('gmshLayerStackNote')
        self._stackNote.setWordWrap(True)
        self._stackNote.setVisible(False)

        # DP-52/DP-53/DP-88. An assembly carries layers only where every
        # ticked patch bounds the same volume, and the run seam refuses
        # anything else. Grading the selection here means the answer is
        # met before the stack is filled in rather than after.
        self._assemblyNote = QLabel(self)
        self._assemblyNote.setObjectName('gmshLayerAssemblyNote')
        self._assemblyNote.setWordWrap(True)
        self._assemblyNote.setVisible(False)

        index = layout.indexOf(self._guided) + 1
        for widget in (self._surfaceBox, self._stackBox,
                       self._stackNote, self._assemblyNote):
            layout.insertWidget(index, widget)
            index += 1

    # -- where each control lands ------------------------------------------ #

    def field_form(self, field_id: str, classification):
        """Put the where-question above the how-thick one (LAYER-01).

        Both halves are `DERIVATION` fields, so the shared form would draw
        them in declaration order inside one `Guided` box, with the free-text
        patch list first and the named list of boundaries nowhere near it.
        """
        if not hasattr(self, '_surfaceForm'):
            return None
        if field_id == self.PATCH_MODE_FIELD:
            return self._surfaceForm
        if field_id in self._STACK_FIELDS.values():
            return self._stackForm
        if field_id == self.PATCH_FIELD:
            # LAYER-02. The alternative, not the answer: a patch the prepared
            # geometry cannot name still has to be typeable, and that is an
            # advanced need rather than the first thing the step asks.
            if field_id not in self._advanced_fields:
                self._advanced_fields.append(field_id)
            return self._advanced.layout()
        return None

    def field_forms(self):
        return ((self._surfaceForm, self._stackForm)
                if hasattr(self, '_surfaceForm') else ())

    def advancedHolds(self, editor) -> bool:
        """Whether the Advanced box is what draws this editor's row."""
        return self._advanced.isAncestorOf(editor.editor)

    def derived_quantities(self) -> tuple:
        """LAYER-04. The total the four inputs add up to, in metres.

        R117 showed the stack twice and let the two copies disagree. The
        total is the number a reader checks the near-wall resolution with,
        so it is stated beside the inputs rather than left to be multiplied
        out by hand.
        """
        stack, _values = self.derivedStack()
        if stack is None:
            return ()
        return ((self.tr('Total thickness'),
                 '%.6g' % stack.total_thickness, 'm'),)

    def refresh(self) -> None:
        super().refresh()
        if not hasattr(self, '_stackNote'):
            return
        editor = self._editors.get(self.PATCH_MODE_FIELD)
        if editor is not None:
            editor.valueChanged.connect(self._onPatchModeChanged)
        self.updatePatchSelector()
        # The editors are rebuilt on every refresh, so the live hook is
        # reconnected here rather than once at construction.
        for field_id in self._STACK_FIELDS.values():
            editor = self._editors.get(field_id)
            if editor is not None:
                editor.valueChanged.connect(self._onStackFieldChanged)
        self.updateStackNote()
        self.updateAssemblyNote()

    # -- which patches grow layers ----------------------------------------- #

    def preparedPatches(self) -> list:
        """``(patch name, boundary category)`` for the prepared geometry.

        R118. These are the names the user gave the boundaries two steps
        earlier -- `inlet`, `outlet`, `wall_venturi` -- which is what makes a
        selection something a user can make. The category is the one snappy
        publication reads, so both engines agree on which patch is a wall.
        """
        # DP-92. Through the canonical map, because the second half of an
        # interface written twice is merged away before the mesher sees it:
        # offering it here is offering a patch layers cannot grow on.
        canonical = self.canonicalNames()
        patches, seen = [], set()
        for group in self.preparedGroupManifest().get('groups') or ():
            if not isinstance(group, dict):
                continue
            name = str(group.get('solver_name')
                       or group.get('name') or '').strip()
            if not name or canonical.get(name, name) != name or name in seen:
                continue
            seen.add(name)
            patches.append((name, str(group.get('category') or 'wall').lower()))
        return patches

    def preparedIdentity(self) -> dict:
        """Patch name -> what the viewport and the selection know it by.

        LAYER-01. A row that names a surface has to be able to point at it:
        the actor is keyed by geometry id and the selection service by the
        prepared boundary's stable id, and both are recorded on the group the
        name came from.
        """
        identity = {}
        for group in self.preparedGroupManifest().get('groups') or ():
            if not isinstance(group, dict):
                continue
            name = str(group.get('solver_name')
                       or group.get('name') or '').strip()
            if not name or name in identity:
                continue
            geometry_id = str(group.get('geometry_id') or '').strip()
            if not geometry_id:
                for ref in group.get('source_refs') or ():
                    if isinstance(ref, dict):
                        geometry_id = str(ref.get('geometry_id') or '').strip()
                        if geometry_id:
                            break
            identity[name] = (geometry_id,
                              str(group.get('patch_uuid') or '').strip())
        return identity

    def surfaceColour(self, geometry_id: str):
        """The colour the viewport draws one prepared boundary in."""
        if not geometry_id:
            return None
        window = getattr(app, 'window', None)
        manager = getattr(window, 'geometryManager', None)
        if manager is None:
            return None
        try:
            info = manager.actorInfo(geometry_id)
        except Exception:                        # noqa: BLE001 - advisory only
            return None
        return None if info is None else info.color()

    def surfaceSwatches(self) -> dict:
        """Patch name -> the swatch drawn beside its row."""
        return dict(getattr(self, '_patchSwatches', {}))

    def eligibleWalls(self) -> tuple:
        """The prepared boundaries a layer may stand on.

        LAYER-03. One rule, shared with the run: `eligible_wall_names` is the
        same function `runner_v1` calls when the choice is every eligible
        wall, so the list a user ticks from cannot differ from the list Gmsh
        extrudes.
        """
        return tuple(eligible_wall_names(self.preparedPatches()))

    def patchMode(self) -> str:
        """Whether the ticks are the answer, or every eligible wall is."""
        editor = self._editors.get(self.PATCH_MODE_FIELD)
        if editor is None:
            return ''
        return normalise_mode(editor.value())

    def preparedFaceFacts(self) -> dict:
        """The same, with each interface folded onto the face that survives.

        DP-92. The merged-away half of an interface takes its neighbours with
        it. Leaving them behind is how the first version of this read that
        the `annulus_shell` interface touched nothing in its own volume,
        which is the whole of what [DP-91](#dp-91) refuses.
        """
        measured = self.measuredFaces()
        canonical = {name: (fact['interface'] or name)
                     for name, fact in measured.items()}
        facts = {}
        for name, fact in measured.items():
            home = canonical.get(name, name)
            entry = facts.setdefault(
                home, {'planar': fact['planar'], 'adjacent': set(),
                       'interface': fact['interface']})
            entry['adjacent'].update(
                canonical.get(other, other) for other in fact['adjacent'])
        for name, entry in facts.items():
            entry['adjacent'] = sorted(entry['adjacent'] - {name})
        return facts

    def selectedPatches(self) -> list:
        """The patch names the field holds, in the order they were written."""
        editor = self._editors.get(self.PATCH_FIELD)
        raw = editor.value() if editor is not None else ''
        names, seen = [], set()
        for candidate in str(raw or '').replace('\n', ',').split(','):
            name = candidate.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def writeSelection(self, names) -> None:
        """Put a selection into the field the runner actually reads."""
        editor = self._editors.get(self.PATCH_FIELD)
        if editor is None:
            return
        value = ', '.join(names)
        if str(editor.value() or '') == value:
            return
        editor.set_value(value)
        # `set_value` blocks the editor's own signal, so the edit has to be
        # recorded here or `Update` would stay disabled over a changed field.
        self._on_field_changed(self.PATCH_FIELD, value)

    def updatePatchSelector(self) -> None:
        """Rebuild the tick boxes, and default them to the walls.

        Plan 33 section 1.1. An empty field used to mean every boundary
        surface, which is what the pipeline shipped and what put prisms on
        the venturi inlet and outlet planes. It now means nothing is chosen
        and the run is refused, so the page proposes the wall boundaries and
        says that it has done so. `All eligible walls` is the other answer:
        the ticks then follow the geometry in front of the run.
        """
        if not hasattr(self, '_surfaceBox'):
            return
        editor = self._editors.get(self.PATCH_FIELD)
        if editor is not None:
            editor.valueChanged.connect(self._onPatchFieldEdited)
        patches = self.preparedPatches()
        regions = self.preparedRegions()
        selected = self.selectedPatches()
        mode = self.patchMode()
        known = {name for name, _category in patches}
        if (patches and selected and self._proposedNames
                and set(selected) == set(self._proposedNames)
                and self._proposedAgainst != known):
            # LAYER-02. The list is the current prepared revision, so a
            # proposal this page made against an earlier one is withdrawn and
            # made again. Only the page's own proposal is dropped: a name a
            # user typed is theirs, and it goes on showing even where the
            # prepared geometry cannot answer to it.
            self.writeSelection([])
            selected = []
        proposed = []
        if mode == MODE_ALL_WALLS:
            # The choice is the answer, so the names are only a record of it.
            # Keeping them in step means a saved case still reads as the set
            # it would grow on, and a reader can see which surfaces those are.
            walls = list(self.eligibleWalls())
            if walls and set(walls) != set(selected):
                self.writeSelection(walls)
                self.rememberProposal(walls, known)
                self.storeProposal()
                selected = self.selectedPatches()
        elif patches and not selected:
            proposed = self.proposedSelection(patches, regions)
            if proposed:
                self.writeSelection(proposed)
                self.rememberProposal(proposed, known)
                self.storeProposal()
                selected = self.selectedPatches()
        self.rebuildPatchChecks(patches, selected, regions)
        self.updateCatalogueNote(patches)
        self.updateSelectedCount(mode)
        self.updatePatchNote(patches, selected, proposed)

    def _onPatchModeChanged(self, *_args) -> None:
        """Choosing `All eligible walls` re-reads the prepared geometry."""
        if getattr(self, '_readingMode', False):
            return
        self._readingMode = True
        try:
            self.updatePatchSelector()
            self.updateAssemblyNote()
        finally:
            self._readingMode = False

    #: The selection this page proposed, and the boundaries it was proposed
    #: against. A proposal is the page's own answer to one prepared revision;
    #: a name the user typed is not, and is never withdrawn behind their back.
    _proposedNames: tuple = ()
    _proposedAgainst: frozenset = frozenset()

    def rememberProposal(self, names, known) -> None:
        """Record that this selection is the page's own, not the user's."""
        self._proposedNames = tuple(names)
        self._proposedAgainst = frozenset(known)

    def storeProposal(self) -> None:
        """Write the proposed selection through, instead of leaving it dirty.

        R118 pre-ticks the wall patches so layers do not grow across the inlet
        and outlet. It wrote that proposal into the widget only. The stored
        field stayed empty, and an empty field means *every* boundary surface
        -- so the page showed wall-only ticks while a run would have layered
        the flow faces, which is the exact defect R118 records fixing.

        MEASURED in the T1 strict-GUI sweep, every Gmsh run of ten: the
        proposal re-dirtied the page inside `refresh()` immediately after each
        successful Update, so `Update` stayed lit over an edit the user had
        just saved, the page could never reach a clean state, and the harness
        recorded `the page refused the edit and kept it pending` on all ten.

        The proposal is the page's own, not the user's, so it is stored at the
        moment it is made. `apply()` refreshes on success, which re-enters
        `updatePatchSelector`; by then the field answers and no second
        proposal is made, but the guard makes that termination explicit rather
        than incidental.
        """
        if getattr(self, '_storingProposal', False):
            return
        self._storingProposal = True
        try:
            self.apply()
        finally:
            self._storingProposal = False

    def rebuildPatchChecks(self, patches, selected, regions=()) -> None:
        while self._patchLayout.count():
            item = self._patchLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparented before it is queued for deletion: `deleteLater`
                # alone leaves the row in the widget tree until the event
                # loop next runs, so a refresh that renamed every boundary
                # showed both namings at once.
                widget.setParent(None)
                widget.deleteLater()
        self._patchChecks = {}
        self._patchSwatches = {}
        chosen = set(selected)
        locked = self.patchMode() == MODE_ALL_WALLS
        identity = self.preparedIdentity()
        # DP-88. On an assembly the rule is which volume a patch bounds,
        # and a user told to "name the patches of one volume" had nothing
        # on screen saying which those were.
        owners = self.regionsByPatch(regions) if len(regions) > 1 else {}
        for name, category in patches:
            label = f'{name} ({category})'
            volumes = sorted(owners.get(name, ()))
            if volumes:
                label = str(self.tr('%s — %s')) % (label,
                                                   ', '.join(volumes))
            self.addPatchCheck(name, label, name in chosen,
                               identity.get(name, ('', ''))[0], locked)
        # A name in the field that no prepared patch answers to still has to
        # show, or the boxes would quietly contradict the field -- and the
        # runner warns about exactly that name when the run reaches it.
        for name in selected:
            if name not in self._patchChecks:
                self.addPatchCheck(
                    name,
                    str(self.tr('%s — not in the prepared geometry'))
                    % name,
                    True, '', locked)

    def addPatchCheck(self, name: str, label: str, checked: bool,
                      geometry_id: str = '', locked: bool = False) -> None:
        """One row: the colour the surface is drawn in, then its name.

        LAYER-01. The colour is the only thing that ties a name in this
        column to a face in the viewport, and it is read from the actor
        rather than assigned here, so a row cannot show a colour the surface
        is not drawn in.
        """
        row = QWidget(self._surfaceBox)
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        swatch = QLabel(row)
        swatch.setObjectName('gmshLayerSwatch')
        swatch.setFixedSize(16, 16)
        swatch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        apply_color_swatch(swatch, self.surfaceColour(geometry_id))
        line.addWidget(swatch)
        box = QCheckBox(label, row)
        box.setObjectName('gmshLayerPatch')
        box.setProperty('foammeshLayerPatch', name)
        box.setAccessibleName(str(self.tr('Grow layers on %s')) % name)
        box.setChecked(checked)
        box.setEnabled(not locked)
        box.toggled.connect(
            lambda state, key=name: self._onPatchToggled(key, state))
        line.addWidget(box, 1)
        self._patchLayout.addWidget(row)
        self._patchChecks[name] = box
        self._patchSwatches[name] = swatch

    def updateCatalogueNote(self, patches) -> None:
        """LAYER-02. One sentence when the boundaries cannot be listed."""
        if not hasattr(self, '_catalogueNote'):
            return
        empty = not patches
        if empty:
            self._catalogueNote.setText(self.tr(
                'No prepared geometry has been read, so the boundaries of '
                'this case cannot be listed here yet'))
        # W-O1. Never on the form. See `describeSurfaceBox`.
        self._catalogueNote.setVisible(False)
        self.describeSurfaceBox(empty)

    def describeSurfaceBox(self, empty: bool) -> None:
        """Say what the tick list is, and why it is empty when it is.

        W-O1. One tooltip on one box rather than two paragraphs stacked over
        it. Composed here so the two sentences cannot disagree about whether
        there is anything to tick.
        """
        box = getattr(self, '_surfaceBox', None)
        scope = getattr(self, '_scopeNote', None)
        if box is None or scope is None:
            return
        said = scope.text()
        if empty:
            said = (said + ' ' + self._catalogueNote.text()).strip()
        box.setToolTip(said)
        box.setAccessibleDescription(said)

    def updateSelectedCount(self, mode: str = '') -> None:
        """LAYER-01. How many of the listed surfaces a layer stands on."""
        if not hasattr(self, '_selectedCount'):
            return
        total = len(self._patchChecks)
        chosen = sum(1 for box in self._patchChecks.values()
                     if box.isChecked())
        text = str(self.tr('%d of %d surfaces chosen')) % (chosen, total)
        if mode == MODE_ALL_WALLS:
            text += ' ' + self.tr(
                'The run reads them off the geometry it imports, so this '
                'list follows the geometry rather than the other way round.')
        self._selectedCount.setText(text)
        # W-O1. A count of a list that has no rows counts nothing: on a case
        # with no prepared geometry this read "0 of 0 surfaces chosen" over
        # an empty box. It is a derived quantity once there is something to
        # derive it from, and Plan 33 section 1 keeps those.
        self._selectedCount.setVisible(total > 0)

    def highlightSurface(self, name: str) -> None:
        """Show the surface a row names, where the user can see it.

        LAYER-01. The same stable id every other page keys a boundary by, so
        ticking a row here lights up the same face the geometry list does.
        The lighting up is the `select` call below; the signal above it only
        says the page moved on, for anything holding a stale selection.
        """
        reference = self.preparedIdentity().get(name, ('', ''))[1]
        self.selectedSurfaceChanged.emit(reference)
        if not reference:
            return
        try:
            if app.selectionService.entity(reference) is not None:
                app.selectionService.select((reference,))
        except (AttributeError, ValueError):
            pass

    def _onPatchToggled(self, name: str = '', checked: bool = False) -> None:
        self.writeSelection([key for key, box in self._patchChecks.items()
                             if box.isChecked()])
        self.updateSelectedCount(self.patchMode())
        self.updatePatchNote(self.preparedPatches(), self.selectedPatches(),
                             [])
        self.updateAssemblyNote()
        if name and checked:
            self.highlightSurface(name)

    def _onPatchFieldEdited(self, *_args) -> None:
        """Typing in the field is the same edit as ticking a box."""
        patches = self.preparedPatches()
        selected = self.selectedPatches()
        self.rebuildPatchChecks(patches, selected, self.preparedRegions())
        self.updatePatchNote(patches, selected, [])
        self.updateAssemblyNote()

    def updatePatchNote(self, patches, selected, proposed) -> None:
        """Say what a run would do now, in patch names."""
        if not patches and not selected:
            self._patchNote.setText(self.tr(
                'No prepared geometry yet, so there are no patch names to '
                'offer. Prepare the geometry, or type a comma-separated list '
                'of patch names into Patches.'))
            # W-O1. The state of the case, plus what to type where. Neither
            # is a setting, and the second one names a field this page owns,
            # so both go onto that field and the box around the list.
            self._patchNote.setVisible(False)
            editor = self._editors.get(self.PATCH_FIELD)
            if editor is not None:
                editor.editor.setToolTip(self._patchNote.text())
            return
        if not selected:
            # Plan 33 section 1.1. This used to read "layers grow on every
            # boundary surface", which is what the runner did and what put
            # prism cells across the inlet and the outlet. Nothing ticked is
            # now nothing grown, and the run says so before it starts.
            text = str(self.tr(
                'Nothing is ticked, so no layer would grow anywhere and the '
                'run will be refused. Tick the surfaces a layer stands on, '
                'or choose every eligible wall above.'))
            # DP-444. When the offer itself came back empty, the sentence
            # above says what to do and not why there is nothing to do it
            # with. The names are on screen; the categories are not, and the
            # category is the entire reason a surface was not offered. So
            # when every prepared surface was disqualified, name the readings
            # that disqualified them.
            if patches and not eligible_wall_names(patches):
                text += ' ' + str(self.tr(
                    'None of the %d prepared surfaces is offered: a layer '
                    'grows on a wall, and these read as %s. A surface takes '
                    'its category from the leading word of its name unless '
                    'the Geometry page sets one.')) % (
                        len(patches), self.categoriesRead(patches))
            self._patchNote.setText(text)
            self._patchNote.setVisible(True)
            return
        chosen = set(selected)
        flat = [name for name, _category in patches if name not in chosen]
        text = str(self.tr('Layers grow on %s.')) % ', '.join(selected)
        if flat:
            text += ' ' + str(self.tr(
                '%s stay flat; the runner rebuilds them from the inner edge '
                'of the layers, so the mesh still closes.')) % ', '.join(flat)
        if proposed:
            # Plan 32 W4: say which targets were defaulted, in the words the
            # snappy layers page uses for the same event. The sentence this
            # replaced also read wrongly -- `storeProposal` saves the
            # selection as it is made, so there was nothing left for the
            # user to press Update for.
            text += ' ' + defaulted_targets_sentence(proposed)
        self._patchNote.setText(text)
        self._patchNote.setVisible(True)

    def categoriesRead(self, patches) -> str:
        """The categories the prepared surfaces announce, in first order.

        DP-444. Read through the same rule the offer itself is built from,
        so the note cannot name a category the offer disagreed with.
        """
        seen = []
        for name, category in (patches or ()):
            reading = boundary_category(name, category)
            if reading not in seen:
                seen.append(reading)
        return ', '.join(seen)

    # -- derived-vs-entered thickness -------------------------------------- #

    def _onStackFieldChanged(self, *_args) -> None:
        self.updateStackNote()

    def stackValues(self) -> dict:
        """The layer stack as the derivation would read it, live.

        Editor values are preferred over persisted ones so the note answers
        the numbers on screen, not the ones last accepted.
        """
        try:
            stored = self._client.field_values(
                tuple(self._STACK_FIELDS.values()))
        except Exception:                        # noqa: BLE001 - advisory note
            stored = {}
        values = {}
        for key, field_id in self._STACK_FIELDS.items():
            editor = self._editors.get(field_id)
            value = (editor.value() if editor is not None
                     else stored.get(field_id))
            if value is None:
                value = stored.get(field_id)
            values[key] = value
        return values

    def derivedStack(self):
        """What ``derive_boundary_layers`` makes of the values on screen."""
        from foammesh.core.gmsh.layers import LayerError, derive_boundary_layers

        values = dict(self.stackValues())
        # The note describes the stack the numbers define; whether layers are
        # switched on is a separate question and is answered separately.
        values['enabled'] = True
        try:
            return derive_boundary_layers(values), values
        except (LayerError, TypeError, ValueError):
            return None, values

    # `preparedTopology` lives on :class:`GmshTaskPage`: DP-115 gave the same
    # reading to the Compute Mesh page, which shuts the run button this page
    # only warns about.

    def layersEnabled(self) -> bool:
        """Whether layers are switched on, reading the screen before the file.

        The geometry cannot carry layers either way, but a page whose layer
        switch is off has nothing to warn about, and a warning shown then is
        one the user learns to scroll past.
        """
        field_id = 'gmsh.boundary_layers.enabled'
        editor = self._editors.get(field_id)
        if editor is not None:
            return bool(editor.value())
        try:
            return bool(self._client.field_values((field_id,)).get(field_id))
        except Exception:                        # noqa: BLE001 - advisory only
            return False

    def proposedSelection(self, patches, regions) -> list:
        """The patches the page pre-ticks when nothing is stored yet.

        DP-88. On a single volume this is every wall, which is what R118
        shipped. On an assembly it is the walls of *one* volume: the runner
        carves a single core out and refuses a selection that spans two, so
        ticking every wall of a two-volume geometry proposed the one
        selection that cannot mesh. MEASURED on `gmsh annulus_shell` in the
        strict-GUI sweep: the page proposed all seven walls, saved them for
        the user, and the run refused them 71 s later. The volume proposed is
        the one with the most wall patches, then the first by name, so the
        same geometry always proposes the same thing.
        """
        # Plan 32 W4. The role rule is asked for, not re-implemented: the
        # snappy page asks the same function the same question, so the two
        # engines cannot disagree about which boundary is a wall.
        # Plan 33 LAYER-03. The rule the run applies, not a second copy of
        # it: `eligible_wall_names` is what `runner_v1` calls for
        # `All eligible walls`, so the page cannot propose a surface the run
        # would not have grown on.
        walls = list(eligible_wall_names(patches))
        if len(regions) < 2 or not walls:
            return walls
        # DP-123. The same defect, on the geometry the volume rule cannot
        # see: a wrapped STL holding a box inside a box is prepared as one
        # patch whose faces bound the domain outside it and the domain
        # inside it. MEASURED on `two_solid_block` -- two regions, one wall
        # each, so the tie went to `two_solid_block` by name, it was
        # proposed, stored, and refused by the runner 69 s later. A patch
        # that cannot be placed on one volume cannot be the proposal.
        spanning = self.spanningSelection(walls)
        if spanning:
            walls = [name for name in walls if name not in set(spanning)]
            if not walls:
                return []
        ranked = []
        for label, members in regions:
            owned = [name for name in walls if name in set(members)]
            if owned:
                ranked.append((-len(owned), label, owned))
        if not ranked:
            return walls
        ranked.sort(key=lambda item: (item[0], item[1]))
        return self.selectionThatMeshes(ranked[0][2])

    def selectionThatMeshes(self, walls) -> list:
        """Thin the proposal down to a set the mesher will accept.

        DP-92. One volume is necessary and was not sufficient. Two more
        things decide whether a layer selection meshes, and until the CAD
        import measured them the page could not ask either:

        * a patch left without a layer has its opening closed with a plane,
          so an uncovered patch has to be flat -- [DP-90];
        * bases whose layers grow in opposite directions go into separate
          extrusion calls, and a curve shared by two of them would be
          extruded twice, which Gmsh finds only part-way through the 3D pass
          -- [DP-91].

        Within one volume the faces that can be wound the other way are its
        interfaces, so a pair is in doubt when they share an edge and exactly
        one of them is an interface. Those pairs are broken by dropping the
        flat side, which is the side the rebuild can close, largest first.

        MEASURED on `annulus_shell`: the volume's walls are the interface
        cylinder and the outer cylinder, which do not touch, and the two end
        caps, which touch the interface. Dropping the caps leaves
        `annulus_shell_wall1` and `annulus_shell_wall4` -- 302474 cells and
        0.01256056 m3 against an analytic 0.01256637, in 46 s. Every other
        selection the page could reach was refused.

        Where nothing was measured, or where no flat side can be dropped, the
        proposal is returned as it was: the run then refuses with the message
        [DP-90](#dp-90) or [DP-91](#dp-91) writes, which names the way out.
        """
        facts = self.preparedFaceFacts()
        held = [name for name in walls if name in facts]
        if len(held) < 2:
            return list(walls)
        # A face the file carried twice, one copy per solid. It is the only
        # kind that can be wound the other way from its own volume's walls.
        interfaces = {name for name in held if facts[name]['interface']}
        kept = list(walls)
        for _round in range(len(held)):
            chosen = set(kept)
            touching = [
                (one, other) for one in held if one in chosen
                for other in facts[one]['adjacent']
                if other in chosen and one < other
                and (one in interfaces) != (other in interfaces)]
            if not touching:
                return kept
            counts: dict = {}
            for one, other in touching:
                for name in (one, other):
                    if facts.get(name, {}).get('planar'):
                        counts[name] = counts.get(name, 0) + 1
            if not counts:
                break
            drop = sorted(counts, key=lambda name: (-counts[name], name))[0]
            kept = [name for name in kept if name != drop]
        return list(walls)

    def assemblyNoteText(self, volumes, regions) -> str:
        """What an assembly means for the selection that is on screen now."""
        head = str(self.tr(
            'This geometry holds %d volumes. Boundary layers are supported on '
            'an assembly, but every ticked patch has to bound one and the same '
            'volume: the layer is carved out of that volume, and the others '
            'are meshed exactly as they were imported.')) % volumes
        selected = self.selectedPatches()
        if not selected:
            return head + ' ' + str(self.tr(
                'Nothing is ticked, so no layer would grow on any of the %d '
                'volumes and the run will be refused. Tick the patches of '
                'one volume.')) % volumes
        owners = self.regionsByPatch(regions)
        state, common = self.gradeSelection(selected, owners)
        if state == 'ungraded':
            return head + ' ' + str(self.tr(
                'This prepared revision does not record which patch bounds '
                'which volume, so the selection is graded by the run.'))
        if state == 'single':
            rest = [label for label, _members in regions
                    if label != common[0]]
            text = head + ' ' + str(self.tr(
                'The ticked patches all bound %s, so the layer grows into '
                'that volume.')) % common[0]
            if rest:
                text += ' ' + str(self.tr(
                    '%s are meshed without layers.')) % ', '.join(rest)
            return text
        if state == 'shared':
            return head + ' ' + str(self.tr(
                'Every ticked patch is shared by %d volumes (%s), so nothing '
                'says which side the layer belongs on, and the run will be '
                'refused. Tick a patch that bounds only one of them as '
                'well.')) % (len(common), ', '.join(common))
        spread = '; '.join(
            '%s bounds %s' % (name, ', '.join(sorted(owners.get(name, ())))
                              or str(self.tr('no recorded volume')))
            for name in selected)
        return head + ' ' + str(self.tr(
            'The ticked patches span more than one volume (%s), and the run '
            'will be refused. Untick everything outside one volume.')) % spread

    def updateAssemblyNote(self) -> None:
        """Say what this geometry's volumes mean for the layer selection.

        DP-85 made layers work on an assembly whose bases all bound one
        volume. This note went on saying they were not supported on an
        assembly at all and that the run would be refused, and offered only
        the two things that do not work -- mesh without layers, or split the
        job. DP-88 grades the live selection instead, by the same rule the
        runner applies. MEASURED: `gmsh annulus_shell` reached the real
        refusal 71 s into the run, on a selection this page had proposed. A
        revision prepared before the count was recorded says `unknown`, and
        an unknown count with no regions on it says nothing here.
        """
        if not hasattr(self, '_assemblyNote'):
            return
        # DP-115. The run's own pre-flight, asked at page-open. Everything it
        # reads -- layers on, no patch named, a prepared revision that counted
        # more than one volume -- is known before the user authors anything,
        # and it is a refusal rather than an observation: it is said in the
        # refusal's own words and it shuts the button instead of sitting
        # beside it.
        selected = self.selectedPatches()
        refusal = self.assemblyLayerRefusal(enabled=self.layersEnabled(),
                                            patches=selected)
        self.setAssemblyRefusal(refusal)
        regions = self.preparedRegions()
        spanning = self.spanningSelection(selected)
        if refusal and not (regions and selected and not spanning):
            # DP-123. With a graded selection on screen the note below says
            # which patch bounds which volume and what to untick, which is
            # more than the refusal's own sentence can. Without one -- an
            # empty selection, or a revision that records no membership --
            # the refusal is all there is to say, so it is what is said.
            #
            # A patch that covers shells of two volumes is the third case.
            # The note below would grade it as bounding one volume and say
            # so, because that grading holds one set of volumes per patch
            # name; the refusal is right and the prose would not be, so the
            # refusal is said and the patch is named.
            self._assemblyNote.setText(
                self.spanningNoteText(refusal, spanning) if spanning
                else refusal)
            self._assemblyNote.setVisible(True)
            return
        topology = self.preparedTopology()
        try:
            volumes = int(topology.get('volumes') or 0)
        except (TypeError, ValueError):
            volumes = 0
        if str(topology.get('volumes_source') or 'unknown') == 'unknown':
            volumes = 0
        volumes = max(volumes, len(regions))
        if volumes < 2 or not self.layersEnabled():
            self._assemblyNote.setVisible(False)
            return
        self._assemblyNote.setText(self.assemblyNoteText(volumes, regions))
        self._assemblyNote.setVisible(True)

    def spanningNoteText(self, refusal: str, spanning) -> str:
        """The refusal, plus which ticked patch cannot be placed on a volume.

        DP-123. MEASURED on `two_solid_block`: one STL holding a box inside a
        box is prepared as one patch, and its faces bound the farfield on the
        outside and its own interior on the inside. The refusal alone says to
        name the patches of one volume; it does not say that this patch is
        not one of them, and the list it was ticked from offers no other way
        to find out.
        """
        names = ', '.join(spanning)
        return refusal + ' ' + self.tr(
            '%s covers the boundary of more than one volume on this prepared '
            'geometry, so ticking it alone still spans them. Untick it.'
        ) % names

    def setAssemblyRefusal(self, refusal: str) -> None:
        """Shut this page's run button, in the words the run would use.

        DP-115. The button stays where it is and says why it will not go,
        which is the rule the rest of the workflow follows: a live control
        that cannot do anything reads its silence as a failure.
        """
        self.setRunStageAvailable(not refusal, refusal)
        # DP-123. And the branch's "Run to end", which is the button the run
        # is actually started from.
        self.runAllRefusalChanged.emit(refusal)
        status = 'error' if refusal else ''
        if self._assemblyNote.property('foammeshStatus') != status:
            self._assemblyNote.setProperty('foammeshStatus', status)
            style = self._assemblyNote.style()
            style.unpolish(self._assemblyNote)
            style.polish(self._assemblyNote)

    def updateStackNote(self) -> None:
        """Name the derived member of the stack and print its value.

        R61/R117/R154. In `First and ratio` mode the total is fully determined
        by the other three inputs -- first 0.0003, ratio 1.125, 4 layers stack
        to 0.001444 m -- and the field went on reading 0.002. The value is now
        shown where it was entered and the field is disabled, so the page
        carries one specification of the stack rather than two.
        """
        stack, values = self.derivedStack()
        mode = str(values.get('mode') or '').split('.')[-1].lower()
        derived_id = self._DERIVED_FIELD.get(mode)
        for field_id in self._STACK_FIELDS.values():
            editor = self._editors.get(field_id)
            if editor is not None:
                editor.editor.setEnabled(field_id != derived_id)
        editor = self._editors.get(derived_id) if derived_id else None
        if stack is None or editor is None:
            self._stackNote.setVisible(False)
            return
        derived_value = (stack.total_thickness
                         if mode == 'first_and_ratio' else stack.first_height)
        editor.set_value(derived_value)
        editor.editor.setToolTip(self.tr(
            'Derived from the other layer settings in this mode; the mesher '
            'does not read a value entered here'))
        name = editor.descriptor.title or derived_id.rsplit('.', 1)[-1]
        if mode == 'first_and_ratio':
            self._stackNote.setText(self.tr(
                '%s is derived in this mode: %d layers from %.6g m growing at '
                '%.6g stack to %.6g m. The mesher uses that, not a value '
                'typed here.') % (name, stack.layer_count, stack.first_height,
                                  stack.ratio, stack.total_thickness))
        else:
            self._stackNote.setText(self.tr(
                '%s is derived in this mode: %d layers growing at %.6g to a '
                'total of %.6g m start at %.6g m. The mesher uses that, not a '
                'value typed here.') % (name, stack.layer_count, stack.ratio,
                                        stack.total_thickness,
                                        stack.first_height))
        self._stackNote.setVisible(True)
