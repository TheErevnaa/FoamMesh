"""Engine-neutral meshing descriptors and execution-plan value objects.

These classes deliberately contain no Qt, OpenFOAM, mesher, or process-launch
imports.  They form the serializable contract shared by the facade, desktop,
API, CLI, job manager, and individual engine adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping


class ContractError(ValueError):
    """Raised when an engine publishes an invalid descriptor or plan."""


class TaskCardinality(str, Enum):
    REQUIRED = 'required'
    OPTIONAL = 'optional'
    REPEATABLE = 'repeatable'


class TaskState(str, Enum):
    LOCKED = 'locked'
    READY = 'ready'
    EDITING = 'editing'
    CONFIGURED = 'configured'
    RUNNING = 'running'
    PASSED = 'passed'
    WARNING = 'warning'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    STALE = 'stale'
    #: A valid report was produced. Plan 23 §8.5: evidence completion is not an
    #: engineering pass. A GF2 task whose report says ``fail`` has done its job
    #: and must unblock the summary, but it must never render green -- treating
    #: it as FAILED would lock the summary behind ``_ACCEPTED``, and treating it
    #: as PASSED would show a false verdict.
    COMPLETED = 'completed'
    #: An engineer accepted a non-passing report. Recorded, never green.
    WAIVED = 'waived'


class ArtifactKind(str, Enum):
    FILE = 'file'
    DIRECTORY = 'directory'
    JSON = 'json'
    MED = 'med'
    CANONICAL_MESH = 'canonical_mesh'
    POLY_MESH = 'poly_mesh'
    QUALITY_REPORT = 'quality_report'
    FEATURE_EDGES = 'feature_edges'


class FieldClassification(str, Enum):
    NATIVE = 'native'
    DERIVED = 'derived'
    PRECHECK = 'foammesh_precheck'
    EXPERIMENTAL = 'experimental'
    DEFERRED = 'deferred'


@dataclass(frozen=True)
class CapabilityRequirement:
    capability: str
    required: bool = True
    reason: str = ''

    def __post_init__(self) -> None:
        _require_token(self.capability, 'capability')

    def to_dict(self) -> dict:
        return {
            'capability': self.capability,
            'required': self.required,
            'reason': self.reason,
        }


@dataclass(frozen=True)
class ArtifactContract:
    artifact_id: str
    kind: ArtifactKind
    relative_path: str
    required: bool = True
    checksum: bool = True
    validator: str | None = None

    def __post_init__(self) -> None:
        _require_token(self.artifact_id, 'artifact_id')
        normalized = PurePosixPath(str(self.relative_path).replace('\\', '/'))
        if normalized.is_absolute() or '..' in normalized.parts or str(normalized) in {'', '.'}:
            raise ContractError('artifact relative_path must stay below the run workspace')
        object.__setattr__(self, 'relative_path', normalized.as_posix())

    def to_dict(self) -> dict:
        return {
            'artifact_id': self.artifact_id,
            'kind': self.kind.value,
            'relative_path': self.relative_path,
            'required': self.required,
            'checksum': self.checksum,
            'validator': self.validator,
        }


@dataclass(frozen=True)
class FieldBinding:
    field_id: str
    classification: FieldClassification
    native_name: str | None = None
    calculation_version: str | None = None
    applies_when: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_field_id(self.field_id)
        if self.classification is FieldClassification.NATIVE and not self.native_name:
            raise ContractError(f'native field {self.field_id!r} requires native_name')
        if self.classification is FieldClassification.DERIVED and not self.calculation_version:
            raise ContractError(
                f'derived field {self.field_id!r} requires calculation_version')

    @property
    def runner_consumed(self) -> bool:
        return self.classification in {
            FieldClassification.NATIVE,
            FieldClassification.DERIVED,
            FieldClassification.PRECHECK,
        }

    def to_dict(self) -> dict:
        return {
            'field_id': self.field_id,
            'classification': self.classification.value,
            'native_name': self.native_name,
            'calculation_version': self.calculation_version,
            'applies_when': list(self.applies_when),
            'runner_consumed': self.runner_consumed,
        }


@dataclass(frozen=True)
class WorkflowTask:
    task_id: str
    title: str
    order: int
    cardinality: TaskCardinality = TaskCardinality.REQUIRED
    depends_on: tuple[str, ...] = ()
    capabilities: tuple[CapabilityRequirement, ...] = ()
    artifacts: tuple[ArtifactContract, ...] = ()
    fields: tuple[FieldBinding, ...] = ()
    invalidates: tuple[str, ...] = ()
    description: str = ''
    engine_stage: str | None = None
    accepts_override: bool = False
    #: Acceptance requires a recorded run result rather than a manual Update.
    #: A task that computes or publishes a mesh must be evidence-backed, or a
    #: stage reads "passed" on work that never ran.
    run_gated: bool = False

    def __post_init__(self) -> None:
        _require_token(self.task_id, 'task_id')
        if not self.title.strip():
            raise ContractError(f'task {self.task_id!r} requires a title')
        if self.order < 0:
            raise ContractError(f'task {self.task_id!r} has a negative order')
        if self.task_id in self.depends_on:
            raise ContractError(f'task {self.task_id!r} cannot depend on itself')
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ContractError(f'task {self.task_id!r} has duplicate dependencies')
        if len({item.field_id for item in self.fields}) != len(self.fields):
            raise ContractError(f'task {self.task_id!r} has duplicate field bindings')

    @property
    def optional(self) -> bool:
        return self.cardinality is not TaskCardinality.REQUIRED

    @property
    def repeatable(self) -> bool:
        return self.cardinality is TaskCardinality.REPEATABLE

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'title': self.title,
            'order': self.order,
            'cardinality': self.cardinality.value,
            'optional': self.optional,
            'repeatable': self.repeatable,
            'depends_on': list(self.depends_on),
            'capabilities': [item.to_dict() for item in self.capabilities],
            'artifacts': [item.to_dict() for item in self.artifacts],
            'fields': [item.to_dict() for item in self.fields],
            'invalidates': list(self.invalidates),
            'description': self.description,
            'engine_stage': self.engine_stage,
            'accepts_override': self.accepts_override,
            'run_gated': self.run_gated,
        }


@dataclass(frozen=True)
class WorkflowDescriptor:
    engine_id: str
    version: int
    tasks: tuple[WorkflowTask, ...]
    display_name: str = ''
    description: str = ''
    _by_id: Mapping[str, WorkflowTask] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_token(self.engine_id, 'engine_id')
        if self.version < 1:
            raise ContractError('workflow descriptor version must be positive')
        if not self.tasks:
            raise ContractError('workflow descriptor requires at least one task')
        by_id = {task.task_id: task for task in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ContractError('workflow descriptor has duplicate task IDs')
        orders = [task.order for task in self.tasks]
        if len(set(orders)) != len(orders):
            raise ContractError('workflow descriptor has duplicate task order values')
        for task in self.tasks:
            missing = set(task.depends_on) - set(by_id)
            if missing:
                raise ContractError(
                    f'task {task.task_id!r} has unknown dependencies: {sorted(missing)}')
            invalid = set(task.invalidates) - set(by_id)
            if invalid:
                raise ContractError(
                    f'task {task.task_id!r} invalidates unknown tasks: {sorted(invalid)}')
        _topological_order(by_id)
        object.__setattr__(self, '_by_id', MappingProxyType(by_id))

    def task(self, task_id: str) -> WorkflowTask:
        try:
            return self._by_id[task_id]
        except KeyError as error:
            raise KeyError(f'unknown workflow task: {task_id}') from error

    def ordered_tasks(self) -> tuple[WorkflowTask, ...]:
        return tuple(sorted(self.tasks, key=lambda item: item.order))

    def descendants(self, task_id: str) -> tuple[str, ...]:
        self.task(task_id)
        found: set[str] = set()
        frontier = [task_id]
        while frontier:
            parent = frontier.pop()
            for task in self.tasks:
                if parent in task.depends_on and task.task_id not in found:
                    found.add(task.task_id)
                    frontier.append(task.task_id)
        return tuple(task.task_id for task in self.ordered_tasks() if task.task_id in found)

    def editable_fields(self) -> tuple[FieldBinding, ...]:
        by_id: dict[str, FieldBinding] = {}
        for task in self.ordered_tasks():
            for binding in task.fields:
                if binding.classification not in {
                        FieldClassification.EXPERIMENTAL,
                        FieldClassification.DEFERRED}:
                    prior = by_id.get(binding.field_id)
                    if prior is not None and prior != binding:
                        raise ContractError(
                            f'field {binding.field_id!r} has conflicting workflow bindings')
                    by_id[binding.field_id] = binding
        return tuple(by_id[key] for key in sorted(by_id))

    def to_dict(self) -> dict:
        return {
            'engine_id': self.engine_id,
            'version': self.version,
            'display_name': self.display_name or self.engine_id,
            'description': self.description,
            'tasks': [task.to_dict() for task in self.ordered_tasks()],
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class EngineDescriptor:
    engine_id: str
    display_name: str
    workflow_version: int
    summary: str
    cell_families: tuple[str, ...]
    runtime_kind: str
    optional_runtime: bool = True

    def __post_init__(self) -> None:
        _require_token(self.engine_id, 'engine_id')
        if self.workflow_version < 1:
            raise ContractError('workflow_version must be positive')
        if not self.cell_families:
            raise ContractError('at least one cell family is required')

    def to_dict(self) -> dict:
        return {
            'engine_id': self.engine_id,
            'display_name': self.display_name,
            'workflow_version': self.workflow_version,
            'summary': self.summary,
            'cell_families': list(self.cell_families),
            'runtime_kind': self.runtime_kind,
            'optional_runtime': self.optional_runtime,
        }


@dataclass(frozen=True)
class PreparedGeometryRef:
    revision_id: str
    root: Path
    geometry_path: Path
    manifest_path: Path
    group_manifest_path: Path
    fingerprint: str

    def __post_init__(self) -> None:
        _require_token(self.revision_id, 'revision_id')
        if len(self.fingerprint) != 64 or any(
                char not in '0123456789abcdef' for char in self.fingerprint.lower()):
            raise ContractError('prepared geometry fingerprint must be a SHA-256 hex digest')
        root = Path(self.root).resolve()
        object.__setattr__(self, 'root', root)
        for name in ('geometry_path', 'manifest_path', 'group_manifest_path'):
            path = Path(getattr(self, name)).resolve()
            if root != path and root not in path.parents:
                raise ContractError(f'{name} must stay below prepared geometry root')
            object.__setattr__(self, name, path)

    def to_dict(self) -> dict:
        return {
            'revision_id': self.revision_id,
            'root': str(self.root),
            'geometry_path': str(self.geometry_path),
            'manifest_path': str(self.manifest_path),
            'group_manifest_path': str(self.group_manifest_path),
            'fingerprint': self.fingerprint,
        }


@dataclass(frozen=True)
class MeshingIntent:
    units: str = 'm'
    global_target_size: float | None = None
    minimum_size: float | None = None
    maximum_cells: int | None = None
    growth_rate: float | None = None
    curvature_policy: str = 'automatic'
    quality_policy: str = 'balanced'
    local_sizes: tuple[dict, ...] = ()
    edge_controls: tuple[dict, ...] = ()
    zone_controls: tuple[dict, ...] = ()
    interface_pairs: tuple[dict, ...] = ()
    layer_controls: tuple[dict, ...] = ()
    native: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.units != 'm':
            raise ContractError('MeshingIntent uses metres as its canonical unit')
        for name in ('global_target_size', 'minimum_size', 'growth_rate'):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ContractError(f'{name} must be positive')
        if self.maximum_cells is not None and self.maximum_cells < 1:
            raise ContractError('maximum_cells must be positive')
        if (self.global_target_size is not None and self.minimum_size is not None
                and self.minimum_size > self.global_target_size):
            raise ContractError('minimum_size cannot exceed global_target_size')
        object.__setattr__(self, 'native', MappingProxyType(dict(self.native)))

    def to_dict(self) -> dict:
        return {
            'units': self.units,
            'global_target_size': self.global_target_size,
            'minimum_size': self.minimum_size,
            'maximum_cells': self.maximum_cells,
            'growth_rate': self.growth_rate,
            'curvature_policy': self.curvature_policy,
            'quality_policy': self.quality_policy,
            'local_sizes': list(self.local_sizes),
            'edge_controls': list(self.edge_controls),
            'zone_controls': list(self.zone_controls),
            'interface_pairs': list(self.interface_pairs),
            'layer_controls': list(self.layer_controls),
            'native': dict(self.native),
        }


@dataclass(frozen=True)
class EnginePlanRequest:
    case_path: Path
    run_path: Path
    intent: MeshingIntent
    prepared_geometry: PreparedGeometryRef | None = None
    configuration_revision: int | str | None = None
    resource_policy: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        case = Path(self.case_path).resolve()
        run = Path(self.run_path).resolve()
        if case != run and case not in run.parents:
            raise ContractError('run_path must stay inside the case')
        object.__setattr__(self, 'case_path', case)
        object.__setattr__(self, 'run_path', run)
        object.__setattr__(
            self, 'resource_policy',
            MappingProxyType(dict(self.resource_policy)))


@dataclass(frozen=True)
class PlannedTask:
    task_id: str
    stage: str | None
    depends_on: tuple[str, ...]
    capabilities: tuple[CapabilityRequirement, ...]
    expected_artifacts: tuple[ArtifactContract, ...]
    derived_settings: Mapping[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_token(self.task_id, 'task_id')
        object.__setattr__(self, 'derived_settings',
                           MappingProxyType(dict(self.derived_settings)))

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'stage': self.stage,
            'depends_on': list(self.depends_on),
            'capabilities': [item.to_dict() for item in self.capabilities],
            'expected_artifacts': [item.to_dict() for item in self.expected_artifacts],
            'derived_settings': dict(self.derived_settings),
            'warnings': list(self.warnings),
        }


@dataclass(frozen=True)
class EngineExecutionPlan:
    schema_version: int
    engine_id: str
    workflow_digest: str
    input_fingerprint: str
    run_path: Path
    tasks: tuple[PlannedTask, ...]
    warnings: tuple[str, ...] = ()
    requested_resources: Mapping[str, object] = field(default_factory=dict)
    backend_constraints: Mapping[str, object] = field(default_factory=dict)
    layout_expectation: str = 'reconstructed'
    publication_policy: str = 'validate_then_publish'

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ContractError('execution plan schema_version must be positive')
        _require_token(self.engine_id, 'engine_id')
        object.__setattr__(self, 'run_path', Path(self.run_path).resolve())
        object.__setattr__(
            self, 'requested_resources',
            MappingProxyType(dict(self.requested_resources)))
        object.__setattr__(
            self, 'backend_constraints',
            MappingProxyType(dict(self.backend_constraints)))
        ids = [item.task_id for item in self.tasks]
        if len(ids) != len(set(ids)):
            raise ContractError('execution plan has duplicate task IDs')
        known: set[str] = set()
        for task in self.tasks:
            missing = set(task.depends_on) - known
            if missing:
                raise ContractError(
                    f'planned task {task.task_id!r} is ordered before dependencies {sorted(missing)}')
            known.add(task.task_id)

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'engine_id': self.engine_id,
            'workflow_digest': self.workflow_digest,
            'input_fingerprint': self.input_fingerprint,
            'run_path': str(self.run_path),
            'tasks': [task.to_dict() for task in self.tasks],
            'warnings': list(self.warnings),
            'requested_resources': dict(self.requested_resources),
            'backend_constraints': dict(self.backend_constraints),
            'layout_expectation': self.layout_expectation,
            'publication_policy': self.publication_policy,
        }

    @property
    def digest(self) -> str:
        # The run directory is an allocation detail, not meshing intent.
        # Excluding it makes repeated runs of the same accepted revision
        # demonstrably reproducible while each immutable run still records its
        # own absolute run_path in ``to_dict()``.
        document = self.to_dict()
        document.pop('run_path', None)
        payload = json.dumps(
            document, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def fingerprint_plan_inputs(*payloads: object) -> str:
    """Return a deterministic SHA-256 over JSON-compatible plan inputs."""
    encoded = json.dumps(
        payloads, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        default=_json_default).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    raise TypeError(f'cannot serialize {type(value).__name__}')


def _require_token(value: str, name: str) -> None:
    token = str(value)
    if not token or token.strip() != token:
        raise ContractError(f'{name} must be a non-empty trimmed token')
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')
    if any(char not in allowed for char in token):
        raise ContractError(f'{name} contains unsupported characters: {token!r}')


def _require_field_id(value: str) -> None:
    token = str(value)
    if not token or token.strip() != token:
        raise ContractError('field_id must be a non-empty trimmed token')
    normalized = token.replace('/{id}/', '.').replace('{id}', 'id')
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/')
    if any(char not in allowed for char in normalized):
        raise ContractError(f'field_id contains unsupported characters: {token!r}')
    if '..' in token or token.startswith('/') or token.endswith('/'):
        raise ContractError(f'field_id has invalid structure: {token!r}')


def _topological_order(tasks: Mapping[str, WorkflowTask]) -> tuple[str, ...]:
    remaining = {key: set(task.depends_on) for key, task in tasks.items()}
    result: list[str] = []
    while remaining:
        ready = sorted(
            (key for key, dependencies in remaining.items() if not dependencies),
            key=lambda key: tasks[key].order)
        if not ready:
            raise ContractError(
                f'workflow dependency cycle: {sorted(remaining)}')
        for key in ready:
            result.append(key)
            remaining.pop(key)
            for dependencies in remaining.values():
                dependencies.discard(key)
    return tuple(result)


def validate_field_budget(descriptor: WorkflowDescriptor, maximum: int) -> int:
    if maximum < 0:
        raise ValueError('maximum field budget cannot be negative')
    count = len(descriptor.editable_fields())
    if count > maximum:
        raise ContractError(
            f'{descriptor.engine_id} publishes {count} editable fields; budget is {maximum}')
    return count


def merge_capabilities(tasks: Iterable[WorkflowTask]) -> tuple[CapabilityRequirement, ...]:
    merged: dict[str, CapabilityRequirement] = {}
    for task in tasks:
        for requirement in task.capabilities:
            prior = merged.get(requirement.capability)
            if prior is None or requirement.required and not prior.required:
                merged[requirement.capability] = requirement
    return tuple(merged[key] for key in sorted(merged))
