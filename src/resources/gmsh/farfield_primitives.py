"""The farfield primitive: what shape, where, how big, and which face is which.

Plan 37 UF13, section 4.4. One authored farfield -- a box, a sphere or a
cylinder around the geometry -- is consumed by every engine adapter. This file
holds the geometry of it and nothing else, so the Gmsh runner (which executes
inside WSL with nothing of the application on its path) and the host side
(``foammesh.core.mesh.farfield_spec``) ask the same code:

* :func:`normalise` checks an authored spec and returns its canonical form;
* :func:`resolve` turns that spec and the model bounds into a concrete
  primitive and refuses one that does not contain every body with clearance;
* :func:`classify_point` names the face a point on the primitive lies on;
* :func:`volume` is the primitive's own volume, for the fluid-volume check.

Everything is in metres. The runner sets ``Geometry.OCCTargetUnit`` to ``M``
and the schema stores metres, so no length is converted here -- units are
handled once, where a user types them.

No import of gmsh or of the application: this is pure arithmetic.
"""

from __future__ import annotations

import math

BOX = 'box'
SPHERE = 'sphere'
CYLINDER = 'cylinder'
SHAPES = (BOX, SPHERE, CYLINDER)

CENTRE_AUTO = 'auto'
CENTRE_EXPLICIT = 'explicit'
CENTRE_MODES = (CENTRE_AUTO, CENTRE_EXPLICIT)

DEFAULT_PADDING = 2.0
DEFAULT_RADIUS = 1.0
DEFAULT_LENGTH = 2.0
DEFAULT_AXIS = (1.0, 0.0, 0.0)

#: The gap every body must keep from a sphere or cylinder wall, as a fraction
#: of the model's bounding-box diagonal. A body touching the primitive would
#: leave the cut a face shared by the body and the farfield, which is neither
#: a wall nor a far field and meshes as a sliver. The box is exempt: its
#: padding is already a standoff measured the same way.
CLEARANCE_FRACTION = 0.01

#: The names the outer faces publish under. ``far_field`` is the category the
#: publisher reads out of a patch name, so every one starts with it.
FAR_FIELD = 'far_field'
CYLINDER_SIDE = 'far_field_side'
CYLINDER_INLET = 'far_field_inlet'
CYLINDER_OUTLET = 'far_field_outlet'


class FarfieldError(ValueError):
    """The authored farfield cannot be built as it stands."""


def _finite(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise FarfieldError(f'the farfield {name} is not a number: '
                            f'{value!r}') from error
    if not math.isfinite(number):
        raise FarfieldError(f'the farfield {name} is not finite: {value!r}')
    return number


def _vector(value, name):
    if isinstance(value, dict):
        value = (value.get('x', 0.0), value.get('y', 0.0), value.get('z', 0.0))
    try:
        items = list(value)
    except TypeError as error:
        raise FarfieldError(f'the farfield {name} is not a vector: '
                            f'{value!r}') from error
    if len(items) != 3:
        raise FarfieldError(f'the farfield {name} needs three components, '
                            f'not {len(items)}')
    return tuple(_finite(item, name) for item in items)


def _positive(value, name):
    number = _finite(value, name)
    if number <= 0:
        raise FarfieldError(f'the farfield {name} must be greater than '
                            f'zero, not {number:g}')
    return number


def unit(vector):
    """``vector`` scaled to length one; a zero vector is refused."""
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-12:
        raise FarfieldError('the farfield cylinder axis has zero length, so '
                            'it points nowhere; give it a direction')
    return tuple(component / length for component in vector)


def normalise(values):
    """The canonical form of an authored spec, or :class:`FarfieldError`.

    ``values`` uses the schema's own keys (``shape``, ``centreMode``,
    ``centre``, ``padding``, ``radius``, ``length``, ``axis``); a missing key
    takes its default, so a project saved before UF13 -- which holds only
    ``padding`` -- normalises to today's padded box. Only the dimensions the
    shape uses are checked: a stale radius does not stop a box.
    """
    values = dict(values or {})
    shape = str(values.get('shape') or BOX).strip().lower()
    if shape not in SHAPES:
        raise FarfieldError(f'the farfield shape {shape!r} is not one of '
                            f'{", ".join(SHAPES)}')
    mode = str(values.get('centreMode') or CENTRE_AUTO).strip().lower()
    if mode not in CENTRE_MODES:
        raise FarfieldError(f'the farfield centre mode {mode!r} is not one of '
                            f'{", ".join(CENTRE_MODES)}')
    spec = {'shape': shape, 'centreMode': mode,
            'centre': _vector(values.get('centre', (0.0, 0.0, 0.0)), 'centre')}
    if shape == BOX:
        spec['padding'] = _positive(values.get('padding', DEFAULT_PADDING),
                                    'padding')
    else:
        spec['radius'] = _positive(values.get('radius', DEFAULT_RADIUS),
                                   'radius')
    if shape == CYLINDER:
        spec['length'] = _positive(values.get('length', DEFAULT_LENGTH),
                                   'length')
        spec['axis'] = unit(_vector(values.get('axis', DEFAULT_AXIS), 'axis'))
    return spec


def bounds_of(bounds):
    """``(xmin, ymin, zmin, xmax, ymax, zmax)``, the order Gmsh reports."""
    items = [float(item) for item in bounds]
    if len(items) != 6:
        raise FarfieldError('the model bounds need six numbers')
    return items


def diagonal_of(bounds):
    low, high = bounds[0:3], bounds[3:6]
    return math.dist(low, high)


def corners(bounds):
    low, high = bounds[0:3], bounds[3:6]
    return [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1])
            for z in (low[2], high[2])]


def centre_of(spec, bounds):
    if spec['centreMode'] == CENTRE_EXPLICIT:
        return tuple(spec['centre'])
    return tuple((bounds[index] + bounds[index + 3]) / 2.0
                 for index in range(3))


def resolve(values, bounds):
    """The primitive this spec builds around geometry spanning ``bounds``.

    Returns a dict the runner builds from and the host checks against:

    * box: ``origin`` and ``span`` (the arguments of ``occ.addBox``);
    * sphere: ``centre`` and ``radius``;
    * cylinder: ``centre``, unit ``axis``, ``radius``, ``length`` and
      ``base`` -- the centre of the inlet cap, ``centre - axis * length/2``,
      which with ``axis * length`` are the arguments of ``occ.addCylinder``.

    Every primitive carries ``shape``, ``volume`` and ``clearance`` (the
    smallest gap between a bounding-box corner and the primitive wall).

    The auto-centred box is exactly the box the runner built before UF13 --
    ``padding`` diagonals of standoff on every side -- computed the same way,
    so an existing project's mesh does not move by a rounding error. An
    explicitly centred box keeps that standoff from the side of the model
    furthest from its centre.
    """
    spec = normalise(values)
    bounds = bounds_of(bounds)
    diagonal = diagonal_of(bounds)
    shape = spec['shape']
    if shape == BOX:
        standoff = spec['padding'] * diagonal
        if spec['centreMode'] == CENTRE_AUTO:
            origin = [bounds[index] - standoff for index in range(3)]
            span = [bounds[index + 3] - bounds[index] + 2 * standoff
                    for index in range(3)]
        else:
            centre = spec['centre']
            half = [max(centre[index] - bounds[index],
                        bounds[index + 3] - centre[index]) + standoff
                    for index in range(3)]
            origin = [centre[index] - half[index] for index in range(3)]
            span = [2 * half[index] for index in range(3)]
        primitive = {'shape': BOX, 'origin': origin, 'span': span,
                     'standoff': standoff, 'padding': spec['padding']}
    elif shape == SPHERE:
        primitive = {'shape': SPHERE, 'centre': list(centre_of(spec, bounds)),
                     'radius': spec['radius']}
    else:
        centre = centre_of(spec, bounds)
        axis = spec['axis']
        length = spec['length']
        base = [centre[index] - axis[index] * length / 2.0
                for index in range(3)]
        primitive = {'shape': CYLINDER, 'centre': list(centre),
                     'axis': list(axis), 'radius': spec['radius'],
                     'length': length, 'base': base}
    primitive['volume'] = volume(primitive)
    primitive['clearance'] = clearance(primitive, bounds)
    check_containment(primitive, bounds)
    return primitive


def _axial(primitive, point):
    centre, axis = primitive['centre'], primitive['axis']
    offset = [point[index] - centre[index] for index in range(3)]
    along = sum(offset[index] * axis[index] for index in range(3))
    radial = math.sqrt(max(sum(component * component for component in offset)
                           - along * along, 0.0))
    return along, radial


def clearance(primitive, bounds):
    """The smallest gap from a bounding-box corner to the primitive wall.

    Negative when a corner is outside. The primitives are convex, so the
    bounding box -- and every body inside it -- is contained exactly when all
    eight corners are: this is conservative (a sphere refuses a long thin body
    lying along a diagonal only if its corners stick out), never optimistic.
    """
    gaps = []
    for corner in corners(bounds):
        if primitive['shape'] == BOX:
            origin, span = primitive['origin'], primitive['span']
            for index in range(3):
                gaps.append(corner[index] - origin[index])
                gaps.append(origin[index] + span[index] - corner[index])
        elif primitive['shape'] == SPHERE:
            gaps.append(primitive['radius']
                        - math.dist(corner, primitive['centre']))
        else:
            along, radial = _axial(primitive, corner)
            gaps.append(primitive['length'] / 2.0 - abs(along))
            gaps.append(primitive['radius'] - radial)
    return min(gaps)


def required(primitive, bounds):
    """The smallest dimensions that would contain ``bounds`` with clearance."""
    margin = CLEARANCE_FRACTION * diagonal_of(bounds)
    if primitive['shape'] == SPHERE:
        return {'radius': max(math.dist(corner, primitive['centre'])
                              for corner in corners(bounds)) + margin}
    if primitive['shape'] == CYLINDER:
        alongs, radials = zip(*(_axial(primitive, corner)
                                for corner in corners(bounds)))
        return {'radius': max(radials) + margin,
                'length': 2.0 * (max(abs(item) for item in alongs) + margin)}
    return {}


def check_containment(primitive, bounds):
    """Refuse a sphere or cylinder that does not hold every body."""
    if primitive['shape'] == BOX:
        return
    margin = CLEARANCE_FRACTION * diagonal_of(bounds)
    if primitive['clearance'] >= margin:
        return
    needed = required(primitive, bounds)
    shape = primitive['shape']
    if shape == SPHERE:
        raise FarfieldError(
            f'the farfield sphere of radius {primitive["radius"]:.6g} m does '
            'not contain the geometry with clearance: it needs a radius of at '
            f'least {needed["radius"]:.6g} m about this centre')
    short = []
    if primitive['radius'] < needed['radius']:
        short.append(f'a radius of at least {needed["radius"]:.6g} m '
                     f'(it has {primitive["radius"]:.6g} m)')
    if primitive['length'] < needed['length']:
        short.append(f'a length of at least {needed["length"]:.6g} m '
                     f'(it has {primitive["length"]:.6g} m)')
    raise FarfieldError(
        'the farfield cylinder does not contain the geometry with clearance: '
        f'along this axis it needs {" and ".join(short)}')


def suggest(shape, bounds, padding=DEFAULT_PADDING, axis=DEFAULT_AXIS):
    """Dimensions for ``shape`` that stand ``padding`` diagonals off the model.

    What a page offers when a user switches shape, so a new sphere or cylinder
    starts out containing the geometry rather than refused.
    """
    bounds = bounds_of(bounds)
    diagonal = diagonal_of(bounds)
    standoff = float(padding) * diagonal
    if shape == BOX:
        return {'padding': float(padding)}
    centre = centre_of({'centreMode': CENTRE_AUTO, 'centre': (0, 0, 0)},
                       bounds)
    probe = {'shape': shape, 'centre': list(centre), 'axis': list(unit(axis)),
             'radius': 0.0, 'length': 0.0}
    needed = required(probe, bounds)
    result = {'radius': needed['radius'] + standoff}
    if shape == CYLINDER:
        result['length'] = needed['length'] + 2 * standoff
    return result


def volume(primitive):
    shape = primitive['shape']
    if shape == BOX:
        span = primitive['span']
        return span[0] * span[1] * span[2]
    if shape == SPHERE:
        return 4.0 / 3.0 * math.pi * primitive['radius'] ** 3
    return math.pi * primitive['radius'] ** 2 * primitive['length']


def tolerance(primitive):
    """How far off the wall a point may lie and still be on it."""
    if primitive['shape'] == BOX:
        size = math.sqrt(sum(item * item for item in primitive['span']))
    elif primitive['shape'] == SPHERE:
        size = 2 * primitive['radius']
    else:
        size = math.hypot(2 * primitive['radius'], primitive['length'])
    return max(size, 1e-9) * 1e-6


def classify_point(primitive, point, within=None):
    """The patch name of the primitive face ``point`` lies on, else ``None``.

    Classification is geometric, from the primitive the runner built: a
    boolean renumbers every face, so a face is known by where it is, never by
    its position in an entity list.

    * box: ``far_field_xMin`` .. ``far_field_zMax`` by the plane (the names
      the runner has published since Plan 30 WP12);
    * sphere: ``far_field``;
    * cylinder: ``far_field_side`` on the curved wall, ``far_field_inlet`` on
      the cap at ``base`` and ``far_field_outlet`` on the cap along ``axis``.
      Inlet and outlet are names ordered along the axis, not boundary
      conditions.

    ``point`` must lie on the face (for a curved face its centre of mass does
    not); a point on no primitive face -- an inherited body face -- returns
    ``None``.
    """
    within = tolerance(primitive) if within is None else float(within)
    shape = primitive['shape']
    if shape == BOX:
        origin, span = primitive['origin'], primitive['span']
        for index, name in enumerate('xyz'):
            if abs(point[index] - origin[index]) <= within:
                return f'{FAR_FIELD}_{name}Min'
            if abs(point[index] - origin[index] - span[index]) <= within:
                return f'{FAR_FIELD}_{name}Max'
        return None
    if shape == SPHERE:
        if abs(math.dist(point, primitive['centre'])
               - primitive['radius']) <= within:
            return FAR_FIELD
        return None
    along, radial = _axial(primitive, point)
    half = primitive['length'] / 2.0
    if abs(along + half) <= within and radial <= primitive['radius'] + within:
        return CYLINDER_INLET
    if abs(along - half) <= within and radial <= primitive['radius'] + within:
        return CYLINDER_OUTLET
    if abs(radial - primitive['radius']) <= within and abs(along) <= half + within:
        return CYLINDER_SIDE
    return None


def patch_names(shape):
    """Every outer patch a primitive of ``shape`` publishes."""
    if shape == SPHERE:
        return (FAR_FIELD,)
    if shape == CYLINDER:
        return (CYLINDER_SIDE, CYLINDER_INLET, CYLINDER_OUTLET)
    return tuple(f'{FAR_FIELD}_{name}{end}' for name in 'xyz'
                 for end in ('Min', 'Max'))
