"""Read-only and mutating mesh operation services."""

from .info import (
    BoundaryPatch, FoamFileMetadata, MeshBounds, MeshInfo, MeshInfoService, MeshZone,
)
from .recovery import MeshRecoveryPoint, MeshRecoveryService, MeshRestoreOutcome
from .transform import (
    MeshTransformRequest, MeshTransformRun, MeshTransformService,
    TransformPointsProfile, parse_vector,
)
from .model import (
    BoundaryBlock, CanonicalMesh, CanonicalMeshError, CanonicalMeshStore,
    CellBlock, CellType,
)
from .validate import MeshIssue, MeshValidationReport, validate_mesh
from .layout import MeshArtifactRef, MeshLayout, MeshStateError, MeshStateStore

__all__ = [
    'BoundaryPatch', 'FoamFileMetadata', 'MeshBounds', 'MeshInfo', 'MeshInfoService',
    'MeshZone', 'MeshRecoveryPoint', 'MeshRecoveryService', 'MeshRestoreOutcome',
    'MeshTransformRequest', 'MeshTransformRun', 'MeshTransformService',
    'TransformPointsProfile', 'parse_vector',
    'RepairOperation', 'RepairPreview', 'RepairRequest', 'RepairRun', 'MeshRepairService',
    'BoundaryBlock', 'CanonicalMesh', 'CanonicalMeshError', 'CanonicalMeshStore',
    'CellBlock', 'CellType', 'MeshIssue', 'MeshValidationReport', 'validate_mesh',
    'MeshArtifactRef', 'MeshLayout', 'MeshStateError', 'MeshStateStore',
]


def __getattr__(name):
    """Load quality-dependent repair services lazily to avoid jobs/mesh cycles."""
    if name in {'MeshRepairService', 'RepairOperation', 'RepairPreview',
                'RepairRequest', 'RepairRun'}:
        from . import repair
        return getattr(repair, name)
    raise AttributeError(name)
