"""Size-field graph assembly.

Rows become Gmsh fields, and the runner combines them with a ``Min`` field set
as the background mesh, so the finest request at any point wins. Rows are
validated here rather than in the runner: a row that cannot become a field is
rejected while the user is looking at it, not halfway through a mesh.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import sys
from pathlib import Path

from .expressions import ExpressionError, cost, validate_syntax

CALCULATION_VERSION = 'gmsh.size_fields.v1'

#: Field types and the Gmsh field they become. ``distance_threshold`` is two
#: fields -- a Distance feeding a Threshold -- which is why it is not a
#: one-to-one map.
FIELD_KINDS = {
    'distance_threshold': ('Distance', 'Threshold'),
    'box': ('Box',),
    'ball': ('Ball',),
    'cylinder': ('Cylinder',),
    'frustum': ('Frustum',),
    'math_eval': ('MathEval',),
    # Plan 30 WP12. A Restrict clips another field to a list of entities, so
    # the row becomes a Constant carrying the inside size and a Restrict
    # holding it to the scope. Curvature likewise wraps an input field: the
    # curvature of the distance to the scoped surfaces, mapped to a size by a
    # Threshold.
    'restrict': ('Constant', 'Restrict'),
    'curvature': ('Distance', 'Curvature', 'Threshold'),
}

#: Types positioned by their own coordinates rather than by a geometry scope.
ANALYTIC_KINDS = frozenset({'box', 'ball', 'cylinder', 'frustum', 'math_eval'})

#: Types whose size comes from an expression rather than from size numbers.
EXPRESSION_KINDS = frozenset({'math_eval'})

#: Plan 30 WP12. Field types that ramp between two sizes, and so need both.
#: A ``restrict`` row is the exception: it holds one size inside the entities
#: its scope names and contributes nothing outside them, where the global size
#: stands. Demanding an outside size from it would refuse a row for a
#: parameter the editor no longer shows -- the applicability register in
#: ``core.gmsh.fields`` lists ``sizeInside`` alone for this type.
OUTSIDE_SIZE_KINDS = frozenset(set(FIELD_KINDS) - EXPRESSION_KINDS
                               - {'restrict'})


class SizeFieldError(ValueError):
    pass


@dataclass(frozen=True)
class SizeField:
    control_id: str
    name: str
    field_type: str
    order: int
    scope_token: str = ''
    #: Gmsh surface tags named directly, for a row that needs no prepared
    #: scope. Plan 29 WP8: a per-surface size knows its own surface, and
    #: forcing it through a scope token would mean the user could only refine
    #: face groups the geometry catalogue had already prepared.
    surfaces: tuple[int, ...] = ()
    size_inside: float = 0.0
    size_outside: float = 0.0
    distance_min: float = 0.0
    distance_max: float = 0.0
    centre: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: WP-01 F-16. A Box field is two opposite corners. It used to be a corner
    #: called `centre` plus an `extent`, so the number the user typed as a
    #: centre was read as the minimum corner.
    box_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    box_max: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    radius: float = 0.0
    #: WP-01 F-16. The frustum's radius at the far end of the axis. The runner
    #: used to write one radius onto both ends, so every frustum was a tube.
    radius_end: float = 0.0
    thickness: float = 0.0
    #: Plan 30 WP12. `Distance.Sampling`, and the curvature row's own three.
    sampling: int = 20
    curvature_delta: float = 0.001
    curvature_min: float = 0.0
    curvature_max: float = 1.0
    expression: str = ''
    #: What the expression asks for, recorded so a run can be judged against
    #: the estimate that let it through.
    expression_cost: dict | None = None

    @property
    def gmsh_kinds(self) -> tuple[str, ...]:
        return FIELD_KINDS[self.field_type]

    @property
    def needs_scope(self) -> bool:
        if self.surfaces:
            return False
        return self.field_type not in ANALYTIC_KINDS

    def to_dict(self) -> dict:
        return {
            'controlId': self.control_id, 'name': self.name,
            'fieldType': self.field_type, 'order': self.order,
            'scopeToken': self.scope_token,
            'surfaces': [int(tag) for tag in self.surfaces],
            'sizeInside': self.size_inside, 'sizeOutside': self.size_outside,
            'distanceMin': self.distance_min, 'distanceMax': self.distance_max,
            'centre': list(self.centre),
            'boxMin': list(self.box_min), 'boxMax': list(self.box_max),
            'axis': list(self.axis), 'radius': self.radius,
            'radiusEnd': self.radius_end, 'thickness': self.thickness,
            'sampling': self.sampling,
            'curvatureDelta': self.curvature_delta,
            'curvatureMin': self.curvature_min,
            'curvatureMax': self.curvature_max,
            'expression': self.expression,
            'expressionCost': self.expression_cost,
            'gmshKinds': list(self.gmsh_kinds),
        }


@dataclass(frozen=True)
class SizeFieldPlan:
    fields: tuple[SizeField, ...] = ()
    warnings: tuple[str, ...] = ()
    calculation_version: str = CALCULATION_VERSION

    def to_dict(self) -> dict:
        return {
            'fields': [item.to_dict() for item in self.fields],
            'warnings': list(self.warnings),
            'calculation_version': self.calculation_version,
        }

    def merged_with(self, fields, warnings=()) -> 'SizeFieldPlan':
        """One plan carrying both the hand-built rows and the per-surface ones.

        They have to share a plan rather than reach Gmsh separately: the runner
        combines every field into a single ``Min`` background field, and a
        second plan would mean a second background field silently replacing the
        first.
        """
        combined = tuple(sorted((*self.fields, *fields),
                                key=lambda item: (-item.order, item.name)))
        return SizeFieldPlan(fields=combined,
                             warnings=(*self.warnings, *warnings),
                             calculation_version=self.calculation_version)


def _enum(value, default=''):
    if value is None:
        return default
    return str(getattr(value, 'value', value)).split('.')[-1].lower()


def _vector(value, default=(0.0, 0.0, 0.0)):
    if isinstance(value, dict):
        return (float(value.get('x', default[0])),
                float(value.get('y', default[1])),
                float(value.get('z', default[2])))
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(float(item) for item in value)
    return tuple(float(item) for item in default)


def field_graph_module():
    """The runner's field-graph builder, loaded from the resource tree.

    Plan 31 CP-08 item 1. One implementation of the wiring for the host and
    the runtime: a second copy here is how the plan the user is shown and the
    fields Gmsh is given would come to disagree.
    """
    module = sys.modules.get('foammesh._field_graph')
    if module is not None:
        return module
    from resources import resource

    location = Path(resource.file('gmsh/field_graph.py')).resolve()
    spec = importlib.util.spec_from_file_location(
        'foammesh._field_graph', location)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass resolves its own annotations
    # through ``sys.modules`` and fails on a module that is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_field_graph(fields) -> dict:
    """Refuse a plan whose fields cannot be wired, and say why.

    The runner builds the same graph from the same module before it creates a
    single Gmsh field, so what is checked here is what will be built.
    """
    graph_module = field_graph_module()
    rows = [item.to_dict() if hasattr(item, 'to_dict') else dict(item)
            for item in (fields or ())]
    try:
        return graph_module.build_graph(rows).to_dict()
    except graph_module.FieldGraphError as error:
        raise SizeFieldError(str(error)) from error


def derive_size_fields(rows, bbox=None) -> SizeFieldPlan:
    """Validate and order the enabled size-field rows.

    ``bbox`` is needed to cost a ``math_eval`` expression; without it such a
    row is refused rather than accepted unpriced.
    """
    derived: list[SizeField] = []
    warnings: list[str] = []
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        if not bool(row.get('enabled', True)):
            continue
        control_id = str(row.get('control_id') or row.get('controlId') or index)
        name = str(row.get('name') or f'field-{control_id}')
        kind = _enum(row.get('fieldType'), 'distance_threshold')
        if kind not in FIELD_KINDS:
            raise SizeFieldError(
                f'size field {name!r} has unknown type {kind!r}; expected one '
                f'of {", ".join(sorted(FIELD_KINDS))}')

        if kind in EXPRESSION_KINDS:
            derived.append(_expression_field(row, name, control_id, bbox,
                                             warnings))
            continue

        inside = float(row.get('sizeInside', 0.0) or 0.0)
        outside = float(row.get('sizeOutside', 0.0) or 0.0)
        if inside <= 0:
            raise SizeFieldError(
                f'size field {name!r} needs a positive inside size')
        if outside <= 0 and kind in OUTSIDE_SIZE_KINDS:
            raise SizeFieldError(
                f'size field {name!r} needs a positive outside size')
        if outside > 0 and inside > outside:
            warnings.append(
                f'size field {name!r} is coarser inside ({inside:g}) than '
                f'outside ({outside:g}); it will coarsen rather than refine')

        scope = str(row.get('scopeToken') or row.get('scope_token') or '').strip()
        item = SizeField(
            control_id=control_id, name=name, field_type=kind,
            order=int(row.get('priority', 0) or 0), scope_token=scope,
            size_inside=inside, size_outside=outside,
            distance_min=float(row.get('distanceMin', 0.0) or 0.0),
            distance_max=float(row.get('distanceMax', 0.0) or 0.0),
            centre=_vector(row.get('centre')),
            box_min=_vector(row.get('boxMin')),
            box_max=_vector(row.get('boxMax')),
            axis=_vector(row.get('axis'), (0.0, 0.0, 1.0)),
            radius=float(row.get('radius', 0.0) or 0.0),
            radius_end=float(row.get('radiusEnd', 0.0) or 0.0),
            thickness=float(row.get('thickness', 0.0) or 0.0),
            sampling=int(row.get('sampling', 20) or 20),
            curvature_delta=float(row.get('curvatureDelta', 0.001) or 0.001),
            curvature_min=float(row.get('curvatureMin', 0.0) or 0.0),
            curvature_max=float(row.get('curvatureMax', 1.0) or 1.0))

        if item.needs_scope and not scope:
            raise SizeFieldError(
                f'size field {name!r} measures distance to a surface, so it '
                'needs a prepared geometry scope')
        if kind == 'curvature' and item.curvature_max <= item.curvature_min:
            raise SizeFieldError(
                f'size field {name!r} needs curvatureMax greater than '
                'curvatureMin, or the curvature maps onto one size everywhere')
        if kind == 'curvature' and item.curvature_delta <= 0:
            raise SizeFieldError(
                f'size field {name!r} needs a positive curvature step')
        if kind == 'distance_threshold' and item.distance_max <= item.distance_min:
            raise SizeFieldError(
                f'size field {name!r} needs distanceMax greater than '
                'distanceMin, or the threshold has no ramp')
        if kind in {'ball', 'cylinder', 'frustum'} and item.radius <= 0:
            raise SizeFieldError(f'size field {name!r} needs a positive radius')
        if kind == 'box' and not all(
                high > low for low, high in zip(item.box_min, item.box_max)):
            raise SizeFieldError(
                f'size field {name!r} needs a maximum corner greater than its '
                'minimum corner in x, y and z')
        if kind == 'frustum' and item.radius_end <= 0:
            raise SizeFieldError(
                f'size field {name!r} needs a positive radius at each end')
        derived.append(item)

    ordered = tuple(sorted(derived, key=lambda item: (-item.order, item.name)))
    return SizeFieldPlan(fields=ordered, warnings=tuple(warnings))


def derive_surface_sizes(rows, *, target_size: float,
                         prepared_geometry=None) -> tuple[SizeField, ...]:
    """Compile per-surface target sizes into Distance+Threshold pairs.

    Plan 29 WP8. The row says only "this surface, this size"; everything the
    Threshold needs beyond that is derived here rather than asked for:

    * the outer size is the global target, so the refinement blends back into
      the rest of the mesh instead of ending at a cliff;
    * a blend distance of zero becomes one global cell, because a Threshold
      whose ``DistMax`` equals its ``DistMin`` is a step change in cell size
      and Gmsh will not build it.

    The result is ordinary :class:`SizeField` rows, so they join the same
    background field as every other size source.

    Plan 33 W-G1 (FIELD-03). ``prepared_geometry`` is how the row finds its
    surface. A Gmsh tag is a position in import order, so the saved number
    means nothing once the boundaries are renumbered; the row carries the
    prepared boundary it was authored on and the tag is resolved from that,
    against the revision being meshed. Passing nothing keeps the old
    behaviour -- the number is taken as given -- which is what a caller that
    has no prepared geometry to resolve against can honestly do.
    """
    target = float(target_size or 0.0)
    if target <= 0:
        raise SizeFieldError(
            'a per-surface size blends back to the global target size, so a '
            'positive global target must be derived first')
    derived: list[SizeField] = []
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        if not bool(row.get('enabled', True)):
            continue
        control_id = str(row.get('control_id') or row.get('controlId') or index)
        name = str(row.get('name') or f'surface-{control_id}')
        if prepared_geometry is None:
            surfaces = (_surface_tag(row),)
            if surfaces[0] < 1:
                raise SizeFieldError(
                    f'surface size {name!r} needs a surface number; Gmsh '
                    'numbers imported surfaces from 1')
        else:
            scope = resolve_surface_rows((row,), prepared_geometry)[0]
            if scope.reason:
                raise SizeFieldError(scope.reason)
            surfaces = scope.tags
        size = float(row.get('targetSize', 0.0) or 0.0)
        if size <= 0:
            raise SizeFieldError(
                f'surface size {name!r} needs a positive target size')
        blend = float(row.get('blendDistance', 0.0) or 0.0)
        if blend <= 0:
            blend = target
        derived.append(SizeField(
            control_id=control_id, name=name,
            field_type='distance_threshold',
            order=int(row.get('priority', 0) or 0),
            surfaces=tuple(surfaces), size_inside=size, size_outside=target,
            distance_min=0.0, distance_max=blend))
    return tuple(sorted(derived, key=lambda item: (-item.order, item.name)))


# --------------------------------------------------------------------------- #
# Plan 33 W-G1. Which boundary a row refines, and whether it still exists.
# --------------------------------------------------------------------------- #

#: What a row that cannot be placed reads as, on the page and in every
#: refusal built from one. One string so the page and the run agree.
NEEDS_SELECTION = 'Needs selection'


@dataclass(frozen=True)
class SurfaceScope:
    """One per-surface size row resolved against a prepared revision.

    ``reference`` is the prepared boundary the row names -- the same
    ``patch_uuid`` the geometry catalogue and every other scoped control use.
    ``tags`` are the Gmsh surface tags that boundary holds in *this*
    revision, and ``reason`` is why there are none.
    """

    name: str
    reference: str
    tags: tuple[int, ...] = ()
    reason: str = ''

    @property
    def tag(self) -> int:
        """The first tag, or 0 -- what a single-surface row means by itself."""
        return self.tags[0] if self.tags else 0

    @property
    def resolved(self) -> bool:
        return bool(self.tags) and not self.reason


def _surface_tag(row) -> int:
    """The saved Gmsh tag on a row, as an integer. 0 when there is none.

    The configuration store hands numbers back as strings, so the saved value
    is not always an ``int`` even though the schema says it is.
    """
    for key in ('surfaceId', 'surface_id'):
        if key in row:
            try:
                return int(row.get(key) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _surface_reference(row) -> str:
    for key in ('surfaceRef', 'surface_ref'):
        value = str(row.get(key) or '').strip()
        if value:
            return value
    return ''


def surface_tags_for_reference(prepared_geometry, reference) -> tuple[int, ...]:
    """The Gmsh tags a prepared boundary holds in this revision.

    Empty when the boundary is not in the revision at all -- renamed is the
    same boundary, merged or reimported is not.
    """
    # Imported here rather than at module scope: `execution` imports the
    # derivation, which imports this module.
    from .execution import scope_surface_map

    indices = scope_surface_map(prepared_geometry).get(
        str(reference or '').strip()) or ()
    return tuple(sorted(int(index) + 1 for index in indices))


def surface_reference_for_tag(prepared_geometry, tag) -> str:
    """The prepared boundary a Gmsh surface tag belongs to, or ``''``.

    The migration rule for a row saved before references existed, and the rule
    the facade stamps every newly authored row with. A tag two boundaries claim
    is not an identity, so it resolves to nothing rather than to whichever one
    is first.

    DP-470. This reads `_global_surface_rows` rather than doing the index
    arithmetic itself, because it is the inverse of `surface_names` and the two
    have to agree: the picker offers a tag taken from one of them and the
    facade turns that tag back into a boundary with the other. While this
    counted within one file and that one did too, they agreed by both being
    wrong for a multi-source model -- every tag claimed twice, so nothing
    offered and nothing resolved. With the per-source offset applied in one
    place they agree by both being right.
    """
    from .execution import _global_surface_rows

    try:
        wanted = int(tag or 0)
    except (TypeError, ValueError):
        return ''
    if wanted < 1:
        return ''
    claimed = [token
               for token, _label, _source, tags in _global_surface_rows(
                   prepared_geometry)
               if token and wanted in set(tags)]
    return claimed[0] if len(claimed) == 1 else ''


def resolve_surface_rows(rows, prepared_geometry) -> tuple[SurfaceScope, ...]:
    """Resolve each per-surface size row against the prepared revision.

    One :class:`SurfaceScope` per row, in the order given, enabled or not --
    the caller decides which of them are its business.
    """
    resolved: list[SurfaceScope] = []
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        control_id = str(row.get('control_id') or row.get('controlId')
                         or index)
        name = str(row.get('name') or f'surface-{control_id}')
        reference = _surface_reference(row)
        tag = _surface_tag(row)
        if reference:
            tags = surface_tags_for_reference(prepared_geometry, reference)
            if tags:
                resolved.append(SurfaceScope(name, reference, tags))
                continue
            resolved.append(SurfaceScope(
                name, reference, (),
                f'size field {name!r} refines a boundary this geometry no '
                f'longer has: {NEEDS_SELECTION}'))
            continue
        # A row saved before references existed. Its number means the
        # revision it was authored against, and the only revision there is
        # to read it against is the one prepared now.
        migrated = surface_reference_for_tag(prepared_geometry, tag)
        if migrated:
            resolved.append(SurfaceScope(name, migrated, (tag,)))
            continue
        resolved.append(SurfaceScope(
            name, '', (),
            f'size field {name!r} names surface {tag}, which no prepared '
            f'boundary claims: {NEEDS_SELECTION}'))
    return tuple(resolved)


def _native_rows(value) -> tuple[dict, ...]:
    """The rows of one ``IntKeyList`` section, in key order."""
    if isinstance(value, dict):
        return tuple(dict(item, control_id=str(key))
                     for key, item in sorted(value.items(),
                                             key=lambda pair: str(pair[0]))
                     if isinstance(item, dict))
    if isinstance(value, (list, tuple)):
        return tuple(dict(item) for item in value if isinstance(item, dict))
    return ()


def unresolved_scopes(native, prepared_geometry) -> tuple[str, ...]:
    """Why each enabled control cannot be placed on this geometry.

    Plan 33 W-G1 (FIELD-06, CURVE-04). Empty when every enabled control
    resolves, which is the only state a run may start in: a scope that
    reaches nothing used to be found by the runner, inside WSL and minutes
    into a run, or -- for a row naming its own Gmsh tags -- not at all.
    """
    from .execution import scope_surface_map, scope_volume_map

    native = dict(native or {})
    complaints: list[str] = []
    for scope in resolve_surface_rows(
            [row for row in _native_rows(native.get('surfaceSizes'))
             if bool(row.get('enabled', True))], prepared_geometry):
        if scope.reason:
            complaints.append(scope.reason)

    surfaces = set(scope_surface_map(prepared_geometry))
    volumes = set(scope_volume_map(prepared_geometry))
    for label, key, allowed in (
            ('size field', 'sizeFields', surfaces | volumes),
            ('curve control', 'curveControls', surfaces)):
        for index, row in enumerate(_native_rows(native.get(key))):
            if not bool(row.get('enabled', True)):
                continue
            if key == 'sizeFields' and str(
                    row.get('fieldType') or '') in ANALYTIC_KINDS:
                continue
            token = str(row.get('scopeToken')
                        or row.get('scope_token') or '').strip()
            if not token or token in allowed:
                continue
            control_id = str(row.get('control_id') or row.get('controlId')
                             or index)
            name = str(row.get('name') or f'{label}-{control_id}')
            complaints.append(
                f'{label} {name!r} is scoped to geometry this case no longer '
                f'has: {NEEDS_SELECTION}')
    return tuple(complaints)


def _expression_field(row, name, control_id, bbox, warnings) -> SizeField:
    """Validate and cost one math_eval row.

    The expression is checked for names Gmsh knows, then sampled across the
    domain. A mistyped exponent parses perfectly and means an enormous mesh,
    so the cost is the check that matters.
    """
    raw = str(row.get('expression') or '').strip()
    try:
        expression = validate_syntax(raw)
        estimate = cost(expression, bbox)
    except ExpressionError as error:
        raise SizeFieldError(f'size field {name!r}: {error}') from error
    # a warning, not a refusal: said when the mesh would take more than
    # half the RAM that is free (the cost refuses what the RAM cannot hold)
    from foammesh.support import resource_budget

    needed = resource_budget.mesher_memory_needed(
        'gmsh', estimate.estimated_elements)
    if resource_budget.mesher_memory_refusal(
            'gmsh', 2 * estimate.estimated_elements) is not None:
        warnings.append(
            f'size field {name!r} asks for roughly '
            f'{estimate.estimated_elements:,} elements, about '
            f'{resource_budget.format_bytes(needed)} of RAM for Gmsh, more '
            'than half of what is free')
    return SizeField(
        control_id=control_id, name=name, field_type='math_eval',
        order=int(row.get('priority', 0) or 0), expression=expression,
        expression_cost=estimate.to_dict())
