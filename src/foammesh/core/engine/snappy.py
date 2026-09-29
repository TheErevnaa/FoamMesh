"""SnappyHexMesh implementation of the engine-neutral meshing seam."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import MappingProxyType

from foammesh.core.quality import MeshCheckService
from foammesh.core.quantities import aligned, count_text
from foammesh.core.shell import CapabilityRegistry
from foammesh.openfoam.case_builder import CaseBuilder

from .base import EngineProbe, StageDefinition, StageRun
from .contracts import (
    ArtifactContract, ArtifactKind, CapabilityRequirement, EngineDescriptor,
    EngineExecutionPlan, EnginePlanRequest, FieldBinding, FieldClassification,
    PlannedTask, TaskCardinality, WorkflowDescriptor, WorkflowTask,
    fingerprint_plan_inputs,
)


def _fields(*paths: str):
    """Bind schema paths to the stage that reads them.

    A stage that names no fields cannot tell the wizard what it consumes, and
    nothing can decide whether a stage went stale when a control changed. The
    register in ``openfoam.snappy_controls`` already knows which dictionary
    keyword each path becomes, so the binding is derived rather than retyped --
    a control that reaches nothing cannot be bound here at all.
    """
    from foammesh.core.facade.fields import REGISTRY
    from foammesh.openfoam.snappy_controls import CONTROLS, WRITER

    bindings = []
    for path in paths:
        control = CONTROLS[path]
        descriptor = REGISTRY.by_storage_path(path)
        if descriptor is None:
            raise KeyError(
                f'{path} has no field descriptor, so no stage can declare it')
        bindings.append(FieldBinding(
            field_id=descriptor.id,
            classification=(FieldClassification.NATIVE if control.consumer == WRITER
                            else FieldClassification.DERIVED),
            native_name=control.key if control.consumer == WRITER else None,
            calculation_version=(None if control.consumer == WRITER
                                 else 'snappy.derivation.v1')))
    return tuple(bindings)


def _collection_fields(*paths: str):
    """The same, for keyed collections, which have rows rather than a value."""
    from foammesh.core.facade.fields import REGISTRY
    from foammesh.openfoam.snappy_controls import CONTROLS

    by_path = {collection.storage_path: collection_id
               for collection_id, collection in REGISTRY.collections.items()}
    bindings = []
    for path in paths:
        if path not in by_path:
            raise KeyError(f'{path} is not a registered collection')
        bindings.append(FieldBinding(
            field_id=by_path[path],
            classification=FieldClassification.NATIVE,
            native_name=CONTROLS[path].key))
    return tuple(bindings)


_POLY_MESH = ArtifactContract(
    'snappy.poly_mesh', ArtifactKind.POLY_MESH, 'constant/polyMesh',
    validator='openfoam.poly_mesh.complete')
_FEATURE_EDGES = ArtifactContract(
    'snappy.feature_edges', ArtifactKind.FEATURE_EDGES, 'constant/triSurface',
    validator='openfoam.feature_edges.present')
_QUALITY = ArtifactContract(
    'snappy.quality', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/latest.json', validator='foammesh.quality_report.v1')
_FIDELITY_SNAP = ArtifactContract(
    'snappy.fidelity_snap', ArtifactKind.QUALITY_REPORT,
    'foammesh/quality/fidelity-snap.json',
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


SNAPPY_WORKFLOW = WorkflowDescriptor(
    engine_id='snappy', version=1, display_name='snappyHexMesh',
    description='OpenFOAM-native hex-dominant staged meshing workflow.',
    tasks=(
        WorkflowTask(
            'snappy.domain_regions', 'Domain & regions', 10,
            description='Define fluid seeds, CFD patches, and region intent.'),
        # GF0. Evidence readiness, not an engineering decision -- so it is the
        # one geometry task that does NOT accept an override (§8.6): waiving
        # "we have no reference" would waive the ability to measure anything.
        WorkflowTask(
            'common.reference_readiness', 'Reference readiness', 15,
            depends_on=('snappy.domain_regions',),
            description='Confirm a validation reference and feature manifest '
                        'exist for the prepared geometry.'),
        WorkflowTask(
            'snappy.base_grid', 'Base grid', 20,
            depends_on=('snappy.domain_regions',), engine_stage='blockMesh',
            capabilities=(CapabilityRequirement('blockMesh'),),
            fields=_fields(
                'baseGrid/sizingMode', 'baseGrid/targetCellSize',
                'baseGrid/numCellsX', 'baseGrid/numCellsY',
                'baseGrid/numCellsZ', 'baseGrid/boundingHex6',
                'baseGrid/scale',
                'baseGrid/grading/x', 'baseGrid/grading/y',
                'baseGrid/grading/z',
                'baseGrid/boundaryTypes/xMin', 'baseGrid/boundaryTypes/xMax',
                'baseGrid/boundaryTypes/yMin', 'baseGrid/boundaryTypes/yMax',
                'baseGrid/boundaryTypes/zMin', 'baseGrid/boundaryTypes/zMax',
                # FS-B. The name and the category of a background face were
                # registered fields that no task declared, so the writer's
                # refusal -- "declared a inlet but still carries the name this
                # product generated for it" -- was a dead end from the GUI:
                # the category half of the feature could be demanded and never
                # satisfied. Declaring them here is what puts them on a page.
                'baseGrid/boundaryNames/xMin', 'baseGrid/boundaryNames/xMax',
                'baseGrid/boundaryNames/yMin', 'baseGrid/boundaryNames/yMax',
                'baseGrid/boundaryNames/zMin', 'baseGrid/boundaryNames/zMax',
                'baseGrid/boundaryCategories/xMin',
                'baseGrid/boundaryCategories/xMax',
                'baseGrid/boundaryCategories/yMin',
                'baseGrid/boundaryCategories/yMax',
                'baseGrid/boundaryCategories/zMin',
                'baseGrid/boundaryCategories/zMax',
            ) + _collection_fields(
                # FS-B. The authored multi-block background mesh. The writer
                # and its validation existed and were live-proven; no widget
                # referenced any of these collections, so the capability was
                # real for a script and did not exist for the product. Empty
                # blocks still mean the derived single box, byte for byte.
                'baseGrid/vertices', 'baseGrid/blocks', 'baseGrid/edges',
                'baseGrid/patches', 'baseGrid/mergePairs'),
            artifacts=(_POLY_MESH,), run_gated=True),
        # R20. The task was called "Surface Features & Refinement" and carried
        # no refinement control of any kind: no per-surface levels, no
        # refinement regions. Both live on Castellation, the next task, so the
        # name sent the user looking here for half a page that was never
        # built. Renaming it costs a workflow-digest change, which resets
        # saved task progress with a notice -- cheaper than a standing lie in
        # the outline.
        WorkflowTask(
            'snappy.surface_features', 'Surface features', 30,
            depends_on=('snappy.base_grid',), engine_stage='surfaceFeatures',
            capabilities=(CapabilityRequirement('surfaceFeatures'),),
            description='Extract feature edges from the prepared surfaces at '
                        'the included angle. Refinement levels and regions '
                        'are set on Castellation.',
            artifacts=(_FEATURE_EDGES,), run_gated=True),
        WorkflowTask(
            'snappy.castellation', 'Castellation', 40,
            depends_on=('snappy.surface_features',), engine_stage='castellation',
            capabilities=(CapabilityRequirement('snappyHexMesh'),),
            fields=_fields(
                'castellation/nCellsBetweenLevels',
                'castellation/resolveFeatureAngle',
                'castellation/maxGlobalCells', 'castellation/maxLocalCells',
                'castellation/minRefinementCells',
                'castellation/maxLoadUnbalance',
                'castellation/allowFreeStandingZoneFaces',
                'castellation/gapLevelIncrement', 'castellation/planarAngle',
                'castellation/useTopologicalSnapDetection',
                'castellation/handleSnapProblems',
                'castellation/extendedRefinementSpan',
                'snappyAdvanced/keepPatches',
                'snappyAdvanced/writeFlags/scalarLevels',
                'snappyAdvanced/writeFlags/layerSets',
                'snappyAdvanced/writeFlags/layerFields',
                'snappyAdvanced/debugFlags/mesh',
                'snappyAdvanced/debugFlags/intersections',
                'snappyAdvanced/debugFlags/featureSeeds',
                'snappyAdvanced/debugFlags/attraction',
                'snappyAdvanced/debugFlags/layerInfo',
            ) + _collection_fields(
                'castellation/refinementSurfaces',
                'castellation/refinementVolumes',
                'castellation/featureBands',
                'castellation/volumeBands'),  # DP-586
            artifacts=(_POLY_MESH,), run_gated=True),
        WorkflowTask(
            'snappy.snap', 'Snap', 50,
            depends_on=('snappy.castellation',), engine_stage='snap',
            capabilities=(CapabilityRequirement('snappyHexMesh'),),
            fields=_fields(
                'snap/nSmoothPatch', 'snap/nSolveIter', 'snap/nRelaxIter',
                'snap/nFeatureSnapIter', 'snap/implicitFeatureSnap',
                'snap/explicitFeatureSnap',
                'snap/multiRegionFeatureSnap',
                'snap/detectNearSurfacesSnap', 'snap/tolerance'),
            artifacts=(_POLY_MESH,), run_gated=True),
        # GF1, the blocking gate. Measured against the snap checkpoint, before
        # layers mutate the boundary in place -- after layers the surface that
        # was snapped no longer exists to measure.
        WorkflowTask(
            'snappy.fidelity_snap', 'Snap fidelity', 55,
            depends_on=('snappy.snap', 'common.reference_readiness'),
            artifacts=(_FIDELITY_SNAP,), accepts_override=True,
            run_gated=True,
            invalidates=('snappy.layers', 'common.fidelity', 'snappy.qa',
                         'common.resolution', 'common.summary',
                         'common.export'),
            description='Geometry fidelity of the snapped boundary.'),
        WorkflowTask(
            'snappy.layers', 'Boundary layers', 60,
            cardinality=TaskCardinality.OPTIONAL,
            depends_on=('snappy.fidelity_snap',), engine_stage='layers',
            capabilities=(CapabilityRequirement('snappyHexMesh'),),
            fields=_fields(
                'addLayers/nGrow', 'addLayers/featureAngle',
                'addLayers/slipFeatureAngle',
                'addLayers/maxFaceThicknessRatio',
                'addLayers/nSmoothSurfaceNormals',
                'addLayers/nSmoothThickness', 'addLayers/minMedialAxisAngle',
                'addLayers/maxThicknessToMedialRatio',
                'addLayers/nSmoothNormals', 'addLayers/nRelaxIter',
                'addLayers/nBufferCellsNoExtrude', 'addLayers/nLayerIter',
                'addLayers/nRelaxedIter', 'addLayers/nMedialAxisIter',
                'addLayers/nSmoothDisplacement',
                'addLayers/detectExtrusionIsland',
                'addLayers/additionalReporting', 'addLayers/meshShrinker',
            ) + _collection_fields('addLayers/layers'),
            artifacts=(_POLY_MESH,), run_gated=True),
        # GF2, MQ and RA are siblings: each depends only on the final mesh, so
        # none blocks another and all three run even when one fails. §8.1's
        # "complete diagnosis" is exactly this shape.
        WorkflowTask(
            'common.fidelity', 'Geometry fidelity', 65,
            depends_on=('snappy.layers', 'common.reference_readiness'),
            artifacts=(_FIDELITY,), accepts_override=True,
            run_gated=True,
            invalidates=('common.summary', 'common.export'),
            description='Geometry fidelity of the published mesh.'),
        # MQ. Run-gated like every other stage: Proceed on this row used to
        # send a plain `accept`, so the tree read "passed" on the strength of
        # whatever checkMesh had last run -- on the live walk, the base grid,
        # three mutating stages earlier. Only a checkMesh run advances it now.
        WorkflowTask(
            'snappy.qa', 'Quality', 70,
            depends_on=('snappy.layers',), engine_stage='checkMesh',
            capabilities=(CapabilityRequirement('checkMesh'),),
            # The thresholds do double duty: snappy undoes a phase that breaks
            # them, and checkMesh judges the finished mesh by them. They are
            # declared once, here, where they are judged.
            fields=_fields(
                'meshQuality/maxNonOrtho', 'meshQuality/maxBoundarySkewness',
                'meshQuality/maxInternalSkewness', 'meshQuality/maxConcave',
                'meshQuality/minVol', 'meshQuality/minTetQuality',
                'meshQuality/minVolCollapseRatio', 'meshQuality/minArea',
                'meshQuality/minTwist', 'meshQuality/minDeterminant',
                'meshQuality/minFaceWeight', 'meshQuality/minVolRatio',
                'meshQuality/nSmoothScale', 'meshQuality/errorReduction',
                'meshQuality/mergeTolerance',
                'meshQuality/relaxed/maxNonOrtho',
                'meshQuality/relaxed/maxBoundarySkewness',
                'meshQuality/relaxed/maxInternalSkewness',
                'meshQuality/relaxed/maxConcave', 'meshQuality/relaxed/minVol',
                'meshQuality/relaxed/minTetQuality',
                'meshQuality/relaxed/minVolCollapseRatio',
                'meshQuality/relaxed/minArea', 'meshQuality/relaxed/minTwist',
                'meshQuality/relaxed/minDeterminant',
                'meshQuality/relaxed/minFaceWeight',
                'meshQuality/relaxed/minVolRatio'),
            artifacts=(_QUALITY,), accepts_override=True, run_gated=True,
            invalidates=('common.summary', 'common.export')),
        WorkflowTask(
            'common.resolution', 'Resolution adequacy', 74,
            depends_on=('snappy.layers', 'common.reference_readiness'),
            artifacts=(_RESOLUTION,), accepts_override=True,
            run_gated=True,
            invalidates=('common.summary', 'common.export'),
            description='Whether the mesh resolves the geometry it captured.'),
        # Q. Not overridable: it is the record that binds the decision
        # together, so waiving it would waive the evidence of the waiver.
        WorkflowTask(
            'common.summary', 'Qualification summary', 78,
            depends_on=('common.fidelity', 'common.resolution', 'snappy.qa'),
            artifacts=(_SUMMARY,), run_gated=True,
            invalidates=('common.export',),
            description='Compose the three verdicts into one disposition.'),
        WorkflowTask(
            'common.export', 'Export', 80,
            depends_on=('common.summary',),
            description='Export through the common target registry.'),
    ))


SNAPPY_DESCRIPTOR = EngineDescriptor(
    engine_id='snappy', display_name='snappyHexMesh', workflow_version=1,
    summary='Hex-dominant OpenFOAM-native meshing.',
    # Plan 28: 'hexahedron', not 'hex'. Gmsh already said 'hexahedron' for
    # the same shape, so every set comparison across the two engines was
    # wrong before it started. 'polyhedron' is the family that keeps this
    # engine away from SU2, which reads no such cell.
    cell_families=('hexahedron', 'polyhedron', 'prism', 'tetrahedron'),
    runtime_kind='openfoam')


_STAGES = MappingProxyType({
    'surfaceFeatures': StageDefinition(
        'surfaceFeatures', 'surfaceFeatures', 'surfaceFeaturesDict',
        task_id='snappy.surface_features', capabilities=('surfaceFeatures',),
        expected_artifacts=('constant/triSurface',),
        depends_on=('snappy.base_grid',)),
    'blockMesh': StageDefinition(
        'blockMesh', 'blockMesh', 'blockMeshDict', task_id='snappy.base_grid',
        capabilities=('blockMesh',), expected_artifacts=('constant/polyMesh',),
        depends_on=('snappy.domain_regions',)),
    'snappyHexMesh': StageDefinition(
        'snappyHexMesh', 'snappyHexMesh', 'snappyHexMeshDict', (True, True, True),
        task_id='snappy.castellation', capabilities=('snappyHexMesh',),
        expected_artifacts=('constant/polyMesh',),
        depends_on=('snappy.surface_features',)),
    'castellation': StageDefinition(
        'castellation', 'snappyHexMesh', 'snappyHexMeshDict', (True, False, False),
        task_id='snappy.castellation', capabilities=('snappyHexMesh',),
        expected_artifacts=('constant/polyMesh',),
        depends_on=('snappy.surface_features',)),
    'snap': StageDefinition(
        'snap', 'snappyHexMesh', 'snappyHexMeshDict', (False, True, False),
        task_id='snappy.snap', capabilities=('snappyHexMesh',),
        expected_artifacts=('constant/polyMesh',),
        depends_on=('snappy.castellation',)),
    'layers': StageDefinition(
        'layers', 'snappyHexMesh', 'snappyHexMeshDict', (False, False, True),
        task_id='snappy.layers', capabilities=('snappyHexMesh',),
        expected_artifacts=('constant/polyMesh',),
        depends_on=('snappy.snap',)),
})


class SnappyMeshingEngine:
    engine_id = 'snappy'
    #: One ``workflow.run_pipeline`` executes blockMesh, surfaceFeatures,
    #: snappyHexMesh (castellate, snap, layers) and checkMesh in a single
    #: supervised DAG, so it is evidence for exactly these tasks. The domain
    #: regions are consumed by that run, which is what "accepted" means for a
    #: manual task. The fidelity gates are deliberately absent: the recorder
    #: stops at the first one still unmet and reports it as ``blocked``
    #: rather than passing a gate on the strength of a run that never
    #: measured anything (Plan 23 §8.5). Without this tuple the tree stayed
    #: grey after a complete mesh, because nothing ever told the task store
    #: that anything had happened.
    ATOMIC_RUN_TASKS = (
        'snappy.domain_regions', 'snappy.base_grid', 'snappy.surface_features',
        'snappy.castellation', 'snappy.snap', 'snappy.layers', 'snappy.qa',
    )
    requires_prepared_geometry = False
    native_section = ''
    #: Plan 30 F-02. Deliberately empty: snappy writes `constant/polyMesh` as
    #: it goes, so by the time checkMesh judges the mesh the mesh exists.
    #: Accepting one re-runs `snappy.qa` and records the decision against it,
    #: rather than meshing the case again -- and never, as it did before,
    #: running Gmsh over a snappyHexMesh result.
    accept_quality_operation = ''
    #: snappy meshes from the dictionaries `workflow.generate_dictionaries`
    #: writes, so a run before that generation has nothing to read.
    needs_generated_dictionaries = True
    #: GF1, the snap-fidelity gate. Gmsh has no equivalent, which is why the
    #: facade asks the engine rather than asking whether it is snappy.
    blocking_gate_task = 'snappy.fidelity_snap'
    #: DP-638. snappy stages a CAD import's facets beside any STL, so one
    #: case may hold both.
    mixes_cad_and_surfaces = True
    #: DP-641. An NCC pair is written for OpenFOAM's non-conformal coupling.
    builds_non_conformal_interfaces = True
    #: Plan 36 RP11. A snappy region is the space a seed point picks out.
    regions_are_solids = False

    @property
    def descriptor(self) -> EngineDescriptor:
        return SNAPPY_DESCRIPTOR

    def workflow_descriptor(self) -> WorkflowDescriptor:
        return SNAPPY_WORKFLOW

    def probe(self, capabilities=None, *, refresh: bool = False,
              target_solver: str = '') -> EngineProbe:
        # snappyHexMesh writes polyMesh and nothing else, so the target solver
        # cannot change what this engine needs; it is accepted so every engine
        # answers the same question (Plan 30 WP-07, F-40).
        capabilities = capabilities or CapabilityRegistry()
        required = (
            'blockMesh', 'surfaceFeatures', 'snappyHexMesh', 'checkMesh',
            'decomposePar', 'reconstructPar', 'mpirun',
        )
        probed = tuple((name, capabilities.utility(name))
                       for name in required)
        results = tuple((name, capability.available)
                        for name, capability in probed)
        missing = tuple(name for name, available in results if not available)
        # R194. Seven utility names is a list of symptoms. The registry
        # already knows the cause -- a runtime still starting, a launcher
        # that could not be run, a distribution that is not there -- and
        # without it a cold WSL boot is indistinguishable from an
        # OpenFOAM that was never installed.
        causes = (getattr(capability, 'reason', '')
                  for _name, capability in probed
                  if not capability.available)
        cause = next((reason for reason in causes if reason), '')
        diagnostics = (
            capabilities.runtime_diagnostics()
            if hasattr(capabilities, 'runtime_diagnostics') else {})
        selected = diagnostics.get('selected_profile') or {}
        return EngineProbe(
            self.engine_id, not missing, results,
            '' if not missing else (
                f"runtime unavailable: {', '.join(missing)}"
                + (f' — {cause}' if cause else '')),
            profile_id=selected.get('profile_id'),
            runtime_fingerprint=selected.get('fingerprint'),
            version=selected.get('version'))

    def validate(self, stage: str) -> StageDefinition:
        try:
            return _STAGES[str(stage)]
        except KeyError as error:
            raise ValueError(f'unknown Snappy meshing stage: {stage}') from error

    def create_plan(self, request: EnginePlanRequest) -> EngineExecutionPlan:
        workflow = self.workflow_descriptor()
        tasks = tuple(PlannedTask(
            task_id=task.task_id,
            stage=task.engine_stage,
            depends_on=task.depends_on,
            capabilities=task.capabilities,
            expected_artifacts=task.artifacts,
            derived_settings={'configuration_contract': 'foammesh.v2'},
        ) for task in workflow.ordered_tasks())
        geometry_fingerprint = (
            request.prepared_geometry.fingerprint
            if request.prepared_geometry is not None else 'foammesh-db-geometry-v2')
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
                {'mode': 'auto', 'cpu_ranks': 1, 'threads_per_rank': 1}),
            backend_constraints={'runtime': 'openfoam',
                                 'parallel_backend': 'openfoam-mpi'},
        )

    def generate_config(self, db, bbox, case_path, prepared_geometry=None):
        return CaseBuilder(db, bbox).write_case_staged(
            case_path, prepared_geometry=prepared_geometry)

    def write_parallel_config(self, db, case_path, ranks: int) -> Path:
        """Materialize the OpenFOAM decomposition contract behind the engine seam.

        Plan 31 CP-07 item 4. This wrote the file itself, the facade wrote it
        again with a hardcoded method, and the case builder wrote a third
        version from the CPU ceiling. All three now go through
        :func:`foammesh.openfoam.decomposition.write`, so the ranks in the
        dictionary are the ranks the caller is about to launch and the method
        is the one the project stored.
        """
        from foammesh.openfoam import decomposition

        return decomposition.write(
            case_path, int(ranks), decomposition.DecompositionSettings.read(db))

    #: Pipeline nodes that are one snappy phase rather than a whole run. Their
    #: node IDs match the dictionary-regeneration stages exactly, because the
    #: three phases differ only in which enable flags the dictionary carries --
    #: the command line is identical (Plan 23 §8.2).
    SPLIT_PHASES = ('castellation', 'snap', 'layers')

    def write_phase_dictionary(self, db, case_path, phase: str):
        """Point ``snappyHexMeshDict`` at one phase of a decomposed run.

        The split route invokes ``snappyHexMesh`` three times against the same
        case; what changes between them is this dictionary. OpenFOAM 13
        overwrites ``constant/polyMesh`` in place, so each phase picks up where
        the last one left off.
        """
        if phase not in self.SPLIT_PHASES:
            raise ValueError(f'not a decomposable snappy phase: {phase}')
        return CaseBuilder(db, _unit_bbox()).regenerate_stage(case_path, phase)

    def layers_skipped(self, case_path) -> bool:
        """Whether the user skipped the optional Boundary layers task.

        DP-591 (field audit 0924 snappy-back D12). The skip was recorded in
        the task tree and read by nothing that builds a run, so "Run to end"
        grew every configured layer group anyway and then recorded the task
        PASSED over the skip. An unreadable tree answers False: the run then
        does what it always did.
        """
        from foammesh.core.workflow.task_state_store import (
            EngineTaskStateStore,
        )

        from .contracts import TaskState

        try:
            graph = EngineTaskStateStore(
                case_path, self.workflow_descriptor()).load()
            return graph.state('snappy.layers') is TaskState.SKIPPED
        except Exception:  # noqa: BLE001 - the tree is advisory here
            return False

    def refresh_pipeline_config(self, db, case_path):
        """Regenerate every pipeline dictionary from the current revision."""
        builder = CaseBuilder(db, _unit_bbox())
        builder.regenerate_stage(case_path, 'blockMesh')
        builder.regenerate_stage(case_path, 'surfaceFeatures')
        return builder.regenerate_stage(
            case_path, 'snappyHexMesh',
            castellation=True, snap=True,
            layers=not self.layers_skipped(case_path))

    #: The generated inputs every node of a pipeline run has to agree on.
    #: ``snappyHexMeshDict`` is deliberately absent: the split route rewrites
    #: it between phases on purpose, and it is regenerated from the same
    #: configuration, which ``configuration_sha256`` already covers.
    SEALED_DICTIONARIES = (
        'blockMeshDict', 'surfaceFeaturesDict', 'decomposeParDict',
        'controlDict')

    @classmethod
    def revision_seal(cls, case_path, *, prepared_revision: str = '') -> dict:
        """One digest over everything a run must not change under itself.

        Plan 31 CP-07 item 5. Feature generation, decomposition, meshing,
        reconstruction and checking are five utilities reading files off disk
        in sequence; nothing checked that they were reading the *same* files.
        ``decomposeParDict`` was the sharp case -- it is written outside the
        dictionary manifest, so a dictionary left by an earlier run at a
        different rank count was invisible to every staleness check the
        product had, and ``decomposePar`` would happily split into it while
        ``mpirun`` started a different number of ranks.

        The seal is recorded when the run starts and re-checked before its
        mesh is published, so a mid-run divergence is a named failure instead
        of a mesh whose provenance is a guess.
        """
        case = Path(case_path)
        manifest_path = (
            case / 'system' / 'foammesh-dictionaries-manifest.json')
        configuration = ''
        if manifest_path.is_file():
            try:
                configuration = str(json.loads(manifest_path.read_text(
                    encoding='utf-8')).get('configuration_sha256') or '')
            except ValueError:
                configuration = ''
        files = {}
        for name in cls.SEALED_DICTIONARIES:
            path = case / 'system' / name
            files[name] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() else '')
        document = {
            'configuration_sha256': configuration,
            'prepared_revision': str(prepared_revision or ''),
            'files': files,
        }
        document['seal'] = hashlib.sha256(json.dumps(
            document, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        return document

    @classmethod
    def seal_divergence(cls, seal, current) -> str:
        """Say what moved, or '' when the run read one revision throughout."""
        if not seal or not current or seal.get('seal') == current.get('seal'):
            return ''
        if seal.get('configuration_sha256') != current.get(
                'configuration_sha256'):
            return ('the case settings changed while the run was in progress, '
                    'so its dictionaries no longer describe one revision')
        if seal.get('prepared_revision') != current.get('prepared_revision'):
            return ('the prepared geometry changed while the run was in '
                    'progress')
        moved = sorted(
            name for name, digest in (seal.get('files') or {}).items()
            if (current.get('files') or {}).get(name) != digest)
        return ('rewritten under the run: ' + ', '.join(moved)) if moved             else 'the sealed revision changed under the run'

    @staticmethod
    def snapshot_run_config(case_path):
        return CaseBuilder.snapshot_run_dictionaries(case_path)

    @staticmethod
    def mark_stage_run(db, case_path, stage):
        builder = CaseBuilder(db, _unit_bbox())
        builder.load_case_context(case_path)
        return builder.mark_stage_run(case_path, stage)

    @staticmethod
    def requested_layer_counts(db, case_path) -> dict:
        """Map generated patch name -> requested ``nSurfaceLayers``.

        Reporting achieved layer coverage needs the request to compare against,
        and only the writer knows which patch name each layer group became.
        """
        builder = CaseBuilder(db, _unit_bbox())
        builder.load_case_context(case_path)
        # DP-492. Keyed by patch, not by dictionary key: a pattern group's
        # key is its quoted expression, which no patch in the log is called.
        return builder.requested_layer_counts()

    @staticmethod
    def frozen_layer_patches(db, case_path) -> set:
        """Patches the dictionary froze with ``nSurfaceLayers 0``.

        A frozen patch appears in the achieved-layer table with no layers,
        which is indistinguishable from a failed extrusion unless the report
        is told the difference was asked for.
        """
        builder = CaseBuilder(db, _unit_bbox())
        builder.load_case_context(case_path)
        return {str(name) for name in builder.frozen_layer_patches()}

    @staticmethod
    def mark_pipeline_run(db, case_path):
        builder = CaseBuilder(db, _unit_bbox())
        builder.load_case_context(case_path)
        return builder.mark_pipeline_run(case_path)

    #: The stages that rewrite ``constant/polyMesh`` in place, in the order
    #: OpenFOAM applies them. The undecomposed ``snappyHexMesh`` run starts
    #: from the same mesh as ``castellation``, so it shares that key.
    MESH_STAGE_ORDER = ('castellation', 'snap', 'layers')

    @staticmethod
    def _snapshot_key(stage: str) -> str:
        return 'castellation' if stage == 'snappyHexMesh' else stage

    def prepare_stage_input_mesh(self, case_path, stage: str) -> None:
        """Put ``constant/polyMesh`` back to what this stage started from.

        R84. Each snappy phase reads the mesh the previous one left behind and
        overwrites it, so the case directory holds exactly one mesh and no
        record of the earlier ones. Re-running a phase therefore fed it its own
        output: MEASURED on venturi.stl, a second Castellation run on the
        already-snapped 36,533-cell mesh core-dumped inside ``hexRef8`` --
        "cell 15500 of level 1 does not seem to have 8 points of equal or lower
        level" -- and the GUI reported only "Castellation refinement failed."
        That is reachable from the product's own instructions: the staleness
        guard tells the user to re-run an upstream stage, and doing so was what
        broke the case.

        The first time a phase runs, the mesh it inherits is copied aside; every
        later run of that phase restores the copy first, so a re-run is always
        the same computation as the first run. Running a phase invalidates the
        copies belonging to the phases after it, and ``blockMesh`` -- which
        builds the background mesh from nothing -- discards all of them.
        """
        case = Path(case_path)
        root = case / 'foammesh' / 'mesh-snapshots'
        mesh = case / 'constant' / 'polyMesh'
        key = self._snapshot_key(stage)
        if key not in self.MESH_STAGE_ORDER:
            if stage == 'blockMesh' and root.is_dir():
                shutil.rmtree(root, ignore_errors=True)
            return
        for later in self.MESH_STAGE_ORDER[
                self.MESH_STAGE_ORDER.index(key) + 1:]:
            shutil.rmtree(root / later, ignore_errors=True)
        snapshot = root / key
        if snapshot.is_dir():
            shutil.rmtree(mesh, ignore_errors=True)
            shutil.copytree(snapshot, mesh)
        elif mesh.is_dir():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(mesh, snapshot)

    async def run(self, request):
        """One whole snappy mesh: blockMesh through checkMesh (Plan 30 F-03).

        The DAG, the supervised executor and the mutation recovery all live on
        the orchestrator, because they are the facade's machinery and are
        shared with every other operation that launches a process. What this
        method carries is the fact that *this* is what running a snappy mesh
        means, so `workflow.run_pipeline` no longer has to know.
        """
        return await request.orchestrator.run_snappy_pipeline(
            request.session, request.command)

    def reset(self, case_path, target: str = '') -> dict:
        """Undo *target*, in the vocabulary every engine answers in (F-15)."""
        state = self.reset_stage(case_path, target or 'blockMesh')
        return {
            'engine_id': self.engine_id,
            'target': state['stage'],
            'forgotten': tuple(state['forgotten_stages']),
            'restored_from': str(state['restored_from'] or ''),
            'has_mesh': bool(state['has_mesh']),
            'removed': (),
        }

    def snapshot(self, case_path, target: str = '') -> dict:
        """Keep the current mesh as *target*'s input, so a re-run is honest.

        The same copy :meth:`prepare_stage_input_mesh` makes on a stage's first
        run, taken on demand -- which is what a user asking to keep a mesh
        before trying something else is asking for.
        """
        case = Path(case_path)
        key = self._snapshot_key(target or self.MESH_STAGE_ORDER[0])
        if key not in self.MESH_STAGE_ORDER:
            raise ValueError(f'unknown mesh stage: {target}')
        mesh = case / 'constant' / 'polyMesh'
        if not (mesh / 'boundary').exists():
            raise ValueError('there is no mesh in the case to keep')
        kept = case / 'foammesh' / 'mesh-snapshots' / key
        shutil.rmtree(kept, ignore_errors=True)
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(mesh, kept)
        return {
            'engine_id': self.engine_id,
            'target': key,
            'path': str(kept),
            'saved': sorted(item.name for item in kept.iterdir()),
            # One shape across the seam (CP-05 item 6): a snapshot says what
            # it could not keep. A snappy stage snapshot is the mesh in the
            # case root and nothing else, so there is never anything to leave
            # out -- but the caller reads the same keys from either engine.
            'omitted': (),
        }

    def requested_cell_size(self, db, *, bounds=None, hex_bounds=None):
        """The finest cell the castellation was told to make (Plan 30 F-14).

        The base-grid cell divided by two to the power of the deepest surface
        refinement level. Returned as ``(size, source, reason)``; a size that
        cannot be established is ``(None, 'unavailable', why)`` rather than a
        guess, because a ratio against a guessed size is a number that looks
        like evidence.
        """
        value = _db_value(db)
        mode = str(value('baseGrid/sizingMode', 'counts') or 'counts')
        if mode == 'target_size':
            stored = value('baseGrid/targetCellSize')
            if stored is None or str(stored).strip() == '':
                # DP-669: unset is "Auto", the block diagonal / 40.
                from foammesh.core.mesh.sizing import auto_target_cell_size

                extent = hex_bounds or bounds
                base = (auto_target_cell_size(extent)
                        if extent is not None else None)
                if base is None:
                    return (None, 'unavailable',
                            'baseGrid/targetCellSize is Auto and there is no '
                            'mesh extent to derive it from')
                base_reason = f'base-grid target cell size {base:.4g} (Auto)'
            else:
                try:
                    base = float(stored)
                except (TypeError, ValueError):
                    return (None, 'unavailable',
                            'baseGrid/targetCellSize is not set')
                base_reason = f'base-grid target cell size {base:.4g}'
        else:
            try:
                counts = tuple(int(value(f'baseGrid/numCells{axis}'))
                               for axis in 'XYZ')
            except (TypeError, ValueError):
                return None, 'unavailable', 'baseGrid cell counts are not set'
            extent = hex_bounds or bounds
            if extent is None or min(counts) < 1:
                return (None, 'unavailable',
                        'no bounding hex and no mesh extent to derive the '
                        'base-grid cell from')
            spans = (extent[1] - extent[0], extent[3] - extent[2],
                     extent[5] - extent[4])
            cells = [abs(span) / count for span, count in zip(spans, counts)]
            base = float(sum(cells) / len(cells))
            base_reason = ('base-grid cell '
                           + ' × '.join(aligned(cells))
                           + f' from counts {counts[0]}×{counts[1]}×{counts[2]}')
        level = 0
        try:
            for key in dict(db.getElements('castellation/refinementSurfaces')):
                found = value(f'castellation/refinementSurfaces/{key}/'
                              'surfaceRefinement/maximumLevel', 0)
                level = max(level, int(found or 0))
        except Exception:                                   # noqa: BLE001
            level = 0
        return (base / float(2 ** level), 'derived',
                f'{base_reason}, refined {count_text(level, "level")}')

    def reset_stage(self, case_path, stage: str) -> dict:
        """Forget a recorded stage run and put the mesh back to its input.

        R85. The counterpart of :meth:`prepare_stage_input_mesh`: that method
        restores a stage's input so a *re-run* is honest, this one restores it
        so the user can step back. Which stages exist, which of them rewrite
        ``constant/polyMesh`` in place, and where their snapshots live are all
        facts about this engine, so the decision belongs here rather than in
        the facade -- the facade only knows that some engine can reset a stage.

        Resetting ``blockMesh`` leaves the case with no mesh at all, because
        the background grid is built from nothing. Resetting a later stage
        restores the snapshot that stage started from. Either way the stages
        after it are discarded: their results were built on the mesh being
        thrown away.
        """
        case = Path(case_path)
        if stage not in CaseBuilder.STAGE_ORDER:
            raise ValueError(f'unknown mesh stage: {stage}')
        builder = CaseBuilder(None, _unit_bbox())
        forgotten = builder.clear_stage_runs(case, stage)

        mesh = case / 'constant' / 'polyMesh'
        snapshots = case / 'foammesh' / 'mesh-snapshots'
        restored = None
        if stage == 'blockMesh':
            for target in (mesh, snapshots):
                shutil.rmtree(target, ignore_errors=True)
            for processor in case.glob('processor*'):
                shutil.rmtree(processor, ignore_errors=True)
        else:
            snapshot = snapshots / self._snapshot_key(stage)
            if snapshot.is_dir():
                shutil.rmtree(mesh, ignore_errors=True)
                shutil.copytree(snapshot, mesh)
                restored = str(snapshot)
            for later in CaseBuilder.STAGE_ORDER[
                    CaseBuilder.STAGE_ORDER.index(stage):]:
                shutil.rmtree(snapshots / later, ignore_errors=True)

        return {
            'stage': stage,
            'forgotten_stages': list(forgotten),
            'restored_from': restored,
            'has_mesh': (mesh / 'boundary').exists(),
        }

    def run_stage(self, stage: str, *, db, case_path, executable: str) -> StageRun:
        definition = self.validate(stage)
        case = Path(case_path)
        builder = CaseBuilder(db, _unit_bbox())
        manifest = case / 'system' / 'foammesh-dictionaries-manifest.json'
        if not manifest.is_file():
            raise ValueError(
                'generated dictionary manifest is required; generate the '
                'current FoamMesh dictionaries before running a Snappy stage')
        builder.load_case_context(case)
        builder.require_current_stage_dependencies(
            case, definition.stage)
        self.prepare_stage_input_mesh(case, definition.stage)
        if definition.snappy_flags is not None:
            castellation, snap, layers = definition.snappy_flags
            builder.regenerate_stage(
                case, definition.stage,
                castellation=castellation, snap=snap, layers=layers)
        else:
            builder.regenerate_stage(case, definition.stage)
        if definition.stage in {'blockMesh', 'surfaceFeatures'}:
            argv = (executable, '-case', str(case))
        else:
            argv = (executable, '-case', str(case))
        return StageRun(definition, argv, case)

    def collect_quality(self, case_path):
        return MeshCheckService.load_report(case_path)


def _db_value(db):
    """Read a configuration key tolerantly, the way the facade always has."""
    def value(path, default=None):
        try:
            found = db.getValue(path)
        except Exception:                                   # noqa: BLE001
            return default
        found = getattr(found, 'value', found)
        return default if found in (None, '') else found

    return value


def _unit_bbox():
    # Stage selection only rewrites the three Snappy enable flags. Geometry
    # bounds are not consulted by ``snappy_hex_mesh_dict`` today.
    from foammesh.core.geometry import BBox
    return BBox(0, 1, 0, 1, 0, 1)
