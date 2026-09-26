"""Engine-neutral contracts for meshing configuration and stage execution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .contracts import (
    EngineDescriptor, EngineExecutionPlan, EnginePlanRequest, WorkflowDescriptor,
)


@dataclass(frozen=True)
class EngineProbe:
    engine_id: str
    available: bool
    capabilities: tuple[tuple[str, bool], ...]
    reason: str = ''
    profile_id: str | None = None
    runtime_fingerprint: str | None = None
    version: str | None = None
    algorithms: tuple[str, ...] = ()
    supported_element_types: tuple[str, ...] = ()
    self_test: str = 'not_run'
    failures: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {
            'engine_id': self.engine_id,
            'available': self.available,
            'capabilities': dict(self.capabilities),
            'reason': self.reason,
            'profile_id': self.profile_id,
            'runtime_fingerprint': self.runtime_fingerprint,
            'version': self.version,
            'algorithms': list(self.algorithms),
            'supported_element_types': list(self.supported_element_types),
            'self_test': self.self_test,
            'failures': list(self.failures),
        }


@dataclass(frozen=True)
class StageDefinition:
    stage: str
    utility: str
    dictionary: str
    snappy_flags: tuple[bool, bool, bool] | None = None
    task_id: str | None = None
    capabilities: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    mutation: bool = True
    timeout_seconds: int = 3600


@dataclass(frozen=True)
class StageRun:
    definition: StageDefinition
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str] | None = None
    expected_artifacts: tuple[Path, ...] = ()


@dataclass(frozen=True)
class EngineRunRequest:
    """Everything an engine needs to run a whole mesh, in one argument.

    Plan 30 F-03. The *orchestration* of a whole run -- the supervised
    executor, mutation recovery, the checkpoint and evidence writers -- belongs
    to the facade and stays there; what the run *consists of* belongs to the
    engine. So the engine is handed the orchestrator rather than a copy of its
    machinery, and the facade stops asking which engine it is talking to
    before it can start one.
    """

    session: object
    command: object
    orchestrator: object


@runtime_checkable
class MeshingEngine(Protocol):
    """Minimal seam used by workflow/facade call sites.

    Implementations generate engine configuration and prepare a stage run;
    process execution and mutation recovery remain owned by the facade's
    operation executor.
    """

    engine_id: str

    #: Whether planning is impossible without a current prepared geometry.
    #: Engines that consume CAD directly set this; engines that mesh from
    #: surfaces in the case do not.
    requires_prepared_geometry: bool = False

    #: The configuration section holding this engine's native state, or an
    #: empty string when the engine has none.
    native_section: str = ''

    #: The facade operation that re-runs this engine's work when a human has
    #: accepted a mesh its quality gate refused. An empty string -- the
    #: normal case -- means the engine has already published its mesh by the
    #: time anything judged it, so accepting re-runs only :func:`qa_task_id`'s
    #: check. Only an engine whose single atomic operation both meshes and
    #: gates (Gmsh) names one here.
    accept_quality_operation: str = ''

    #: Whether ``workflow.generate_dictionaries`` has to have run before this
    #: engine can mesh. snappy meshes from dictionaries on disk; Gmsh writes
    #: its own job at the moment it runs.
    needs_generated_dictionaries: bool = False

    #: The workflow task whose gate blocks this engine's later work, or an
    #: empty string for an engine that has no such gate.
    blocking_gate_task: str = ''

    #: Whether one case may hold solid models (STEP/IGES/BREP) beside
    #: triangulated surfaces (STL/OBJ). snappy meshes both from the surfaces
    #: it stages; the Gmsh job imports one kind or the other (DP-638).
    mixes_cad_and_surfaces: bool = True

    #: Whether a non-conformal (NCC) interface pair is built by this engine.
    #: snappy stages it for OpenFOAM's NCC; Gmsh has no such coupling
    #: (DP-641), so the Geometry page disables the choice for it.
    builds_non_conformal_interfaces: bool = True

    #: What one whole-mesh run of this engine performs, in order.
    ATOMIC_RUN_TASKS: tuple = ()

    @property
    def descriptor(self) -> EngineDescriptor: ...

    def workflow_descriptor(self) -> WorkflowDescriptor: ...

    def probe(self, capabilities=None, *, refresh: bool = False,
              target_solver: str = '') -> EngineProbe: ...

    def create_plan(self, request: EnginePlanRequest) -> EngineExecutionPlan: ...

    def validate(self, stage: str) -> StageDefinition: ...

    def generate_config(self, db, bbox, case_path, prepared_geometry=None): ...

    def run_stage(self, stage: str, *, db, case_path, executable: str) -> StageRun: ...

    def collect_quality(self, case_path): ...

    # -- whole-mesh services, which every engine owes (Plan 30 F-15) -------- #

    async def run(self, request: EngineRunRequest): ...

    def reset(self, case_path, target: str = '') -> dict: ...

    def snapshot(self, case_path, target: str = '') -> dict: ...

    def requested_cell_size(self, db, *, bounds=None, hex_bounds=None): ...


# --------------------------------------------------------------------------- #
# Which task -- and which operation -- judges a finished mesh
# --------------------------------------------------------------------------- #
#
# Plan 30 F-24. This rule had three copies: the facade's `_qa_operation_for`,
# the main window's `_qaOperationName` and the engine branch's
# `_qa_operation_name`, plus a fourth spelling of the task id as an f-string
# in `_record_qa_run`. Three surfaces deciding separately which check answers
# "is this mesh good?" is three chances to run the wrong one -- which is
# exactly what F-02 was: **Accept anyway** re-ran Gmsh on a snappy case.
# Every caller reads it from here now.

#: Every engine's workflow ends its meshing with one QA task, named
#: ``<engine>.qa`` (``snappy.qa``, ``gmsh.qa``). The suffix is a contract
#: between the workflow descriptors and everything that looks the task up.
QA_TASK_SUFFIX = 'qa'

#: checkMesh is OpenFOAM's opinion of an OpenFOAM mesh and needs an OpenFOAM
#: runtime to hold it.
OPENFOAM_QA_OPERATION = 'mesh.check'

#: Plan 28. A Gmsh user meshing for SU2 usually has no OpenFOAM at all, so
#: their mesh is judged by whether SU2 can read it.
SU2_QA_OPERATION = 'quality.su2_readiness'


def qa_task_id(engine) -> str:
    """The workflow task that judges *engine*'s finished mesh.

    Accepts an engine id or an engine object. Raises rather than guessing:
    a QA task id derived from nothing is how a waiver ends up bound to a task
    that does not exist.
    """
    token = str(getattr(engine, 'engine_id', engine) or '').strip().lower()
    if not token:
        raise ValueError('a QA task id needs an engine; none was given')
    return f'{token}.{QA_TASK_SUFFIX}'


def qa_operation(target_solver=None) -> str:
    """Which quality check answers "is this mesh good?" for this solver.

    Unknown, unset and unreadable all mean checkMesh, which is what every
    surface has always run and what an OpenFOAM project needs.
    """
    from foammesh.db.configurations_schema import TargetSolver

    token = str(getattr(target_solver, 'value', target_solver) or '').lower()
    if token == TargetSolver.SU2.value:
        return SU2_QA_OPERATION
    return OPENFOAM_QA_OPERATION


def accept_quality_route(engine, *, target_solver=None) -> str:
    """The operation that re-runs *engine*'s work under a recorded acceptance.

    Plan 30 F-02. **Accept anyway** used to call ``mesh.gmsh.run`` whatever
    the case held, so accepting a snappy mesh ran the wrong engine -- meshing
    a case with Gmsh because a user agreed to keep a snappyHexMesh result.
    The route is the engine's own (:attr:`MeshingEngine.accept_quality_operation`)
    and, for an engine that has already published by then, its QA check.
    """
    from .registry import ENGINE_REGISTRY, EngineNotRegisteredError

    engine_id = str(getattr(engine, 'engine_id', engine) or '').strip().lower()
    try:
        route = str(getattr(ENGINE_REGISTRY.get(engine_id),
                            'accept_quality_operation', '') or '')
    except (EngineNotRegisteredError, LookupError):
        # No engine selected, or one this build does not know. Re-running an
        # engine-specific pipeline on a guess is the F-02 defect; the QA check
        # judges whatever mesh is actually in the case.
        route = ''
    return route or qa_operation(target_solver)
