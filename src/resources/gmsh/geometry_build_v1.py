"""Geometry build operations on the resident Gmsh session (Plan 41 GN3).

Gmsh's Geometry module, ``Elementary entities > Add`` and ``Transform``, as one
function: ``apply(gmsh, kind, params)``.  Every operation runs on the
OpenCASCADE kernel (the kernel of record), synchronises the model so the new
entities are visible, and returns ``{"created": [[dim, tag], ...], ...}``.

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
    'mirror', 'extrude', 'boolean', 'delete', 'farfield')

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


def _tag(params):
    return int(params.get('tag', -1) if params.get('tag') is not None
               else -1)


def apply(gmsh, kind: str, params: dict) -> dict:
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise BuildError('unknown geometry operation %r' % kind)
    _require_occ(gmsh)
    return handler(gmsh, dict(params or {}))


# -- parameters and points --------------------------------------------------- #

def _parameter(gmsh, p):
    name, value = _need(p, 'name', 'value')
    if not str(name).isidentifier():
        raise BuildError('%r is not a valid parameter name' % (name,))
    gmsh.parser.setNumber(str(name), [float(value)])
    return {'created': [], 'name': str(name), 'value': float(value)}


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
    return _done(gmsh, [(3, gmsh.model.occ.addSphere(
        x, y, z, r, _tag(p), -math.pi / 2, math.pi / 2,
        _angle(p, 'angle', 360.0)))])


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
    layers = int(p.get('layers') or 0)
    if layers < 0:
        raise BuildError('layers cannot be negative')
    return ([layers] if layers else []), ([1.0] if layers else [])


def _extrude(gmsh, p):
    selection = _dimtags(p.get('entities'))
    _require_occ(gmsh, selection)
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
                 layers=int(p.get('layers') or 0), recombine=recombine)


# -- booleans, delete -------------------------------------------------------- #

def _boolean(gmsh, p):
    how = str(p.get('operation') or '')
    if how not in BOOLEANS:
        raise BuildError('boolean operation must be one of %s'
                         % ', '.join(BOOLEANS))
    objects, tools = _dimtags(p.get('objects')), _dimtags(p.get('tools'))
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
}
