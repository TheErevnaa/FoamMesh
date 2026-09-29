"""Gmsh implementation of the engine-neutral meshing seam.

The eight protocol members are the whole surface the facade sees. Everything
Gmsh-specific -- runtime probing, job derivation, the subprocess runner and
publication -- lives under :mod:`foammesh.core.gmsh` behind this adapter.
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from .base import EngineProbe, StageDefinition, StageRun
from .contracts import (
    ArtifactContract, ArtifactKind, CapabilityRequirement, EngineDescriptor,
    EngineExecutionPlan, EnginePlanRequest, FieldBinding, FieldClassification,
    PlannedTask, TaskCardinality, WorkflowDescriptor, WorkflowTask,
    fingerprint_plan_inputs,
)


_GMSH_MESH = ArtifactContract(
    'gmsh.mesh', ArtifactKind.FILE, 'foammesh/runs/{run_id}/mesh.msh',
    validator='gmsh.msh.present')
_POLY_MESH = ArtifactContract(
    'gmsh.poly_mesh', ArtifactKind.POLY_MESH, 'constant/polyMesh',
    validator='openfoam.poly_mesh.complete')
_QUALITY = ArtifactContract(
    'gmsh.quality', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/latest.json', validator='foammesh.quality_report.v1')


def _fields(*paths):
    """Bind schema paths to the engine contract from the control register.

    The register is the only place that says what a control reaches, so a
    binding cannot claim a native mapping the runner does not implement.
    """
    from foammesh.core.facade.fields import REGISTRY
    from foammesh.core.gmsh.fields import CONTROLS, GATE

    bindings = []
    for path in paths:
        control = CONTROLS[path]
        descriptor = REGISTRY.by_storage_path(path)
        if descriptor is None:
            raise KeyError(
                f'{path} has no field descriptor, so a page bound to it would '
                'render nothing')
        classification = (
            FieldClassification.PRECHECK if control.consumer is GATE
            else FieldClassification.DERIVED if control.calculation_version
            else FieldClassification.NATIVE)
        bindings.append(FieldBinding(
            field_id=descriptor.id,
            classification=classification,
            native_name=control.native_name or None,
            calculation_version=control.calculation_version or None))
    return tuple(bindings)


_FIDELITY_NATIVE = ArtifactContract(
    'gmsh.fidelity_native', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/fidelity-native.json',
    validator='foammesh.geometry_fidelity.v1')
_FIDELITY = ArtifactContract(
    'common.fidelity', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/fidelity.json',
    validator='foammesh.geometry_fidelity.v1')
_RESOLUTION = ArtifactContract(
    'common.resolution', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/resolution.json',
    validator='foammesh.resolution_adequacy.v1')
_SUMMARY = ArtifactContract(
    'common.summary', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/summary.json',
    validator='foammesh.qualification_summary.v1')


GMSH_WORKFLOW = WorkflowDescriptor(
    engine_id='gmsh', version=1, display_name='Gmsh',
    description=(
        'Direct CAD meshing with composable size fields, structured curve '
        'controls, periodic pairs and 3D prism boundary layers.'),
    tasks=(
        WorkflowTask(
            'gmsh.describe_geometry', 'Describe geometry', 10,
            description=(
                'Confirm the prepared geometry Gmsh will import, and the '
                'healing options applied on import.'),
            fields=_fields(
                'gmsh/healing/importTolerance', 'gmsh/healing/sewFaces',
                'gmsh/healing/fixDegenerated', 'gmsh/healing/makeSolids',
                'gmsh/healing/removeDuplicateNodes',
                'gmsh/healing/removeDuplicateFaces',
                'gmsh/healing/classificationAngle',
                # Plan 31, "Geometry kernels and healing". The explicit OCC
                # repair pass and the rest of the Geometry.* family. A Gmsh
                # option nobody can set is not a feature, so each one is
                # bound to this page rather than only to the register.
                'gmsh/healing/healShapes',
                'gmsh/healing/fixSmallEdges', 'gmsh/healing/fixSmallFaces',
                'gmsh/healing/autoFix', 'gmsh/healing/unionUnify',
                'gmsh/healing/booleanTolerance',
                'gmsh/healing/importScaling', 'gmsh/healing/importLabels',
                'gmsh/healing/occParallel')),
        # GF0. Evidence readiness rather than an engineering decision, so it
        # does not accept an override (§8.6).
        WorkflowTask(
            'common.reference_readiness', 'Reference readiness', 15,
            depends_on=('gmsh.describe_geometry',),
            description='Confirm a validation reference and feature manifest '
                        'exist for the prepared geometry.'),
        WorkflowTask(
            'gmsh.global_sizing', 'Global sizing', 20,
            depends_on=('gmsh.describe_geometry',),
            description=(
                'Target and minimum element size, curvature adaptation, and '
                'the meshing algorithms.'),
            fields=_fields(
                'gmsh/globalSizing/targetSize', 'gmsh/globalSizing/minimumSize',
                'gmsh/globalSizing/sizeFactor', 'gmsh/globalSizing/fromCurvature',
                'gmsh/globalSizing/fromPoints',
                'gmsh/globalSizing/extendFromBoundary',
                # Plan 31 follow-up. How two size fields that both cover a
                # cell are reconciled. The facade has named this page as its
                # home since PC5 and the page never drew it, so the value the
                # derivation read was always the default and no user could
                # say otherwise.
                'gmsh/globalSizing/fieldCombiner',
                # Plan 31 FC-B. The curve-discretisation floors sit with
                # fromCurvature because that is the question they answer:
                # how finely a curved edge is walked.
                'gmsh/globalSizing/minimumCirclePoints',
                'gmsh/globalSizing/minimumCurvePoints',
                'gmsh/globalSizing/minimumElementsPerTwoPi',
                'gmsh/algorithms/surface', 'gmsh/algorithms/volume',
                'gmsh/algorithms/cellShape',
                'gmsh/algorithms/algorithmFallback',
                'gmsh/globalSizing/barycentricRefinement',
                # Plan 31 follow-up. Recombination itself, and the recombiner
                # it chooses. FC-C shipped splitQuadrangles -- which is only
                # read when recombination is on -- onto a page where
                # recombination could not be switched on, so the only thing
                # that control could produce from the GUI was the warning
                # saying nothing recombines them. The precondition has to be
                # reachable for the control above it to mean anything.
                'gmsh/algorithms/recombine',
                'gmsh/algorithms/recombinationAlgorithm',
                'gmsh/algorithms/splitQuadrangles',
                'gmsh/parallel/threads')),
        WorkflowTask(
            'gmsh.size_fields', 'Size fields', 30,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('gmsh.global_sizing',),
            description=(
                'Spatial refinement: distance-to-surface thresholds and '
                'analytic box, ball, cylinder and frustum regions.')),
        WorkflowTask(
            'gmsh.curve_controls', 'Curve controls', 40,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('gmsh.size_fields',),
            description='Structured (transfinite) or locally sized curves.'),
        WorkflowTask(
            'gmsh.volume_controls', 'Volume controls', 50,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('gmsh.curve_controls',),
            description='Per-volume sizing, inclusion, and region typing.',
            fields=_fields('gmsh/structuring/automatic',
                           'gmsh/structuring/transfiniteTri')),
        WorkflowTask(
            'gmsh.boundary_layers', 'Boundary layers', 60,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('gmsh.volume_controls',),
            # R118. The description used to say the stack could only apply
            # to the whole boundary. MEASURED: that put prisms on the venturi
            # inlet and outlet planes. The runner rebuilds an un-extruded
            # patch from the extrusion's inner rim, so a selection closes.
            # Plan 33 section 1.1 retired the second half of that sentence:
            # an empty selection grew layers on every boundary, which is the
            # very thing the first half warns against.
            description=(
                'Prism layers grown into the volume. Choose the surfaces '
                'that grow layers — normally the walls; layers on an inlet '
                'or an outlet distort the flow face.'),
            fields=_fields(
                'gmsh/boundaryLayers/enabled',
                'gmsh/boundaryLayers/patchMode',
                'gmsh/boundaryLayers/patches',
                'gmsh/boundaryLayers/mode',
                'gmsh/boundaryLayers/firstHeight', 'gmsh/boundaryLayers/ratio',
                'gmsh/boundaryLayers/layerCount',
                'gmsh/boundaryLayers/totalThickness',
                'gmsh/boundaryLayers/quads')),
        WorkflowTask(
            'gmsh.periodic', 'Periodic pairs', 70,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('gmsh.boundary_layers',),
            description=(
                'Translational or rotational periodicity, published as '
                'matched OpenFOAM cyclic patches.')),
        WorkflowTask(
            # Plan 32 §4.3 names this row `Generate mesh`, and the title is
            # what a reader sees: it is the outline row, the panel heading and
            # the name every "waiting on ..." sentence uses, and it was the
            # one surface still calling the step compute.
            #
            # The title only. `gmsh.compute` is the task id -- persisted in
            # saved task state, named by `depends_on` and `invalidates` across
            # both engines, and the key of the `compute` stage -- and renaming
            # it would strand every case saved before this release.
            'gmsh.compute', 'Generate mesh', 80,
            depends_on=('gmsh.periodic',), engine_stage='compute',
            capabilities=(CapabilityRequirement('gmsh'),),
            artifacts=(_GMSH_MESH,), run_gated=True,
            # Plan 26 WP1.4. The quality gate can refuse a 141,486-element
            # mesh over three elements, and a refusal a human cannot act on is
            # a dead end. Accepting one is recorded as a waiver bound to that
            # mesh -- never a pass, and never available for an inverted cell.
            accepts_override=True,
            invalidates=('gmsh.fidelity_native', 'gmsh.publish', 'gmsh.qa',
                         'common.fidelity', 'common.resolution',
                         'common.summary', 'common.export'),
            description='Run Gmsh and record requested against achieved values.',
            fields=_fields(
                'gmsh/optimization/optimize',
                # DP-627 (field audit 0924 gmsh-generate-export D2). The
                # runner reads these three and no page offered them: Netgen
                # was a hidden switch left on, and the smoothing steps and
                # the optimiser's threshold could not be changed at all.
                'gmsh/optimization/netgen', 'gmsh/optimization/netgenPasses',
                'gmsh/optimization/smoothing',
                'gmsh/optimization/optimizeThreshold',
                'gmsh/optimization/qualityType',
                'gmsh/optimization/minQuality',
                'gmsh/optimization/allowedFraction',
                'gmsh/optimization/allowedCount',
                'gmsh/optimization/hardFloor',
                # Plan 31 FC-D. The repair pass belongs beside the limits it
                # is triggered by: it runs only when the mesh has failed one.
                'gmsh/optimization/repairPoorElements',
                # Plan 29 WP8. Element order sits with the run rather than on a
                # sizing page because it is a solver contract: only the SU2
                # route can read a second-order mesh.
                'gmsh/output/elementOrder',
                'gmsh/output/secondOrderIncomplete',
                # Plan 31 FC-D. The high-order knobs, on the page that owns
                # the order they modify. `highOrderOptimize` was in the
                # register and reachable from no page at all.
                'gmsh/output/secondOrderLinear',
                'gmsh/optimization/highOrderOptimize',
                'gmsh/output/renumber',
                # DP-672 (field audit 0924 gmsh-generate-export D2). The
                # runner reads the mode when it picks `generate(2)` or
                # `generate(3)` and the publisher builds the one-cell
                # extrusion or wedge from the other five, and no page offered
                # any of them: a 2D or axisymmetric mesh was reachable from a
                # script only.
                'gmsh/dimensionality/mode',
                'gmsh/dimensionality/thickness',
                'gmsh/dimensionality/wedgeAngle',
                'gmsh/dimensionality/wedgeAxis',
                'gmsh/dimensionality/frontPatch',
                'gmsh/dimensionality/backPatch',
                # DP-675 (D11): names for the section's boundary curves.
                'gmsh/dimensionality/edgeNames')),
        # GF1 on Gmsh is OPTIONAL and diagnostic -- it measures the native
        # .msh before publication. It does not gate: Gmsh meshes the CAD volume
        # directly, so a boundary face cannot be lost the way snappy can lose
        # one, and making it blocking would stop the engine for a reading that
        # carries no equivalent risk.
        WorkflowTask(
            'gmsh.fidelity_native', 'Native mesh fidelity', 85,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('gmsh.compute', 'common.reference_readiness'),
            artifacts=(_FIDELITY_NATIVE,), run_gated=True,
            invalidates=('common.summary', 'common.export'),
            description='Diagnostic fidelity of the native Gmsh mesh.'),
        WorkflowTask(
            'gmsh.publish', 'Publish polyMesh', 90,
            depends_on=('gmsh.compute',), engine_stage='publish',
            artifacts=(_POLY_MESH,), run_gated=True,
            invalidates=('gmsh.qa', 'common.fidelity', 'common.resolution',
                         'common.summary', 'common.export'),
            description=(
                'Write constant/polyMesh directly, typing patches from the '
                'prepared boundary categories.')),
        WorkflowTask(
            'common.fidelity', 'Geometry fidelity', 95,
            depends_on=('gmsh.publish', 'common.reference_readiness'),
            artifacts=(_FIDELITY,), accepts_override=True,
            run_gated=True,
            invalidates=('common.summary', 'common.export'),
            description='Geometry fidelity of the published mesh.'),
        # Run-gated: a checkMesh run is the only thing that accepts it, so a
        # QA row can never read "passed" for a mesh OpenFOAM did not open.
        WorkflowTask(
            'gmsh.qa', 'Quality', 100,
            depends_on=('gmsh.publish',), engine_stage='checkMesh',
            capabilities=(CapabilityRequirement('checkMesh'),),
            artifacts=(_QUALITY,), accepts_override=True, run_gated=True,
            invalidates=('common.summary', 'common.export'),
            description='OpenFOAM 13 checkMesh on the published mesh.'),
        WorkflowTask(
            'common.resolution', 'Resolution adequacy', 105,
            depends_on=('gmsh.publish', 'common.reference_readiness'),
            artifacts=(_RESOLUTION,), accepts_override=True,
            run_gated=True,
            invalidates=('common.summary', 'common.export'),
            description='Whether the mesh resolves the geometry it captured.'),
        WorkflowTask(
            'common.summary', 'Qualification summary', 108,
            depends_on=('common.fidelity', 'common.resolution', 'gmsh.qa'),
            artifacts=(_SUMMARY,), run_gated=True,
            invalidates=('common.export',),
            description='Compose the three verdicts into one disposition.'),
        WorkflowTask(
            'common.export', 'Export', 110,
            depends_on=('common.summary',),
            description='Export through the common target registry.'),
    ))


GMSH_DESCRIPTOR = EngineDescriptor(
    engine_id='gmsh', display_name='Gmsh', workflow_version=1,
    summary='Direct CAD meshing with size fields and 3D prism layers.',
    cell_families=('tetrahedron', 'prism', 'hexahedron', 'pyramid'),
    runtime_kind='gmsh')


_STAGES = MappingProxyType({
    'compute': StageDefinition(
        'compute', 'gmsh', 'gmsh-job.json', task_id='gmsh.compute',
        capabilities=('gmsh',),
        expected_artifacts=('foammesh/runs',),
        depends_on=('gmsh.periodic',)),
    'publish': StageDefinition(
        'publish', 'gmsh-publish', 'gmsh-job.json', task_id='gmsh.publish',
        expected_artifacts=('constant/polyMesh',),
        depends_on=('gmsh.compute',)),
    'checkMesh': StageDefinition(
        'checkMesh', 'checkMesh', 'gmsh-job.json', task_id='gmsh.qa',
        capabilities=('checkMesh',),
        expected_artifacts=('foammesh/quality/latest.json',),
        depends_on=('gmsh.publish',)),
})


class GmshMeshingEngine:
    engine_id = 'gmsh'
    #: What one ``mesh.gmsh.run`` actually performs, in order.
    #:
    #: The run computes, publishes and checks in a single process, so it is
    #: evidence for exactly these three. Naming them is what keeps the recorder
    #: bounded: the alternative -- advancing every task that declares a stage --
    #: would pass the fidelity gates on the strength of a run that never
    #: measured anything (Plan 23 §8.5).
    ATOMIC_RUN_TASKS = ('gmsh.compute', 'gmsh.publish', 'gmsh.qa')
    #: Gmsh imports the prepared CAD itself, so planning without a current
    #: prepared revision would mesh nothing.
    requires_prepared_geometry = True
    native_section = 'gmsh'
    #: Plan 30 F-02. Gmsh's gate refuses *before* the mesh is published, so a
    #: mesh a human accepts has to be made again: the whole atomic run is the
    #: only thing that can publish it. Every other engine has already
    #: published by the time anything judged the result.
    # Plan 31 CP-05 item 4. This was `mesh.gmsh.run`: accepting a candidate
    # meshed the case again, under a new run id, and recorded the decision
    # against the run nobody had inspected. Accepting is not meshing --
    # `mesh.run.accept` names the candidate it is about and publishes that one.
    accept_quality_operation = 'mesh.run.accept'
    #: Gmsh writes its own job at the moment it runs, so nothing has to have
    #: generated a dictionary for it first.
    needs_generated_dictionaries = False
    #: No gate blocks a later Gmsh task: its one gate is inside the atomic run.
    blocking_gate_task = ''
    #: DP-638. The job imports solid models or surfaces, never both
    #: (runner_v1.py refuses the mix), so the case is refused it up front.
    mixes_cad_and_surfaces = False
    #: DP-641. Gmsh has no non-conformal coupling to build an NCC pair with.
    builds_non_conformal_interfaces = False
    #: Plan 36 RP11. Each solid is meshed as its own volume, so a region is a
    #: solid: detection answers from the solids and accepting types them.
    regions_are_solids = True

    def __init__(self, *, profile=None, runtime_probe=None):
        self._profile = profile
        # One probe service for the life of the engine. It was built fresh on
        # every call, so its per-process cache was never read once: the window
        # asks for this probe after every commit, and each ask booted WSL
        # again (Plan 29 WP1).
        self._runtime_probe = runtime_probe
        self._owned_probe = None

    # -- description ------------------------------------------------------- #

    @property
    def descriptor(self) -> EngineDescriptor:
        return GMSH_DESCRIPTOR

    def workflow_descriptor(self) -> WorkflowDescriptor:
        return GMSH_WORKFLOW

    def derivation_warnings(self, db, *, bbox=None) -> dict:
        """Per-task derivation warnings for the current configuration.

        Plan 26 WP2.1. Derived on every read rather than persisted: a warning
        is a pure function of the configuration, so recomputing it cannot go
        stale, and it stays off both the workflow descriptor -- whose digest
        invalidates saved task state -- and the task-state store, which is for
        state a run produced.

        A configuration the derivation refuses outright is not a warning to
        show on a page; the page that owns the offending control raises it in
        its own validation. Returning nothing here is therefore correct.
        """
        from foammesh.core.gmsh.execution import _native_section
        from foammesh.core.gmsh.plan_derivation import (
            PlanDerivationError, derive_from_native,
        )

        from foammesh.core.engine.registry import configured_target_solver

        from foammesh.core.gmsh.sizing import SizingError

        try:
            intent = derive_from_native(
                _native_section(db), bbox=bbox,
                target_solver=configured_target_solver(db))
        except SizingError as error:
            # DP-614: an "Auto" size with no geometry to derive it from, or a
            # size the domain cannot hold. Said on the page that owns it
            # rather than swallowed with every other warning.
            return {'gmsh.global_sizing': [str(error)]}
        except (PlanDerivationError, ValueError, KeyError, TypeError):
            return {}
        return {task_id: list(texts) for task_id, texts
                in intent.derived_warnings_by_task().items()}

    # -- runtime ----------------------------------------------------------- #

    def resolve_profile(self):
        """The qualified profile, or ``None`` when the host has no runtime."""
        if self._profile is not None:
            return self._profile
        from foammesh.core.gmsh.launch_profiles import configured_profiles
        profiles = configured_profiles()
        return profiles[0] if profiles else None

    def probe(self, capabilities=None, *, refresh: bool = False,
              target_solver: str = '') -> EngineProbe:
        """Report whether Gmsh can mesh, and say separately what will not run.

        DP-50. Two questions were being answered with one word. Whether Gmsh
        can build a mesh is about Gmsh; whether OpenFOAM will approve of the
        result is about checkMesh; and this returned the *conjunction*, so a
        host with a working Gmsh and no OpenFOAM reported the Gmsh engine
        unavailable and greyed its card out. The reason offered named
        OpenFOAM, on a route the user may never have pointed at OpenFOAM.

        Nothing in the pipeline justified it. `publish` writes
        `constant/polyMesh` in pure Python -- no `gmshToFoam`, no utility, no
        shell -- so Gmsh meshes and publishes for OpenFOAM without OpenFOAM
        being present at all. checkMesh reads a mesh that by then already
        exists and says whether the solver will like it. It is a quality gate,
        and it was wired as a power switch.

        Plan 30 WP-07 (F-40) had already carved out the SU2 route, which was
        right as far as it went, but it treated the exemption as the special
        case. It is the general one: the check is a check on every route. So
        availability is Gmsh's own answer, the checkMesh row is reported for
        what it is, and a missing check becomes an advisory the page shows
        next to an engine the user can still choose.
        """
        from foammesh.core.gmsh.plan_derivation import solver_token
        from foammesh.core.gmsh.runtime import (
            GmshRuntimeProbe, REQUIRED_CAPABILITIES, unavailable_reason,
        )

        needs_checkmesh = solver_token(target_solver) != 'su2'
        profile = self.resolve_profile()
        if profile is None:
            return EngineProbe(
                self.engine_id, False,
                tuple((name, False) for name in REQUIRED_CAPABILITIES),
                unavailable_reason(()))

        service = self._runtime_probe
        if service is None:
            if self._owned_probe is None:
                self._owned_probe = GmshRuntimeProbe()
            service = self._owned_probe
        report = service.probe(profile, refresh=bool(refresh))

        # Publication runs checkMesh from the same distribution, so the mesh is
        # judged by the runtime that produced it.
        checkmesh = True
        checkmesh_reason = ''
        if capabilities is not None and hasattr(capabilities, 'utility'):
            capability = capabilities.utility('checkMesh')
            checkmesh = bool(capability.available)
            checkmesh_reason = '' if checkmesh else str(
                getattr(capability, 'reason', '') or '')
        results = (*report.capabilities, ('checkMesh', checkmesh))
        reason = report.reason
        if needs_checkmesh and report.available and not checkmesh:
            # Reported, not enforced. The run itself has always coped with the
            # check being absent -- the publish path records the mesh as
            # unchecked and leaves QA to be run rather than claiming it passed
            # -- so the only thing the old refusal added was to stop the user
            # reaching a pipeline that would have worked.
            #
            # R194. Which is it: no OpenFOAM on this machine, or a
            # distribution that had not finished starting when the page
            # opened? The sentence used to read the same either way.
            reason = ('Gmsh is available but OpenFOAM 13 checkMesh is not; a '
                      'published mesh could not be validated'
                      + (f' — {checkmesh_reason}' if checkmesh_reason else ''))
        return EngineProbe(
            self.engine_id, bool(report.available), results, reason,
            profile_id=report.profile_id,
            runtime_fingerprint=report.runtime_fingerprint,
            version=report.version,
            # Plan 31 FC-B added three, each on a measurement: the two quad
            # surfacers (gated to a target that reads quadrilaterals) and
            # MMG3D, which won the catalogue's quality gate at matched size.
            algorithms=('meshadapt', 'automatic', 'delaunay',
                        'frontal_delaunay', 'frontal_delaunay_quads',
                        'packing_parallelograms', 'quasi_structured_quad',
                        'frontal', 'hxt', 'mmg3d'),
            supported_element_types=('tetrahedron', 'prism', 'hexahedron',
                                     'pyramid'),
            failures=() if report.available else ({
                'category': report.category.value, 'reason': report.reason},))

    # -- planning ---------------------------------------------------------- #

    def validate(self, stage: str) -> StageDefinition:
        try:
            return _STAGES[str(stage)]
        except KeyError as error:
            raise ValueError(f'unknown Gmsh meshing stage: {stage}') from error

    async def run(self, request):
        """One whole Gmsh mesh: write the job, compute, publish, check.

        Plan 30 F-03. This was a second orchestrator reached by its own
        operation (`mesh.gmsh.run`), so nothing above the seam could say "run
        this case's mesh" without first asking which engine the case held. It
        is reached through :meth:`run` now; the body stays with the executor
        that runs it.
        """
        return await request.orchestrator.run_gmsh_pipeline(
            request.session, request.command)

    # -- keeping and discarding a run (Plan 30 F-15) ------------------------ #

    @staticmethod
    def _runs_root(case_path) -> Path:
        return Path(case_path) / 'foammesh' / 'runs'

    def _resolve_run(self, case_path, target: str) -> Path:
        """The run directory *target* names, or the newest one.

        A Gmsh case has no "current stage" to step back to -- it has runs, each
        one whole -- so the thing a reset or a keep acts on is a run id.
        """
        root = self._runs_root(case_path)
        if target:
            found = root / str(target)
            if not found.is_dir():
                raise ValueError(f'there is no Gmsh run to act on: {target}')
            return found
        runs = [item for item in (root.iterdir() if root.is_dir() else ())
                if item.is_dir()]
        if not runs:
            raise ValueError('this case has no Gmsh run')
        return max(runs, key=lambda item: (item.stat().st_mtime, item.name))

    def reset(self, case_path, target: str = '') -> dict:
        """Discard a Gmsh run and its manifest.

        F-15. There was no way to discard one at all: every snappy stage could
        be stepped back, a Gmsh run could only be superseded, so a case
        accumulated run directories and the Runs list grew monotonically
        whatever the user did. The published `constant/polyMesh` is left alone
        -- it belongs to whichever run was last accepted, which may not be
        this one, and throwing away a mesh the user never asked to throw away
        is not what "reset this run" means.
        """
        import shutil

        case = Path(case_path)
        root = self._resolve_run(case, target)
        run_id = root.name
        shutil.rmtree(root, ignore_errors=True)
        return {
            'engine_id': self.engine_id,
            'target': run_id,
            'forgotten': (run_id,),
            'restored_from': '',
            'has_mesh': (case / 'constant' / 'polyMesh' / 'boundary').exists(),
            'removed': (str(root),),
        }

    @staticmethod
    def _run_document(root: Path) -> dict:
        """This run's manifest, or an empty document when it has none."""
        import json

        manifest = Path(root) / 'run-manifest.json'
        if not manifest.is_file():
            return {}
        try:
            document = json.loads(manifest.read_text(encoding='utf-8'))
        except ValueError:
            return {}
        return document if isinstance(document, dict) else {}

    def snapshot(self, case_path, target: str = '') -> dict:
        """Keep one run's mesh: everything that run wrote, and nothing else.

        Plan 31 CP-05 item 6. A snapshot names a run, so what it holds has to
        belong to that run. MEASURED before this change on a case with an
        accepted run A and a later run B publishing over the top: a snapshot
        of A contained A's ``mesh.msh`` beside B's ``constant/polyMesh``, one
        directory presented as one mesh, and the run's ``mesh.su2`` -- the
        only form a second-order mesh has -- was not copied at all.

        The root polyMesh is copied only while its five files still match the
        checksums this run recorded when it published them; otherwise it is
        left out and the reason is reported, because a mesh that is no longer
        this run's is not this run's mesh to keep.
        """
        import shutil

        from foammesh.core.gmsh.manifest import published_mesh_is

        case = Path(case_path)
        root = self._resolve_run(case, target)
        run_id = root.name
        document = self._run_document(root)
        kept = case / 'foammesh' / 'mesh-snapshots' / run_id
        shutil.rmtree(kept, ignore_errors=True)
        kept.mkdir(parents=True, exist_ok=True)
        saved = []
        omitted = []
        for name in ('mesh.msh', 'mesh.su2', 'run-manifest.json'):
            source = root / name
            if source.is_file():
                shutil.copy2(source, kept / name)
                saved.append(name)
        published = case / 'constant' / 'polyMesh'
        if (published / 'boundary').exists():
            if published_mesh_is(document, case):
                shutil.copytree(published, kept / 'polyMesh')
                saved.append('polyMesh')
            else:
                omitted.append(
                    "the case's constant/polyMesh is not the mesh run "
                    f'{run_id} published, so it was left out of this snapshot')
        if not [name for name in saved if name != 'run-manifest.json']:
            raise ValueError(f'run {run_id} has no mesh to keep')
        return {
            'engine_id': self.engine_id,
            'target': run_id,
            'path': str(kept),
            'saved': sorted(saved),
            'omitted': tuple(omitted),
        }

    def meshes_a_section(self, db) -> bool:
        """Whether this case asks for a 2D or axisymmetric section.

        DP-673. A section is one planar face and bounds no volume by design,
        so the run seams must not refuse it for that. Asked of the engine
        rather than decided in the facade by engine name: only an engine
        that meshes sections answers this at all.
        """
        try:
            mode = db.getValue('gmsh/dimensionality/mode')
        except Exception:                                   # noqa: BLE001
            return False
        mode = str(getattr(mode, 'value', mode) or '')
        return mode.split('.')[-1].lower() in ('two_d', 'axisymmetric')

    def requested_cell_size(self, db, *, bounds=None, hex_bounds=None):
        """The element size Gmsh was asked for (Plan 30 F-14).

        The configured target size, or the same bounding-box derivation the
        runner applies when it is unset, times the size factor. Returned as
        ``(size, source, reason)``.
        """
        from types import SimpleNamespace

        from foammesh.core.gmsh.sizing import SizingError, derive_global_sizing

        def value(path):
            try:
                found = db.getValue(path)
            except Exception:                               # noqa: BLE001
                return None
            found = getattr(found, 'value', found)
            return None if found in (None, '') else found

        bbox = None
        if bounds is not None:
            bbox = SimpleNamespace(xmin=bounds[0], xmax=bounds[1],
                                   ymin=bounds[2], ymax=bounds[3],
                                   zmin=bounds[4], zmax=bounds[5])
        values = {key: value(f'gmsh/globalSizing/{key}')
                  for key in ('targetSize', 'minimumSize', 'sizeFactor',
                              'fromCurvature')}
        try:
            sizing = derive_global_sizing(values, bbox)
        except SizingError as error:
            return None, 'unavailable', str(error)
        size = float(sizing.target_size) * float(sizing.size_factor or 1.0)
        return (size, str(sizing.sources.get('targetSize') or 'derived'),
                f'gmsh target size {sizing.target_size:.4g} × '
                f'size factor {sizing.size_factor:g}')

    def create_plan(self, request: EnginePlanRequest) -> EngineExecutionPlan:
        from foammesh.core.gmsh.plan_derivation import derive_job_intent

        workflow = self.workflow_descriptor()
        intent = derive_job_intent(request)
        by_task = intent.derived_settings_by_task()
        tasks = tuple(PlannedTask(
            task_id=task.task_id,
            stage=task.engine_stage,
            depends_on=task.depends_on,
            capabilities=task.capabilities,
            expected_artifacts=task.artifacts,
            derived_settings={
                'configuration_contract': 'foammesh.v2',
                **by_task.get(task.task_id, {}),
            },
        ) for task in workflow.ordered_tasks())
        geometry_fingerprint = (
            request.prepared_geometry.fingerprint
            if request.prepared_geometry is not None else 'foammesh-db-geometry-v2')
        profile = self.resolve_profile()
        return EngineExecutionPlan(
            schema_version=2,
            engine_id=self.engine_id,
            workflow_digest=workflow.digest,
            input_fingerprint=fingerprint_plan_inputs(
                request.intent.to_dict(), geometry_fingerprint,
                request.configuration_revision),
            run_path=request.run_path,
            tasks=tasks,
            requested_resources=(
                dict(request.resource_policy) or
                {'mode': 'auto', 'cpu_ranks': 1,
                 'threads_per_rank': intent.parallel.threads}),
            backend_constraints={
                'runtime': 'gmsh',
                'parallel_backend': 'gmsh-threads',
                'profile_id': profile.profile_id if profile else '',
                # Say plainly whether the thread count will reach the volume
                # pass. Only HXT threads in 3D; asking Delaunay or Frontal for
                # sixteen threads meshes on one and reports nothing.
                'volume_threaded': intent.parallel.volume_threaded,
                'effective_volume_threads': (
                    intent.parallel.threads
                    if intent.parallel.volume_threaded else 1),
            },
        )

    # -- execution --------------------------------------------------------- #

    def generate_config(self, db, bbox, case_path, prepared_geometry=None):
        """Write the job this run will be given, threads and all.

        Plan 33 DP-X2. The job the runner reads is derived from the ``gmsh``
        section of the project, and the thread count lives in the execution
        policy, which that section knows nothing about. MEASURED: a case whose
        page said three cores wrote a job saying one thread, and the runner
        read the job. The policy is resolved into the section here, once, so
        the writer keeps deriving from one place and the number it derives is
        the number the page asked for.
        """
        from foammesh.core.gmsh.execution import write_job
        from foammesh.core.gmsh.plan_derivation import resolve_parallel_threads

        policy = execution_policy(db)
        threads = resolve_parallel_threads(
            policy, requested=_saved_thread_request(db))
        return write_job(_ResolvedThreads(db, threads), bbox, case_path,
                         prepared_geometry=prepared_geometry,
                         profile=self.resolve_profile())

    def run_stage(self, stage: str, *, db, case_path, executable: str) -> StageRun:
        from foammesh.core.gmsh.execution import stage_run

        return stage_run(self, stage, db=db, case_path=Path(case_path),
                         executable=executable, profile=self.resolve_profile())

    def collect_quality(self, case_path):
        from foammesh.core.quality import MeshCheckService

        return MeshCheckService.load_report(case_path)


def _project_data(db) -> dict:
    reader = getattr(db, 'data', None)
    if not callable(reader):
        return {}
    try:
        return dict(reader() or {})
    except Exception:                                       # noqa: BLE001
        return {}


def execution_policy(db) -> dict:
    """The execution policy, read off the project the run is starting from.

    The same two readings the facade takes from the configuration document,
    taken here from the database the engine seam is handed, because the seam
    is given no configuration and the runner needs the answer.
    """
    execution = ((_project_data(db).get('mesh') or {}).get('execution') or {})
    try:
        ceiling = int(execution.get('maxCpuCores') or 0)
    except (TypeError, ValueError):
        ceiling = 0
    return {'mode': str(execution.get('mode', 'auto')).split('.')[-1].lower(),
            'max_cpu_cores': ceiling or None}


def _saved_thread_request(db) -> int:
    parallel = ((_project_data(db).get('gmsh') or {}).get('parallel') or {})
    try:
        threads = int(parallel.get('threads') or 0)
    except (TypeError, ValueError):
        return 0
    return threads if threads > 1 else 0


class _ResolvedThreads:
    """The project, with the thread count the execution policy resolved.

    A view rather than a write: the number is derived per run from the ceiling
    and the machine, and saving it into the project would turn this machine's
    core count into a stored setting that travels with the case.
    """

    def __init__(self, db, threads: int):
        self._db = db
        self._threads = int(threads)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def data(self) -> dict:
        data = _project_data(self._db)
        gmsh = dict(data.get('gmsh') or {})
        parallel = dict(gmsh.get('parallel') or {})
        parallel['threads'] = self._threads
        gmsh['parallel'] = parallel
        data['gmsh'] = gmsh
        return data
