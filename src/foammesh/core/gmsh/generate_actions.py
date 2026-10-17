"""The Generate actions of the Gmsh route, as data (Plan 41 GN12).

Gmsh's Mesh module offers 1D, 2D, 3D, Optimize, Set order, Refine by
splitting, Recombine 2D, Partition, Smooth, Delete mesh, Renumber and
Remove duplicates, and each of them acts on the model that is already open.
This module is the table of those actions for the resident session
(``core/gmsh/session.py``): what each one is called, what it needs, which
Gmsh options belong to it, and the exact session operations it expands to.

Nothing here talks to Gmsh.  :func:`plan` turns ``(action, values)`` into a
:class:`Plan` -- an ordered list of session operations -- or raises
:class:`ActionRefusal` with the reason.  :func:`run` plays a plan on a
session and records one :class:`Stage` per action, which is the timeline.

A value the caller does not pass is never sent: Gmsh keeps what the project
(or the Options panel) set, so an action does not overwrite an option the
user typed elsewhere.  Option values are checked against the option
catalogue first (kind, enum, build), so Gmsh never silently rewrites one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable

from . import option_catalogue as catalogue

#: Optimiser methods of ``gmsh.model.mesh.optimize`` in 4.15.2, with the
#: dimension each one works on.  ``''`` is the tetrahedral optimiser of the
#: Optimize 3D button, ``Netgen`` the Optimize 3D (Netgen) button.
OPTIMIZE_METHODS = (
    ('', 'Optimize 3D (Gmsh)', 3),
    ('Netgen', 'Optimize 3D (Netgen)', 3),
    ('HighOrder', 'High-order optimiser', 3),
    ('HighOrderElastic', 'High-order elastic smoother', 3),
    ('HighOrderFastCurving', 'Fast curving', 3),
    ('Laplace2D', 'Laplace smoothing (2D)', 2),
    ('Relocate2D', 'Node relocation (2D)', 2),
    ('Relocate3D', 'Node relocation (3D)', 3),
    ('QuadQuasiStructured', 'Quad mesh optimisation', 2),
    ('UntangleMeshGeometry', 'Untangle mesh geometry', 3),
)

#: ``Mesh.SubdivisionAlgorithm``: 0 none, 1 all quads, 2 all hexes,
#: 3 barycentric (Gmsh 4.15.2).  All four are offered; nothing is clamped.
SUBDIVISION = ((0, 'None'), (1, 'All quads'), (2, 'All hexahedra'),
               (3, 'Barycentric'))

# DP-1471. One list: the export-time renumber reads the same one.
from .plan_derivation import RENUMBER_METHOD as _RENUMBER_METHOD
RENUMBER_METHODS = tuple(_RENUMBER_METHOD.values())

#: Every option of Set order (A-05, O-06).
HIGH_ORDER_OPTIONS = (
    'Mesh.SecondOrderLinear', 'Mesh.SecondOrderIncomplete',
    'Mesh.HighOrderOptimize', 'Mesh.HighOrderNumLayers',
    'Mesh.HighOrderPassMax', 'Mesh.HighOrderThresholdMin',
    'Mesh.HighOrderThresholdMax', 'Mesh.HighOrderDistCAD',
    'Mesh.HighOrderIterMax', 'Mesh.HighOrderPeriodic',
    'Mesh.HighOrderPoissonRatio', 'Mesh.HighOrderPrimSurfMesh',
    'Mesh.HighOrderMaxInnerIter', 'Mesh.HighOrderCurveOuterBL',
    'Mesh.HighOrderSavePeriodic', 'Mesh.HighOrderFastCurvingNewAlgo',
    'Mesh.HighOrderFixBoundaryNodes',
)

#: Every option of Recombine 2D / 3D (A-07).
RECOMBINE_OPTIONS = (
    'Mesh.RecombineAll', 'Mesh.RecombinationAlgorithm',
    'Mesh.RecombineOptimizeTopology', 'Mesh.RecombineNodeRepositioning',
    'Mesh.RecombineMinimumQuality', 'Mesh.Recombine3DAll',
    'Mesh.Recombine3DLevel', 'Mesh.Recombine3DConformity',
)

#: Every option of Partition (A-10).
PARTITION_OPTIONS = (
    'Mesh.PartitionCreateTopology', 'Mesh.PartitionCreatePhysicals',
    'Mesh.PartitionCreateGhostCells', 'Mesh.PartitionSplitMeshFiles',
    'Mesh.PartitionOldStyleMsh2', 'Mesh.PartitionConvertMsh2',
    'Mesh.PartitionTopologyFile', 'Mesh.PartitionLineWeight',
    'Mesh.PartitionTriWeight', 'Mesh.PartitionQuadWeight',
    'Mesh.PartitionTetWeight', 'Mesh.PartitionPyramidWeight',
    'Mesh.PartitionPrismWeight', 'Mesh.PartitionHexWeight',
    'Mesh.PartitionTrihedronWeight', 'Mesh.MetisAlgorithm',
    'Mesh.MetisEdgeMatching', 'Mesh.MetisRefinementAlgorithm',
    'Mesh.MetisObjective', 'Mesh.MetisMinConn', 'Mesh.MetisMaxLoadImbalance',
)

REFINE_OPTIONS = ('Mesh.SecondOrderLinear', 'Mesh.SubdivisionAlgorithm',
                  'Mesh.RefineSteps')
OPTIMIZE_OPTIONS = ('Mesh.Optimize', 'Mesh.OptimizeThreshold',
                    'Mesh.OptimizeNetgen', 'Mesh.Smoothing')
SMOOTH_OPTIONS = ('Mesh.Smoothing', 'Mesh.SmoothRatio', 'Mesh.SmoothNormals',
                  'Mesh.SmoothCrossField')
SCOPE_OPTIONS = ('Mesh.MeshOnlyVisible', 'Mesh.MeshOnlyEmpty')


class ActionRefusal(ValueError):
    """An action Gmsh (or the model's state) cannot do, with the reason."""


@dataclass(frozen=True)
class Op:
    """One session operation: ``session.call(name, *args, **kwargs)``."""

    name: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    timeout: float | None = None

    def as_request(self) -> dict:
        return {'op': self.name, 'args': list(self.args),
                'kwargs': dict(self.kwargs)}


@dataclass(frozen=True)
class Plan:
    action: str
    ops: tuple
    notes: tuple = ()


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    group: str                       #: Gmsh's own button group
    help: str
    needs: int                       #: lowest meshed dimension required, -1 none
    options: tuple = ()              #: option names this action accepts
    arguments: tuple = ()            #: non-option argument names
    expand: Callable | None = None


@dataclass
class Stage:
    """One row of the stage timeline."""

    action: str
    title: str
    seconds: float = 0.0
    ok: bool = True
    error: str = ''
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    notes: tuple = ()

    def as_dict(self) -> dict:
        return {'action': self.action, 'title': self.title,
                'seconds': round(self.seconds, 3), 'ok': self.ok,
                'error': self.error, 'before': self.before,
                'after': self.after, 'notes': list(self.notes)}


# --------------------------------------------------------------------------- #
# Values.

def _option_ops(names, values: dict, allowed: tuple) -> list:
    """``option.set*`` operations for the options present in *values*."""
    ops = []
    for name in names:
        if name not in values or values[name] is None:
            continue
        if name not in allowed:
            raise ActionRefusal(f'{name} is not an option of this action')
        try:
            parsed = catalogue.parse_value(name, values[name])
        except catalogue.OptionRefusal as error:
            raise ActionRefusal(str(error)) from None
        kind = ('option.setString' if isinstance(parsed, str)
                else 'option.setNumber')
        if isinstance(parsed, (list, tuple)):
            raise ActionRefusal(f'{name} is not a number or a string')
        ops.append(Op(kind, (name, parsed)))
    return ops


def _check_unknown(action: Action, values: dict) -> None:
    known = set(action.options) | set(action.arguments) | set(SCOPE_OPTIONS)
    unknown = sorted(set(values) - known)
    if unknown:
        raise ActionRefusal(
            f'{action.title}: {", ".join(unknown)} is not a control of this '
            f'action (it takes: {", ".join(sorted(known)) or "nothing"})')


def dim_tags(text) -> list:
    """``"2:3, 3:1"`` or ``[(2, 3)]`` as a list of ``[dim, tag]`` pairs."""
    if text in (None, '', [], ()):
        return []
    if isinstance(text, str):
        pairs = []
        for item in text.replace(';', ',').split(','):
            item = item.strip()
            if not item:
                continue
            try:
                dim, tag = item.split(':')
                pairs.append([int(dim), int(tag)])
            except ValueError:
                raise ActionRefusal(
                    f'"{item}" is not an entity; write dimension:tag, for '
                    'example 2:5') from None
    else:
        pairs = [[int(a), int(b)] for a, b in text]
    for dim, tag in pairs:
        if dim not in (0, 1, 2, 3) or tag <= 0:
            raise ActionRefusal(f'{dim}:{tag} is not a Gmsh entity')
    return pairs


def _int(values: dict, key: str, default, low, high) -> int:
    raw = values.get(key, default)
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise ActionRefusal(f'{key} must be a whole number, not {raw!r}'
                            ) from None
    if not low <= number <= high:
        raise ActionRefusal(f'{key} must be between {low} and {high}')
    return number


# --------------------------------------------------------------------------- #
# Expanders: values -> operations.

def _generate(dim: int):
    def expand(action, values):
        entities = dim_tags(values.get('entities'))
        ops = _option_ops(SCOPE_OPTIONS, values, SCOPE_OPTIONS)
        if entities:
            raise ActionRefusal(
                'Generate meshes the whole model (or what is visible or '
                'empty); to redo some entities use "Regenerate affected"')
        ops.append(Op('mesh.generate', (dim,)))
        return ops, ()
    return expand


def _regenerate(action, values):
    entities = dim_tags(values.get('entities'))
    if not entities:
        raise ActionRefusal(
            'Regenerate affected needs the entities to redo, written '
            'dimension:tag (for example 2:5)')
    dim = _int(values, 'dim', 3, 1, 3)
    if dim < max(d for d, _ in entities):
        raise ActionRefusal(
            f'the target dimension {dim} is lower than the entities to redo')
    return [Op('session.regenerate', (entities, dim))], (
        'what depends on the entities (their volumes) is cleared and '
        'regenerated; entities that already have a mesh are left. Gmsh '
        'meshes only the empty entities when each dimension is generated '
        'in turn, so the session runs 1D, 2D, 3D up to the target',)


def _optimize(action, values):
    method = values.get('method', '')
    known = {item[0] for item in OPTIMIZE_METHODS}
    if method not in known:
        raise ActionRefusal(
            f'"{method}" is not an optimiser of this Gmsh; the methods are: '
            + ', '.join(repr(m) for m in sorted(known)))
    ops = _option_ops(OPTIMIZE_OPTIONS, values, OPTIMIZE_OPTIONS)
    iterations = _int(values, 'niter', 1, 1, 1000)
    tags = dim_tags(values.get('entities'))
    ops.append(Op('model.mesh.optimize', (method,), {
        'force': bool(values.get('force', False)), 'niter': iterations,
        'dimTags': tags}))
    return ops, ()


def _set_order(action, values):
    order = _int(values, 'order', 2, 1, 3)
    return (_option_ops(HIGH_ORDER_OPTIONS, values, HIGH_ORDER_OPTIONS)
            + [Op('model.mesh.setOrder', (order,))]), ()


def _refine(action, values):
    steps = _int(values, 'steps', 1, 1, 10)
    ops = _option_ops(REFINE_OPTIONS, values, REFINE_OPTIONS)
    ops += [Op('model.mesh.refine') for _ in range(steps)]
    return ops, ('refining resets high-order elements to order 1',)


def _recombine(action, values):
    return (_option_ops(RECOMBINE_OPTIONS, values, RECOMBINE_OPTIONS)
            + [Op('model.mesh.recombine')]), ()


def _subdivide(action, values, state=None):
    algorithm = _int(values, 'Mesh.SubdivisionAlgorithm', 1, 0, 3)
    if algorithm == 0:
        raise ActionRefusal('Subdivision algorithm 0 is "none"; choose all '
                            'quads, all hexahedra or barycentric')
    if (algorithm == 3 and state is not None
            and meshed_dimension(state) < 3):
        # MEASURED on Gmsh 4.15.2 (elbow, 1836 triangles): refine with
        # SubdivisionAlgorithm 3 leaves a triangle mesh exactly as it was.
        raise ActionRefusal(
            'barycentric subdivision splits tetrahedra and the model has no '
            'volume mesh (Gmsh leaves a surface mesh unchanged); run 3D '
            'first, or choose all quads')
    ops = _option_ops(
        ('Mesh.SecondOrderLinear',), values, ('Mesh.SecondOrderLinear',))
    ops.append(Op('option.setNumber', ('Mesh.SubdivisionAlgorithm',
                                       float(algorithm))))
    ops.append(Op('model.mesh.refine'))
    return ops, ('Gmsh applies the subdivision algorithm as it splits '
                 'the elements once',)


def _partition(action, values):
    parts = _int(values, 'parts', 2, 1, 1_000_000)
    ops = _option_ops(PARTITION_OPTIONS, values, PARTITION_OPTIONS)
    ops.append(Op('option.setNumber', ('Mesh.NbPartitions', float(parts))))
    ops.append(Op('model.mesh.partition', (parts,)))
    return ops, ()


def _unpartition(action, values):
    return [Op('model.mesh.unpartition')], ()


def _smooth(action, values):
    ops = _option_ops(SMOOTH_OPTIONS, values, SMOOTH_OPTIONS)
    ops.append(Op('model.mesh.optimize', ('Laplace2D',), {
        'force': False, 'niter': _int(values, 'niter', 1, 1, 1000),
        'dimTags': dim_tags(values.get('entities'))}))
    return ops, ()


def _delete(action, values):
    entities = dim_tags(values.get('entities'))
    return [Op('model.mesh.clear', (entities,))], (
        ('a mesh can only be deleted from an entity that is not on the '
         'boundary of one that still has a mesh',) if entities else ())


def _renumber(action, values):
    method = values.get('method', 'RCMK')
    if method not in RENUMBER_METHODS:
        raise ActionRefusal(
            f'"{method}" is not a renumbering method; Gmsh 4.15.2 has '
            + ', '.join(RENUMBER_METHODS))
    return [Op('session.renumber', (method,),
               {'elements': bool(values.get('elements', True))})], ()


def _remove_duplicates(action, values):
    entities = dim_tags(values.get('entities'))
    ops = []
    if values.get('nodes', True):
        ops.append(Op('model.mesh.removeDuplicateNodes', (entities,)))
    if values.get('elements', True):
        ops.append(Op('model.mesh.removeDuplicateElements', (entities,)))
    if not ops:
        raise ActionRefusal('choose nodes, elements or both')
    return ops, ()


def _reclassify(action, values):
    angle = float(values.get('angle', 40.0))
    if not 0.0 < angle <= 180.0:
        raise ActionRefusal('the classification angle is in degrees, '
                            'greater than 0 and at most 180')
    import math
    curve = float(values.get('curve_angle', 180.0))
    return [Op('model.mesh.classifySurfaces', (math.radians(angle),), {
        'boundary': bool(values.get('boundary', True)),
        'forReparametrization': bool(values.get('reparametrization', False)),
        'curveAngle': math.radians(curve),
        'exportDiscrete': bool(values.get('export_discrete', True))}),
        Op('model.mesh.createGeometry')], (
        'this makes discrete surfaces and curves from the surface mesh',)


def _only(option: str, flag_name: str):
    def expand(action, values):
        ops = [Op('option.setNumber', (option, 1.0 if values.get(
            flag_name, True) else 0.0))]
        return ops, ()
    return expand


def _visibility(action, values):
    entities = dim_tags(values.get('entities'))
    if not entities:
        raise ActionRefusal('give the entities to show or hide, written '
                            'dimension:tag')
    visible = 1 if values.get('visible', True) else 0
    return [Op('model.setVisibility', (entities, visible),
               {'recursive': bool(values.get('recursive', True))})], ()


ACTIONS: dict = {}


def _register(*actions: Action) -> None:
    for action in actions:
        ACTIONS[action.id] = action


_register(
    Action('generate1d', '1D', 'Mesh', 'Mesh the curves.', -1,
           expand=_generate(1)),
    Action('generate2d', '2D', 'Mesh', 'Mesh the surfaces.', -1,
           expand=_generate(2)),
    Action('generate3d', '3D', 'Mesh', 'Mesh the volumes.', -1,
           expand=_generate(3)),
    Action('generate_all', 'All', 'Mesh', '1D, 2D and 3D in one go.', -1,
           expand=_generate(3)),
    Action('regenerate', 'Regenerate affected', 'Mesh',
           'Redo only the entities you name and what depends on them.', 1,
           arguments=('entities', 'dim'), expand=_regenerate),
    Action('optimize', 'Optimize', 'Mesh',
           'Run one of Gmsh\'s mesh optimisers on the current mesh.', 2,
           OPTIMIZE_OPTIONS, ('method', 'niter', 'force', 'entities'),
           _optimize),
    Action('set_order', 'Set order', 'Mesh',
           'Change the element order (1, 2 or 3) with the high-order '
           'options.', 1, HIGH_ORDER_OPTIONS, ('order',), _set_order),
    Action('refine', 'Refine by splitting', 'Mesh',
           'Split every element, as many times as asked.', 1,
           REFINE_OPTIONS, ('steps',), _refine),
    Action('recombine', 'Recombine 2D', 'Mesh',
           'Turn triangles into quadrangles (and, in 3D, tetrahedra into '
           'hexahedra where Gmsh can).', 2, RECOMBINE_OPTIONS, (),
           _recombine),
    Action('subdivide', 'Subdivide', 'Mesh',
           'Split with a subdivision algorithm: all quads, all hexahedra '
           'or barycentric.', 2,
           ('Mesh.SecondOrderLinear', 'Mesh.SubdivisionAlgorithm'), (),
           _subdivide),
    Action('partition', 'Partition', 'Mesh',
           'Split the mesh into partitions with Metis.', 1,
           PARTITION_OPTIONS, ('parts',), _partition),
    Action('unpartition', 'Unpartition', 'Mesh',
           'Undo the partitioning.', 1, (), (), _unpartition),
    Action('smooth', 'Smooth 2D', 'Mesh',
           'Laplacian smoothing of the surface mesh.', 2, SMOOTH_OPTIONS,
           ('niter', 'entities'), _smooth),
    Action('delete', 'Delete mesh', 'Mesh',
           'Delete the whole mesh, or the mesh of the entities named.', 0,
           (), ('entities',), _delete),
    Action('renumber', 'Renumber', 'Mesh',
           'Renumber nodes (and elements) with RCMK, Hilbert or Metis.', 1,
           (), ('method', 'elements'), _renumber),
    Action('remove_duplicates', 'Remove duplicates', 'Mesh',
           'Remove duplicate nodes and elements.', 1, (),
           ('nodes', 'elements', 'entities'), _remove_duplicates),
    Action('reclassify', 'Reclassify 2D', 'Mesh',
           'Re-colour the surface mesh by an angle threshold and create the '
           'discrete entities.', 2, (),
           ('angle', 'curve_angle', 'boundary', 'reparametrization',
            'export_discrete'), _reclassify),
    Action('only_visible', 'Mesh only visible', 'Mesh',
           'Make the next generate act on visible entities only.', -1, (),
           ('visible',), _only('Mesh.MeshOnlyVisible', 'visible')),
    Action('only_empty', 'Mesh only empty', 'Mesh',
           'Make the next generate act on entities with no mesh yet.', -1,
           (), ('empty',), _only('Mesh.MeshOnlyEmpty', 'empty')),
    Action('visibility', 'Show or hide entities', 'Mesh',
           'Set which entities are visible (what "mesh only visible" '
           'meshes).', -1, (), ('entities', 'visible', 'recursive'),
           _visibility),
)


def action(action_id: str) -> Action:
    try:
        return ACTIONS[action_id]
    except KeyError:
        raise ActionRefusal(
            f'"{action_id}" is not a Generate action; they are: '
            + ', '.join(ACTIONS)) from None


def plan(action_id: str, values: dict | None = None,
         state: dict | None = None) -> Plan:
    """The session operations for *action_id* with *values*, or a refusal.

    *state* is a ``session.state`` answer; the few actions whose outcome
    depends on what is meshed (barycentric subdivision) read it.
    """
    chosen = action(action_id)
    values = dict(values or {})
    _check_unknown(chosen, values)
    if chosen.id == 'subdivide':
        ops, notes = _subdivide(chosen, values, state)
    else:
        ops, notes = chosen.expand(chosen, values)
    return Plan(chosen.id, tuple(ops), tuple(notes))


# --------------------------------------------------------------------------- #
# Playing a plan on a session; the stage timeline.

def meshed_dimension(state: dict) -> int:
    """Highest dimension with elements in a ``session.state`` answer."""
    dims = state.get('dims_meshed') or []
    return max(dims) if dims else 0


def refusal_for_state(chosen: Action, state: dict) -> str:
    """Why the model cannot take this action right now ('' when it can)."""
    if chosen.needs < 0:
        return ''
    highest = meshed_dimension(state)
    nodes = int(state.get('nodes') or 0)
    if chosen.needs == 0:
        return ''
    if not nodes:
        return (f'{chosen.title} needs a mesh; run 1D, 2D or 3D first')
    if highest < chosen.needs and chosen.id not in ('set_order',):
        return (f'{chosen.title} needs a {chosen.needs}D mesh and the model '
                f'has {highest}D; run {chosen.needs}D first')
    return ''


class Timeline:
    """The stages run so far on one session."""

    def __init__(self) -> None:
        self.stages: list[Stage] = []

    def add(self, stage: Stage) -> None:
        self.stages.append(stage)

    def as_list(self) -> list:
        return [stage.as_dict() for stage in self.stages]


def run(session, action_id: str, values: dict | None = None, *,
        timeline: Timeline | None = None, measure: bool = True,
        timeout: float | None = None) -> Stage:
    """Play an action on *session*; record and return its stage."""
    chosen = action(action_id)
    stage = Stage(chosen.id, chosen.title)
    try:
        before = session.state()
        planned = plan(action_id, values, before)
        stage.notes = planned.notes
        stage.before = _summary(before)
        reason = refusal_for_state(chosen, before)
        if reason:
            raise ActionRefusal(reason)
        started = time.monotonic()
        for operation in planned.ops:
            call_timeout = operation.timeout or timeout
            session.call(operation.name, *operation.args,
                         timeout=call_timeout, **operation.kwargs)
        stage.seconds = time.monotonic() - started
        stage.after = _summary(session.state())
        if measure:
            stage.after['quality'] = session.call('session.measure')
    except ActionRefusal as error:
        stage.ok, stage.error = False, str(error)
    except Exception as error:  # noqa: BLE001 - the stage says what failed
        stage.ok, stage.error = False, str(error)
        raise
    finally:
        if timeline is not None:
            timeline.add(stage)
    return stage


def _summary(state: dict) -> dict:
    return {'nodes': state.get('nodes', 0),
            'elements': dict(state.get('elements') or {}),
            'dims_meshed': list(state.get('dims_meshed') or [])}
