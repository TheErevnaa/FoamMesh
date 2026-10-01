"""Shared base for the Gmsh task pages."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.core.gmsh.execution import (
    canonical_patch_names, grade_layer_selection,
    layer_selection_refusal, measured_faces, prepared_regions,
    regions_by_patch, sources_that_span_volumes,
)
from foammesh.core.gmsh.plan_derivation import POLY_MESH_TARGETS
from foammesh.core.quantities import count_text
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query


#: What the user calls each target. ``unselected`` is deliberately absent: it
#: is a state, not a solver, and every sentence that reaches for it needs its
#: own words rather than a solver-shaped placeholder.
SOLVER_NAMES = {'openfoam': 'OpenFOAM', 'su2': 'SU2'}


class GmshTaskPage(EngineTaskPage):
    """One Gmsh workflow task.

    The signature is ``(facade_client, parent)``: the engine branch constructs
    every page positionally as ``page_class(client, self)``, so a page that
    took its task id second would receive the parent widget as a task id.
    Each concrete page names its own task instead.
    """

    engine_id = 'gmsh'
    run_all_task_id = 'gmsh.compute'
    task_id_default = ''

    #: Which page a runner warning belongs to, matched on the wording the
    #: runner writes (``runner_v1.apply_volume_controls`` and friends). R66/
    #: R67: ``result.json`` carried ``volume control 'tee_fluid' has no
    #: resolvable volume scope and was skipped`` and the background-field
    #: warning that names Global Sizing, and neither reached any surface in
    #: the GUI -- the Console tab showed nine progress lines and the run
    #: reported success. First match wins, so the background-size-field
    #: warning is attributed before the generic ``size field`` rule.
    RUN_WARNING_TASKS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ('gmsh.global_sizing', ('background size field',)),
        ('gmsh.size_fields', ('size field',)),
        ('gmsh.curve_controls', ('curve control',)),
        ('gmsh.volume_controls', ('volume control',)),
        ('gmsh.boundary_layers', ('boundary layer', 'prism')),
        ('gmsh.periodic', ('periodic pair',)),
        # Plan 31 CP-08 item 7. The farfield cut is part of describing
        # the geometry, and what it did with the volumes it left is the
        # first thing a reader of that page needs: a discarded sealed
        # cavity is a region the mesh does not contain.
        ('gmsh.describe_geometry',
         ('classified surface', 'sealed cavity', 'farfield')),
    )

    def __init__(self, facade_client, parent=None):
        super().__init__(facade_client, self.task_id_default, parent)
        self._runWarnings = QLabel(self)
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
        self._runNotes = QLabel(self)
        self._runNotes.setObjectName('gmshRunNotes')
        self._runNotes.setWordWrap(True)
        self._runNotes.setProperty('foammeshStatus', 'info')
        self._runNotes.setAccessibleName(self.tr('Notes from the last Gmsh run'))
        self._runNotes.setVisible(False)
        self._body_layout.insertWidget(1, self._runNotes)
        self.updateRunWarnings()

    #: Pages that show every warning the last run produced rather than the
    #: ones routed to them: Compute Mesh, where the run happened, and the
    #: Quality page, where its result is read. DP-528 (MA G1-P2): no runner
    #: wording routes to ``gmsh.qa``, so the G1 run's ``face6`` warning could
    #: never appear on the page the user checks the mesh on.
    SHOWS_EVERY_RUN_WARNING = False

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

    # -- what this case is being meshed for -------------------------------- #

    def target_solver(self) -> str:
        """Which solver the case is meshed for: openfoam, su2 or unselected.

        CP-09 item 3: the target has to be visible in QA and export wording,
        and an SU2 page must not describe checkMesh or polyMesh as mandatory.
        A page that cannot read it says ``unselected`` rather than assuming
        OpenFOAM, because assuming OpenFOAM is what put a polyMesh sentence
        on the SU2 route in the first place.
        """
        try:
            values = self._client.field_values(('mesh.target_solver',))
        except Exception:                                    # noqa: BLE001
            return 'unselected'
        return str(values.get('mesh.target_solver') or 'unselected')

    def target_solver_name(self) -> str:
        return SOLVER_NAMES.get(self.target_solver(),
                                self.tr('no solver chosen yet'))

    def element_order(self) -> int:
        try:
            values = self._client.field_values(('gmsh.compute.element_order',))
        except Exception:                                    # noqa: BLE001
            return 1
        try:
            return int(float(values.get('gmsh.compute.element_order') or 1))
        except (TypeError, ValueError):
            return 1

    def publication_plan(self) -> tuple[bool, str]:
        """``(publishes, why not)`` for a run of this case.

        The rule is the one ``core.gmsh.plan_derivation`` applies, read here
        rather than restated: a target in :data:`POLY_MESH_TARGETS` at first
        order publishes ``constant/polyMesh``, and nothing else does.
        """
        if self.element_order() > 1:
            return False, self.tr(
                'This mesh is second order and constant/polyMesh is a '
                'first-order format, so the run keeps its native Gmsh .msh. '
                'There is nothing to publish.')
        if self.target_solver() not in POLY_MESH_TARGETS:
            return False, self.tr(
                'The SU2 route reads the mesh file Gmsh wrote, so this case '
                'publishes no polyMesh. Nothing downstream of it needs one.')
        return True, ''

    # -- warnings the run produced ----------------------------------------- #

    def last_run(self) -> dict:
        """The newest Gmsh run manifest, or ``{}`` when there is none."""
        try:
            payload = query(self._client, 'mesh.gmsh.runs', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory note
            return {}
        runs = [item for item in (payload.get('runs') or ())
                if isinstance(item, dict)]
        if not runs:
            return {}
        # `created_at` has one-second resolution, so the listing order settles
        # ties -- `list_runs` walks the run directory in sorted order.
        return max(enumerate(runs),
                   key=lambda item: (str(item[1].get('created_at') or ''),
                                     item[0]))[1]

    def warning_task(self, text: str) -> str:
        """Which task page a runner warning is about."""
        lowered = str(text).lower()
        for task_id, keywords in self.RUN_WARNING_TASKS:
            if any(keyword in lowered for keyword in keywords):
                return task_id
        return self.run_all_task_id or ''

    def run_warnings(self) -> tuple[str, tuple[str, ...]]:
        """``(run_id, warnings)`` from the last run, for this page.

        The Compute Mesh page shows every warning -- it is the page the run
        happened on -- and each other page shows the ones about its own
        controls, so a discarded volume control is reported where the user
        would go to check it.
        """
        run = self.last_run()
        if not run:
            return '', ()
        texts, seen = [], set()
        # DP-567. Only a pair that did not take effect is a warning; the
        # applied ones are notes, read by `run_notes`.
        pairs = self.interface_pair_lines(run, applied=False)
        for value in (list(run.get('warnings') or ())
                      + list(run.get('runner_warnings') or ())):
            text = str(value).strip()
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
        if (self.task_id != self.run_all_task_id
                and not self.SHOWS_EVERY_RUN_WARNING):
            texts = [text for text in texts
                     if self.warning_task(text) == self.task_id]
        elif pairs:
            # DP-549. The pair's own line says what its runner warning says,
            # with its faces; saying it twice reads as two problems.
            texts = [text for text in texts
                     if not text.startswith('interface pair ')] + pairs
        return str(run.get('run_id') or ''), tuple(texts)

    def run_notes(self) -> tuple[str, ...]:
        """What the last run did that is worth saying and is not a problem.

        DP-567. 0924 rerun, G6: "interface pair 'tee_plug_contact':
        conformal, 36 faces matched" is the pair working, and it was printed
        in the warning colour among the warnings. Shown on the pages that show
        every warning, in the informational colour.
        """
        if (self.task_id != self.run_all_task_id
                and not self.SHOWS_EVERY_RUN_WARNING):
            return ()
        return tuple(self.interface_pair_lines(self.last_run(), applied=True))

    #: The pair states that mean the pair took effect (DP-548 grades them).
    APPLIED_PAIR_STATES = frozenset({'conformal'})

    @classmethod
    def interface_pair_lines(cls, run, applied=None) -> list[str]:
        """One line per authored interface pair: its faces, state and why.

        DP-549. G6 (audit 0924) saved ``tee_plug_contact`` and the Generate
        page said nothing about it, so a 36-face interface could not be told
        apart from an effective pair. The run now grades each pair (DP-548)
        and this is where the user reads the grade.

        ``applied`` keeps only the pairs that took effect (``True``) or only
        the ones that did not (``False``); ``None`` keeps both.
        """
        lines = []
        for row in (run or {}).get('interface_pairs') or ():
            if not isinstance(row, dict):
                continue
            if (applied is not None and applied
                    != (row.get('state') in cls.APPLIED_PAIR_STATES)):
                continue

            def side(key):
                # DP-566. By the face the user picked, and the name fusion
                # gave it only as a note: G6's slave body1_face4 was shown as
                # body0_face2, a pair that read as one face paired with itself.
                values = row.get(key) or {}
                meshed = [str(name) for name in values.get('names') or ()]
                authored = [str(name) for name in values.get('authored') or ()]
                text = f'{key} ' + (', '.join(authored or meshed) or 'nothing')
                if authored and meshed and authored != meshed:
                    text += f' (meshed as {", ".join(meshed)})'
                return text

            faces = int(row.get('matchedFaces') or 0)
            counted = f', {count_text(faces, "face")} matched' if faces else ''
            lines.append(
                f'interface pair {row.get("name")!r}: '
                f'{row.get("state") or "unknown"}{counted} '
                f'({side("master")}; {side("slave")}) — '
                f'{row.get("reason") or ""}'.rstrip(' —'))
        return lines

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

    def updateRunWarnings(self) -> None:
        run_id, texts = self.run_warnings()
        notes = self.run_notes() if hasattr(self, '_runNotes') else ()
        heading = (self.tr('The last run (%s) reported:') % run_id if run_id
                   else self.tr('The last run reported:'))
        self._runWarnings.setText(
            heading + '\n' + '\n'.join(f'• {text}' for text in texts)
            if texts else '')
        self._runWarnings.setVisible(bool(texts))
        if not hasattr(self, '_runNotes'):
            return
        # One heading for the run: the notes carry it only when no warning
        # line above them already did.
        self._runNotes.setText(
            ('' if texts else heading + '\n')
            + '\n'.join(f'• {text}' for text in notes) if notes else '')
        self._runNotes.setVisible(bool(notes))
