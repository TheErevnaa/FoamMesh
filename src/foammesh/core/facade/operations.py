"""Operation descriptor registry (§4.4) for discovery and confirmation policy.

Each registered operation carries validation intent, capability needs, impact
class, confirmation policy, timeout, artifact contract, recovery strategy, and
a structured result type. The registry backs ``GET /api/v1/operations`` and the
OpenAPI generation, and lets the dispatcher derive the confirmation class for a
plan without hard-coding it per call site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from foammesh.core.naming import humanise_option

from .policy import ConfirmationClass, ImpactClass, confirmation_for


class OperationKind(str, Enum):
    """How the command scheduler must treat an operation (Plan 30 WP-08).

    ``query`` is the only kind that may skip the serialized write queue, so
    the rule that decides it is deliberately narrow: a READ impact *and* no
    artifact contract. An operation that declares an artifact writes a file
    even when reading is all it does to the configuration, and a file written
    beside a running mutation is exactly what the queue exists to prevent.
    """

    QUERY = 'query'
    MUTATION = 'mutation'
    JOB = 'job'


class ExecutionMode(str, Enum):
    READ = 'read'
    TRANSACTION = 'transaction'
    ARTIFACT = 'artifact'
    JOB = 'job'
    PRESENTATION = 'presentation'


@dataclass(frozen=True)
class OperationDescriptor:
    operation: str
    title: str
    scope: str                       # 'case' | 'application' | 'presentation'
    impact: ImpactClass
    summary: str = ''
    capabilities: tuple[str, ...] = ()      # required OpenFOAM utilities
    parameters_schema: dict = field(default_factory=dict)
    result_type: str = 'operation_result'
    artifact_contract: tuple[str, ...] = ()
    recovery: str = 'none'           # 'none' | 'undo' | 'restore_point'
    timeout_seconds: int | None = None
    reversible: bool = False
    execution_mode: ExecutionMode | None = None

    @property
    def confirmation(self) -> ConfirmationClass:
        return confirmation_for(self.impact)

    @property
    def kind(self) -> OperationKind:
        """``query`` bypasses the serial queue; ``mutation`` and ``job`` wait.

        Replaces the four-name ``UNSERIALIZED_OPERATIONS`` allowlist that
        F-09 measured: a cold Gmsh probe held the single write queue for about
        32 s, and every read the GUI asked for afterwards -- opening a dialog
        included -- waited behind it. The four names it used to carry are all
        READ with no artifact contract, so each of them is still a query.
        """
        if self.impact is ImpactClass.READ and not self.artifact_contract:
            return OperationKind.QUERY
        if self.impact is ImpactClass.EXPENSIVE_JOB:
            return OperationKind.JOB
        return OperationKind.MUTATION

    @property
    def mode(self) -> ExecutionMode:
        """Required observable side effect for conformance tests and clients."""
        if self.execution_mode is not None:
            return self.execution_mode
        if self.scope == 'presentation':
            return ExecutionMode.PRESENTATION
        if self.operation == 'job.cancel' or self.impact is ImpactClass.EXPENSIVE_JOB:
            return ExecutionMode.JOB
        if self.impact is ImpactClass.READ:
            return ExecutionMode.READ
        if self.impact is ImpactClass.REVERSIBLE_EDIT:
            return ExecutionMode.TRANSACTION
        return ExecutionMode.ARTIFACT

    def to_dict(self) -> dict:
        return {
            'operation': self.operation, 'title': self.title, 'scope': self.scope,
            'summary': self.summary, 'impact': self.impact.value,
            'confirmation': self.confirmation.value,
            'capabilities': list(self.capabilities),
            'parameters_schema': self.parameters_schema,
            'result_type': self.result_type,
            'artifact_contract': list(self.artifact_contract),
            'recovery': self.recovery, 'timeout_seconds': self.timeout_seconds,
            'reversible': self.reversible, 'execution_mode': self.mode.value,
            'kind': self.kind.value,
        }


class OperationRegistry:
    def __init__(self):
        self._by_id: dict[str, OperationDescriptor] = {}

    def register(self, descriptor: OperationDescriptor) -> OperationDescriptor:
        self._by_id[descriptor.operation] = descriptor
        return descriptor

    def __contains__(self, operation: str) -> bool:
        return operation in self._by_id

    def get(self, operation: str) -> OperationDescriptor | None:
        return self._by_id.get(operation)

    def descriptors(self) -> tuple[OperationDescriptor, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))

    def to_list(self) -> list[dict]:
        return [descriptor.to_dict() for descriptor in self.descriptors()]

    def openapi_paths(self) -> dict:
        """Minimal OpenAPI path stubs generated from the operation surface."""
        paths = {}
        for descriptor in self.descriptors():
            if descriptor.scope == 'application':
                route = '/api/v1/application/operations/{operation}:execute'
            else:
                route = '/api/v1/cases/{case_id}/operations/{operation}:execute'
            paths.setdefault(route, {})['post'] = {
                'operationId': descriptor.operation,
                'summary': descriptor.summary or descriptor.title,
                'x-impact': descriptor.impact.value,
                'x-confirmation': descriptor.confirmation.value,
                'x-execution-mode': descriptor.mode.value,
                'x-kind': descriptor.kind.value,
            }
        return paths


def _collection_operations(collection_ids) -> list[OperationDescriptor]:
    descriptors = []
    for collection_id in collection_ids:
        descriptors.extend([
            OperationDescriptor(
                f'{collection_id}.create', f'Create {collection_id} entity', 'case',
                ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
                summary=f'Add a new entity to {collection_id}.'),
            OperationDescriptor(
                f'{collection_id}.patch', f'Edit {collection_id} entity', 'case',
                ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
                summary=f'Edit fields of an existing {collection_id} entity.'),
            OperationDescriptor(
                f'{collection_id}.remove', f'Remove {collection_id} entity', 'case',
                ImpactClass.DESTRUCTIVE, recovery='undo',
                summary=f'Remove an entity from {collection_id}.'),
        ])
    return descriptors


def _lifecycle_operations() -> list[OperationDescriptor]:
    return [
        OperationDescriptor('case.create', 'Create a case', 'case', ImpactClass.FILE_PRODUCING,
                            recovery='none', summary='Create a new FoamMesh case at a path.'),
        OperationDescriptor('case.open', 'Open a case', 'case', ImpactClass.READ,
                            summary='Attach a persistent session to an existing case.'),
        OperationDescriptor('case.close', 'Close a case', 'case', ImpactClass.READ),
        OperationDescriptor('case.classify', 'Classify a case', 'case', ImpactClass.READ),
        OperationDescriptor('case.save', 'Save the case', 'case', ImpactClass.FILE_PRODUCING),
        OperationDescriptor('case.copy', 'Copy the case', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('case-copy',)),
        OperationDescriptor('case.archive', 'Archive the case', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('archive',)),
        OperationDescriptor('case.clean.preview', 'Preview a clean', 'case', ImpactClass.READ),
        OperationDescriptor('case.clean', 'Clean the case', 'case', ImpactClass.DESTRUCTIVE,
                            recovery='none'),
        OperationDescriptor('case.parallel.redistribute', 'Redistribute the case', 'case',
                            ImpactClass.MESH_MUTATION, recovery='restore_point',
                            capabilities=('decomposePar', 'reconstructPar'),
                            artifact_contract=('mesh',)),
        OperationDescriptor('client_shell.terminal', 'Open terminal here', 'case',
                            ImpactClass.READ, capabilities=('blockMesh',)),
        OperationDescriptor('artifact.stage.clear', 'Clear a generated stage', 'case',
                            ImpactClass.DESTRUCTIVE, recovery='none'),
        OperationDescriptor('history.query', 'Query transaction history', 'case', ImpactClass.READ),
    ]


def _geometry_operations() -> list[OperationDescriptor]:
    return [
        OperationDescriptor('geometry.import', 'Import geometry', 'case', ImpactClass.MESH_MUTATION,
                            recovery='undo', summary='Import a surface/volume geometry file.'),
        OperationDescriptor('geometry.primitive.create', 'Create geometry primitive', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo'),
        OperationDescriptor('geometry.edit', 'Edit geometry', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo'),
        OperationDescriptor('geometry.delete', 'Delete geometry', 'case',
                            ImpactClass.DESTRUCTIVE, recovery='undo'),
        OperationDescriptor('geometry.diagnostics', 'Geometry diagnostics', 'case', ImpactClass.READ),
        OperationDescriptor('geometry.readiness', 'Geometry readiness report', 'case',
                            ImpactClass.READ, artifact_contract=('readiness_report',)),
        OperationDescriptor('geometry.fluid_seed.suggest',
                            'Suggest an interior fluid seed', 'case',
                            ImpactClass.READ),
        # R164. The Region page took any point as the material point with no
        # test that it lies inside the geometry, and the failure surfaced
        # several tasks later as an unexplained meshing error. This is the
        # same probe the launch validation uses, asked early enough to be
        # useful.
        OperationDescriptor('geometry.fluid_seed.check',
                            'Check a fluid seed against the geometry', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.rename',
                            'Rename a geometry and its boundary patch', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True),
        OperationDescriptor('geometry.patches.list',
                            'List the boundary patches', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.patches.rename',
                            'Rename a boundary patch', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True),
        OperationDescriptor('geometry.patches.merge',
                            'Merge boundary patches into one', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True),
        OperationDescriptor('geometry.patches.split',
                            'Split a merged boundary patch', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True),
        OperationDescriptor('geometry.patches.split_by_angle',
                            'Split a surface into boundaries by feature angle',
                            'case', ImpactClass.MESH_MUTATION,
                            recovery='artifact_backup'),
        OperationDescriptor('geometry.split_interfaces',
                            'Cut an assembly into its interfaces and outer skin',
                            'case', ImpactClass.MESH_MUTATION,
                            recovery='artifact_backup'),
        OperationDescriptor('quality.tolerance.get',
                            'Read the project fidelity tolerance', 'case',
                            ImpactClass.READ),
        OperationDescriptor('quality.tolerance.set',
                            'Set the project fidelity tolerance', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True),
        OperationDescriptor('geometry.preparation.decide', 'Record geometry preparation decision',
                            'case', ImpactClass.REVERSIBLE_EDIT, reversible=True,
                            recovery='undo'),
        OperationDescriptor('geometry.classify', 'Classify geometry', 'case', ImpactClass.READ),
        OperationDescriptor('geometry.split', 'Split geometry components', 'case',
                            ImpactClass.MESH_MUTATION, recovery='artifact_backup'),
        OperationDescriptor('geometry.combine', 'Combine geometry components', 'case',
                            ImpactClass.MESH_MUTATION, recovery='artifact_backup'),
        OperationDescriptor('geometry.transform', 'Transform geometry', 'case',
                            ImpactClass.MESH_MUTATION, recovery='artifact_backup'),
        OperationDescriptor('geometry.repair', 'Repair a surface', 'case', ImpactClass.MESH_MUTATION,
                            recovery='undo'),
        OperationDescriptor('geometry.repair.suggest', 'Suggest geometry repair plan', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.repair.preview', 'Preview geometry repair plan', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.repair.apply', 'Apply geometry repair plan', 'case',
                            ImpactClass.MESH_MUTATION, recovery='artifact_revision'),
        OperationDescriptor('geometry.repair.rollback', 'Restore a geometry revision', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True,
                            recovery='artifact_revision'),
        OperationDescriptor('geometry.wrap.preview', 'Estimate experimental surface wrap', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.wrap.estimate', 'Size experimental surface wrap grid', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.wrap.apply', 'Apply experimental surface wrap', 'case',
                            ImpactClass.MESH_MUTATION, recovery='artifact_revision'),
        OperationDescriptor('geometry.prepare.cancel', 'Cancel geometry preparation', 'case',
                            ImpactClass.READ,
                            summary='Stop repair or wrap at the next stage boundary.'),
        OperationDescriptor('geometry.prepared.create', 'Freeze prepared geometry', 'case',
                            ImpactClass.FILE_PRODUCING,
                            artifact_contract=('prepared-geometry', 'group-manifest')),
        OperationDescriptor('geometry.prepared.load', 'Load prepared geometry', 'case',
                            ImpactClass.READ),
        OperationDescriptor('geometry.prepared.current', 'Get selected prepared geometry',
                            'case', ImpactClass.READ),
        OperationDescriptor('geometry.prepared.select', 'Select prepared geometry',
                            'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('prepared-geometry-selection',)),
        # Plan 33 GEO-08. The counterpart of `create`: unlocking the
        # geometry step throws the published revision away, because the
        # meshers read it and it describes a file the reader is replacing.
        # FILE_PRODUCING rather than DESTRUCTIVE -- the revision folders are
        # kept and `select` can bring one back, so what this unwrites is the
        # selection, not the work.
        OperationDescriptor('geometry.prepared.discard',
                            'Discard prepared geometry', 'case',
                            ImpactClass.FILE_PRODUCING,
                            artifact_contract=('prepared-geometry-selection',),
                            summary='Reopen preparation after the geometry '
                                    'it described has changed.'),
    ]


def _workflow_operations() -> list[OperationDescriptor]:
    return [
        OperationDescriptor('mesh.engine.list', 'List meshing engines', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.engine.probe', 'Probe meshing engine', 'case',
                            ImpactClass.READ),
        OperationDescriptor('openfoam.runtime.diagnostics',
                            'OpenFOAM 13 runtime diagnostics', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.execution.decomposition_methods',
                            'Decomposition methods this runtime has', 'case',
                            ImpactClass.READ,
                            summary='Every decomposition method the writer '
                                    'can write, each said to be available '
                                    'only when the selected OpenFOAM runtime '
                                    'ships the library it needs.'),
        OperationDescriptor('mesh.execution.plan',
                            'What the next run will run on', 'case',
                            ImpactClass.READ,
                            summary='Serial or parallel, on how many '
                                    'workers, and whether the CPU ceiling '
                                    'cut the request down — resolved by the '
                                    'same allocator the run uses.'),
        OperationDescriptor('mesh.engine.workflow', 'Describe engine workflow', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.engine.select', 'Select meshing engine', 'case',
                            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo'),
        OperationDescriptor('mesh.target_solver.get', 'Read the target solver',
                            'case', ImpactClass.READ),
        OperationDescriptor('mesh.target_solver.set', 'Select the target solver',
                            'case', ImpactClass.REVERSIBLE_EDIT, reversible=True,
                            recovery='undo',
                            summary='Record which solver this mesh is for. '
                                    'Decides which engines are offered, which '
                                    'quality check runs and which export '
                                    'format is the default.'),
        OperationDescriptor('mesh.plan.derive', 'Derive meshing execution plan', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.gmsh.run', 'Run Gmsh and publish the mesh', 'case',
                            ImpactClass.EXPENSIVE_JOB,
                            capabilities=('gmsh', 'checkMesh'),
                            recovery='restore_point',
                            artifact_contract=('gmsh-run', 'mesh'),
                            summary='Write the job, run Gmsh in the qualified '
                                    'runtime, publish constant/polyMesh, and '
                                    'record requested against achieved.'),
        OperationDescriptor('mesh.gmsh.runs', 'List Gmsh runs', 'case',
                            ImpactClass.READ),
        # Plan 31 CP-05 item 4. Accepting a candidate is not the same act as
        # making one, and it used to be spelled as one: **Accept anyway** ran
        # `mesh.gmsh.run` again, so the mesh that got accepted was never the
        # mesh that was inspected. This names the candidate it is about and
        # publishes that one. No `gmsh` capability, because nothing is meshed
        # -- only checkMesh, on the polyMesh this publishes.
        OperationDescriptor('mesh.run.accept', 'Accept a stored candidate mesh',
                            'case', ImpactClass.FILE_PRODUCING,
                            capabilities=('checkMesh',),
                            recovery='restore_point',
                            artifact_contract=('gmsh-run', 'mesh'),
                            summary='Publish the run being inspected as the '
                                    'case mesh, recording any override '
                                    'against the report that run wrote.'),
        OperationDescriptor('mesh.workflow.task_state', 'Engine task states', 'case',
                            ImpactClass.READ, result_type='task_state'),
        # Plan 30 WP-08 (F-22). One task page used to cost three synchronous
        # facade calls -- the workflow descriptor twice and the task state
        # once -- every time the graph moved. This is the whole page in one
        # query: the task, the titles its prerequisites are named by, and its
        # live status and warnings.
        OperationDescriptor('mesh.workflow.task_page',
                            'Everything one task page draws', 'case',
                            ImpactClass.READ, result_type='task_page',
                            summary='The task descriptor, the workflow task '
                                    'titles, and the live status and warnings '
                                    'of this task, in one read.'),
        OperationDescriptor('mesh.workflow.task_transition',
                            'Apply an engine task lifecycle transition', 'case',
                            ImpactClass.FILE_PRODUCING, recovery='none',
                            artifact_contract=('workflow-task-state',),
                            summary='Accept, configure, skip, revert, or fail one '
                                    'engine workflow task and persist the state.'),
        OperationDescriptor('workflow.status', 'Workflow status', 'case', ImpactClass.READ),
        OperationDescriptor('workflow.start_authored', 'Start authored workflow', 'case',
                            ImpactClass.MESH_MUTATION, recovery='restore_point'),
        OperationDescriptor('workflow.return_to_external', 'Return to external workflow', 'case',
                            ImpactClass.MESH_MUTATION, recovery='restore_point'),
        OperationDescriptor('workflow.generate_dictionaries', 'Generate dictionaries', 'case',
                            ImpactClass.FILE_PRODUCING, artifact_contract=('dictionaries',)),
        OperationDescriptor(
            'workflow.effective_dictionaries', 'Effective dictionaries',
            'case', ImpactClass.READ, artifact_contract=('dictionaries',)),
        OperationDescriptor('workflow.run_stage', 'Run a meshing stage', 'case',
                            ImpactClass.EXPENSIVE_JOB, capabilities=('blockMesh', 'snappyHexMesh'),
                            recovery='restore_point', artifact_contract=('mesh',)),
        OperationDescriptor(
            'workflow.reset_stage', 'Reset a meshing stage', 'case',
            ImpactClass.DESTRUCTIVE, recovery='none',
            artifact_contract=('mesh',),
            summary='Discard a meshing stage and everything built on it so '
                    'it can be run again.'),
        OperationDescriptor(
            'workflow.run_pipeline', 'Run the resource-aware meshing pipeline',
            'case', ImpactClass.EXPENSIVE_JOB,
            capabilities=('blockMesh', 'surfaceFeatures', 'snappyHexMesh',
                          'checkMesh', 'decomposePar', 'reconstructPar',
                          'mpirun'),
            recovery='restore_point', artifact_contract=('mesh-state', 'mesh')),
    ]


def _mesh_operations() -> list[OperationDescriptor]:
    return [
        OperationDescriptor('mesh.info', 'Mesh info', 'case', ImpactClass.READ),
        OperationDescriptor('mesh.canonical.info', 'Canonical mesh info', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.canonical.validate', 'Validate canonical mesh', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.canonical.export.openfoam', 'Export canonical OpenFOAM mesh',
                            'case', ImpactClass.MESH_MUTATION, recovery='restore_point',
                            artifact_contract=('polyMesh',)),
        OperationDescriptor('quality.canonical', 'Evaluate canonical mesh quality', 'case',
                            ImpactClass.FILE_PRODUCING,
                            artifact_contract=('canonical-quality',)),
        OperationDescriptor('quality.canonical.failed_set.export',
                            'Export canonical failed set', 'case',
                            ImpactClass.FILE_PRODUCING,
                            artifact_contract=('canonical-failed-set',)),
        OperationDescriptor('mesh.canonical.selection_capabilities',
                            'Canonical selection capabilities', 'case', ImpactClass.READ),
        OperationDescriptor('mesh.check', 'Check mesh quality', 'case', ImpactClass.EXPENSIVE_JOB,
                            capabilities=('checkMesh',), artifact_contract=('quality',)),
        # Plan 28 WP4. The SU2 counterpart of `mesh.check`: same report,
        # same QA row, no `capabilities` -- it is arithmetic over the
        # polyMesh, so it needs no OpenFOAM runtime to reach a verdict.
        OperationDescriptor('quality.su2_readiness',
                            'Check the mesh is readable by SU2', 'case',
                            ImpactClass.READ,
                            artifact_contract=('quality',)),
        OperationDescriptor('mesh.reconstruct', 'Gather the decomposed mesh', 'case',
                            ImpactClass.EXPENSIVE_JOB,
                            capabilities=('reconstructPar',),
                            artifact_contract=('mesh',)),
        # Plan 26 WP5.2/WP6.2. `surfaceFeatures` is a run-gated task whose
        # output nothing could list and nothing could draw, so `includedAngle`
        # -- the control that governs the whole extraction -- had no visible
        # effect at all.
        OperationDescriptor('mesh.feature_edges', 'List extracted feature edges',
                            'case', ImpactClass.READ),
        # Plan 26 WP6.4/WP6.5. The Display Control selector offered four
        # metrics against cell arrays nothing ever wrote, and checkMesh
        # reports a maximum -- which cannot distinguish one bad cell from
        # eight thousand. Computed here because Foundation v13's checkMesh has
        # no `-writeAllFields`; that flag is an ESI extension, measured absent
        # from the live utility's own help output.
        OperationDescriptor('quality.cell_fields', 'Compute per-cell quality fields',
                            'case', ImpactClass.READ),
        # Plan 26 WP6.1. `layer_coverage` was computed on every layer run and
        # read by nothing; only the warning list was consumed, and only for
        # patches below the 50% floor, so a patch that got its layers was
        # never reported at all.
        OperationDescriptor('mesh.layer_coverage', 'Read achieved layer coverage',
                            'case', ImpactClass.READ),
        # Plan 26 WP8. A NEW id: `quality.report` is already a READ that loads
        # the persisted checkMesh JSON, and `quality.report.export` is already
        # taken and .json/.csv-only. Only the document itself was absent.
        OperationDescriptor('quality.mesh_report', 'Write the mesh report', 'case',
                            ImpactClass.FILE_PRODUCING,
                            artifact_contract=('quality-report',)),
        OperationDescriptor('quality.report', 'Load quality report', 'case', ImpactClass.READ),
        OperationDescriptor('quality.failed_sets', 'List failed quality sets', 'case',
                            ImpactClass.READ),
        OperationDescriptor('quality.failed_set.select', 'Select a failed quality set', 'case',
                            ImpactClass.READ),
        OperationDescriptor('quality.compare', 'Compare quality reports', 'case',
                            ImpactClass.READ),
        OperationDescriptor('quality.report.export', 'Export quality report', 'case',
                            ImpactClass.FILE_PRODUCING, artifact_contract=('quality-report',)),
        # Plan 23 §8.6. FILE_PRODUCING because the waiver *is* the artifact:
        # an immutable record of an engineering acceptance decision, and the
        # only route to WAIVED.
        # Plan 23 §5. EXPENSIVE_JOB: it walks every boundary section against
        # the validation reference and is bounded by the diagnostic budget.
        OperationDescriptor('quality.resolution', 'Measure resolution adequacy',
                            'case', ImpactClass.EXPENSIVE_JOB,
                            artifact_contract=('quality-resolution',)),
        OperationDescriptor('quality.fidelity', 'Measure geometry fidelity',
                            'case', ImpactClass.EXPENSIVE_JOB,
                            artifact_contract=('quality-fidelity',)),
        OperationDescriptor('quality.waiver.record', 'Record a qualification waiver',
                            'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('quality-waiver',)),
        # Plan 23 §9. Composes the three verdicts; produces summary.json,
        # which every engineering export is authorized against.
        OperationDescriptor('quality.summary', 'Compose the qualification summary',
                            'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('quality-summary',)),
        OperationDescriptor('quality.summary.read', 'Load the qualification summary',
                            'case', ImpactClass.READ),
        # Plan 23 §9's parity rule. `quality.summary.read` published one of the
        # four qualification reports; the fidelity and resolution documents had
        # no reader, so the pages built to display them had nothing to ask for
        # and rendered empty (R31/R41/R68/R90/R103/R123/R160).
        OperationDescriptor('quality.evidence.read',
                            'Load a qualification report and its readout',
                            'case', ImpactClass.READ),
        OperationDescriptor('mesh.repair.preview', 'Preview mesh repair', 'case', ImpactClass.READ),
        OperationDescriptor('mesh.repair.recommendations', 'Recommend mesh repairs', 'case',
                            ImpactClass.READ),
        OperationDescriptor('mesh.repair', 'Repair the mesh', 'case', ImpactClass.MESH_MUTATION,
                            recovery='restore_point', artifact_contract=('mesh',)),
        OperationDescriptor('mesh.recovery.list', 'List recovery points', 'case', ImpactClass.READ),
        OperationDescriptor('mesh.restore', 'Restore a recovery point', 'case',
                            ImpactClass.MESH_MUTATION, recovery='restore_point'),
        OperationDescriptor('mesh.transform.rotate', 'Rotate the mesh', 'case',
                            ImpactClass.MESH_MUTATION, capabilities=('transformPoints',),
                            recovery='restore_point', artifact_contract=('mesh',)),
        OperationDescriptor('mesh.transform.translate', 'Translate the mesh', 'case',
                            ImpactClass.MESH_MUTATION, capabilities=('transformPoints',),
                            recovery='restore_point', artifact_contract=('mesh',)),
        OperationDescriptor('mesh.transform.scale', 'Scale the mesh', 'case',
                            ImpactClass.MESH_MUTATION, capabilities=('transformPoints',),
                            recovery='restore_point', artifact_contract=('mesh',)),
        OperationDescriptor('mesh.extrude', 'Extrude the mesh', 'case', ImpactClass.MESH_MUTATION,
                            capabilities=('extrudeMesh',), recovery='restore_point',
                            artifact_contract=('mesh',)),
    ]


def _import_export_operations() -> list[OperationDescriptor]:
    return [
        OperationDescriptor('mesh.import.native', 'Import a native mesh', 'case',
                            ImpactClass.MESH_MUTATION, recovery='restore_point',
                            artifact_contract=('mesh',)),
        OperationDescriptor('mesh.import.converter', 'Import via converter', 'case',
                            ImpactClass.MESH_MUTATION, recovery='restore_point',
                            artifact_contract=('mesh',)),
        # Plan 33 W-P. No artifact contract, because listing is not
        # producing: `_export_entries` reads the format registry and censuses
        # `constant/polyMesh`, and writes nothing anywhere. The contract it
        # used to carry made it a MUTATION, which `DesktopFacadeClient.query`
        # refuses -- so the Export step's synchronous read raised on every
        # call and the step fell back to a one-item list. The rule the
        # contract encodes is untouched: every writer below still carries it.
        OperationDescriptor('case.export.entries',
                            'List export formats for this case', 'case',
                            ImpactClass.READ),
        OperationDescriptor('case.export.native', 'Export native case', 'case',
                            ImpactClass.FILE_PRODUCING, artifact_contract=('export',)),
        OperationDescriptor('case.export.vtk', 'Export VTK', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('export',)),
        OperationDescriptor('case.export.cgns', 'Export CGNS', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('export',)),
        OperationDescriptor('case.export.gmsh', 'Export Gmsh', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('export',)),
        OperationDescriptor('case.export.su2', 'Export SU2', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('export',)),
        # Plan 31 FC-F. Written by converting the accepted Gmsh run's own
        # mesh, so they produce a file at a destination exactly as the other
        # single-file writers do.
        OperationDescriptor('case.export.med', 'Export MED', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('export',)),
        OperationDescriptor('case.export.unv', 'Export UNV', 'case', ImpactClass.FILE_PRODUCING,
                            artifact_contract=('export',)),
        OperationDescriptor('case.export.fluent', 'Export Fluent', 'case', ImpactClass.FILE_PRODUCING,
                            capabilities=('foamMeshToFluent',), artifact_contract=('export',)),
        OperationDescriptor('case.export.format_convert', 'Convert case format', 'case',
                            ImpactClass.FILE_PRODUCING,
                            capabilities=('foamFormatConvert',),
                            artifact_contract=('export',)),
        OperationDescriptor('case.export.authored', 'Export authored OpenFOAM mesh', 'case',
                            ImpactClass.FILE_PRODUCING,
                            capabilities=('splitMeshRegions', 'createZones',
                                          'createPatch', 'extrudeMesh',
                                          'collapseEdges', 'reconstructPar',
                                          'mpirun'),
                            artifact_contract=('export',)),
    ]


def _presentation_operations(presentation_ids) -> list[OperationDescriptor]:
    # DP-224. The label a presentation operation is listed under
    # is a name on a screen, so the one rule spells it.
    return [OperationDescriptor(operation, humanise_option(operation.rsplit('.', 1)[-1]),
                                'presentation', ImpactClass.READ, result_type='presentation_state')
            for operation in presentation_ids]


def build_operation_registry(field_json_schema: dict, collection_ids,
                             presentation_ids=()) -> OperationRegistry:
    registry = OperationRegistry()
    core = [
        OperationDescriptor(
            'configuration.patch', 'Patch configuration fields', 'case',
            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
            summary='Batch-edit scalar configuration fields by semantic ID.',
            parameters_schema={'type': 'object', 'properties': {
                'patch': field_json_schema}}),
        OperationDescriptor(
            'configuration.dry_run', 'Dry-run a configuration patch', 'case',
            ImpactClass.READ, result_type='dry_run_report',
            summary='Validate a patch and return the before/after diff without mutating.'),
        OperationDescriptor(
            'configuration.commit_working_copy', 'Commit a GUI working copy', 'case',
            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
            summary='In-process desktop GUI commits an edited working copy through '
                    'the facade dispatcher (not reachable from REST/agent).'),
        OperationDescriptor(
            'history.undo', 'Undo the latest change set', 'case',
            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
            summary='Local human GUI owner undoes the latest reversible change set.'),
        OperationDescriptor(
            'history.redo', 'Redo the last undone change set', 'case',
            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo'),
        OperationDescriptor(
            'history.revert_change_set', 'Revert the latest change set', 'case',
            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
            summary='Revert only the latest reversible authored change set.'),
        OperationDescriptor(
            'history.revert_plan', 'Revert an executed plan', 'case',
            ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo'),
        OperationDescriptor(
            'artifact.report.generate', 'Generate a report artifact', 'case',
            ImpactClass.FILE_PRODUCING, artifact_contract=('report',),
            recovery='none', result_type='artifact_result'),
        OperationDescriptor(
            'job.test.start', 'Start a deterministic test job', 'case',
            ImpactClass.EXPENSIVE_JOB, recovery='none', result_type='job_result',
            timeout_seconds=300),
        OperationDescriptor(
            'job.cancel', 'Cancel a running job', 'case', ImpactClass.READ,
            result_type='job_result'),
        # Plan 30 WP-08 (F-09). Every progress surface offers Cancel, and none
        # of them knows a job id: the run they are showing was started as one
        # facade command whose job ids live inside it. This stops whatever
        # this case is running -- snappy's WSL process group and the Gmsh
        # runner alike, because both reach the machine through the one
        # ``JobManager`` that owns their process groups.
        OperationDescriptor(
            'job.cancel_active', 'Cancel whatever this case is running',
            'case', ImpactClass.READ, result_type='job_result',
            summary='Stop every job running for this case, including the '
                    'engine process group of a meshing stage.'),
        OperationDescriptor(
            'application.settings.patch', 'Patch application settings', 'application',
            ImpactClass.REVERSIBLE_EDIT, reversible=True,
            summary='Edit per-user application preferences, independent of any case.'),
    ]
    for descriptor in core:
        registry.register(descriptor)
    for descriptor in _collection_operations(collection_ids):
        registry.register(descriptor)
    for descriptor in (_lifecycle_operations() + _geometry_operations()
                       + _workflow_operations() + _mesh_operations()
                       + _import_export_operations()
                       + _presentation_operations(presentation_ids)):
        registry.register(descriptor)
    return registry
