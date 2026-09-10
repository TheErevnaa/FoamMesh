"""Public meshing-engine seam."""

from .base import EngineProbe, MeshingEngine, StageDefinition, StageRun
from .contracts import (
    ArtifactContract, ArtifactKind, CapabilityRequirement, ContractError,
    EngineDescriptor, EngineExecutionPlan, EnginePlanRequest,
    FieldBinding, FieldClassification, MeshingIntent, PlannedTask,
    PreparedGeometryRef, TaskCardinality, TaskState, WorkflowDescriptor,
    WorkflowTask, fingerprint_plan_inputs, merge_capabilities,
    validate_field_budget,
)
# Concrete engines and the registry are resolved lazily (PEP 562).  Importing
# ``foammesh.core.engine.contracts`` is intentionally safe from every other
# core package: eagerly importing the registry pulled in Snappy, which pulled
# in quality/jobs/mesh/geometry and re-entered this package through prepared
# geometry.  That made public imports depend on test collection order.
_LAZY = {
    'ENGINE_REGISTRY': '.registry',
    'EngineNotRegisteredError': '.registry',
    'EngineRegistry': '.registry',
    'configured_engine_id': '.registry',
    'resolve_engine': '.registry',
    'SNAPPY_DESCRIPTOR': '.snappy',
    'SnappyMeshingEngine': '.snappy',
    'GMSH_DESCRIPTOR': '.gmsh',
    'GmshMeshingEngine': '.gmsh',
    'EngineSelectionService': '.selection',
    'EngineSwitchPreview': '.selection',
}


def __getattr__(name):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from importlib import import_module
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))

__all__ = [
    'ArtifactContract', 'ArtifactKind', 'CapabilityRequirement', 'ContractError',
    'ENGINE_REGISTRY', 'EngineDescriptor', 'EngineExecutionPlan',
    'EngineNotRegisteredError', 'EnginePlanRequest', 'EngineProbe',
    'EngineRegistry', 'EngineSelectionService', 'EngineSwitchPreview',
    'FieldBinding', 'FieldClassification', 'MeshingEngine',
    'MeshingIntent', 'PlannedTask', 'PreparedGeometryRef',
    'GMSH_DESCRIPTOR', 'GmshMeshingEngine',
    'SNAPPY_DESCRIPTOR', 'SnappyMeshingEngine',
    'StageDefinition', 'StageRun', 'TaskCardinality', 'TaskState',
    'WorkflowDescriptor', 'WorkflowTask', 'configured_engine_id',
    'fingerprint_plan_inputs', 'merge_capabilities', 'resolve_engine',
    'validate_field_budget',
]
