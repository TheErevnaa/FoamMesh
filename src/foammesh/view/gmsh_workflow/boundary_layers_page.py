"""Gmsh workflow page: gmsh.boundary_layers."""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QVBoxLayout

from .base import GmshTaskPage
from foammesh.view.facade_client import query


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

    #: The field the selection is stored in. It stays visible as a text field
    #: as well: it is what the job file records, and a patch the prepared
    #: geometry cannot name has to remain typeable.
    PATCH_FIELD = 'gmsh.boundary_layers.patches'

    #: Categories whose patches are pre-selected. MEASURED on the venturi:
    #: layers on the inlet and the outlet put the worst elements of the mesh
    #: on the two flow faces (z=0.004999 and z=0.5937), which is wrong for
    #: every flow case.
    LAYER_CATEGORIES = ('wall',)

    SCOPE_NOTE = (
        'Layers grow on the patches ticked below. With none ticked they grow '
        'on every boundary surface, inlets and outlets included, where prism '
        'cells sit across the flow face.')

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
        scope = QLabel(self.tr(self.SCOPE_NOTE), self)
        scope.setObjectName('gmshLayerScopeNote')
        scope.setWordWrap(True)
        layout.addWidget(scope)

        # R118. The selection the runner honours, offered as the patch names
        # the user gave the boundaries two steps earlier rather than as tags.
        self._patchBox = QGroupBox(self.tr('Patches that grow layers'), self)
        self._patchBox.setObjectName('gmshLayerPatchBox')
        self._patchLayout = QVBoxLayout(self._patchBox)
        self._patchChecks = {}
        layout.addWidget(self._patchBox)

        self._patchNote = QLabel(self)
        self._patchNote.setObjectName('gmshLayerPatchNote')
        self._patchNote.setWordWrap(True)
        layout.addWidget(self._patchNote)

        measured = QLabel(self.tr(
            'After a run, the achieved first-layer height is measured from '
            'the mesh and reported beside the requested one.'), self)
        measured.setWordWrap(True)
        layout.addWidget(measured)

        # R117. `Total Thickness` stayed at its shipped 0.002 while first
        # height / ratio / count implied 0.001444, and nothing on the page
        # marked it inactive or recomputed it.
        self._stackNote = QLabel(self)
        self._stackNote.setObjectName('gmshLayerStackNote')
        self._stackNote.setWordWrap(True)
        self._stackNote.setVisible(False)
        layout.addWidget(self._stackNote)

    def refresh(self) -> None:
        super().refresh()
        if not hasattr(self, '_stackNote'):
            return
        self.updatePatchSelector()
        # The editors are rebuilt on every refresh, so the live hook is
        # reconnected here rather than once at construction.
        for field_id in self._STACK_FIELDS.values():
            editor = self._editors.get(field_id)
            if editor is not None:
                editor.valueChanged.connect(self._onStackFieldChanged)
        self.updateStackNote()

    # -- which patches grow layers ----------------------------------------- #

    def preparedPatches(self) -> list:
        """``(patch name, boundary category)`` for the prepared geometry.

        R118. These are the names the user gave the boundaries two steps
        earlier -- `inlet`, `outlet`, `wall_venturi` -- which is what makes a
        selection something a user can make. The category is the one snappy
        publication reads, so both engines agree on which patch is a wall.
        """
        try:
            payload = query(self._client,
                            'geometry.prepared.current', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory only
            return []
        prepared = payload.get('prepared')
        if not isinstance(prepared, dict):
            return []
        manifest = prepared.get('group_manifest')
        if not isinstance(manifest, dict) or not manifest:
            manifest = prepared if 'groups' in prepared else {}
        patches, seen = [], set()
        for group in manifest.get('groups') or ():
            if not isinstance(group, dict):
                continue
            name = str(group.get('solver_name')
                       or group.get('name') or '').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            patches.append((name, str(group.get('category') or 'wall').lower()))
        return patches

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

        R118. An empty field means every boundary surface, which is what the
        pipeline shipped and what put prisms on the venturi inlet and outlet
        planes. With prepared geometry to read, the page proposes the wall
        patches instead and says that it has done so, rather than quietly
        leaving the two flow faces layered.
        """
        if not hasattr(self, '_patchBox'):
            return
        editor = self._editors.get(self.PATCH_FIELD)
        if editor is not None:
            editor.valueChanged.connect(self._onPatchFieldEdited)
        patches = self.preparedPatches()
        selected = self.selectedPatches()
        proposed = []
        if patches and not selected:
            proposed = [name for name, category in patches
                        if category in self.LAYER_CATEGORIES]
            if proposed:
                self.writeSelection(proposed)
                self.storeProposal()
                selected = self.selectedPatches()
        self.rebuildPatchChecks(patches, selected)
        self.updatePatchNote(patches, selected, proposed)

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

    def rebuildPatchChecks(self, patches, selected) -> None:
        while self._patchLayout.count():
            item = self._patchLayout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._patchChecks = {}
        chosen = set(selected)
        for name, category in patches:
            self.addPatchCheck(name, f'{name} ({category})', name in chosen)
        # A name in the field that no prepared patch answers to still has to
        # show, or the boxes would quietly contradict the field -- and the
        # runner warns about exactly that name when the run reaches it.
        for name in selected:
            if name not in self._patchChecks:
                self.addPatchCheck(
                    name,
                    str(self.tr('%s - not in the prepared geometry')) % name,
                    True)
        self._patchBox.setVisible(bool(self._patchChecks))

    def addPatchCheck(self, name: str, label: str, checked: bool) -> None:
        box = QCheckBox(label, self._patchBox)
        box.setObjectName('gmshLayerPatch')
        box.setAccessibleName(str(self.tr('Grow layers on %s')) % name)
        box.setChecked(checked)
        box.toggled.connect(self._onPatchToggled)
        self._patchLayout.addWidget(box)
        self._patchChecks[name] = box

    def _onPatchToggled(self, *_args) -> None:
        self.writeSelection([name for name, box in self._patchChecks.items()
                             if box.isChecked()])
        self.updatePatchNote(self.preparedPatches(), self.selectedPatches(), [])

    def _onPatchFieldEdited(self, *_args) -> None:
        """Typing in the field is the same edit as ticking a box."""
        patches = self.preparedPatches()
        selected = self.selectedPatches()
        self.rebuildPatchChecks(patches, selected)
        self.updatePatchNote(patches, selected, [])

    def updatePatchNote(self, patches, selected, proposed) -> None:
        """Say what a run would do now, in patch names."""
        if not patches and not selected:
            self._patchNote.setText(self.tr(
                'No prepared geometry yet, so there are no patch names to '
                'offer. Prepare the geometry, or type a comma-separated list '
                'of patch names into Patches.'))
            self._patchNote.setVisible(True)
            return
        if not selected:
            self._patchNote.setText(str(self.tr(
                'Nothing is ticked, so layers grow on every boundary surface: '
                '%s.')) % ', '.join(name for name, _category in patches))
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
            text += ' ' + str(self.tr(
                'Nothing was saved for this case, so the wall patches are '
                'proposed here; press Update to save the selection.'))
        self._patchNote.setText(text)
        self._patchNote.setVisible(True)

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
            'does not read a value entered here.'))
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
