"""Geometry build operations on the resident Gmsh session (Plan 41 GN3).

Gmsh's Geometry module, ``Elementary entities > Add`` and ``Transform``, as one
function: ``apply(gmsh, kind, params)``.  An operation runs on the kernel the
project chose (Plan 48 D3): OpenCASCADE, the kernel of record and the default,
or the built-in kernel (``params['kernel'] == 'geo'``).  Each kind lists the
kernels it exists on (:data:`KERNELS_OF`); on the other kernel it is refused
with the reason.  The model is synchronised so the new entities are visible,
and the result is ``{"created": [[dim, tag], ...], ...}``.

The functions take plain numbers.  Named parameters (``G-01``) are resolved by
the client before the call; ``parameter`` also hands the value to Gmsh's own
parser (``gmsh.parser.setNumber``) so a ``.geo`` script written later carries
it.  Nothing here imports Gmsh: the module is given the API object, which is
what lets the tests drive it with a recording stand-in.
"""
from __future__ import annotations

import math

KINDS = (
    'parameter', 'point', 'line', 'circle_arc', 'ellipse_arc', 'spline',
    'bspline', 'bezier', 'curve_loop', 'plane_surface', 'surface_filling',
    'surface_loop', 'volume', 'rectangle', 'disk', 'box', 'sphere',
    'cylinder', 'cone', 'torus', 'wedge', 'translate', 'rotate', 'scale',
    'mirror', 'extrude', 'boolean', 'delete', 'farfield',
    # Plan 48 GM5
    'circle', 'ellipse', 'wire', 'split_curve', 'pipe', 'thrusections',
    'thick_solid', 'offset_curve', 'fillet', 'chamfer', 'fillet_2d',
    'chamfer_2d', 'defeature', 'to_nurbs', 'heal', 'surface_from_curves',
    'volume_from_surfaces', 'twist',
    # Plan 48 GM6
    'extrude_boundary_layer', 'case_2d', 'axisymmetric_wedge',
    # Plan 48 GB
    'bspline_filling', 'bezier_filling', 'bspline_surface', 'bezier_surface',
    'trimmed_surface', 'affine_transform', 'copy', 'polyline',
    'compound_spline', 'compound_bspline', 'point_on_geometry',
    'parametric_geometry')

#: The geometry kernels (Geometry > Elementary entities > Set geometry
#: kernel): OpenCASCADE and the built-in one.
KERNELS = ('occ', 'geo')
KERNEL_TITLES = {'occ': 'OpenCASCADE', 'geo': 'Built-in'}

_BOTH = ('occ', 'geo')
#: kind -> the kernels it exists on.  A kind absent from this table is
#: OpenCASCADE only.  ``tests/plan48/gm5`` checks the client catalogue
#: (``core/gmsh/geometry_build.py``) says the same.
KERNELS_OF = {
    'parameter': _BOTH, 'point': _BOTH, 'line': _BOTH, 'circle_arc': _BOTH,
    'ellipse_arc': _BOTH, 'spline': _BOTH, 'bspline': _BOTH,
    'bezier': _BOTH, 'curve_loop': _BOTH, 'plane_surface': _BOTH,
    'surface_filling': _BOTH, 'surface_loop': _BOTH, 'volume': _BOTH,
    'translate': _BOTH, 'rotate': _BOTH, 'scale': _BOTH, 'mirror': _BOTH,
    'extrude': _BOTH, 'delete': _BOTH, 'surface_from_curves': _BOTH,
    'volume_from_surfaces': _BOTH,
    'split_curve': ('geo',), 'twist': ('geo',),
    'extrude_boundary_layer': ('geo',), 'case_2d': _BOTH,
    'axisymmetric_wedge': _BOTH,
    # Plan 48 GB: a copy exists on both kernels; the fillings, the surfaces
    # from control points and the affine transformation are OpenCASCADE only
    # (absent here); the polyline, the compound splines and the point on a
    # geometry are built-in kernel operations.
    'copy': _BOTH, 'polyline': ('geo',), 'compound_spline': ('geo',),
    'compound_bspline': ('geo',), 'point_on_geometry': ('geo',),
    'parametric_geometry': ('geo',),
}

#: Why an operation is not offered on a kernel (the panel greys it with this).
_WHY_NOT_BUILTIN = {
    'rectangle': 'the built-in kernel has no primitive solids or surfaces '
                 '(rectangle, disk, box, sphere, cylinder, cone, torus, '
                 'wedge); build them from points, lines and extrusions',
    'booleans': 'the built-in kernel has no Boolean operations (union, '
                'intersection, difference, fragments); they are '
                'OpenCASCADE operations',
    'shape': 'this is an OpenCASCADE shape operation; the built-in kernel '
             'has no equivalent',
}
_WHY_NOT_OCC = {
    'split_curve': 'Split curve is a built-in kernel operation; '
                   'OpenCASCADE has no equivalent',
    'twist': 'Twist is a built-in kernel operation; Gmsh refuses a '
             'twisting extrusion with OpenCASCADE geometry',
    'extrude_boundary_layer': 'Extrude along normals is a built-in kernel '
                              'operation (geo.extrudeBoundaryLayer); '
                              'OpenCASCADE has no equivalent',
    'polyline': 'Polyline is a built-in kernel operation; OpenCASCADE has '
                'no equivalent (use a Spline, or a Wire of lines)',
    'compound_spline': 'Compound spline is a built-in kernel operation; '
                       'OpenCASCADE has no equivalent',
    'compound_bspline': 'Compound B-spline is a built-in kernel operation; '
                        'OpenCASCADE has no equivalent',
    'point_on_geometry': 'Point on a geometry is a built-in kernel '
                         'operation; OpenCASCADE has no equivalent',
    'parametric_geometry': 'A parametric geometry is a built-in kernel '
                           'operation; OpenCASCADE has no equivalent',
}
_OCC_GROUP = {
    'rectangle': 'rectangle', 'disk': 'rectangle', 'box': 'rectangle',
    'sphere': 'rectangle', 'cylinder': 'rectangle', 'cone': 'rectangle',
    'torus': 'rectangle', 'wedge': 'rectangle', 'boolean': 'booleans',
    'farfield': 'booleans',
}


def kernel_refusal(kind: str, kernel: str) -> str:
    """The reason *kind* is not available on *kernel*, or ``''``."""
    if kernel not in KERNELS:
        return 'the geometry kernel must be one of %s' % ', '.join(KERNELS)
    if kernel in KERNELS_OF.get(kind, ('occ',)):
        return ''
    if kernel == 'geo':
        return _WHY_NOT_BUILTIN[_OCC_GROUP.get(kind, 'shape')]
    return _WHY_NOT_OCC.get(kind, 'this operation exists only on the '
                                  'built-in kernel')

BOOLEANS = ('fuse', 'intersect', 'cut', 'fragment')
EXTRUDE_MODES = ('translate', 'rotate', 'normal')


class BuildError(ValueError):
    """The operation cannot be done; the message says why."""


def _dimtags(items):
    out = []
    for item in items or ():
        dim, tag = int(item[0]), int(item[1])
        out.append((dim, tag))
    if not out:
        raise BuildError('no entities were given')
    return out


def _tags(values, what):
    tags = [int(v) for v in values or ()]
    if not tags:
        raise BuildError('give at least one %s' % what)
    return tags


def _need(params, *names):
    missing = [n for n in names if params.get(n) is None]
    if missing:
        raise BuildError('missing %s' % ', '.join(missing))
    return [params[n] for n in names]


def _created(items):
    return [[int(d), int(t)] for d, t in items]


def _done(gmsh, created=(), **extra):
    gmsh.model.occ.synchronize()
    out = {'created': _created(created)}
    out.update(extra)
    return out


def _done_geo(gmsh, created=(), **extra):
    gmsh.model.geo.synchronize()
    out = {'created': _created(created), 'kernel': 'geo'}
    out.update(extra)
    return out


def _require_occ(gmsh, selection=()):
    """Refuse OCC operations on a model that holds only a discrete mesh."""
    if not hasattr(gmsh.model, 'occ'):
        raise BuildError('this Gmsh has no OpenCASCADE kernel')
    for dim, tag in selection:
        try:
            kind = gmsh.model.getType(dim, tag)
        except Exception:                                  # noqa: BLE001
            kind = ''
        if kind in ('Discrete surface', 'Discrete curve', 'Discrete volume',
                    'discrete'):
            raise BuildError(
                'entity %s%d is a discrete (mesh) entity, which the '
                'OpenCASCADE kernel cannot transform or combine; build from '
                'STEP/IGES/BREP geometry or from primitives'
                % ('PCSV'[dim], tag))
    _need_kernel(gmsh, selection, 'occ', 'this operation')


def _tag(params):
    return int(params.get('tag', -1) if params.get('tag') is not None
               else -1)


def apply(gmsh, kind: str, params: dict) -> dict:
    """Run one operation on the kernel in ``params['kernel']`` (default
    OpenCASCADE).  An operation the kernel lacks is refused with the reason."""
    params = dict(params or {})
    kernel = str(params.pop('kernel', None) or 'occ')
    if kind not in _HANDLERS and kind not in _GEO_HANDLERS:
        raise BuildError('unknown geometry operation %r' % kind)
    reason = kernel_refusal(kind, kernel)
    if reason:
        raise BuildError(reason)
    if kernel == 'geo':
        if not hasattr(gmsh.model, 'geo'):
            raise BuildError('this Gmsh has no built-in geometry kernel')
        handler = _GEO_HANDLERS.get(kind)
        if handler is None:
            raise BuildError(kernel_refusal(kind, 'geo')
                             or 'unknown geometry operation %r' % kind)
        return handler(gmsh, params)
    _require_occ(gmsh)
    return _HANDLERS[kind](gmsh, params)


def entity_kernel(gmsh, dim: int, tag: int) -> str:
    """``'geo'`` when the entity is in the model but OpenCASCADE did not
    build it (the built-in kernel or a discrete entity), else ``'occ'``.
    Gmsh cannot mix the two kernels in one operation, so the operations check
    this and say so.  An entity nobody can place is not refused here: Gmsh
    says what is wrong with it."""
    try:
        in_model = {(int(d), int(t)) for d, t in gmsh.model.getEntities(dim)}
        in_occ = {(int(d), int(t)) for d, t in gmsh.model.occ.getEntities(dim)}
    except Exception:                                      # noqa: BLE001
        return 'occ'
    key = (int(dim), int(tag))
    return 'geo' if key in in_model and key not in in_occ else 'occ'


def _need_kernel(gmsh, selection, kernel: str, what: str) -> None:
    """Refuse a selection that the other kernel built."""
    other = 'geo' if kernel == 'occ' else 'occ'
    clash = [(d, t) for d, t in selection
             if d >= 0 and entity_kernel(gmsh, d, t) == other]
    if clash:
        raise BuildError(
            '%s needs entities built by the %s kernel, but %s %s built by '
            'the %s kernel; Gmsh cannot mix the two kernels in one '
            'operation (rebuild them on the %s kernel, or switch the '
            'project kernel)' % (
                what, KERNEL_TITLES[kernel],
                ', '.join('%s%d' % ('PCSV'[d], t) for d, t in clash),
                'was' if len(clash) == 1 else 'were',
                KERNEL_TITLES[other], KERNEL_TITLES[kernel]))


# -- parameters and points --------------------------------------------------- #

def _parameter(gmsh, p):
    name, value = _need(p, 'name', 'value')
    if not str(name).isidentifier():
        raise BuildError('%r is not a valid parameter name' % (name,))
    gmsh.parser.setNumber(str(name), [float(value)])
    # Gmsh's Parameter dialog (scriptAddParameter): the ONELAB name is
    # path/label, path/name when there is no label, label alone when there
    # is no path, and the name itself when there is neither.
    onelab = onelab_name(str(name), p.get('label'), p.get('path'))
    gmsh.onelab.setNumber(onelab, [float(value)])
    return {'created': [], 'name': str(name), 'value': float(value),
            'onelab': onelab}


def onelab_name(name: str, label=None, path=None) -> str:
    label = str(label or '').strip()
    path = str(path or '').strip()
    if path and label:
        return '%s/%s' % (path, label)
    if path:
        return '%s/%s' % (path, name)
    return label or name


def _point(gmsh, p):
    x, y, z = _need(p, 'x', 'y', 'z')
    size = float(p.get('size') or 0.0)
    tag = gmsh.model.occ.addPoint(float(x), float(y), float(z), size,
                                  _tag(p))
    return _done(gmsh, [(0, tag)])


# -- curves ------------------------------------------------------------------ #

def _line(gmsh, p):
    a, b = _need(p, 'start', 'end')
    if int(a) == int(b):
        raise BuildError('a line needs two different points')
    return _done(gmsh, [(1, gmsh.model.occ.addLine(int(a), int(b),
                                                   _tag(p)))])


def _circle_arc(gmsh, p):
    a, c, b = _need(p, 'start', 'center', 'end')
    return _done(gmsh, [(1, gmsh.model.occ.addCircleArc(
        int(a), int(c), int(b), _tag(p)))])


def _ellipse_arc(gmsh, p):
    a, c, m, b = _need(p, 'start', 'center', 'major', 'end')
    return _done(gmsh, [(1, gmsh.model.occ.addEllipseArc(
        int(a), int(c), int(m), int(b), _tag(p)))])


def _through_points(name, minimum, call):
    def build(gmsh, p):
        points = _tags(p.get('points'), 'point')
        if len(points) < minimum:
            raise BuildError('a %s needs at least %d points' % (name,
                                                                 minimum))
        return _done(gmsh, [(1, call(gmsh.model.occ, points, _tag(p)))])
    return build


# -- loops, surfaces, volumes ------------------------------------------------ #

def _curve_loop(gmsh, p):
    curves = _tags(p.get('curves'), 'curve')
    return _done(gmsh, [], loop=int(gmsh.model.occ.addCurveLoop(
        curves, _tag(p))))


def _plane_surface(gmsh, p):
    loops = _tags(p.get('loops'), 'curve loop')
    return _done(gmsh, [(2, gmsh.model.occ.addPlaneSurface(loops,
                                                           _tag(p)))])


def _surface_filling(gmsh, p):
    loops = _tags(p.get('loops'), 'curve loop')
    return _done(gmsh, [(2, gmsh.model.occ.addSurfaceFilling(
        loops[0], _tag(p)))])


def _surface_loop(gmsh, p):
    surfaces = _tags(p.get('surfaces'), 'surface')
    return _done(gmsh, [], loop=int(gmsh.model.occ.addSurfaceLoop(
        surfaces, _tag(p))))


def _volume(gmsh, p):
    shells = _tags(p.get('shells'), 'surface loop')
    return _done(gmsh, [(3, gmsh.model.occ.addVolume(shells, _tag(p)))])


# -- primitives -------------------------------------------------------------- #

def _positive(p, *names):
    for name in names:
        if float(p[name]) <= 0:
            raise BuildError('%s must be greater than zero' % name)


def _rectangle(gmsh, p):
    x, y, z, dx, dy = _need(p, 'x', 'y', 'z', 'dx', 'dy')
    _positive(p, 'dx', 'dy')
    return _done(gmsh, [(2, gmsh.model.occ.addRectangle(
        x, y, z, dx, dy, _tag(p), float(p.get('rounded') or 0.0)))])


def _disk(gmsh, p):
    x, y, z, rx = _need(p, 'x', 'y', 'z', 'rx')
    ry = float(p.get('ry') or rx)
    _positive(p, 'rx')
    return _done(gmsh, [(2, gmsh.model.occ.addDisk(x, y, z, rx, ry,
                                                   _tag(p)))])


def _box(gmsh, p):
    x, y, z, dx, dy, dz = _need(p, 'x', 'y', 'z', 'dx', 'dy', 'dz')
    return _done(gmsh, [(3, gmsh.model.occ.addBox(x, y, z, dx, dy, dz,
                                                  _tag(p)))])


def _sphere(gmsh, p):
    x, y, z, r = _need(p, 'x', 'y', 'z', 'r')
    _positive(p, 'r')
    lat1 = math.radians(float(p.get('lat1') if p.get('lat1') is not None
                              else -90.0))
    lat2 = math.radians(float(p.get('lat2') if p.get('lat2') is not None
                              else 90.0))
    if not (-math.pi / 2 - 1e-12 <= lat1 < lat2 <= math.pi / 2 + 1e-12):
        raise BuildError('the sphere latitudes must satisfy '
                         '-90 <= lat1 < lat2 <= 90 degrees')
    return _done(gmsh, [(3, gmsh.model.occ.addSphere(
        x, y, z, r, _tag(p), lat1, lat2, _angle(p, 'angle', 360.0)))])


def _angle(p, name, default):
    """Degrees in, radians out; a full turn is ``2 pi``."""
    value = p.get(name)
    value = default if value is None else float(value)
    if not 0 < value <= 360:
        raise BuildError('%s must be in (0, 360] degrees' % name)
    return math.radians(value)


def _cylinder(gmsh, p):
    x, y, z, dx, dy, dz, r = _need(p, 'x', 'y', 'z', 'dx', 'dy', 'dz', 'r')
    _positive(p, 'r')
    return _done(gmsh, [(3, gmsh.model.occ.addCylinder(
        x, y, z, dx, dy, dz, r, _tag(p), _angle(p, 'angle', 360.0)))])


def _cone(gmsh, p):
    x, y, z, dx, dy, dz, r1, r2 = _need(p, 'x', 'y', 'z', 'dx', 'dy', 'dz',
                                        'r1', 'r2')
    if float(r1) < 0 or float(r2) < 0 or (float(r1) == 0 == float(r2)):
        raise BuildError('a cone needs non-negative radii, not both zero')
    return _done(gmsh, [(3, gmsh.model.occ.addCone(
        x, y, z, dx, dy, dz, r1, r2, _tag(p), _angle(p, 'angle', 360.0)))])


def _torus(gmsh, p):
    x, y, z, r1, r2 = _need(p, 'x', 'y', 'z', 'r1', 'r2')
    _positive(p, 'r1', 'r2')
    return _done(gmsh, [(3, gmsh.model.occ.addTorus(
        x, y, z, r1, r2, _tag(p), _angle(p, 'angle', 360.0)))])


def _wedge(gmsh, p):
    x, y, z, dx, dy, dz = _need(p, 'x', 'y', 'z', 'dx', 'dy', 'dz')
    return _done(gmsh, [(3, gmsh.model.occ.addWedge(
        x, y, z, dx, dy, dz, _tag(p), float(p.get('ltx') or 0.0)))])


# -- transforms -------------------------------------------------------------- #

def _transform(name):
    def run(gmsh, p):
        selection = _dimtags(p.get('entities'))
        _require_occ(gmsh, selection)
        occ = gmsh.model.occ
        target = selection
        created = []
        if p.get('copy'):
            target = [(int(d), int(t)) for d, t in occ.copy(selection)]
            created = target
        if name == 'translate':
            dx, dy, dz = _need(p, 'dx', 'dy', 'dz')
            occ.translate(target, dx, dy, dz)
        elif name == 'rotate':
            x, y, z, ax, ay, az, angle = _need(p, 'x', 'y', 'z', 'ax', 'ay',
                                               'az', 'angle')
            if not any((ax, ay, az)):
                raise BuildError('the rotation axis is the zero vector')
            occ.rotate(target, x, y, z, ax, ay, az, math.radians(angle))
        elif name == 'scale':
            x, y, z, a, b, c = _need(p, 'x', 'y', 'z', 'a', 'b', 'c')
            if 0 in (float(a), float(b), float(c)):
                raise BuildError('a scale factor of zero collapses the '
                                 'entity')
            occ.dilate(target, x, y, z, a, b, c)
        else:
            a, b, c, d = _need(p, 'a', 'b', 'c', 'd')
            if not any((a, b, c)):
                raise BuildError('the mirror plane normal is the zero '
                                 'vector')
            occ.mirror(target, a, b, c, d)
        return _done(gmsh, created, moved=_created(target))
    return run


# -- extrusion --------------------------------------------------------------- #

def _layers(p):
    """``(numElements, heights)`` of an extrusion.

    A layer table (``layer_counts`` with the cumulative ``layer_heights``, a
    fraction of the whole height rising to 1) wins over the plain ``layers``
    count (Plan 48 GM6: Gmsh's ``Layers{{n1, n2}, {h1, h2}}``).
    """
    counts = p.get('layer_counts')
    heights = p.get('layer_heights')
    if counts or heights:
        return _layer_table(counts, heights)
    layers = int(p.get('layers') or 0)
    if layers < 0:
        raise BuildError('layers cannot be negative')
    return ([layers] if layers else []), ([1.0] if layers else [])


def _layer_table(counts, heights):
    counts = [int(c) for c in counts or ()]
    heights = [float(h) for h in heights or ()]
    if not counts or len(counts) != len(heights):
        raise BuildError('the layer table needs a count and a cumulative '
                         'height for every row')
    if any(c < 1 for c in counts):
        raise BuildError('every row of the layer table needs at least one '
                         'layer')
    last = 0.0
    for h in heights:
        if not (last < h <= 1.0 + 1e-9):
            raise BuildError('the cumulative heights of the layer table must '
                             'rise and end at 1 (the whole extrusion)')
        last = h
    if abs(heights[-1] - 1.0) > 1e-9:
        raise BuildError('the last cumulative height of the layer table must '
                         'be 1 (the whole extrusion)')
    heights[-1] = 1.0
    return counts, heights


def _layer_total(p):
    counts, _heights = _layers(p)
    return int(sum(counts))


def _curve_ends(gmsh, kernel, tag):
    """Start and end point of a curve, ``(xyz, xyz)``."""
    getattr(gmsh.model, kernel).synchronize()
    ends = gmsh.model.getBoundary([(1, int(tag))], False, False, False)
    ends = [int(t) for d, t in ends if int(d) == 0]
    if len(ends) < 2 or ends[0] == ends[-1]:
        raise BuildError('curve %d is closed (or has no end points) and so '
                         'has no direction; pick an open curve or type the '
                         'vector' % int(tag))
    a = [float(v) for v in gmsh.model.getValue(0, ends[0], [])[:3]]
    b = [float(v) for v in gmsh.model.getValue(0, ends[-1], [])[:3]]
    return a, b


def _picked(gmsh, p, kernel):
    """Fill the vector or the axis of a sweep from a picked curve.

    ``direction_curve``: dx, dy, dz are the chord from the start to the end of
    the curve (times ``direction_scale``, negated by ``direction_reverse``);
    ``axis_curve``: the axis passes through the start point along the chord.
    A curve picked as well as typed numbers: the picked curve wins.
    """
    p = dict(p)
    scale = float(p.get('direction_scale') or 1.0)
    if p.get('direction_reverse'):
        scale = -scale
    if p.get('direction_curve') not in (None, '', 0, []):
        a, b = _curve_ends(gmsh, kernel, _one_tag(p['direction_curve']))
        for name, i in (('dx', 0), ('dy', 1), ('dz', 2)):
            p[name] = (b[i] - a[i]) * scale
    if p.get('axis_curve') not in (None, '', 0, []):
        a, b = _curve_ends(gmsh, kernel, _one_tag(p['axis_curve']))
        for name, i in (('x', 0), ('y', 1), ('z', 2)):
            p[name] = a[i]
        for name, i in (('ax', 0), ('ay', 1), ('az', 2)):
            p[name] = b[i] - a[i]
    return p


def _one_tag(value):
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise BuildError('pick exactly one curve')
        value = value[0]
    return int(value)


def _extrude(gmsh, p):
    selection = _dimtags(p.get('entities'))
    _require_occ(gmsh, selection)
    if p.get('quadtri') or p.get('scale_last'):
        raise BuildError('QuadTri and ScaleLast are built-in kernel '
                         'extrusion keywords; choose the built-in kernel '
                         'for this extrusion')
    p = _picked(gmsh, p, 'occ')
    mode = str(p.get('mode') or 'translate')
    if mode not in EXTRUDE_MODES:
        raise BuildError('extrusion mode must be one of %s'
                         % ', '.join(EXTRUDE_MODES))
    counts, heights = _layers(p)
    recombine = bool(p.get('recombine'))
    occ = gmsh.model.occ
    if mode == 'translate':
        dx, dy, dz = _need(p, 'dx', 'dy', 'dz')
        if not any((dx, dy, dz)):
            raise BuildError('the extrusion vector is zero')
        out = occ.extrude(selection, dx, dy, dz, counts, heights, recombine)
    elif mode == 'rotate':
        x, y, z, ax, ay, az, angle = _need(p, 'x', 'y', 'z', 'ax', 'ay',
                                           'az', 'angle')
        if not any((ax, ay, az)):
            raise BuildError('the rotation axis is the zero vector')
        out = occ.revolve(selection, x, y, z, ax, ay, az,
                          math.radians(angle), counts, heights, recombine)
    else:
        distance = float(_need(p, 'distance')[0])
        if distance == 0:
            raise BuildError('the extrusion distance is zero')
        out = []
        for dim, tag in selection:
            if dim != 2:
                raise BuildError('extrusion along the normal needs surfaces')
            occ.synchronize()
            box = gmsh.model.getBoundingBox(dim, tag)
            centre = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
            closest, uv = gmsh.model.getClosestPoint(dim, tag, centre)
            normal = gmsh.model.getNormal(tag, list(uv))
            vec = [float(n) * distance for n in normal[:3]]
            out.extend(occ.extrude([(dim, tag)], vec[0], vec[1], vec[2],
                                   counts, heights, recombine))
    return _done(gmsh, [(int(d), int(t)) for d, t in out],
                 layers=_layer_total(p), recombine=recombine)


# -- booleans, delete -------------------------------------------------------- #

def _boolean(gmsh, p):
    how = str(p.get('operation') or '')
    if how not in BOOLEANS:
        raise BuildError('boolean operation must be one of %s'
                         % ', '.join(BOOLEANS))
    objects = _dimtags(p.get('objects'))
    # Fragments work on the objects alone (Gmsh's Boolean > Fragments with no
    # tool entities); the other three need a tool.
    tools = (_dimtags(p.get('tools')) if p.get('tools') or how != 'fragment'
             else [])
    _require_occ(gmsh, objects + tools)
    remove_object = bool(p.get('remove_object', True))
    remove_tool = bool(p.get('remove_tool', True))
    occ = gmsh.model.occ
    function = {'fuse': occ.fuse, 'intersect': occ.intersect,
                'cut': occ.cut, 'fragment': occ.fragment}[how]
    out, _map = function(objects, tools, -1, remove_object, remove_tool)
    if not out:
        raise BuildError('the %s left nothing: the objects and tools do not '
                         'overlap as the operation needs' % how)
    return _done(gmsh, [(int(d), int(t)) for d, t in out], operation=how)


def _delete(gmsh, p):
    selection = _dimtags(p.get('entities'))
    gmsh.model.occ.remove(selection, bool(p.get('recursive')))
    return _done(gmsh, [], deleted=_created(selection))


# -- the farfield preset ----------------------------------------------------- #

def _farfield(gmsh, p):
    """A box, sphere or cylinder around the model, with the bodies cut out.

    The same shared specification the Preparation farfield uses
    (``farfield_primitives``), here as a named preset of primitive plus cut.
    """
    try:
        import farfield_primitives
    except ImportError:                                    # pragma: no cover
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import farfield_primitives
    occ = gmsh.model.occ
    bodies = gmsh.model.getEntities(3)
    if not bodies:
        raise BuildError('there is no solid to put a farfield around')
    bounds = gmsh.model.getBoundingBox(-1, -1)
    spec = {'shape': p.get('shape') or 'box',
            'padding': p.get('padding', 2.0)}
    for key in ('radius', 'length', 'axis', 'centre', 'centreMode'):
        if p.get(key) is not None:
            spec[key] = p[key]
    try:
        primitive = farfield_primitives.resolve(spec, bounds)
    except farfield_primitives.FarfieldError as error:
        raise BuildError(str(error)) from error
    shape = primitive['shape']
    if shape == farfield_primitives.BOX:
        tool = occ.addBox(*primitive['origin'], *primitive['span'])
    elif shape == farfield_primitives.SPHERE:
        tool = occ.addSphere(*primitive['centre'], primitive['radius'])
    else:
        length = primitive['length']
        tool = occ.addCylinder(*primitive['base'],
                               *(a * length for a in primitive['axis']),
                               primitive['radius'])
    out, _map = occ.cut([(3, tool)], list(bodies), -1, True, True)
    if not out:
        raise BuildError('cutting the %s against the geometry left no '
                         'volume' % shape)
    return _done(gmsh, [(int(d), int(t)) for d, t in out], shape=shape)


# -- Plan 48 GM5: the rest of Geometry > Elementary entities ----------------- #

TRIHEDRA = ('', 'DiscreteTrihedron', 'CorrectedFrenet', 'Fixed', 'Frenet',
            'ConstantNormal', 'Darboux', 'GuideAC', 'GuidePlan',
            'GuideACWithContact', 'GuidePlanWithContact')
CONTINUITIES = ('', 'C0', 'G1', 'C1', 'G2', 'C2', 'C3', 'CN')
PARAMETRIZATIONS = ('', 'ChordLength', 'Centripetal', 'IsoParametric')


def _floats(value, what):
    """``'0.1, 0.2'`` / ``[0.1, 0.2]`` / ``0.1`` to ``[0.1, 0.2]``."""
    if value is None or value == '':
        raise BuildError('give at least one %s' % what)
    if isinstance(value, (int, float)):
        items = [value]
    elif isinstance(value, str):
        items = [v for v in value.replace(';', ',').replace(' ', ',').split(',')
                 if v]
    else:
        items = list(value)
    try:
        numbers = [float(v) for v in items]
    except (TypeError, ValueError):
        raise BuildError('%s must be numbers separated by commas' % what
                         ) from None
    if not numbers:
        raise BuildError('give at least one %s' % what)
    return numbers


def _optional_vector(p, a, b, c):
    values = [p.get(a), p.get(b), p.get(c)]
    if all(v is None or float(v) == 0.0 for v in values):
        return []
    return [float(v or 0.0) for v in values]


def _arc_angles(p):
    a1 = float(p.get('angle1') if p.get('angle1') is not None else 0.0)
    a2 = float(p.get('angle2') if p.get('angle2') is not None else 360.0)
    if not 0 <= a1 < a2 <= 360:
        raise BuildError('the arc angles must satisfy 0 <= start < end '
                         '<= 360 degrees')
    return math.radians(a1), math.radians(a2)


def _circle(gmsh, p):
    x, y, z, r = _need(p, 'x', 'y', 'z', 'r')
    _positive(p, 'r')
    a1, a2 = _arc_angles(p)
    tag = gmsh.model.occ.addCircle(
        x, y, z, r, _tag(p), a1, a2, _optional_vector(p, 'zx', 'zy', 'zz'),
        _optional_vector(p, 'xx', 'xy', 'xz'))
    return _done(gmsh, [(1, tag)])


def _ellipse(gmsh, p):
    x, y, z, r1, r2 = _need(p, 'x', 'y', 'z', 'r1', 'r2')
    _positive(p, 'r1', 'r2')
    if float(r2) > float(r1):
        raise BuildError('the major radius (r1) must be at least the minor '
                         'radius (r2); turn the ellipse with its X axis '
                         'instead of swapping them')
    a1, a2 = _arc_angles(p)
    tag = gmsh.model.occ.addEllipse(
        x, y, z, r1, r2, _tag(p), a1, a2,
        _optional_vector(p, 'zx', 'zy', 'zz'),
        _optional_vector(p, 'xx', 'xy', 'xz'))
    return _done(gmsh, [(1, tag)])


def _wire(gmsh, p):
    curves = _tags(p.get('curves'), 'curve')
    _require_occ(gmsh, [(1, c) for c in curves])
    wire = gmsh.model.occ.addWire(curves, _tag(p),
                                  bool(p.get('check_closed')))
    return _done(gmsh, [], wire=int(wire))


def _wire_of(gmsh, curves):
    curves = [int(c) for c in curves]
    _require_occ(gmsh, [(1, c) for c in curves])
    return int(gmsh.model.occ.addWire(curves, -1, False))


def _pipe(gmsh, p):
    profile = _dimtags(p.get('profile'))
    _require_occ(gmsh, profile)
    trihedron = str(p.get('trihedron') or '')
    if trihedron not in TRIHEDRA:
        raise BuildError('the trihedron must be one of %s'
                         % ', '.join(t or '(default)' for t in TRIHEDRA))
    if p.get('wire') not in (None, '', 0):
        wire = int(p['wire'])
    else:
        wire = _wire_of(gmsh, _tags(p.get('path'), 'path curve'))
    out = gmsh.model.occ.addPipe(profile, wire, trihedron)
    return _done(gmsh, [(int(d), int(t)) for d, t in out], wire=wire,
                 trihedron=trihedron or 'DiscreteTrihedron')


def _thrusections(gmsh, p):
    wires = [int(w) for w in (p.get('wires') or ())]
    for section in p.get('sections') or ():
        wires.append(_wire_of(gmsh, section))
    if len(wires) < 2:
        raise BuildError('a loft needs at least two sections (wires or '
                         'curve loops)')
    continuity = str(p.get('continuity') or '')
    parametrization = str(p.get('parametrization') or '')
    if continuity not in CONTINUITIES:
        raise BuildError('continuity must be one of %s'
                         % ', '.join(c or '(default)' for c in CONTINUITIES))
    if parametrization not in PARAMETRIZATIONS:
        raise BuildError('parametrization must be one of %s' % ', '.join(
            c or '(default)' for c in PARAMETRIZATIONS))
    out = gmsh.model.occ.addThruSections(
        wires, _tag(p), bool(p.get('make_solid', True)),
        bool(p.get('make_ruled')), int(p.get('max_degree') or -1),
        continuity, parametrization, bool(p.get('smoothing')))
    return _done(gmsh, [(int(d), int(t)) for d, t in out], wires=wires)


def _thick_solid(gmsh, p):
    volumes = _tags(p.get('volume'), 'volume')
    if len(volumes) != 1:
        raise BuildError('a thick solid is made from one volume')
    offset = float(_need(p, 'offset')[0])
    if offset == 0:
        raise BuildError('the thickness (offset) is zero')
    excluded = [int(t) for t in p.get('exclude') or ()]
    _require_occ(gmsh, [(3, volumes[0])] + [(2, t) for t in excluded])
    out = gmsh.model.occ.addThickSolid(volumes[0], excluded, offset,
                                       _tag(p))
    return _done(gmsh, [(int(d), int(t)) for d, t in out])


def _offset_curve(gmsh, p):
    curves = _tags(p.get('curves'), 'curve')
    offset = float(_need(p, 'offset')[0])
    if offset == 0:
        raise BuildError('the offset is zero')
    _require_occ(gmsh, [(1, c) for c in curves])
    loop = int(gmsh.model.occ.addCurveLoop(curves, -1))
    out = gmsh.model.occ.offsetCurve(loop, offset)
    return _done(gmsh, [(int(d), int(t)) for d, t in out], loop=loop)


def _fillet(gmsh, p):
    volumes = _tags(p.get('volumes'), 'volume')
    curves = _tags(p.get('curves'), 'curve')
    radii = _floats(p.get('radii'), 'radius')
    if any(r <= 0 for r in radii):
        raise BuildError('a fillet radius must be greater than zero')
    if len(radii) not in (1, len(curves)):
        raise BuildError('give one radius for all curves or one per curve '
                         '(%d curves, %d radii)' % (len(curves), len(radii)))
    _require_occ(gmsh, [(3, v) for v in volumes] + [(1, c) for c in curves])
    out = gmsh.model.occ.fillet(volumes, curves, radii,
                                bool(p.get('remove_volume', True)))
    return _done(gmsh, [(int(d), int(t)) for d, t in out])


def _chamfer(gmsh, p):
    volumes = _tags(p.get('volumes'), 'volume')
    curves = _tags(p.get('curves'), 'curve')
    surfaces = _tags(p.get('surfaces'), 'surface')
    distances = _floats(p.get('distances'), 'distance')
    if any(d <= 0 for d in distances):
        raise BuildError('a chamfer distance must be greater than zero')
    if len(curves) != len(surfaces):
        raise BuildError(
            'a chamfer pairs every curve with the surface it is measured '
            'on: pick as many surfaces as curves (%d curves, %d surfaces)'
            % (len(curves), len(surfaces)))
    if len(distances) not in (1, len(curves)):
        raise BuildError('give one distance for all curves or one per '
                         'curve (%d curves, %d distances)'
                         % (len(curves), len(distances)))
    if len(distances) == 1:
        distances = distances * len(curves)
    _require_occ(gmsh, [(3, v) for v in volumes] + [(1, c) for c in curves]
                 + [(2, s) for s in surfaces])
    out = gmsh.model.occ.chamfer(volumes, curves, surfaces, distances,
                                 bool(p.get('remove_volume', True)))
    return _done(gmsh, [(int(d), int(t)) for d, t in out])


def _two_curves(p, what):
    curves = _tags(p.get('curves'), 'curve')
    if len(curves) != 2:
        raise BuildError('a 2D %s joins exactly two curves that share a '
                         'point (%d picked)' % (what, len(curves)))
    return curves


def _fillet_2d(gmsh, p):
    a, b = _two_curves(p, 'fillet')
    radius = float(_need(p, 'radius')[0])
    if radius <= 0:
        raise BuildError('the fillet radius must be greater than zero')
    _require_occ(gmsh, [(1, a), (1, b)])
    tag = gmsh.model.occ.fillet2D(a, b, radius, _tag(p), -1,
                                  bool(p.get('reverse')))
    return _done(gmsh, [(1, tag)])


def _chamfer_2d(gmsh, p):
    a, b = _two_curves(p, 'chamfer')
    d1 = float(_need(p, 'distance1')[0])
    d2 = float(p.get('distance2') or d1)
    if d1 <= 0 or d2 <= 0:
        raise BuildError('the chamfer distances must be greater than zero')
    _require_occ(gmsh, [(1, a), (1, b)])
    tag = gmsh.model.occ.chamfer2D(a, b, d1, d2, _tag(p))
    return _done(gmsh, [(1, tag)])


def _defeature(gmsh, p):
    volumes = _tags(p.get('volumes'), 'volume')
    surfaces = _tags(p.get('surfaces'), 'surface')
    _require_occ(gmsh, [(3, v) for v in volumes]
                 + [(2, s) for s in surfaces])
    out = gmsh.model.occ.defeature(volumes, surfaces,
                                   bool(p.get('remove_volume', True)))
    return _done(gmsh, [(int(d), int(t)) for d, t in out])


def _to_nurbs(gmsh, p):
    selection = _dimtags(p.get('entities'))
    _require_occ(gmsh, selection)
    gmsh.model.occ.convertToNURBS(selection)
    return _done(gmsh, [], converted=_created(selection))


def _heal(gmsh, p):
    selection = [(int(d), int(t)) for d, t in p.get('entities') or ()]
    _require_occ(gmsh, selection)
    tolerance = float(p.get('tolerance') if p.get('tolerance') is not None
                      else 1e-8)
    if tolerance <= 0:
        raise BuildError('the healing tolerance must be greater than zero')
    out = gmsh.model.occ.healShapes(
        selection, tolerance, bool(p.get('fix_degenerated', True)),
        bool(p.get('fix_small_edges', True)),
        bool(p.get('fix_small_faces', True)), bool(p.get('sew_faces', True)),
        bool(p.get('make_solids', True)))
    dims = {int(d) for d, _t in selection}
    top = sorted({(int(d), int(t)) for d, t in out if int(d) in dims})
    return _done(gmsh, top, healed=_created(selection))


def _surface_from_curves(ns_name):
    def build(gmsh, p):
        curves = _tags(p.get('curves'), 'curve')
        if ns_name == 'occ':
            _require_occ(gmsh, [(1, c) for c in curves])
        else:
            _need_kernel(gmsh, [(1, c) for c in curves], 'geo', 'a surface')
        ns = getattr(gmsh.model, ns_name)
        loop = int(ns.addCurveLoop(curves, _tag(p)))
        surface = int(ns.addPlaneSurface([loop], -1))
        done = _done if ns_name == 'occ' else _done_geo
        return done(gmsh, [(2, surface)], loop=loop)
    return build


def _volume_from_surfaces(ns_name):
    def build(gmsh, p):
        surfaces = _tags(p.get('surfaces'), 'surface')
        if ns_name == 'occ':
            _require_occ(gmsh, [(2, s) for s in surfaces])
        else:
            _need_kernel(gmsh, [(2, s) for s in surfaces], 'geo', 'a volume')
        ns = getattr(gmsh.model, ns_name)
        shell = int(ns.addSurfaceLoop(surfaces, _tag(p)))
        volume = int(ns.addVolume([shell], -1))
        done = _done if ns_name == 'occ' else _done_geo
        return done(gmsh, [(3, volume)], loop=shell)
    return build


# -- the built-in kernel (Geometry > Set geometry kernel > Built-in) --------- #

def _geo_selection(gmsh, p, name='entities'):
    selection = _dimtags(p.get(name))
    _need_kernel(gmsh, selection, 'geo', 'this operation')
    return selection


def _geo_point(gmsh, p):
    x, y, z = _need(p, 'x', 'y', 'z')
    tag = gmsh.model.geo.addPoint(float(x), float(y), float(z),
                                  float(p.get('size') or 0.0), _tag(p))
    return _done_geo(gmsh, [(0, tag)])


def _geo_line(gmsh, p):
    a, b = _need(p, 'start', 'end')
    if int(a) == int(b):
        raise BuildError('a line needs two different points')
    return _done_geo(gmsh, [(1, gmsh.model.geo.addLine(int(a), int(b),
                                                       _tag(p)))])


def _geo_circle_arc(gmsh, p):
    a, c, b = _need(p, 'start', 'center', 'end')
    return _done_geo(gmsh, [(1, gmsh.model.geo.addCircleArc(
        int(a), int(c), int(b), _tag(p)))])


def _geo_ellipse_arc(gmsh, p):
    a, c, m, b = _need(p, 'start', 'center', 'major', 'end')
    return _done_geo(gmsh, [(1, gmsh.model.geo.addEllipseArc(
        int(a), int(c), int(m), int(b), _tag(p)))])


def _geo_through_points(name, minimum, call):
    def build(gmsh, p):
        points = _tags(p.get('points'), 'point')
        if len(points) < minimum:
            raise BuildError('a %s needs at least %d points' % (name,
                                                                 minimum))
        return _done_geo(gmsh, [(1, call(gmsh.model.geo, points, _tag(p)))])
    return build


def _geo_curve_loop(gmsh, p):
    curves = _tags(p.get('curves'), 'curve')
    return _done_geo(gmsh, [], loop=int(gmsh.model.geo.addCurveLoop(
        curves, _tag(p))))


def _geo_plane_surface(gmsh, p):
    loops = _tags(p.get('loops'), 'curve loop')
    return _done_geo(gmsh, [(2, gmsh.model.geo.addPlaneSurface(loops,
                                                               _tag(p)))])


def _geo_surface_filling(gmsh, p):
    loops = _tags(p.get('loops'), 'curve loop')
    return _done_geo(gmsh, [(2, gmsh.model.geo.addSurfaceFilling(
        loops, _tag(p)))])


def _geo_surface_loop(gmsh, p):
    surfaces = _tags(p.get('surfaces'), 'surface')
    return _done_geo(gmsh, [], loop=int(gmsh.model.geo.addSurfaceLoop(
        surfaces, _tag(p))))


def _geo_volume(gmsh, p):
    shells = _tags(p.get('shells'), 'surface loop')
    return _done_geo(gmsh, [(3, gmsh.model.geo.addVolume(shells, _tag(p)))])


def _geo_transform(name):
    def run(gmsh, p):
        selection = _geo_selection(gmsh, p)
        geo = gmsh.model.geo
        target = selection
        created = []
        if p.get('copy'):
            target = [(int(d), int(t)) for d, t in geo.copy(selection)]
            created = target
        if name == 'translate':
            dx, dy, dz = _need(p, 'dx', 'dy', 'dz')
            geo.translate(target, dx, dy, dz)
        elif name == 'rotate':
            x, y, z, ax, ay, az, angle = _need(p, 'x', 'y', 'z', 'ax', 'ay',
                                               'az', 'angle')
            if not any((ax, ay, az)):
                raise BuildError('the rotation axis is the zero vector')
            geo.rotate(target, x, y, z, ax, ay, az, math.radians(angle))
        elif name == 'scale':
            x, y, z, a, b, c = _need(p, 'x', 'y', 'z', 'a', 'b', 'c')
            if 0 in (float(a), float(b), float(c)):
                raise BuildError('a scale factor of zero collapses the '
                                 'entity')
            geo.dilate(target, x, y, z, a, b, c)
        else:
            a, b, c, d = _need(p, 'a', 'b', 'c', 'd')
            if not any((a, b, c)):
                raise BuildError('the mirror plane normal is the zero '
                                 'vector')
            geo.symmetrize(target, a, b, c, d)
        return _done_geo(gmsh, created, moved=_created(target))
    return run


def _surface_normal(gmsh, tag):
    """The unit normal of a surface at the point nearest its centre."""
    box = gmsh.model.getBoundingBox(2, tag)
    centre = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
    _closest, uv = gmsh.model.getClosestPoint(2, tag, centre)
    normal = [float(n) for n in gmsh.model.getNormal(tag, list(uv))[:3]]
    length = math.sqrt(sum(n * n for n in normal)) or 1.0
    return [n / length for n in normal]


def _geo_extrude(gmsh, p):
    selection = _geo_selection(gmsh, p)
    p = _picked(gmsh, p, 'geo')
    mode = str(p.get('mode') or 'translate')
    if mode not in EXTRUDE_MODES:
        raise BuildError('extrusion mode must be one of %s'
                         % ', '.join(EXTRUDE_MODES))
    counts, heights = _layers(p)
    recombine = bool(p.get('recombine'))
    geo = gmsh.model.geo
    if p.get('quadtri') or p.get('scale_last'):
        return _geo_extrude_keywords(gmsh, p, selection, mode, counts,
                                     heights, recombine)
    if mode == 'translate':
        dx, dy, dz = _need(p, 'dx', 'dy', 'dz')
        if not any((dx, dy, dz)):
            raise BuildError('the extrusion vector is zero')
        out = geo.extrude(selection, dx, dy, dz, counts, heights, recombine)
    elif mode == 'rotate':
        x, y, z, ax, ay, az, angle = _need(p, 'x', 'y', 'z', 'ax', 'ay',
                                           'az', 'angle')
        if not any((ax, ay, az)):
            raise BuildError('the rotation axis is the zero vector')
        out = geo.revolve(selection, x, y, z, ax, ay, az,
                          math.radians(angle), counts, heights, recombine)
    else:
        distance = float(_need(p, 'distance')[0])
        if distance == 0:
            raise BuildError('the extrusion distance is zero')
        out = []
        for dim, tag in selection:
            if dim != 2:
                raise BuildError('extrusion along the normal needs surfaces')
            geo.synchronize()
            vec = [n * distance for n in _surface_normal(gmsh, tag)]
            out.extend(geo.extrude([(dim, tag)], vec[0], vec[1], vec[2],
                                   counts, heights, recombine))
    return _done_geo(gmsh, [(int(d), int(t)) for d, t in out],
                     layers=_layer_total(p), recombine=recombine)


def _geo_delete(gmsh, p):
    selection = _geo_selection(gmsh, p)
    gmsh.model.geo.remove(selection, bool(p.get('recursive')))
    return _done_geo(gmsh, [], deleted=_created(selection))


def _geo_split_curve(gmsh, p):
    curves = _tags(p.get('curve'), 'curve')
    if len(curves) != 1:
        raise BuildError('split one curve at a time')
    points = _tags(p.get('points'), 'point')
    _need_kernel(gmsh, [(1, curves[0])] + [(0, t) for t in points], 'geo',
                 'Split curve')
    geo = gmsh.model.geo
    geo.synchronize()
    before = {int(t) for _d, t in gmsh.model.getEntities(1)}
    geo.splitCurve(curves[0], points)
    geo.synchronize()
    after = {int(t) for _d, t in gmsh.model.getEntities(1)}
    return _done_geo(gmsh, [(1, t) for t in sorted(after - before)],
                     replaced=curves[0])


def _geo_twist(gmsh, p):
    selection = _geo_selection(gmsh, p)
    p = _picked(gmsh, p, 'geo')
    x, y, z, dx, dy, dz, ax, ay, az, angle = _need(
        p, 'x', 'y', 'z', 'dx', 'dy', 'dz', 'ax', 'ay', 'az', 'angle')
    if not any((ax, ay, az)):
        raise BuildError('the twist axis is the zero vector')
    if not any((dx, dy, dz)):
        raise BuildError('the extrusion vector is zero')
    counts, heights = _layers(p)
    out = gmsh.model.geo.twist(
        selection, x, y, z, dx, dy, dz, ax, ay, az, math.radians(angle),
        counts, heights, bool(p.get('recombine')))
    return _done_geo(gmsh, [(int(d), int(t)) for d, t in out],
                     layers=_layer_total(p))


# -- Plan 48 GM6: sweeps -------------------------------------------------- #

QUADTRI = ('', 'add_verts', 'no_new_verts')
_QUADTRI_WORD = {'add_verts': 'QuadTriAddVerts',
                 'no_new_verts': 'QuadTriNoNewVerts'}


def _g(value) -> str:
    return '%.16g' % float(value)


def _layers_text(counts, heights) -> str:
    if not counts:
        return ''
    if len(counts) == 1 and list(heights) == [1.0]:
        return 'Layers{ %d };' % int(counts[0])
    return 'Layers{ {%s}, {%s} };' % (
        ', '.join(str(int(c)) for c in counts),
        ', '.join(_g(h) for h in heights))


def _run_geo_text(gmsh, text: str) -> None:
    """Run Gmsh script lines on the live model, the way the session's
    ``geo_lines`` does, and record them as one ``#geo`` row so the exported
    Python and ``.geo`` scripts carry the text.  Without a recorder (the
    batch runner) the lines are just merged."""
    import json
    import os
    import sys
    import tempfile
    session = sys.modules.get(type(gmsh).__module__)
    write_calls = getattr(session, '_write_calls', None)
    log_path = getattr(session, '_log_path', '') or ''
    base = os.path.dirname(log_path) if log_path else tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, 'sweep_lines.geo')
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    raw = getattr(gmsh, '_t', gmsh)
    try:
        raw.merge(path)
    except Exception as error:                             # noqa: BLE001
        raise BuildError('Gmsh refused the extrusion: %s' % error) from error
    if write_calls is not None:
        write_calls(json.dumps({'f': '#geo', 'a': [text], 'k': {}}))


def _geo_extrude_keywords(gmsh, p, selection, mode, counts, heights,
                          recombine):
    """Extrusion with ``QuadTriAddVerts`` / ``QuadTriNoNewVerts`` /
    ``ScaleLastLayer`` (D3): keywords the API call has no argument for, so
    they run as the script statement (``Extrude {...} { Surface{1}; ...}``)."""
    quadtri = str(p.get('quadtri') or '')
    if quadtri not in QUADTRI:
        raise BuildError('QuadTri must be one of %s'
                         % ', '.join(repr(q) for q in QUADTRI))
    if any(d != 2 for d, _t in selection):
        raise BuildError('QuadTri and ScaleLast apply to the extrusion of '
                         'surfaces into volumes; pick surfaces')
    if not counts:
        raise BuildError('QuadTri and ScaleLast need layers: give a layer '
                         'count or a layer table')
    if quadtri or p.get('scale_last'):
        recombine = True          # both are keywords of a recombined layer
    clauses = [_layers_text(counts, heights)]
    if recombine:
        clauses.append('Recombine;')
    if quadtri:
        clauses.append(_QUADTRI_WORD[quadtri] + ';')
    if p.get('scale_last'):
        clauses.append('ScaleLastLayer;')
    body = ' '.join(clauses)
    geo = gmsh.model.geo
    geo.synchronize()
    before = {(int(d), int(t)) for d, t in gmsh.model.getEntities()}
    lines = []
    if mode == 'translate':
        dx, dy, dz = _need(p, 'dx', 'dy', 'dz')
        if not any((dx, dy, dz)):
            raise BuildError('the extrusion vector is zero')
        lines.append('Extrude {%s, %s, %s} { Surface{%s}; %s }' % (
            _g(dx), _g(dy), _g(dz), ', '.join(str(t) for _d, t in selection),
            body))
    elif mode == 'rotate':
        x, y, z, ax, ay, az, angle = _need(p, 'x', 'y', 'z', 'ax', 'ay',
                                           'az', 'angle')
        if not any((ax, ay, az)):
            raise BuildError('the rotation axis is the zero vector')
        lines.append('Extrude { {%s, %s, %s}, {%s, %s, %s}, %s } '
                     '{ Surface{%s}; %s }' % (
                         _g(ax), _g(ay), _g(az), _g(x), _g(y), _g(z),
                         _g(math.radians(angle)),
                         ', '.join(str(t) for _d, t in selection), body))
    else:
        distance = float(_need(p, 'distance')[0])
        if distance == 0:
            raise BuildError('the extrusion distance is zero')
        for _dim, tag in selection:
            vec = [n * distance for n in _surface_normal(gmsh, tag)]
            lines.append('Extrude {%s, %s, %s} { Surface{%d}; %s }' % (
                _g(vec[0]), _g(vec[1]), _g(vec[2]), tag, body))
    _run_geo_text(gmsh, '\n'.join(lines) + '\n')
    geo.synchronize()
    after = {(int(d), int(t)) for d, t in gmsh.model.getEntities()}
    return _done_geo(gmsh, sorted(after - before, reverse=True),
                     layers=int(sum(counts)), recombine=recombine,
                     quadtri=quadtri, scale_last=bool(p.get('scale_last')))


def _extrude_boundary_layer(gmsh, p):
    """Extrusion along the normals of curves or surfaces
    (``geo.extrudeBoundaryLayer``): a boundary-layer-style sweep whose
    thickness is a number or, with ``view_index``, a post-processing view."""
    selection = _geo_selection(gmsh, p)
    if any(d not in (1, 2) for d, _t in selection):
        raise BuildError('extrusion along the normals needs curves or '
                         'surfaces')
    counts, fractions = _layers(p)
    view = int(p.get('view_index', -1) if p.get('view_index') is not None
               else -1)
    thickness = float(p.get('thickness') or 0.0)
    if view < 0 and thickness <= 0:
        raise BuildError('give a positive thickness, or the index of a view '
                         'that carries it')
    if not counts:
        counts, fractions = [1], [1.0]
    heights = list(fractions) if view >= 0 else [
        f * thickness for f in fractions]
    out = gmsh.model.geo.extrudeBoundaryLayer(
        selection, counts, heights, bool(p.get('recombine')),
        bool(p.get('second')), view)
    return _done_geo(gmsh, [(int(d), int(t)) for d, t in out],
                     layers=int(sum(counts)), thickness=thickness)


def _group_once(gmsh, dim, tags, name):
    """One physical group *name* over *tags*; a name that is taken is
    refused (the person renames or deletes the old group)."""
    if not name or not tags:
        return None
    taken = {gmsh.model.getPhysicalName(d, t)
             for d, t in gmsh.model.getPhysicalGroups()}
    if name in taken:
        raise BuildError('a physical group named %r already exists; choose '
                         'another name or delete that group' % name)
    return int(gmsh.model.addPhysicalGroup(dim, [int(t) for t in tags], -1,
                                           name))


def _declared_groups(gmsh, wedge, p, tags) -> list:
    """``[{'dim', 'tag', 'name', 'role', 'category'}]`` for the physical
    groups a 2D-case or wedge preset has just made (*tags* in the order the
    preset made them: front, back [or the one front-and-back group], sides,
    volumes)."""
    if wedge:
        plan = [('front', 'wedge'), ('back', 'wedge')]
    elif str(p.get('naming') or 'one_group') == 'pair':
        plan = [('front', 'empty'), ('back', 'empty')]
    else:
        plan = [('front_and_back', 'empty')]
    plan.append(('sides', 'wall'))
    rows = []
    for (role, category), tag in zip(plan, tags):
        if tag is None:
            continue
        rows.append({'dim': 2, 'tag': int(tag),
                     'name': gmsh.model.getPhysicalName(2, int(tag)),
                     'role': role, 'category': category,
                     # DP-5725: the surfaces, which a re-import keeps
                     'surfaces': [int(t) for t in gmsh.model
                                  .getEntitiesForPhysicalGroup(2, int(tag))]})
    return rows


def _sweep_preset(kernel):
    """The 2D-case and the wedge presets, on one kernel."""
    def run(gmsh, p, wedge=False):
        ns = getattr(gmsh.model, kernel)
        if kernel == 'occ':
            selection = _dimtags(p.get('entities'))
            _require_occ(gmsh, selection)
        else:
            selection = _geo_selection(gmsh, p)
        if any(d != 2 for d, _t in selection):
            raise BuildError('pick the surfaces to sweep')
        p = _picked(gmsh, p, kernel)
        if wedge:
            x, y, z, ax, ay, az = [float(p.get(k, v)) for k, v in (
                ('x', 0.0), ('y', 0.0), ('z', 0.0), ('ax', 1.0),
                ('ay', 0.0), ('az', 0.0))]
            if not any((ax, ay, az)):
                raise BuildError('the wedge axis is the zero vector')
            angle = float(p.get('angle', 5.0))
            if not 0.0 < angle < 180.0:
                raise BuildError('the wedge angle must be between 0 and '
                                 '180 degrees')
            symmetric = bool(p.get('symmetric', True))
        else:
            thickness = float(_need(p, 'thickness')[0])
            if thickness <= 0:
                raise BuildError('the thickness must be greater than zero')
            n = [float(p.get(k, v)) for k, v in (('nx', 0.0), ('ny', 0.0),
                                                 ('nz', 1.0))]
            length = math.sqrt(sum(c * c for c in n))
            if not length:
                raise BuildError('the extrusion direction is the zero vector')
            vec = [c / length * thickness for c in n]
        front, back, sides, volumes = [], [], [], []
        if wedge and symmetric:
            # DP-4215. OpenFOAM's wedge wants the two faces symmetric about a
            # coordinate plane (half the angle each side of it), so the
            # picked surfaces turn back by half the angle first and the
            # revolve carries them through the whole angle: the picked plane
            # ends up between the faces.
            ns.rotate(list(selection), x, y, z, ax, ay, az,
                      -math.radians(angle) / 2.0)
        for dim, tag in selection:
            if wedge:
                out = ns.revolve([(dim, tag)], x, y, z, ax, ay, az,
                                 math.radians(angle), [1], [], True)
            else:
                out = ns.extrude([(dim, tag)], vec[0], vec[1], vec[2], [1],
                                 [], True)
            surfaces = [int(t) for d, t in out if int(d) == 2]
            volumes += [int(t) for d, t in out if int(d) == 3]
            front.append(tag)
            if surfaces:
                back.append(surfaces[0])
                sides += surfaces[1:]
        (ns.synchronize)()
        names = []
        if wedge:
            names.append(_group_once(gmsh, 2, front,
                                     str(p.get('front_name') or 'wedgeFront')))
            names.append(_group_once(gmsh, 2, back,
                                     str(p.get('back_name') or 'wedgeBack')))
        elif str(p.get('naming') or 'one_group') == 'pair':
            names.append(_group_once(gmsh, 2, front,
                                     str(p.get('front_name') or 'front')))
            names.append(_group_once(gmsh, 2, back,
                                     str(p.get('back_name') or 'back')))
        else:
            names.append(_group_once(
                gmsh, 2, front + back,
                str(p.get('front_back_name') or 'frontAndBack')))
        names.append(_group_once(gmsh, 2, sides,
                                 str(p.get('sides_name') or '')))
        names.append(_group_once(gmsh, 3, volumes,
                                 str(p.get('fluid_name') or '')))
        made = [(2, t) for t in back + sides] + [(3, t) for t in volumes]
        done = _done_geo if kernel == 'geo' else _done
        # DP-4215. The groups the preset made, by role, with the OpenFOAM
        # category each one is for (``empty`` for a 2D case, ``wedge`` for an
        # axisymmetric one, ``wall`` for the sides), so the host can keep
        # them in the shared patch naming and the publisher can type them.
        declared = _declared_groups(gmsh, wedge, p, names)
        return done(gmsh, made, roles={
            'front': front, 'back': back, 'sides': sides,
            'volumes': volumes, 'symmetric': bool(wedge and symmetric),
            'declared': declared},
            groups=[n for n in names if n is not None])
    return run


def sweep_preview(gmsh, kind: str, params: dict) -> dict:
    """Where the layers of an extrusion will fall, without changing the
    model: for each picked entity the layer boundaries as points along the
    sweep, and the arrow or axis that drives it (the viewport draws it before
    anything is built)."""
    params = dict(params or {})
    kernel = str(params.pop('kernel', None) or 'occ')
    if kernel not in KERNELS:
        raise BuildError('the geometry kernel must be one of %s'
                         % ', '.join(KERNELS))
    if kind not in ('extrude', 'twist', 'case_2d', 'axisymmetric_wedge',
                    'extrude_boundary_layer'):
        raise BuildError('there is no sweep preview for %r' % kind)
    selection = _dimtags(params.get('entities'))
    p = _picked(gmsh, params, kernel)
    counts, heights = _layers(p)
    if kind in ('case_2d', 'axisymmetric_wedge'):
        counts, heights = [1], [1.0]
    elif not counts:
        counts, heights = [1], [1.0]
    fractions, last = [], 0.0
    for count, cum in zip(counts, heights):
        for i in range(1, count + 1):
            fractions.append(last + (cum - last) * i / count)
        last = cum
    mode = str(p.get('mode') or 'translate')
    if kind == 'axisymmetric_wedge':
        mode = 'rotate'
    getattr(gmsh.model, kernel).synchronize()
    entities = []
    arrow = axis = None
    for dim, tag in selection[:50]:
        box = gmsh.model.getBoundingBox(dim, tag)
        origin = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
        if mode == 'rotate':
            x, y, z = (float(p.get(k, 0.0)) for k in ('x', 'y', 'z'))
            a = [float(p.get(k, v)) for k, v in (('ax', 1.0 if kind ==
                                                  'axisymmetric_wedge' else 0.0),
                                                 ('ay', 0.0), ('az', 0.0 if kind
                                                  == 'axisymmetric_wedge' else 1.0))]
            length = math.sqrt(sum(c * c for c in a)) or 1.0
            a = [c / length for c in a]
            angle = math.radians(float(p.get('angle', 5.0)))
            axis = {'point': [x, y, z], 'direction': a,
                    'angle_degrees': math.degrees(angle)}
            # a symmetric wedge starts half the angle behind the picked plane
            start = (angle / 2.0 if kind == 'axisymmetric_wedge'
                     and bool(p.get('symmetric', True)) else 0.0)
            points = [_rotate_about(origin, [x, y, z], a, angle * f - start)
                      for f in fractions]
        else:
            if mode == 'normal' or kind in ('extrude_boundary_layer',):
                if dim != 2:
                    vec = [0.0, 0.0, 0.0]
                else:
                    unit = (_surface_normal(gmsh, tag) if kernel == 'geo'
                            else _occ_normal(gmsh, tag, origin))
                    scale = float(p.get('distance', p.get('thickness', 1.0))
                                  or 0.0)
                    vec = [c * scale for c in unit]
            elif kind == 'case_2d':
                n = [float(p.get(k, v)) for k, v in (('nx', 0.0), ('ny', 0.0),
                                                     ('nz', 1.0))]
                length = math.sqrt(sum(c * c for c in n)) or 1.0
                vec = [c / length * float(p.get('thickness', 0.0)) for c in n]
            else:
                vec = [float(p.get(k, 0.0)) for k in ('dx', 'dy', 'dz')]
            arrow = {'origin': origin, 'vector': vec}
            points = [[origin[i] + vec[i] * f for i in range(3)]
                      for f in fractions]
        entities.append({'entity': [dim, tag], 'origin': origin,
                         'layers': [{'fraction': f, 'point': pt}
                                    for f, pt in zip(fractions, points)]})
    return {'kind': kind, 'mode': mode, 'layer_count': len(fractions),
            'fractions': fractions, 'arrow': arrow, 'axis': axis,
            'entities': entities}


def _occ_normal(gmsh, tag, centre):
    _closest, uv = gmsh.model.getClosestPoint(2, tag, centre)
    normal = [float(n) for n in gmsh.model.getNormal(tag, list(uv))[:3]]
    length = math.sqrt(sum(n * n for n in normal)) or 1.0
    return [n / length for n in normal]


def _rotate_about(point, origin, axis, angle):
    """Rodrigues' rotation of *point* about the line through *origin*."""
    v = [point[i] - origin[i] for i in range(3)]
    c, s = math.cos(angle), math.sin(angle)
    dot = sum(axis[i] * v[i] for i in range(3))
    cross = [axis[1] * v[2] - axis[2] * v[1], axis[2] * v[0] - axis[0] * v[2],
             axis[0] * v[1] - axis[1] * v[0]]
    return [origin[i] + v[i] * c + cross[i] * s + axis[i] * dot * (1 - c)
            for i in range(3)]


# -- Plan 48 GB: fillings, surfaces from control points, copy, affine ------- #

FILLING_TYPES = ('', 'Stretch', 'Curved', 'Coons')


def _int_list(value, what):
    """``'3, 3, 1'`` / ``[3, 3, 1]`` to ``[3, 3, 1]``; empty is ``[]``."""
    if value is None or value == '':
        return []
    numbers = _floats(value, what)
    if any(n != int(n) for n in numbers):
        raise BuildError('%s must be whole numbers separated by commas'
                         % what)
    return [int(n) for n in numbers]


def _optional_floats(value, what):
    if value is None or str(value).strip() == '':
        return []
    return _floats(value, what)


def _filling(method, word):
    def build(gmsh, p):
        loops = _tags(p.get('loops'), 'curve loop')
        if len(loops) != 1:
            raise BuildError('a %s fills one curve loop, not %d'
                             % (word, len(loops)))
        kind = str(p.get('type') or '')
        if kind not in FILLING_TYPES:
            raise BuildError('the filling type must be one of %s'
                             % ', '.join(t or '(default)'
                                         for t in FILLING_TYPES))
        # DP-5562: OpenCASCADE's Coons style of a B-spline filling takes
        # curves of degree 3 or more (4 control points); fewer answer
        # "invalid filling style", raised or as tag -1 in the log.
        coons = BuildError(
            'a Coons %s needs every curve to have at least 4 control points '
            '(degree 3); use Curved or Stretch, or add points to the curves'
            % word)
        try:
            surface = getattr(gmsh.model.occ, method)(loops[0], _tag(p), kind)
        except Exception as error:                       # noqa: BLE001
            if kind == 'Coons' and 'invalid filling style' in str(error):
                raise coons from error
            raise
        if surface is not None and surface <= 0:
            if kind == 'Coons':
                raise coons
            raise BuildError('Gmsh made no %s from curve loop %d'
                             % (word, loops[0]))
        return _done(gmsh, [(2, surface)])
    return build


def _control_grid(gmsh, p, what):
    """``(points, columns, rows, trim wires, wire3d)`` of a control-point
    surface."""
    points = _tags(p.get('points'), 'control point')
    _require_occ(gmsh, [(0, t) for t in points])
    columns = int(p.get('num_points_u') if p.get('num_points_u') is not None
                  else 0)
    if columns < 2:
        raise BuildError('a %s needs at least 2 control points along U'
                         % what)
    if len(points) % columns:
        raise BuildError('%d control points cannot be rows of %d: give the '
                         'points row by row, %d along U each'
                         % (len(points), columns, columns))
    rows = len(points) // columns
    if rows < 2:
        raise BuildError('a %s needs at least 2 rows of control points'
                         % what)
    trim = _int_list(p.get('trim_wires'), 'trimming curve loop')
    return points, columns, rows, trim, bool(p.get('wire_3d'))


def _bspline_surface(gmsh, p):
    points, columns, rows, trim, wire3d = _control_grid(gmsh, p,
                                                        'B-spline surface')
    degree_u = int(p.get('degree_u') if p.get('degree_u') is not None else 3)
    degree_v = int(p.get('degree_v') if p.get('degree_v') is not None else 3)
    if not 1 <= degree_u <= columns - 1:
        raise BuildError('the degree along U must be from 1 to %d (one '
                         'less than the control points along U)'
                         % (columns - 1))
    if not 1 <= degree_v <= rows - 1:
        raise BuildError('the degree along V must be from 1 to %d (one '
                         'less than the rows of control points)'
                         % (rows - 1))
    weights = _optional_floats(p.get('weights'), 'weight')
    if weights and len(weights) != len(points):
        raise BuildError('give one weight for each of the %d control points '
                         '(%d given)' % (len(points), len(weights)))
    surface = gmsh.model.occ.addBSplineSurface(
        points, columns, _tag(p), degree_u, degree_v, weights,
        _optional_floats(p.get('knots_u'), 'knot along U'),
        _optional_floats(p.get('knots_v'), 'knot along V'),
        _int_list(p.get('multiplicities_u'), 'multiplicity along U'),
        _int_list(p.get('multiplicities_v'), 'multiplicity along V'),
        trim, wire3d)
    return _done(gmsh, [(2, surface)], columns=columns, rows=rows)


def _bezier_surface(gmsh, p):
    points, columns, rows, trim, wire3d = _control_grid(gmsh, p,
                                                        'Bezier surface')
    surface = gmsh.model.occ.addBezierSurface(points, columns, _tag(p), trim,
                                              wire3d)
    return _done(gmsh, [(2, surface)], columns=columns, rows=rows)


def _trimmed_surface(gmsh, p):
    surfaces = _tags(p.get('surface'), 'surface')
    if len(surfaces) != 1:
        raise BuildError('trim one surface at a time')
    _require_occ(gmsh, [(2, surfaces[0])])
    wires = _int_list(p.get('trim_wires'), 'trimming curve loop')
    if not wires:
        raise BuildError('give at least one trimming curve loop (the first '
                         'is the outer contour, the others are holes)')
    surface = gmsh.model.occ.addTrimmedSurface(
        surfaces[0], wires, bool(p.get('wire_3d')), _tag(p))
    return _done(gmsh, [(2, surface)], trimmed=surfaces[0])


def _copy(ns_name):
    def build(gmsh, p):
        selection = _dimtags(p.get('entities'))
        if ns_name == 'occ':
            _require_occ(gmsh, selection)
        else:
            selection = _geo_selection(gmsh, p)
        out = getattr(gmsh.model, ns_name).copy(selection)
        done = _done if ns_name == 'occ' else _done_geo
        return done(gmsh, [(int(d), int(t)) for d, t in out],
                    copied=_created(selection))
    return build


def _affine_transform(gmsh, p):
    selection = _dimtags(p.get('entities'))
    _require_occ(gmsh, selection)
    matrix = _floats(p.get('matrix'), 'matrix entry')
    if len(matrix) not in (12, 16):
        raise BuildError('an affine transformation takes 12 numbers (the '
                         'first three rows of the 4 × 4 matrix) or 16 (all '
                         'four rows), row by row; %d were given'
                         % len(matrix))
    occ = gmsh.model.occ
    target = selection
    created = []
    if p.get('copy'):
        target = [(int(d), int(t)) for d, t in occ.copy(selection)]
        created = target
    occ.affineTransform(target, matrix)
    return _done(gmsh, created, moved=_created(target))


def _geo_polyline(gmsh, p):
    points = _tags(p.get('points'), 'point')
    if len(points) < 2:
        raise BuildError('a polyline needs at least 2 points')
    _need_kernel(gmsh, [(0, t) for t in points], 'geo', 'a polyline')
    return _done_geo(gmsh, [(1, gmsh.model.geo.addPolyline(points,
                                                           _tag(p)))])


def _geo_compound(method, default):
    def build(gmsh, p):
        curves = _tags(p.get('curves'), 'curve')
        _need_kernel(gmsh, [(1, t) for t in curves], 'geo',
                     'a compound spline')
        intervals = int(p.get('intervals') if p.get('intervals') is not None
                        else default)
        if intervals < 1:
            raise BuildError('sample each curve at least once (intervals '
                             'from 1)')
        tag = getattr(gmsh.model.geo, method)(curves, intervals, _tag(p))
        return _done_geo(gmsh, [(1, tag)], intervals=intervals)
    return build


def _geo_parametric_geometry(gmsh, p):
    # DP-5558: the geometry a point on a geometry sits on. It is not a model
    # entity, so it creates nothing in the model; its number is returned.
    shape = p.get('shape') or 'Sphere'
    if shape == 'ParametricSurface':
        fx, fy, fz = _need(p, 'fx', 'fy', 'fz')
        numbers, strings = [], [str(fx), str(fy), str(fz)]
    elif shape in ('Sphere', 'PolarSphere'):
        r = float(p.get('r') or 0.0)
        if r <= 0:
            raise BuildError('the radius must be greater than zero')
        numbers = [float(p.get(k) or 0.0) for k in ('x', 'y', 'z')] + [r]
        strings = []
    else:
        raise BuildError('the geometry must be one of Sphere, PolarSphere, '
                         'ParametricSurface')
    tag = gmsh.model.geo.addGeometry(shape, numbers, strings, _tag(p))
    if tag <= 0:
        raise BuildError('Gmsh did not make the %s' % shape)
    return _done_geo(gmsh, [], geometry=int(tag), shape=shape)


def _geo_point_on_geometry(gmsh, p):
    geometry, x, y = _need(p, 'geometry', 'x', 'y')
    try:
        tag = gmsh.model.geo.addPointOnGeometry(
            int(geometry), float(x), float(y), 0.0,
            float(p.get('size') or 0.0), _tag(p))
    except Exception:                                    # noqa: BLE001
        tag = 0
    if tag <= 0:
        # Gmsh 4.15.2 answers "Surface N does not exist" (or 0) for a number
        # that no Parametric geometry gave.
        raise BuildError('there is no parametric geometry %d; make one with '
                         '"Parametric geometry" first' % int(geometry))
    return _done_geo(gmsh, [(0, tag)])


_HANDLERS = {
    'parameter': _parameter, 'point': _point, 'line': _line,
    'circle_arc': _circle_arc, 'ellipse_arc': _ellipse_arc,
    'spline': _through_points('spline', 2, lambda occ, pts, tag:
                              occ.addSpline(pts, tag)),
    'bspline': _through_points('B-spline', 2, lambda occ, pts, tag:
                               occ.addBSpline(pts, tag)),
    'bezier': _through_points('Bezier curve', 2, lambda occ, pts, tag:
                              occ.addBezier(pts, tag)),
    'curve_loop': _curve_loop, 'plane_surface': _plane_surface,
    'surface_filling': _surface_filling, 'surface_loop': _surface_loop,
    'volume': _volume, 'rectangle': _rectangle, 'disk': _disk, 'box': _box,
    'sphere': _sphere, 'cylinder': _cylinder, 'cone': _cone,
    'torus': _torus, 'wedge': _wedge,
    'translate': _transform('translate'), 'rotate': _transform('rotate'),
    'scale': _transform('scale'), 'mirror': _transform('mirror'),
    'extrude': _extrude, 'boolean': _boolean, 'delete': _delete,
    'farfield': _farfield,
    'circle': _circle, 'ellipse': _ellipse, 'wire': _wire, 'pipe': _pipe,
    'thrusections': _thrusections, 'thick_solid': _thick_solid,
    'offset_curve': _offset_curve, 'fillet': _fillet, 'chamfer': _chamfer,
    'fillet_2d': _fillet_2d, 'chamfer_2d': _chamfer_2d,
    'defeature': _defeature, 'to_nurbs': _to_nurbs, 'heal': _heal,
    'surface_from_curves': _surface_from_curves('occ'),
    'volume_from_surfaces': _volume_from_surfaces('occ'),
    'case_2d': _sweep_preset('occ'),
    'axisymmetric_wedge': lambda gmsh, p: _sweep_preset('occ')(
        gmsh, p, wedge=True),
    'bspline_filling': _filling('addBSplineFilling', 'B-spline filling'),
    'bezier_filling': _filling('addBezierFilling', 'Bezier filling'),
    'bspline_surface': _bspline_surface, 'bezier_surface': _bezier_surface,
    'trimmed_surface': _trimmed_surface,
    'affine_transform': _affine_transform, 'copy': _copy('occ'),
}

#: The same kinds on the built-in kernel (``kernel_refusal`` already refused
#: the kinds that have none).
_GEO_HANDLERS = {
    'parameter': _parameter, 'point': _geo_point, 'line': _geo_line,
    'circle_arc': _geo_circle_arc, 'ellipse_arc': _geo_ellipse_arc,
    'spline': _geo_through_points('spline', 2, lambda ns, pts, tag:
                                  ns.addSpline(pts, tag)),
    'bspline': _geo_through_points('B-spline', 2, lambda ns, pts, tag:
                                   ns.addBSpline(pts, tag)),
    'bezier': _geo_through_points('Bezier curve', 2, lambda ns, pts, tag:
                                  ns.addBezier(pts, tag)),
    'curve_loop': _geo_curve_loop, 'plane_surface': _geo_plane_surface,
    'surface_filling': _geo_surface_filling,
    'surface_loop': _geo_surface_loop, 'volume': _geo_volume,
    'translate': _geo_transform('translate'),
    'rotate': _geo_transform('rotate'), 'scale': _geo_transform('scale'),
    'mirror': _geo_transform('mirror'), 'extrude': _geo_extrude,
    'delete': _geo_delete, 'split_curve': _geo_split_curve,
    'twist': _geo_twist,
    'surface_from_curves': _surface_from_curves('geo'),
    'volume_from_surfaces': _volume_from_surfaces('geo'),
    'extrude_boundary_layer': _extrude_boundary_layer,
    'case_2d': _sweep_preset('geo'),
    'axisymmetric_wedge': lambda gmsh, p: _sweep_preset('geo')(
        gmsh, p, wedge=True),
    'copy': _copy('geo'), 'polyline': _geo_polyline,
    'compound_spline': _geo_compound('addCompoundSpline', 5),
    'compound_bspline': _geo_compound('addCompoundBSpline', 20),
    'point_on_geometry': _geo_point_on_geometry,
    'parametric_geometry': _geo_parametric_geometry,
}
