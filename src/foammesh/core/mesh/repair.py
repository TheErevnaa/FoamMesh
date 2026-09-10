"""Recovery-backed adapters for bounded OpenFOAM mesh repair utilities.

These adapters intentionally do not promise a universal mesh repair.  Each
operation has a precise utility, prerequisite, recovery point, and post-run
polyMesh validation.  ``subsetMesh`` is destructive, so it always runs in a
new case copy instead of changing the case that is open in FoamMesh.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from foammesh.core.case import (
    ArtifactState, CaseMetadata, CaseMetadataError, MeshOrigin, classify_case,
    copy_case_directory, fingerprint_poly_mesh, load_case_metadata,
    record_artifact_event, save_case_metadata, workflow_after_mesh_mutation,
)
from foammesh.core.jobs import JobManager, JobRequest, JobResult, JobStatus
from foammesh.core.quality import CheckMeshResult, MeshCheckService

from .info import MeshInfo, MeshInfoService
from .recovery import MeshRecoveryPoint, MeshRecoveryService


class RepairOperation(str, Enum):
    RENUMBER = 'renumber'
    COLLAPSE_EDGES = 'collapse_edges'
    SUBSET_CELLS = 'subset_cells'
    REBUILD_PATCHES = 'rebuild_patches'
    COMBINE_PATCH_FACES = 'combine_patch_faces'

    @property
    def utility_name(self) -> str:
        return {
            RepairOperation.RENUMBER: 'renumberMesh',
            RepairOperation.COLLAPSE_EDGES: 'collapseEdges',
            RepairOperation.SUBSET_CELLS: 'subsetMesh',
            RepairOperation.REBUILD_PATCHES: 'createPatch',
            RepairOperation.COMBINE_PATCH_FACES: 'combinePatchFaces',
        }[self]

    @property
    def label(self) -> str:
        return {
            RepairOperation.RENUMBER: 'Renumber cells',
            RepairOperation.COLLAPSE_EDGES: 'Collapse configured edges',
            RepairOperation.SUBSET_CELLS: 'Extract selected cell set to a new case',
            RepairOperation.REBUILD_PATCHES: 'Rebuild configured patch definitions',
            RepairOperation.COMBINE_PATCH_FACES: 'Combine configured patch faces',
        }[self]


_SET_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*$')


@dataclass(frozen=True)
class RepairRequest:
    operation: RepairOperation
    cell_set: str | None = None

    def argv(self, utility: str) -> tuple[str, ...]:
        if not utility:
            raise ValueError(f'{self.operation.utility_name} is not available')
        if self.operation is RepairOperation.SUBSET_CELLS:
            if not self.cell_set or not _SET_NAME.fullmatch(self.cell_set):
                raise ValueError('subset repair needs a safe, non-empty cell-set name')
            # Foundation v13 contract (verified live): the set is passed via
            # -cellSet; a positional name is rejected ("Wrong number of
            # arguments"). -overwrite is deprecated-but-accepted on v13 and
            # required by older targets.
            return utility, '-cellSet', self.cell_set, '-overwrite'
        return utility, '-overwrite'

    def validate_prerequisites(self, case_path: str | Path):
        case = Path(case_path)
        if self.operation is RepairOperation.COLLAPSE_EDGES:
            required = case / 'system' / 'collapseDict'
            if not required.is_file():
                raise ValueError('collapse repair requires system/collapseDict; configure and review it first')
        elif self.operation is RepairOperation.REBUILD_PATCHES:
            required = case / 'system' / 'createPatchDict'
            if not required.is_file():
                raise ValueError('patch rebuild requires system/createPatchDict; review its patch mapping first')
        elif self.operation is RepairOperation.COMBINE_PATCH_FACES:
            required = case / 'system' / 'combinePatchFacesDict'
            if not required.is_file():
                raise ValueError(
                    'combine patch faces requires system/combinePatchFacesDict; review it first')
        elif self.operation is RepairOperation.SUBSET_CELLS:
            if not self.cell_set or not _SET_NAME.fullmatch(self.cell_set):
                raise ValueError('subset repair needs a safe, non-empty cell-set name')
            set_file = case / 'constant' / 'polyMesh' / 'sets' / self.cell_set
            if not set_file.is_file():
                raise ValueError(f'cell set does not exist: {self.cell_set}')


@dataclass(frozen=True)
class RepairRun:
    request: RepairRequest
    case_path: Path
    job: JobResult
    recovery: MeshRecoveryPoint | None
    restored: bool
    before: MeshInfo
    after: MeshInfo | None
    quality: CheckMeshResult | None = None
    validation_error: str | None = None
    copied_from: Path | None = None

    @property
    def succeeded(self) -> bool:
        return self.job.status is JobStatus.DONE and not self.restored and self.validation_error is None

    def to_dict(self) -> dict:
        return {
            'operation': self.request.operation.value,
            'case_path': str(self.case_path),
            'copied_from': str(self.copied_from) if self.copied_from else None,
            'job': self.job.to_dict(),
            'recovery_id': self.recovery.recovery_id if self.recovery else None,
            'restored': self.restored,
            'before': self.before.to_dict(),
            'after': self.after.to_dict() if self.after else None,
            'quality': self.quality.to_dict() if self.quality else None,
            'validation_error': self.validation_error,
        }


@dataclass(frozen=True)
class RepairPreview:
    request: RepairRequest
    command: tuple[str, ...]
    before: MeshInfo
    destructive: bool
    runs_in_copy: bool
    configuration_path: Path | None = None
    configuration_text: str | None = None

    def to_dict(self) -> dict:
        return {
            'operation': self.request.operation.value,
            'command': list(self.command), 'before': self.before.to_dict(),
            'destructive': self.destructive, 'runs_in_copy': self.runs_in_copy,
            'configuration_path': str(self.configuration_path) if self.configuration_path else None,
            'configuration_text': self.configuration_text,
        }


class MeshRepairService:
    """Run only configured, capability-proven repair utilities safely."""

    def __init__(self, utilities: dict[str, str] | None = None,
                 jobs: JobManager | None = None, recovery: MeshRecoveryService | None = None,
                 *, check_utility: str | None = None, launcher=None):
        self._utilities = dict(utilities or {})
        self._jobs = jobs or JobManager()
        self._recovery = recovery or MeshRecoveryService()
        self._info = MeshInfoService()
        self._check_utility = check_utility
        self._launcher = launcher

    def available_operations(self, case_path: str | Path | None = None) -> tuple[RepairOperation, ...]:
        """Return utilities present in the configured environment and, if supplied,
        whose case-specific dictionary prerequisites are satisfied.
        """
        operations: list[RepairOperation] = []
        for operation in RepairOperation:
            if not self._utilities.get(operation.utility_name):
                continue
            if case_path is not None:
                try:
                    # A subset set name is chosen later, so only validate the
                    # dictionary prerequisites here.
                    if operation is RepairOperation.COLLAPSE_EDGES:
                        RepairRequest(operation).validate_prerequisites(case_path)
                    elif operation is RepairOperation.REBUILD_PATCHES:
                        RepairRequest(operation).validate_prerequisites(case_path)
                    elif operation is RepairOperation.COMBINE_PATCH_FACES:
                        RepairRequest(operation).validate_prerequisites(case_path)
                except ValueError:
                    continue
            operations.append(operation)
        return tuple(operations)

    def preview(self, case_path: str | Path, request: RepairRequest) -> RepairPreview:
        case = Path(case_path).resolve()
        request.validate_prerequisites(case)
        command = request.argv(self._utilities.get(request.operation.utility_name) or '')
        config_name = {
            RepairOperation.COLLAPSE_EDGES: 'collapseDict',
            RepairOperation.REBUILD_PATCHES: 'createPatchDict',
            RepairOperation.COMBINE_PATCH_FACES: 'combinePatchFacesDict',
        }.get(request.operation)
        config_path = case / 'system' / config_name if config_name else None
        config_text = (config_path.read_text(encoding='utf-8', errors='replace')
                       if config_path and config_path.is_file() else None)
        return RepairPreview(
            request=request, command=command, before=self._info.inspect(case),
            destructive=request.operation is not RepairOperation.SUBSET_CELLS,
            runs_in_copy=request.operation is RepairOperation.SUBSET_CELLS,
            configuration_path=config_path, configuration_text=config_text)

    async def run(self, case_path: str | Path, request: RepairRequest,
                  *, subset_destination: str | Path | None = None, on_line=None) -> RepairRun:
        source = Path(case_path).resolve()
        request.validate_prerequisites(source)
        target, copied_from = self._target_case(source, request, subset_destination)
        before = self._info.inspect(target)
        recovery = self._recovery.snapshot(target, operation=f'repair:{request.operation.value}')
        utility = self._utilities.get(request.operation.utility_name)
        if self._launcher is not None:
            semantic = request.argv(request.operation.utility_name)[1:]
            launch = self._launcher(
                request.operation.utility_name, semantic, cwd=target)
            command = launch.argv
            cleanup_argv = launch.cleanup_argv
        else:
            command = request.argv(utility or '')
            cleanup_argv = ()
        job_request = JobRequest(
            name=f'repair {request.operation.value}', argv=command, cwd=target,
            mutation=True, log_path=target / 'foammesh' / 'logs' / f'repair-{request.operation.value}.log',
            timeout=300.0, cleanup_argv=cleanup_argv)
        job = (await self._jobs.run(job_request) if on_line is None else
               await self._jobs.run(job_request, on_line=on_line))
        if job.status is not JobStatus.DONE:
            self._recovery.restore(target, recovery)
            record_artifact_event(
                target, operation=f'repair:{request.operation.value}', status=job.status.value,
                command=command, before_fingerprint=before.fingerprint,
                after_fingerprint=before.fingerprint, recovery_id=recovery.recovery_id,
                recovery_status='restored')
            return RepairRun(request, target, job, recovery, True, before, None, copied_from=copied_from)

        try:
            after = self._info.inspect(target)
            self._record_mutation(target, request)
        except (OSError, ValueError, RuntimeError, CaseMetadataError) as error:
            self._recovery.restore(target, recovery)
            record_artifact_event(
                target, operation=f'repair:{request.operation.value}', status='validation_failed',
                command=command, before_fingerprint=before.fingerprint,
                after_fingerprint=before.fingerprint, recovery_id=recovery.recovery_id,
                recovery_status='restored', details={'error': str(error)})
            return RepairRun(request, target, job, recovery, True, before, None,
                             validation_error=f'post-repair validation failed: {error}', copied_from=copied_from)
        quality = None
        if self._check_utility:
            checked = await MeshCheckService(
                self._jobs, utility=self._check_utility,
                launcher=self._launcher).run(target)
            quality = checked.result
        self._recovery.mark_available(recovery)
        record_artifact_event(
            target, operation=f'repair:{request.operation.value}', status='applied',
            command=command, before_fingerprint=before.fingerprint,
            after_fingerprint=after.fingerprint, recovery_id=recovery.recovery_id,
            recovery_status='available', details={
                'cell_set': request.cell_set,
                'copied_from': str(copied_from) if copied_from else None,
            })
        return RepairRun(request, target, job, recovery, False, before, after, quality,
                         copied_from=copied_from)

    @staticmethod
    def _target_case(source: Path, request: RepairRequest,
                     subset_destination: str | Path | None) -> tuple[Path, Path | None]:
        if request.operation is not RepairOperation.SUBSET_CELLS:
            if subset_destination is not None:
                raise ValueError('a destination is only valid for subset repair')
            return source, None
        if subset_destination is None:
            raise ValueError('subset repair is destructive and requires a new destination case')
        copied = copy_case_directory(source, subset_destination)
        return copied.destination, source

    @staticmethod
    def _record_mutation(case: Path, request: RepairRequest):
        classification = classify_case(case)
        if classification.poly_mesh_path is None:
            raise RuntimeError('repair completed without a complete polyMesh')
        try:
            previous = load_case_metadata(case)
        except CaseMetadataError:
            # A raw native case is a valid repair target.  Its generated mesh
            # cannot be attributed to an authored pipeline, so preserve that
            # truth by entering external-mesh mode after the mutation.
            previous = None
        provenance = dict(previous.provenance) if previous is not None else {}
        provenance['last_mesh_mutation'] = f'repair:{request.operation.value}'
        workflow, suspended = workflow_after_mesh_mutation(previous)
        changes = dict(
            workflow=workflow,
            mesh_origin=MeshOrigin.DERIVED_MUTATION,
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint_poly_mesh(classification.poly_mesh_path),
            authored_workflow_suspended=suspended,
            provenance=provenance,
        )
        save_case_metadata(case, previous.evolve(**changes) if previous else CaseMetadata(**changes))
