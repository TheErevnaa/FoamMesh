"""Case classification, workflow-mode, and mesh-provenance primitives.

This package deliberately has no Qt or OpenFOAM-process dependency.  The GUI,
CLI, and API use the same classification and workflow rules before deciding how
to open a case or present the authored meshing pipeline.
"""

from .model import (
    CASE_METADATA_FILE,
    CASE_METADATA_VERSION,
    SIDECAR_DIRECTORY,
    ArtifactState,
    CaseClassification,
    CaseKind,
    CaseMetadata,
    CaseMetadataError,
    DEFAULT_OPENFOAM_TARGET,
    MeshFingerprint,
    MeshOrigin,
    WorkflowMode,
    WorkflowResolution,
    classify_case,
    fingerprint_poly_mesh,
    load_case_metadata,
    record_generated_mesh,
    resolve_workflow,
    save_case_metadata,
    validate_poly_mesh_headers,
    workflow_after_mesh_mutation,
)
from .summary import ExternalMeshSummary, external_mesh_summary
from .launch import startup_case_path
from .copying import CaseCopyResult, copy_case_directory
from .scratch import (
    SCRATCH_DIRNAME, create_scratch_dir, discard_scratch_case, is_scratch_case,
    prune_stale, scratch_root, suggested_name,
)
from .locking import (
    LOCK_INFO_FILE, CaseLockInfo, CaseLockInspection, CaseLockState,
    inspect_case_lock,
)
from .conflict import CaseConflictError, CaseExternalSnapshot
from .service import CaseService
from .artifact_history import (
    ARTIFACT_HISTORY_FILE, ArtifactHistoryEntry, ArtifactHistoryStore,
    record_artifact_event,
)

__all__ = [
    'CASE_METADATA_FILE',
    'CASE_METADATA_VERSION',
    'SIDECAR_DIRECTORY',
    'ArtifactState',
    'CaseClassification',
    'CaseKind',
    'CaseMetadata',
    'CaseMetadataError',
    'DEFAULT_OPENFOAM_TARGET',
    'MeshFingerprint',
    'MeshOrigin',
    'WorkflowMode',
    'WorkflowResolution',
    'ExternalMeshSummary',
    'CaseCopyResult',
    'classify_case',
    'fingerprint_poly_mesh',
    'load_case_metadata',
    'record_generated_mesh',
    'resolve_workflow',
    'save_case_metadata',
    'validate_poly_mesh_headers',
    'workflow_after_mesh_mutation',
    'external_mesh_summary',
    'startup_case_path',
    'copy_case_directory',
    'SCRATCH_DIRNAME', 'create_scratch_dir', 'discard_scratch_case',
    'is_scratch_case', 'prune_stale', 'scratch_root', 'suggested_name',
    'LOCK_INFO_FILE', 'CaseLockInfo', 'CaseLockInspection', 'CaseLockState',
    'inspect_case_lock',
    'CaseConflictError', 'CaseExternalSnapshot',
    'CaseService',
    'ARTIFACT_HISTORY_FILE', 'ArtifactHistoryEntry', 'ArtifactHistoryStore',
    'record_artifact_event',
    'WorkflowTransitionService',
    'CaseAudit', 'audit_case',
]


def __getattr__(name):
    """Load audit helpers lazily to avoid a case ↔ mesh import cycle.

    The audit service reads mesh recovery and quality data.  Importing it while
    the mesh package is importing case primitives would otherwise make the
    public ``foammesh.core.case`` namespace order-dependent.
    """
    if name in {'CaseAudit', 'audit_case'}:
        from .audit import CaseAudit, audit_case
        return {'CaseAudit': CaseAudit, 'audit_case': audit_case}[name]
    if name == 'WorkflowTransitionService':
        from .workflow import WorkflowTransitionService
        return WorkflowTransitionService
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
