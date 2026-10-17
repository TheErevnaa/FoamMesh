"""Shared base for the Gmsh task pages."""
from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from foammesh.core.gmsh.execution import (
    canonical_patch_names, grade_layer_selection,
    layer_selection_refusal, measured_faces, prepared_regions,
    regions_by_patch, sources_that_span_volumes,
)
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query
from foammesh.view.widgets.folded_note import FoldedNote
from foammesh.view.gmsh_run_readout import (
    GmshOptionsTier, GmshRunReadout, resync_body_spacer)


class GmshTaskPage(GmshRunReadout, GmshOptionsTier, EngineTaskPage):
    """One Gmsh workflow task.

    The signature is ``(facade_client, parent)``: the engine branch constructs
    every page positionally as ``page_class(client, self)``, so a page that
    took its task id second would receive the parent widget as a task id.
    Each concrete page names its own task instead.
    """

    engine_id = 'gmsh'
    run_all_task_id = 'gmsh.compute'
    task_id_default = ''

    def __init__(self, facade_client, parent=None):
        super().__init__(facade_client, self.task_id_default, parent)
        # Plan 44 FX2. Each run read-out is one line until opened.
        self._runWarnings = FoldedNote(self)
        self._runWarnings.setObjectName('gmshRunWarnings')
        self._runWarnings.setWordWrap(True)
        self._runWarnings.setProperty('foammeshStatus', 'warning')
        self._runWarnings.setAccessibleName(
            self.tr('Warnings from the last Gmsh run'))
        self._runWarnings.setVisible(False)
        # Above the calculated-settings table: a control that the run threw
        # away has to be read before the settings it belongs to, not after.
        self._body_layout.insertWidget(0, self._runWarnings)
        # DP-567. What the run did that is not a problem -- an interface pair
        # that took effect -- in the informational colour, under the warnings.
        self._runNotes = FoldedNote(self)
        self._runNotes.setObjectName('gmshRunNotes')
        self._runNotes.setWordWrap(True)
        self._runNotes.setProperty('foammeshStatus', 'info')
        self._runNotes.setAccessibleName(self.tr('Notes from the last Gmsh run'))
        self._runNotes.setVisible(False)
        self._body_layout.insertWidget(1, self._runNotes)
        # DP-2595. The two rows above moved the trailing spacer down.
        resync_body_spacer(self)
        self.updateRunWarnings()
        self.mountOptionsTier()

    def renders_field(self, field_id: str) -> bool:
        """The stored option overrides are edited by the Options panel."""
        from foammesh.core.gmsh.option_catalogue import OVERRIDES_FIELD

        # Plan 41 GN14: the further quality limits are a table, not a text box.
        if field_id in (OVERRIDES_FIELD, 'gmsh.compute.extra_limits'):
            return False
        return super().renders_field(field_id)

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, '_runWarnings'):
            self.updateRunWarnings()

    def refresh_status(self) -> None:
        """Re-read the task state, and the warnings of the run behind it.

        DP-528. When a pipeline ends the window refreshes the branch through
        ``refresh_states``, which calls this and not ``refresh()``, so a page
        built before the run kept the banner it had then -- none.
        """
        super().refresh_status()
        if hasattr(self, '_runWarnings'):
            self.updateRunWarnings()

    # -- the run-start pre-flight, asked at page-open ---------------------- #

    def preparedRevision(self) -> dict:
        """The prepared revision the case has selected, as plain JSON.

        DP-123. One read, because everything the run-start pre-flight grades
        -- the volume count, the group manifest, the regions each patch
        bounds -- comes off the same payload, and three separate queries for
        it is three chances to grade one revision against another.
        """
        try:
            payload = query(self._client,
                            'geometry.prepared.current', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory only
            return {}
        prepared = payload.get('prepared')
        return prepared if isinstance(prepared, dict) else {}

    def preparedTopology(self) -> dict:
        """What the prepared revision recorded about the geometry.

        DP-52/DP-53. The count is read off the revision rather than measured
        here: classifying a cold STL on the event loop is exactly what the
        Boundary Layers page avoided when it took its patch names from the
        same payload.
        """
        prepared = self.preparedRevision()
        if not prepared:
            return {}
        manifest = prepared.get('manifest')
        if not isinstance(manifest, dict):
            manifest = prepared
        preparation = manifest.get('preparation')
        if not isinstance(preparation, dict):
            return {}
        topology = preparation.get('topology')
        return topology if isinstance(topology, dict) else {}

    # -- which volume each patch bounds ----------------------------------- #

    # DP-123. Moved up from the Boundary Layers page: the run-start
    # pre-flight below has to grade a *named* selection against the
    # volumes it spans, and every page that can start the run needs
    # the same answer. Nothing here measures geometry -- it is all
    # read off the prepared revision's group manifest.

    def preparedGroupManifest(self) -> dict:
        """The prepared revision's group manifest, or an empty dict."""
        prepared = self.preparedRevision()
        if not prepared:
            return {}
        manifest = prepared.get('group_manifest')
        if not isinstance(manifest, dict) or not manifest:
            manifest = prepared if 'groups' in prepared else {}
        return manifest if isinstance(manifest, dict) else {}

    def measuredFaces(self) -> dict:
        """Patch name -> what the CAD import measured about that face.

        DP-92, read through the seam since DP-123 so the page and the job
        writer cannot end up with two answers to the same question.
        """
        return measured_faces(self.preparedGroupManifest())

    def canonicalNames(self) -> dict:
        """Patch name -> the name the mesher will know it by."""
        return canonical_patch_names(self.preparedGroupManifest())

    def preparedRegions(self) -> list:
        """``(volume label, [patch names])`` for each prepared region."""
        return prepared_regions(self.preparedGroupManifest())

    @staticmethod
    def regionsByPatch(regions) -> dict:
        """Patch name -> the set of volumes that patch bounds."""
        return regions_by_patch(regions)

    def spanningSelection(self, selected) -> list:
        """The selected patches that cover shells of more than one volume.

        DP-123. The patch/volume grading below cannot see this: it holds one
        set of volumes per patch name, and one patch is one entry, so a patch
        whose own faces bound two volumes reads as bounding one.
        """
        spanning = sources_that_span_volumes(self.preparedRevision())
        return sorted({str(name).strip() for name in (selected or ())}
                      & spanning)

    def gradeSelection(self, selected, owners) -> tuple:
        """Which volume a selection bounds, by the rule the runner applies."""
        return grade_layer_selection(selected, owners)

    #: The two stored fields the run-start pre-flight reads.
    LAYER_GATE_FIELDS = ('gmsh.boundary_layers.enabled',
                         'gmsh.boundary_layers.patches')

    def layerSelectionOnFile(self) -> tuple:
        """``(enabled, patches)`` as the job file would be written now."""
        try:
            values = self._client.field_values(self.LAYER_GATE_FIELDS)
        except Exception:                        # noqa: BLE001 - advisory only
            return False, ()
        raw = values.get(self.LAYER_GATE_FIELDS[1])
        if isinstance(raw, str):
            candidates = raw.replace(chr(10), ',').split(',')
        else:
            candidates = list(raw or ())
        patches = tuple(name for name in
                        (str(item).strip() for item in candidates) if name)
        return bool(values.get(self.LAYER_GATE_FIELDS[0])), patches

    #: Plan 37 F3d. Which way the stored selection is read.
    LAYER_MODE_FIELD = 'gmsh.boundary_layers.patch_mode'

    def layerModeOnFile(self) -> str:
        """The stored patch mode, or ``''`` for a case that predates it."""
        try:
            values = self._client.field_values((self.LAYER_MODE_FIELD,))
        except Exception:                        # noqa: BLE001 - advisory only
            return ''
        return str(values.get(self.LAYER_MODE_FIELD) or '')

    def assemblyLayerRefusal(self, *, enabled=None, patches=None,
                             patch_mode=None) -> str:
        """The sentence the run would refuse with, or `''` if it would not.

        DP-115. `core.gmsh.execution` asks exactly this question -- layers on,
        no patch named, and a prepared revision that counted more than one
        volume -- and asks it at run-start, after WSL has booted and the
        geometry has been imported. MEASURED on the Gmsh leg of
        `two_solid_block`: the page accepted seven authored fields, the
        outline ticked the task, and the refusal arrived at the end of the
        run. Every term of that condition is known when the page opens. The
        refusal is not re-worded here -- it is
        :func:`~foammesh.core.gmsh.execution.assembly_layer_refusal`, so the
        page and the run cannot end up explaining the same stop two ways.

        DP-123. "A patch is named" was read here as "graded", and it is not:
        MEASURED on `two_solid_block`, whose stored selection named both of
        the prepared patches -- one per volume -- passed this gate, passed
        the job writer's, and was refused 76 s later by the runner, which
        grades which volume each named patch bounds. The page already knew:
        `gradeSelection` returns `spanning` for exactly that selection and
        the Boundary Layers banner says in prose that the run will be
        refused. The whole rule now lives at the seam, in
        :func:`~foammesh.core.gmsh.execution.layer_selection_refusal`, and
        this asks it rather than restating a half of it.

        An override is taken for either term so that the page holding those
        controls can grade what is on screen rather than what was last saved.
        """
        if enabled is None or patches is None:
            stored_enabled, stored_patches = self.layerSelectionOnFile()
            enabled = stored_enabled if enabled is None else enabled
            patches = stored_patches if patches is None else patches
        if patch_mode is None:
            # Plan 37 F3d. "Selected" with nothing ticked is refused by the
            # runner on any geometry; the mode says whether empty means that.
            patch_mode = self.layerModeOnFile()
        return layer_selection_refusal(
            self.preparedRevision(), enabled, tuple(patches or ()),
            patch_mode)

    def runAllRefusal(self) -> str:
        """DP-123. The pre-flight, asked of the button that starts the run.

        Every Gmsh page carries the pre-flight, because the Gmsh pipeline runs
        in one go from a control that belongs to the branch rather than to
        whichever task is open. Read from storage: that is what the job file
        would be written from. The Compute page narrows it to the switch on
        screen, which is the one term it can change.
        """
        return self.assemblyLayerRefusal()
