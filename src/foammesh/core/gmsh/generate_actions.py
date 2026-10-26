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


# --------------------------------------------------------------------------- #
# Plan 48 TL: one time limit for the calls that can hang on Gmsh 4.15.2.

#: The start of every refusal that is Gmsh 4.15.2's own limit, not ours.
LIMIT_TAG = 'Gmsh 4.15.2 limit'

#: Seconds each kind of call may take before the session ends it. ``0``
#: means no limit. The user changes them per call, or for the project
#: (``GenerateController.set_time_limit``, ``gmsh.time_limits.set``).
DEFAULT_LIMITS = {
    'plugin': 300.0,        # CurvedBndDist never returns (DP-4309)
    'cross_field': 600.0,   # computeCrossField (DP-4255)
    'topology': 300.0,      # createTopology (DP-4256)
    'reclassify': 300.0,    # Reclassify 2D, whole model or subset
    'recombine3d': 900.0,   # Recombine3D hex-dominant meshing (DP-4217)
}

#: What each kind is called in the refusal.
LIMIT_WHAT = {
    'plugin': 'the plugin',
    'cross_field': 'the cross field (Compute quad layout)',
    'topology': 'createTopology',
    'reclassify': 'Reclassify 2D',
    'recombine3d': 'Recombine3D (hex-dominant meshing)',
}

#: The session operation that each kind guards (host-side limit kinds).
OP_LIMIT_KIND = {
    'session.plugin_run': 'plugin', 'session.plugin_edit': 'plugin',
    'session.quad_layout': 'cross_field',
    'session.create_topology': 'topology',
    'session.reclassify': 'reclassify',
    'session.reclassify_subset': 'reclassify',
    'model.mesh.classifySurfaces': 'reclassify',
}

#: The Generate actions that run one of those calls, and the kind each uses.
ACTION_LIMIT_KIND = {'reclassify_subset': 'reclassify',
                     'reclassify': 'reclassify',
                     'quad_layout': 'cross_field'}


class TimeLimitRefusal(ActionRefusal):
    """A call that did not return in its time limit; the session was
    restarted from its log, so it is alive and the model is as it was."""

    def __init__(self, what: str, seconds: float, restarted: str = ''):
        self.what = what
        self.seconds = float(seconds)
        super().__init__(limit_text(what, seconds, restarted))


def limit_text(what: str, seconds: float, restarted: str = '',
               untouched: bool = False) -> str:
    """The refusal text: ``Gmsh 4.15.2 limit: <what> did not return in <n> s``.

    *untouched*: the work ran in a child process, which was stopped; the
    session was never involved.
    """
    text = (f'{LIMIT_TAG}: {what} did not return in {float(seconds):g} s. '
            'This is a limit of Gmsh 4.15.2, not of the model: the call was '
            'stopped and ')
    if untouched:
        return text + ('the session was not affected. Raise the time limit '
                       'to wait longer')
    if restarted:
        return text + ('the session could not be restarted (' + restarted
                       + '); recover it before the next action')
    return text + ('the session was restarted from its log with the model '
                   'as it was. Raise the time limit to wait longer')


def limit_for(kind: str, limits: dict | None = None) -> float:
    """Seconds for *kind*: the project's value, else the default (0 = none)."""
    if limits and kind in limits and limits[kind] is not None:
        return float(limits[kind])
    return float(DEFAULT_LIMITS[kind])


def is_time_limit(error: BaseException) -> bool:
    """True for a session that ended because a call passed its time limit."""
    from .session import EXIT_TIMEOUT, GmshSessionCrashed

    return (isinstance(error, GmshSessionCrashed)
            and (error.returncode == EXIT_TIMEOUT
                 or 'time limit' in str(error.reason)))


def restart_after_limit(session) -> str:
    """Restart *session* from its log; '' when it worked, else why not."""
    rebuild = getattr(session, 'rebuild', None)
    if rebuild is None:
        return ''
    try:
        rebuild()
    except Exception as error:                         # noqa: BLE001
        return str(error)
    return ''


def call_with_limit(session, what: str, seconds, op: str, *args, **kwargs):
    """``session.call(op, ...)`` under *seconds*; a call that passes it
    raises :class:`TimeLimitRefusal` and the session is alive again."""
    seconds = float(seconds or 0)
    try:
        return session.call(op, *args, timeout=seconds or None, **kwargs)
    except Exception as error:                         # noqa: BLE001
        if seconds and is_time_limit(error):
            raise TimeLimitRefusal(
                what, seconds, restart_after_limit(session)) from None
        raise


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
    results: tuple = ()          #: what the session returned, per operation

    def as_dict(self) -> dict:
        row = {'action': self.action, 'title': self.title,
               'seconds': round(self.seconds, 3), 'ok': self.ok,
               'error': self.error, 'before': self.before,
               'after': self.after, 'notes': list(self.notes)}
        if self.results:
            row['results'] = list(self.results)
        return row


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


#: Gmsh's High-order tools window takes element orders 1 to 10.
ORDER_RANGE = (1, 10)


def _set_order(action, values):
    order = _int(values, 'order', 2, *ORDER_RANGE)
    notes = ()
    if order > 5:
        notes = ('the cost grows steeply with the order: a 0.7-sized box '
                 'has 203,651 nodes at order 10 and takes about 34 s',)
    return (_option_ops(HIGH_ORDER_OPTIONS, values, HIGH_ORDER_OPTIONS)
            + [Op('model.mesh.setOrder', (order,))]), notes


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


# -- Plan 48 GM7: the High-order tools, Reverse, Delete, Reclassify subset,
# -- the Experimental items.

#: The native High-order tools window (``highOrderToolsWindow.cpp``): which of
#: its fields each algorithm leaves active (``chooseopti_cb``) and the range
#: of each field.  The six patch fields of the window (strategy, weight,
#: distance factor and the three adaptation values) are options only in
#: FoamMesh's own Gmsh build (Plan 48 G15 patch, DP-5707); on the stock wheel
#: the session refuses them with the reason.
HIGH_ORDER_PATCH_OPTIONS = (
    'Mesh.HighOrderStrategy', 'Mesh.HighOrderWeight',
    'Mesh.HighOrderDistanceFactor', 'Mesh.HighOrderMaxAdaptBlob',
    'Mesh.HighOrderAdaptBlobLayerFactor', 'Mesh.HighOrderAdaptBlobDistFactor')
REGULARIZE_ALGORITHMS = (
    ('optimization', 'Optimization', 'HighOrder', (
        'Mesh.HighOrderThresholdMin', 'Mesh.HighOrderThresholdMax',
        'Mesh.HighOrderNumLayers', 'Mesh.HighOrderFixBoundaryNodes',
        'Mesh.HighOrderIterMax', 'Mesh.HighOrderPassMax',
        'Mesh.HighOrderPrimSurfMesh', 'Mesh.HighOrderDistCAD')
        + HIGH_ORDER_PATCH_OPTIONS),
    ('elastic', 'Elastic analogy', 'HighOrderElastic', ()),
    ('fast_curving', 'Fast curving', 'HighOrderFastCurving', (
        'Mesh.HighOrderNumLayers', 'Mesh.HighOrderCurveOuterBL')),
    # DP-4257: Fast curving with the thickness switch. FoamMesh's own build
    # (gmsh-4.15.2-bl-curving.patch) reads Gmsh's own option
    # Mesh.HighOrderFastCurvingNewAlgo for it, as the window does; the stock
    # wheel declares the option and never reads it, so the session refuses it.
    ('bl_curving', 'Boundary layer curving (experimental)',
     'HighOrderFastCurving', (
         'Mesh.HighOrderNumLayers', 'Mesh.HighOrderCurveOuterBL')),
)
REGULARIZE_RANGES = {
    'Mesh.HighOrderThresholdMin': (0.0, 1.0),
    'Mesh.HighOrderThresholdMax': (1.0, 10.0),
    'Mesh.HighOrderNumLayers': (1, 250),
    'Mesh.HighOrderIterMax': (1, 10000),
    'Mesh.HighOrderPassMax': (1, 100),
    'Mesh.HighOrderStrategy': (0, 2),
    'Mesh.HighOrderDistanceFactor': (1, 20000),
    'Mesh.HighOrderMaxAdaptBlob': (1, 100),
    'Mesh.HighOrderAdaptBlobLayerFactor': (1, 100),
    'Mesh.HighOrderAdaptBlobDistFactor': (1, 100),
}
REGULARIZE_OPTIONS = tuple(sorted({
    name for _i, _t, _m, names in REGULARIZE_ALGORITHMS for name in names}))
#: Fields of the native window with no Gmsh option behind them, and the value
#: Gmsh's API runs with. Empty since the G15 patch (DP-5707): every field of
#: the High-order tools window is an option of FoamMesh's Gmsh build.
REGULARIZE_UNREACHABLE: dict = {}


def element_tags(text) -> list:
    """``"1, 5-9, 12"`` or ``[1, 2]`` as a list of element tags."""
    if text in (None, '', [], ()):
        return []
    if isinstance(text, (list, tuple, set)):
        tags = [int(t) for t in text]
    else:
        tags = []
        for item in str(text).replace(';', ',').split(','):
            item = item.strip()
            if not item:
                continue
            try:
                if '-' in item:
                    low, high = item.split('-')
                    low, high = int(low), int(high)
                    if high < low or high - low > 5_000_000:
                        raise ValueError
                    tags.extend(range(low, high + 1))
                else:
                    tags.append(int(item))
            except ValueError:
                raise ActionRefusal(
                    f'"{item}" is not an element tag or a range such as '
                    '5-9') from None
    if any(t <= 0 for t in tags):
        raise ActionRefusal('element tags are positive numbers')
    return tags


def compress_tags(tags) -> str:
    """``[1, 2, 3, 7]`` as ``"1-3, 7"``: element tags in the shortest text."""
    ordered = sorted({int(t) for t in tags})
    parts = []
    start = previous = None
    for tag in ordered:
        if start is None:
            start = previous = tag
        elif tag == previous + 1:
            previous = tag
        else:
            parts.append(f'{start}-{previous}' if previous > start
                         else str(start))
            start = previous = tag
    if start is not None:
        parts.append(f'{start}-{previous}' if previous > start
                     else str(start))
    return ', '.join(parts)


def edge_pairs(value) -> list:
    """``"4-9, 12-3"`` or ``[[4, 9]]`` as node pairs (an edge each)."""
    if value in (None, '', [], ()):
        return []
    if isinstance(value, str):
        pairs = []
        for item in value.replace(';', ',').split(','):
            item = item.strip()
            if not item:
                continue
            try:
                a, b = item.replace(':', '-').split('-')
                pairs.append([int(a), int(b)])
            except ValueError:
                raise ActionRefusal(
                    f'"{item}" is not an edge; write two node tags, for '
                    'example 4-9') from None
        return pairs
    try:
        return [[int(a), int(b)] for a, b in value]
    except (TypeError, ValueError):
        raise ActionRefusal('delete_edges is a list of node pairs') from None


def _regularize(action, values):
    chosen = values.get('algorithm', 'optimization')
    row = next((r for r in REGULARIZE_ALGORITHMS if r[0] == chosen), None)
    if row is None:
        raise ActionRefusal(
            f'"{chosen}" is not a regularisation; they are '
            + ', '.join(r[0] for r in REGULARIZE_ALGORITHMS))
    _id, title, method, active = row
    if method is None:
        raise ActionRefusal(
            f'{title} is Fast curving with the thickness switch on, a call '
            'the Gmsh 4.15.2 API does not make (the build hides its private '
            'API); use Fast curving')
    options = {}
    for name in REGULARIZE_OPTIONS:
        if name not in values or values[name] is None:
            continue
        if name not in active:
            raise ActionRefusal(
                f'{name} is not used by {title}; it takes '
                + (', '.join(active) or 'no parameters'))
        try:
            parsed = catalogue.parse_value(name, values[name])
        except catalogue.OptionRefusal as error:
            raise ActionRefusal(str(error)) from None
        low, high = REGULARIZE_RANGES.get(name, (None, None))
        if low is not None and not low <= float(parsed) <= high:
            raise ActionRefusal(f'{name} must be between {low:g} and '
                                f'{high:g}')
        options[name] = float(parsed)
    ops = _option_ops(SCOPE_OPTIONS, values, SCOPE_OPTIONS)
    ops.append(Op('session.regularize', (), {
        'algorithm': chosen, 'options': options,
        'force': bool(values.get('force', False)),
        'niter': _int(values, 'niter', 1, 1, 1000),
        'entities': dim_tags(values.get('entities'))}))
    return ops, ('Gmsh optimises every high-order element of the mesh; the '
                 'entities are not a filter of its optimisers',)


def _need_elements(values, what):
    tags = element_tags(values.get('elements'))
    if not tags:
        raise ActionRefusal(f'give the elements to {what}, as tags such as '
                            '5-9, 12')
    return tags


def _need_entities(values, what):
    entities = dim_tags(values.get('entities'))
    if not entities:
        raise ActionRefusal(f'give the curves, surfaces or volumes to '
                            f'{what}, written dimension:tag')
    if any(d == 0 for d, _ in entities):
        raise ActionRefusal('points have no elements to ' + what)
    return entities


def _reverse_elements(action, values):
    # mesh.reverseElements: Gmsh reverses the nodes of each named element
    return [Op('model.mesh.reverseElements',
               (_need_elements(values, 'reverse'),))], ()


def _reverse_entities(action, values):
    # mesh.reverse: Gmsh reverses every element of the named entities
    return [Op('model.mesh.reverse',
               (_need_entities(values, 'reverse'),))], ()


def _delete_elements(action, values):
    return [Op('session.remove_elements',
               (_need_elements(values, 'delete'),))], (
        'the nodes of a deleted element stay in the model',)


def _delete_entity_mesh(action, values):
    return [Op('session.delete_entity_mesh',
               (_need_entities(values, 'delete the mesh of'),))], (
        "Gmsh's clear call leaves a bounding entity alone without an error; "
        'the elements are then removed directly, as native Gmsh does',)


def _angle(values):
    angle = float(values.get('angle', 40.0))
    if not 0.0 < angle <= 180.0:
        raise ActionRefusal('the threshold angle is in degrees, greater '
                            'than 0 and at most 180')
    return angle


def _reclassify_subset(action, values):
    angle = _angle(values)
    drop = edge_pairs(values.get('delete_edges'))
    return [Op('session.reclassify_subset', (), {
        'angle': angle, 'elements': element_tags(values.get('elements')),
        'entities': dim_tags(values.get('entities')),
        'boundary': bool(values.get('boundary', True)),
        'delete_edges': drop,
        'create_geometry': bool(values.get('create_geometry', True)),
        'export_discrete': bool(values.get('export_discrete', True)),
        'hide_unselected': bool(values.get('hide_unselected', False))})], (
        'with no subset and no edge taken out this is the Gmsh '
        'Reclassify 2D call (classifySurfaces); with one, the surface mesh '
        'is cut into patches along the edges above the angle and elements '
        'outside the subset keep their surfaces, and the curves between the '
        'patches are made (createTopology)',)


def _reclassify_preview(action, values):
    return [Op('session.reclassify_edges', (), {
        'angle': _angle(values),
        'elements': element_tags(values.get('elements')),
        'boundary': bool(values.get('boundary', True))})], ()


def _quad_layout(action, values):
    try:
        limit = float(values.get('timeout', DEFAULT_LIMITS['cross_field']))
    except (TypeError, ValueError):
        raise ActionRefusal('the time limit is a number of seconds') from None
    if limit < 0:
        raise ActionRefusal('the time limit is a number of seconds, 0 for '
                            'none')
    limit = limit or 86400.0
    return [Op('session.quad_layout', (), {'limit': limit},
               timeout=limit + 60.0)], (
        'Gmsh 4.15.2 computes the cross field in a PETSc solve that ends in '
        'an error or a crash on the models measured; it runs in a child '
        'process so the session survives, and a failure is reported with '
        'its exit code',)


def _experimental(method):
    def expand(action, values):
        return [Op('model.mesh.optimize', (method,), {
            'force': False, 'niter': _int(values, 'niter', 1, 1, 1000),
            'dimTags': []})], ()
    return expand


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
    Action('regularize', 'Regularize', 'High-order tools',
           "Optimise high-order elements: optimisation, elastic analogy or "
           "fast curving, with the parameters of Gmsh's window.", 1,
           REGULARIZE_OPTIONS, ('algorithm', 'niter', 'force', 'entities'),
           _regularize),
    Action('reverse_elements', 'Reverse elements', 'Reverse',
           'Flip the orientation of the elements you name.', 1, (),
           ('elements',), _reverse_elements),
    Action('reverse_entities', 'Reverse curves, surfaces, volumes',
           'Reverse', 'Flip the orientation of the mesh of the entities '
           'you name.', 1, (), ('entities',), _reverse_entities),
    Action('delete_elements', 'Delete elements', 'Delete',
           'Remove the elements you name from the mesh.', 1, (),
           ('elements',), _delete_elements),
    Action('delete_entity_mesh', 'Delete curves, surfaces, volumes',
           'Delete', 'Remove the mesh of the entities you name.', 1, (),
           ('entities',), _delete_entity_mesh),
    Action('reclassify_subset', 'Reclassify 2D (subset)', 'Mesh',
           'Cut a subset of the surface mesh into patches by an angle '
           'threshold, with edges you can take out.', 2, (),
           ('angle', 'elements', 'entities', 'boundary', 'delete_edges',
            'create_geometry', 'export_discrete', 'hide_unselected'),
           _reclassify_subset),
    Action('reclassify_preview', 'Reclassify 2D edges', 'Mesh',
           'The edges the angle threshold would cut along.', 2, (),
           ('angle', 'elements', 'boundary'), _reclassify_preview),
    Action('quad_layout', 'Compute quad layout', 'Experimental',
           'Compute the cross field the quad layout is built from.', 2, (),
           ('timeout',), _quad_layout),
    Action('optimize_quad_topology', 'Optimize quad topology',
           'Experimental', 'Improve the topology of a quadrangle mesh.', 2,
           (), ('niter',), _experimental('QuadQuasiStructured')),
    Action('untangle_geometry', 'Untangle geometry', 'Experimental',
           'Untangle inverted elements by moving nodes.', 2, (), ('niter',),
           _experimental('UntangleMeshGeometry')),
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


def _op_what(operation: 'Op', kind: str) -> str:
    if kind == 'plugin' and operation.args:
        return f'the {operation.args[0]} plugin'
    return LIMIT_WHAT[kind]


def run(session, action_id: str, values: dict | None = None, *,
        timeline: Timeline | None = None, measure: bool = True,
        timeout: float | None = None,
        limits: dict | None = None) -> Stage:
    """Play an action on *session*; record and return its stage.

    Plan 48 TL. A call that Gmsh 4.15.2 can leave hanging (a plugin, the
    cross field, createTopology, Reclassify 2D) runs under its time limit
    (*limits*, else :data:`DEFAULT_LIMITS`); one that passes it ends the stage
    with a refusal that names the limit, and the session is restarted from
    its log.  *timeout* still bounds any other call.
    """
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
        results = []
        for operation in planned.ops:
            call_timeout = operation.timeout or timeout
            kind = OP_LIMIT_KIND.get(operation.name)
            if kind is not None and not call_timeout:
                call_timeout = limit_for(kind, limits) or None
            try:
                answer = session.call(operation.name, *operation.args,
                                      timeout=call_timeout, **operation.kwargs)
            except Exception as error:                 # noqa: BLE001
                if call_timeout and is_time_limit(error):
                    what = (_op_what(operation, kind) if kind
                            else chosen.title)
                    raise TimeLimitRefusal(
                        what, call_timeout - (60.0 if kind == 'cross_field'
                                              and operation.timeout else 0.0),
                        restart_after_limit(session)) from None
                raise
            if (kind == 'cross_field' and isinstance(answer, dict)
                    and answer.get('refused')):
                answer = dict(answer, refused=_cross_field_refusal(
                    str(answer['refused']), operation))
            if isinstance(answer, dict) and answer:
                results.append(answer)
            if isinstance(answer, dict) and answer.get('refused'):
                raise ActionRefusal(str(answer['refused']))
        stage.results = tuple(results)
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


def _cross_field_refusal(text: str, operation: 'Op') -> str:
    """The child process's refusal, tagged as Gmsh's own limit."""
    if text.startswith(LIMIT_TAG):
        return text
    if 'was not computed in' in text:
        return limit_text(LIMIT_WHAT['cross_field'],
                          float(operation.kwargs.get('limit') or 0),
                          untouched=True)
    return f'{LIMIT_TAG}: {text}'


def _summary(state: dict) -> dict:
    return {'nodes': state.get('nodes', 0),
            'elements': dict(state.get('elements') or {}),
            'dims_meshed': list(state.get('dims_meshed') or [])}


# --------------------------------------------------------------------------- #
# Plan 48 GM7: the entries of Mesh > Experimental, the High-order tools,
# Reverse, Delete and Reclassify in the Gmsh tool registry (the window's
# mount, GM11b, builds the menu).

MENU_ENTRIES = (
    ('quad_layout', 'Mesh/Experimental', 100),
    ('optimize_quad_topology', 'Mesh/Experimental', 110),
    ('untangle_geometry', 'Mesh/Experimental', 120),
    ('set_order', 'Mesh/High-order tools', 100),
    ('regularize', 'Mesh/High-order tools', 110),
    ('reverse_elements', 'Mesh/Reverse', 100),
    ('reverse_entities', 'Mesh/Reverse', 110),
    ('delete_elements', 'Mesh/Delete', 100),
    ('delete_entity_mesh', 'Mesh/Delete', 110),
    ('reclassify_subset', 'Mesh/Reclassify', 100),
)
MENU_PREFIX = 'gmsh.mesh.'


def register_window_entries(handler=None) -> tuple:
    """Register the Mesh menu entries of GM7 with the Gmsh tool registry.

    *handler* is called with the action id when an entry is chosen (the
    window opens the Generate panel on that action); with none, the entry is
    registered with a handler that says the window has to supply one.
    Entries already registered are left as they are.
    """
    from foammesh.view.gmsh_tools import registry

    def unbound(action_id):
        raise RuntimeError(
            'the Mesh menu entries run in the window; no handler was given')

    made = []
    known = {entry.id for entry in registry.actions(None)}
    for action_id, menu, order in MENU_ENTRIES:
        entry_id = MENU_PREFIX + action_id
        if entry_id in known:
            continue
        chosen = ACTIONS[action_id]

        def run_it(_action_id=action_id):
            return (handler or unbound)(_action_id)

        made.append(registry.register_action(
            entry_id, chosen.title, run_it, tooltip=chosen.help))
        made.append(registry.register_menu_item(
            entry_id + '.menu', menu, entry_id, order=order))
    return tuple(made)


# --------------------------------------------------------------------------- #
# Plan 48 PA2: the form of each action, as data.  The Generate panel builds
# its controls from these tables and the local API publishes the same ones, so
# the window and a client cannot disagree on what an action takes.

#: Gmsh's own keys for the Mesh module's first three buttons.
SHORTCUTS = {'1': 'generate1d', '2': 'generate2d', '3': 'generate3d',
             'F2': 'generate2d', 'F3': 'generate3d'}

#: name -> (label, kind, default, extra)
ARGUMENTS = {
    'method': ('Method', 'choice', '',
               [(title, method) for method, title, _ in OPTIMIZE_METHODS]),
    'niter': ('Passes', 'int', 1, (1, 1000)),
    'force': ('Also discrete entities', 'bool', False, None),
    'entities': ('Entities (dimension:tag, ...)', 'text', '', None),
    'order': ('Order', 'int', 2, ORDER_RANGE),
    'steps': ('Times', 'int', 1, (1, 10)),
    'parts': ('Partitions', 'int', 2, (1, 100000)),
    'dim': ('Up to dimension', 'int', 3, (1, 3)),
    'nodes': ('Nodes', 'bool', True, None),
    'elements': ('Elements', 'bool', True, None),
    'visible': ('Visible', 'bool', True, None),
    'empty': ('Only empty', 'bool', True, None),
    'recursive': ('With their boundaries', 'bool', True, None),
    'angle': ('Angle (degrees)', 'text', '40', None),
    'curve_angle': ('Curve angle (degrees)', 'text', '180', None),
    'boundary': ('Boundary curves', 'bool', True, None),
    'reparametrization': ('For reparametrization', 'bool', False, None),
    'export_discrete': ('Export discrete', 'bool', True, None),
    # Plan 48 GM7
    'algorithm': ('Algorithm', 'choice', 'optimization',
                  [(title, ident) for ident, title, _m, _o in
                   REGULARIZE_ALGORITHMS]),
    'timeout': ('Time limit (s)', 'text', '600', None),
    'hide_unselected': ('Hide unselected', 'bool', False, None),
    'create_geometry': ('Create parametrized discrete model', 'bool', True,
                        None),
    'delete_edges': ('Edges taken out (node-node, ...)', 'text', '', None),
}
for _name, (_label, _default) in REGULARIZE_UNREACHABLE.items():
    ARGUMENTS[_name] = (_label, 'fixed', _default, None)

#: Where an action's argument means something other than the table above:
#: ``elements`` is a yes/no for Renumber and a list of tags for the actions
#: that edit elements.
ARGUMENT_OVERRIDES = {
    (action_id, 'elements'): ('Elements (tags, e.g. 5-9, 12)', 'text', '',
                              None)
    for action_id in ('reverse_elements', 'delete_elements',
                      'reclassify_subset', 'reclassify_preview')
}
#: Plan 48 PA2 (DP-4871). ``method`` is Optimize's list in the table above;
#: Renumber takes Gmsh's three renumbering methods, RCMK first.
ARGUMENT_OVERRIDES[('renumber', 'method')] = (
    'Method', 'choice', RENUMBER_METHODS[0],
    [(method, method) for method in RENUMBER_METHODS])

#: The pick filter each entity argument starts with.
PICK_FILTERS = ('Points', 'Curves', 'Surfaces', 'Volumes')
#: Plan 48 PK (DP-4503). A list of elements can also be picked cell by cell
#: in the mesh on screen; the answer is element tags, not entities.
ELEMENT_PICK_FILTERS = PICK_FILTERS + ('Cells',)


def argument_spec(action_id: str, name: str) -> tuple:
    """``(label, kind, default, extra)`` of argument *name* of an action."""
    return ARGUMENT_OVERRIDES.get((action_id, name)) or ARGUMENTS[name]


def _extra_json(kind: str, extra):
    if kind == 'int':
        return {'low': extra[0], 'high': extra[1]}
    if kind == 'choice':
        return {'choices': [{'title': t, 'value': v} for t, v in extra]}
    return {}


def option_form(option) -> dict:
    """What the panel shows for one Gmsh option of an action."""
    row = {'name': option.name, 'label': option.label, 'kind': option.kind,
           'default': option.default, 'help': option.help,
           'unit': option.unit, 'is_length': bool(option.is_length),
           'choices': [{'value': v, 'title': t} for v, t in option.choices]}
    return row


def form(action_id: str) -> dict:
    """The controls of one action: the arguments and the ticked-to-send
    options, with kind, default, range and choices (what the panel draws)."""
    chosen = action(action_id)
    arguments = []
    for name in chosen.arguments:
        label, kind, default, extra = argument_spec(chosen.id, name)
        row = {'name': name, 'label': label, 'kind': kind,
               'default': default, 'fixed': kind == 'fixed',
               'pick_filters': (
                   list(ELEMENT_PICK_FILTERS) if name == 'elements'
                   else list(PICK_FILTERS)) if name in (
                       'entities', 'elements') and kind == 'text' else []}
        row.update(_extra_json(kind, extra))
        arguments.append(row)
    options = []
    for name in chosen.options:
        found = catalogue.get(name)
        if found is None:
            continue            # an option this Gmsh build lacks is not offered
        options.append(option_form(found))
    return {'id': chosen.id, 'title': chosen.title, 'group': chosen.group,
            'help': chosen.help, 'needs_dimension': chosen.needs,
            'shortcuts': [k for k, v in SHORTCUTS.items() if v == chosen.id],
            'arguments': arguments, 'options': options}
