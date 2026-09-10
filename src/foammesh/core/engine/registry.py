"""Meshing-engine registry and project-state resolution."""
from __future__ import annotations

from foammesh.db.configurations_schema import MeshEngine, TargetSolver

from .base import MeshingEngine
from .gmsh import GmshMeshingEngine
from .snappy import SnappyMeshingEngine


class EngineNotRegisteredError(LookupError):
    pass


class EngineRegistry:
    def __init__(self, engines=()):
        self._engines: dict[str, MeshingEngine] = {}
        for engine in engines:
            self.register(engine)

    def register(self, engine: MeshingEngine) -> None:
        engine_id = str(engine.engine_id)
        if engine_id in self._engines:
            raise ValueError(f'duplicate meshing engine: {engine_id}')
        self._engines[engine_id] = engine

    def get(self, engine_id: str) -> MeshingEngine:
        try:
            return self._engines[engine_id]
        except KeyError as error:
            raise EngineNotRegisteredError(
                f'meshing engine is not registered: {engine_id}') from error

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._engines))


ENGINE_REGISTRY = EngineRegistry((
    SnappyMeshingEngine(),
    GmshMeshingEngine(),
))


def configured_engine_id(db) -> str:
    try:
        value = db.getValue('mesh/engine')
    except Exception:
        return MeshEngine.UNSELECTED.value
    if isinstance(value, MeshEngine):
        return value.value
    token = str(value or MeshEngine.UNSELECTED.value)
    if token in MeshEngine.__members__:
        return MeshEngine[token].value
    return token.lower()


def resolve_engine(db, *, registry: EngineRegistry = ENGINE_REGISTRY) -> MeshingEngine:
    return registry.get(configured_engine_id(db))


# --------------------------------------------------------------------------- #
# Which engine can serve which solver (Plan 28)
# --------------------------------------------------------------------------- #

# What each solver's mesh reader accepts. `None` means "everything": OpenFOAM
# is a polyhedral code and has no restriction to express.
#
# SU2's reader knows tetrahedra, hexahedra, prisms and pyramids and nothing
# else, which is why snappyHexMesh -- whose whole refinement strategy produces
# polyhedral cells at every level transition -- cannot feed it, and why Gmsh
# exists in this application.
SOLVER_CELL_FAMILIES = {
    TargetSolver.OPENFOAM.value: None,
    TargetSolver.SU2.value: frozenset({
        'tetrahedron', 'hexahedron', 'prism', 'pyramid'}),
}

_SOLVER_DISPLAY_NAMES = {
    TargetSolver.OPENFOAM.value: 'OpenFOAM',
    TargetSolver.SU2.value: 'SU2',
}

# The reason string is read by a user deciding what to do about it, so the
# families are named in English rather than as the internal tokens: an
# adjective for what an engine emits, a plural noun for what a solver reads.
_FAMILY_ADJECTIVE = {
    'polyhedron': 'polyhedral', 'tetrahedron': 'tetrahedral',
    'hexahedron': 'hexahedral', 'prism': 'prismatic', 'pyramid': 'pyramidal',
}
_FAMILY_PLURAL = {
    'polyhedron': 'polyhedra', 'tetrahedron': 'tetrahedra',
    'hexahedron': 'hexahedra', 'prism': 'prisms', 'pyramid': 'pyramids',
}
# Coarsest first, which is the order these are conventionally listed in.
_FAMILY_ORDER = ('tetrahedron', 'hexahedron', 'prism', 'pyramid', 'polyhedron')


def _ordered(families):
    known = [name for name in _FAMILY_ORDER if name in families]
    return known + sorted(set(families) - set(_FAMILY_ORDER))


def _english_list(words) -> str:
    words = list(words)
    if len(words) < 2:
        return ''.join(words)
    return ', '.join(words[:-1]) + ' and ' + words[-1]


def solver_display_name(solver) -> str:
    return _SOLVER_DISPLAY_NAMES.get(_solver_token(solver), 'the target solver')


def _solver_token(solver) -> str:
    if isinstance(solver, TargetSolver):
        return solver.value
    token = str(solver or TargetSolver.UNSELECTED.value)
    if token in TargetSolver.__members__:
        return TargetSolver[token].value
    return token.lower()


def configured_target_solver(db) -> str:
    """The solver this project is meshing for, or 'unselected'.

    Tolerant on purpose: a project written before Plan 28 has no such key, and
    reading one must never be the thing that stops a user opening their work.
    """
    try:
        value = db.getValue('mesh/targetSolver')
    except Exception:
        return TargetSolver.UNSELECTED.value
    return _solver_token(value)


def engine_incompatibility(engine_id, solver, *,
                           registry: 'EngineRegistry' = None) -> str:
    """Why this engine cannot serve this solver, or '' when it can.

    Derived from the engine's own declared `cell_families` rather than a
    hard-coded per-engine table, so an engine added later is judged by what it
    says it produces and needs no edit here.
    """
    registry = registry or ENGINE_REGISTRY
    accepted = SOLVER_CELL_FAMILIES.get(_solver_token(solver))
    if accepted is None:                    # unselected, or a permissive solver
        return ''
    try:
        engine = registry.get(str(engine_id))
    except EngineNotRegisteredError:
        return ''
    unreadable = _ordered(set(engine.descriptor.cell_families) - set(accepted))
    if not unreadable:
        return ''
    emits = _english_list(
        _FAMILY_ADJECTIVE.get(name, name) for name in unreadable)
    reads = _english_list(
        _FAMILY_PLURAL.get(name, name) for name in _ordered(accepted))
    return (f'{engine.descriptor.display_name} produces {emits} cells; '
            f'{solver_display_name(solver)} reads {reads} only')


def compatible_engines(solver, *,
                       registry: 'EngineRegistry' = None) -> tuple:
    registry = registry or ENGINE_REGISTRY
    return tuple(
        engine_id for engine_id in registry.ids()
        if not engine_incompatibility(engine_id, solver, registry=registry))
