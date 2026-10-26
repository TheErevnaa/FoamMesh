"""Geometry build on the Gmsh session: the catalogue and the client (Plan 41 GN3).

Gmsh's ``Geometry > Elementary entities > Add`` and ``Transform`` menus, as data:

* :class:`Parameters` is ``Add > Parameter`` -- named values; any number field
  of any operation may hold an expression (``2*R + 1``) that names them.
* :data:`OPERATIONS` is the list of operations with every field, its kind
  (number, integer, boolean, choice, or a list of picked entities of one
  dimension) and its default.  The panel renders the form from it, and
  :func:`plan` turns the typed form into the ``session.build`` call.
* :class:`GeometryBuilder` runs an operation on the project's session and
  keeps the order of what was built, so the picking panel can refresh.

Nothing here imports Qt.  A typed value is never overwritten by a derived one:
expressions are kept as typed and only evaluated into the call.
"""
from __future__ import annotations

import ast
import copy
import json
import math
import operator
import os
from dataclasses import dataclass, field
from pathlib import Path

#: Field kinds.
NUMBER, INTEGER, BOOLEAN, CHOICE, ENTITIES, TEXT = (
    'number', 'integer', 'boolean', 'choice', 'entities', 'text')
_DIM = {'Points': 0, 'Curves': 1, 'Surfaces': 2, 'Volumes': 3}


class BuildRefusal(ValueError):
    """The form cannot be built; the message says what to fix."""


# --------------------------------------------------------------------------- #
# Parameters and expressions
# --------------------------------------------------------------------------- #

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod,
           ast.FloorDiv: operator.floordiv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {name: getattr(math, name) for name in (
    'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2', 'sqrt', 'exp',
    'log', 'log10', 'floor', 'ceil', 'hypot', 'radians', 'degrees')}
_FUNCTIONS.update(abs=abs, min=min, max=max)
_CONSTANTS = {'pi': math.pi, 'Pi': math.pi, 'e': math.e}


def evaluate(expression, names: dict | None = None) -> float:
    """A number or an arithmetic expression over *names*, safely."""
    if isinstance(expression, bool):
        raise BuildRefusal('a yes/no value is not a number')
    if isinstance(expression, (int, float)):
        return float(expression)
    text = str(expression).strip()
    if not text:
        raise BuildRefusal('the value is empty')
    scope = dict(_CONSTANTS)
    scope.update(names or {})
    try:
        tree = ast.parse(text, mode='eval')
    except SyntaxError as error:
        raise BuildRefusal('%r is not a number or an expression' % text
                           ) from error

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)) and not isinstance(node.value,
                                                             bool):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in scope:
                raise BuildRefusal('%r is not a defined parameter' % node.id)
            return float(scope[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](walk(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCTIONS and not node.keywords):
            return float(_FUNCTIONS[node.func.id](
                *[walk(a) for a in node.args]))
        raise BuildRefusal('%r is not a number or an expression' % text)

    try:
        value = walk(tree)
    except BuildRefusal:
        raise
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as error:
        raise BuildRefusal('%r cannot be evaluated: %s' % (text, error)
                           ) from error
    if not math.isfinite(value):
        raise BuildRefusal('%r is not a finite number' % text)
    return value


class Parameters:
    """``Add > Parameter``: named values, each an expression of the earlier."""

    def __init__(self):
        self._rows: dict = {}

    def set(self, name: str, expression) -> float:
        name = str(name).strip()
        if not name.isidentifier() or name in _CONSTANTS \
                or name in _FUNCTIONS:
            raise BuildRefusal('%r is not a usable parameter name' % name)
        value = evaluate(expression, self.values())
        self._rows[name] = (str(expression), value)
        return value

    def remove(self, name: str) -> None:
        self._rows.pop(name, None)

    def values(self) -> dict:
        return {name: value for name, (_text, value) in self._rows.items()}

    def rows(self) -> list:
        return [(name, text, value)
                for name, (text, value) in self._rows.items()]

    def __contains__(self, name) -> bool:
        return name in self._rows


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Field:
    name: str
    title: str
    kind: str = NUMBER
    default: object = 0.0
    dim: int | None = None          # for ENTITIES: 0..3; None = any
    choices: tuple = ()
    optional: bool = False
    help: str = ''
    #: UXB3c. A length in model units: the panel shows and takes the project
    #: display unit and hands the expression over scaled to model units.
    length: bool = False


@dataclass(frozen=True)
class Operation:
    id: str
    title: str
    group: str
    fields: tuple = ()
    row: str = ''                   # catalogue row it serves
    build_kind: str = ''            # session.build kind; '' = same as id
    fixed: tuple = ()               # ((name, value), ...) always sent

    @property
    def kind(self) -> str:
        return self.build_kind or self.id

    @property
    def kernels(self) -> tuple:
        """The geometry kernels this operation exists on."""
        return KERNELS_OF.get(self.kind, ('occ',))


def _f(name, title, kind=NUMBER, default=0.0, **kw):
    return Field(name, title, kind, default, **kw)


_XYZ = (_f('x', 'X', length=True), _f('y', 'Y', length=True),
        _f('z', 'Z', length=True))
_VEC = (_f('dx', 'dX', length=True), _f('dy', 'dY', length=True),
        _f('dz', 'dZ', length=True))
_AXIS = (_f('ax', 'Axis X', default=0.0), _f('ay', 'Axis Y', default=0.0),
         _f('az', 'Axis Z', default=1.0))
_ANGLE = _f('angle', 'Sweep angle (degrees)', default=360.0, optional=True,
            help='A full turn when left at 360.')
_ENTITIES = lambda dim=None, name='entities', title='Entities': _f(   # noqa
    name, title, ENTITIES, (), dim=dim)

# Plan 48 GM6: the layer table, a picked direction or axis, QuadTri / ScaleLast.
#: The layer table (count@cumulative height rows, "2@0.4, 3@1") is edited in
#: its own widget (``layer_preview``), not as a form field; ``plan`` takes it
#: from ``values['layer_table']`` for these operations.
LAYER_TABLE_OPS = ('extrude_translate', 'extrude_rotate', 'twist',
                   'extrude_normal', 'extrude_boundary_layer')
_PICK_DIRECTION = (
    _f('direction_curve', 'Direction from a curve (start to end)', ENTITIES,
       (), dim=1, optional=True,
       help='Pick an open curve: the vector above is taken from its start '
            'to its end point.'),
    _f('direction_scale', 'Scale of the picked direction', default=1.0,
       optional=True),
    _f('direction_reverse', 'Reverse the picked direction', BOOLEAN, False))
_PICK_AXIS = (
    _f('axis_curve', 'Axis from a curve (start to end)', ENTITIES, (),
       dim=1, optional=True,
       help='Pick an open curve: the axis passes through its start point '
            'along it; the point and axis above are replaced.'),)
_QUADTRI = (
    _f('quadtri', 'QuadTri transition (built-in kernel)', CHOICE, '',
       choices=('', 'add_verts', 'no_new_verts'),
       help='Join a recombined layer to an unstructured neighbour with '
            'pyramids and tetrahedra: add_verts (QuadTriAddVerts) or '
            'no_new_verts (QuadTriNoNewVerts). Built-in kernel only.'),
    _f('scale_last', 'Scale the last layer (built-in kernel)', BOOLEAN,
       False, help='ScaleLastLayer: the last layer follows the size of the '
                   'surface mesh. Built-in kernel only.'))

#: The Gmsh filling styles (``addBSplineFilling`` / ``addBezierFilling``).
FILLING_TYPES = ('', 'Stretch', 'Curved', 'Coons')
#: geo.addGeometry: the parametric geometries a point can be placed on.
PARAMETRIC_GEOMETRIES = ('Sphere', 'PolarSphere', 'ParametricSurface')
#: Integer fields that hold a list of loop numbers, and whether the list may
#: be left blank.
TAG_LISTS = ('loops', 'shells', 'trim_wires')

GROUPS = ('Parameter', 'Points', 'Curves', 'Surfaces and volumes',
          'Primitives', 'Shapes', 'Transform', 'Extrude', 'Boolean',
          'Delete', 'Presets')

# Plan 48 GM5: the geometry kernels. OpenCASCADE is the default; the
# built-in kernel has no solids, Booleans or shape operations, and OpenCASCADE
# has no Split curve or Twist. ``tests/plan48/gm5`` checks this table against
# the one in the session (``geometry_build_v1.KERNELS_OF``).
KERNELS = ('occ', 'geo')
KERNEL_TITLES = {'occ': 'OpenCASCADE', 'geo': 'Built-in'}
DEFAULT_KERNEL = 'occ'
_BOTH = ('occ', 'geo')
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
    # Plan 48 GB (the OpenCASCADE-only ones are absent, as everywhere here)
    'copy': _BOTH, 'polyline': ('geo',), 'compound_spline': ('geo',),
    'compound_bspline': ('geo',), 'point_on_geometry': ('geo',),
    'parametric_geometry': ('geo',),
}
_OCC_PRIMITIVES = ('rectangle', 'disk', 'box', 'sphere', 'cylinder', 'cone',
                   'torus', 'wedge')
_OCC_BOOLEANS = ('boolean', 'farfield')
_WHY_NOT_BUILTIN = {
    'primitive': 'the built-in kernel has no primitive solids or surfaces '
                 '(rectangle, disk, box, sphere, cylinder, cone, torus, '
                 'wedge); build them from points, lines and extrusions',
    'boolean': 'the built-in kernel has no Boolean operations (union, '
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

OPERATIONS = (
    Operation('parameter', 'Parameter', 'Parameter',
              (_f('name', 'Name', TEXT, 'R'), _f('value', 'Value', NUMBER,
                                                 1.0),
               _f('label', 'Label (shown in the Gmsh parameter panel)', TEXT,
                  '', optional=True,
                  help='Name, label and path become the ONELAB name '
                       'path/label, as in the Gmsh Parameter dialog.'),
               _f('path', 'Path (group in the parameter panel)', TEXT, '',
                  optional=True)), 'G-01'),
    Operation('point', 'Point', 'Points',
              _XYZ + (_f('size', 'Size at point (0 = none)', default=0.0,
                         optional=True, length=True),), 'G-02'),
    # DP-5558: geo.addPointOnGeometry places a point on a parametric
    # geometry made by geo.addGeometry (a Sphere, a PolarSphere or a
    # ParametricSurface), not on a model surface; (u, v) are that geometry's
    # parameters, not lengths.
    Operation('parametric_geometry', 'Parametric geometry (built-in kernel)',
              'Points',
              (_f('shape', 'Geometry', CHOICE, 'Sphere',
                  choices=PARAMETRIC_GEOMETRIES,
                  help='Sphere and PolarSphere take the centre and the '
                       'radius; ParametricSurface takes x(u, v), y(u, v) '
                       'and z(u, v). Points are then placed on it by '
                       '"Point on a geometry" with its number.'),
               _f('x', 'Centre X', default=0.0, optional=True, length=True),
               _f('y', 'Centre Y', default=0.0, optional=True, length=True),
               _f('z', 'Centre Z', default=0.0, optional=True, length=True),
               _f('r', 'Radius', default=1.0, optional=True, length=True),
               _f('fx', 'x(u, v)', TEXT, '', optional=True),
               _f('fy', 'y(u, v)', TEXT, '', optional=True),
               _f('fz', 'z(u, v)', TEXT, '', optional=True)), 'G-02'),
    Operation('point_on_geometry', 'Point on a geometry (built-in kernel)',
              'Points',
              (_f('geometry', 'Parametric geometry number', INTEGER, 1,
                  help='The number "Parametric geometry" gave.'),
               _f('x', 'U (parameter on the geometry)'),
               _f('y', 'V (parameter on the geometry)'),
               _f('size', 'Size at point (0 = none)', default=0.0,
                  optional=True, length=True)), 'G-02'),
    Operation('line', 'Line', 'Curves',
              (_f('start', 'Start point', ENTITIES, (), dim=0),
               _f('end', 'End point', ENTITIES, (), dim=0)), 'G-03'),
    Operation('circle_arc', 'Circle arc', 'Curves',
              (_f('start', 'Start point', ENTITIES, (), dim=0),
               _f('center', 'Centre point', ENTITIES, (), dim=0),
               _f('end', 'End point', ENTITIES, (), dim=0)), 'G-03'),
    Operation('ellipse_arc', 'Ellipse arc', 'Curves',
              (_f('start', 'Start point', ENTITIES, (), dim=0),
               _f('center', 'Centre point', ENTITIES, (), dim=0),
               _f('major', 'Point on the major axis', ENTITIES, (), dim=0),
               _f('end', 'End point', ENTITIES, (), dim=0)), 'G-03'),
    Operation('circle', 'Circle (or arc by angles)', 'Curves',
              _XYZ + (_f('r', 'Radius', length=True),
                      _f('angle1', 'Start angle (degrees)', default=0.0,
                         optional=True),
                      _f('angle2', 'End angle (degrees)', default=360.0,
                         optional=True),
                      _f('zx', 'Normal X', default=0.0, optional=True),
                      _f('zy', 'Normal Y', default=0.0, optional=True),
                      _f('zz', 'Normal Z', default=0.0, optional=True,
                         help='All three zero: the Z axis.')), 'G-03'),
    Operation('ellipse', 'Ellipse (or arc by angles)', 'Curves',
              _XYZ + (_f('r1', 'Major radius', length=True),
                      _f('r2', 'Minor radius', length=True),
                      _f('angle1', 'Start angle (degrees)', default=0.0,
                         optional=True),
                      _f('angle2', 'End angle (degrees)', default=360.0,
                         optional=True),
                      _f('zx', 'Normal X', default=0.0, optional=True),
                      _f('zy', 'Normal Y', default=0.0, optional=True),
                      _f('zz', 'Normal Z', default=0.0, optional=True),
                      _f('xx', 'Major axis X', default=0.0, optional=True),
                      _f('xy', 'Major axis Y', default=0.0, optional=True),
                      _f('xz', 'Major axis Z', default=0.0, optional=True,
                         help='All three zero: the X axis.')), 'G-03'),
    Operation('wire', 'Wire (curves joined in order)', 'Curves',
              (_f('curves', 'Curves, in order', ENTITIES, (), dim=1),
               _f('check_closed', 'Check that the wire is closed', BOOLEAN,
                  False)), 'G-03'),
    Operation('split_curve', 'Split curve (built-in kernel)', 'Curves',
              (_f('curve', 'Curve', ENTITIES, (), dim=1),
               _f('points', 'Points on the curve, where it is cut', ENTITIES,
                  (), dim=0)), 'G-03'),
    Operation('fillet_2d', 'Fillet two curves (2D)', 'Curves',
              (_f('curves', 'The two curves', ENTITIES, (), dim=1),
               _f('radius', 'Radius', default=0.1, length=True),
               _f('reverse', 'Round the other corner', BOOLEAN, False)),
              'G-03'),
    Operation('chamfer_2d', 'Chamfer two curves (2D)', 'Curves',
              (_f('curves', 'The two curves', ENTITIES, (), dim=1),
               _f('distance1', 'Distance on the first curve', default=0.1,
                  length=True),
               _f('distance2', 'Distance on the second (blank = same)',
                  default=0.0, optional=True, length=True)), 'G-03'),
    Operation('spline', 'Spline', 'Curves',
              (_f('points', 'Points, in order', ENTITIES, (), dim=0),),
              'G-03'),
    Operation('bspline', 'B-spline', 'Curves',
              (_f('points', 'Control points, in order', ENTITIES, (),
                  dim=0),), 'G-03'),
    Operation('bezier', 'Bezier', 'Curves',
              (_f('points', 'Control points, in order', ENTITIES, (),
                  dim=0),), 'G-03'),
    Operation('polyline', 'Polyline (built-in kernel)', 'Curves',
              (_f('points', 'Points, in order', ENTITIES, (), dim=0),),
              'G-03'),
    Operation('compound_spline', 'Compound spline (built-in kernel)',
              'Curves',
              (_f('curves', 'Curves to sample, in order', ENTITIES, (),
                  dim=1),
               _f('intervals', 'Sampling intervals on each curve', INTEGER,
                  5)), 'G-03'),
    Operation('compound_bspline', 'Compound B-spline (built-in kernel)',
              'Curves',
              (_f('curves', 'Curves to sample, in order', ENTITIES, (),
                  dim=1),
               _f('intervals', 'Sampling intervals on each curve', INTEGER,
                  20)), 'G-03'),
    Operation('curve_loop', 'Curve loop', 'Surfaces and volumes',
              (_f('curves', 'Curves, in order', ENTITIES, (), dim=1),),
              'G-04'),
    Operation('plane_surface', 'Plane surface', 'Surfaces and volumes',
              (_f('loops', 'Curve loops (outer first, then holes)', INTEGER,
                  '', help='Loop numbers from "Curve loop", separated '
                           'by commas.'),), 'G-04'),
    Operation('surface_filling', 'Surface filling', 'Surfaces and volumes',
              (_f('loops', 'Curve loop', INTEGER, ''),), 'G-04'),
    Operation('bspline_filling', 'B-spline filling', 'Surfaces and volumes',
              (_f('loops', 'Curve loop (2 to 4 B-spline curves)', INTEGER, ''),
               _f('type', 'Filling type', CHOICE, '',
                  choices=FILLING_TYPES,
                  help='Blank or Curved: the most rounded patch; Stretch: '
                       'the flattest; Coons: a rounded patch with less '
                       'depth (every curve needs at least 4 control '
                       'points).')),
              'G-04'),
    Operation('bezier_filling', 'Bezier filling', 'Surfaces and volumes',
              (_f('loops', 'Curve loop (2 to 4 Bezier curves)', INTEGER, ''),
               _f('type', 'Filling type', CHOICE, '',
                  choices=FILLING_TYPES,
                  help='Blank or Curved: the most rounded patch; Stretch: '
                       'the flattest; Coons: a rounded patch with less '
                       'depth.')), 'G-04'),
    Operation('bspline_surface', 'B-spline surface from control points',
              'Surfaces and volumes',
              (_f('points', 'Control points, row by row', ENTITIES, (),
                  dim=0),
               _f('num_points_u', 'Control points along U (one row)',
                  INTEGER, 3),
               _f('degree_u', 'Degree along U', INTEGER, 3),
               _f('degree_v', 'Degree along V', INTEGER, 3),
               _f('weights', 'Weights (one per control point, blank = 1)',
                  TEXT, '', optional=True),
               _f('knots_u', 'Knots along U (blank = Gmsh computes them)',
                  TEXT, '', optional=True),
               _f('knots_v', 'Knots along V (blank = Gmsh computes them)',
                  TEXT, '', optional=True),
               _f('multiplicities_u', 'Knot multiplicities along U', TEXT,
                  '', optional=True),
               _f('multiplicities_v', 'Knot multiplicities along V', TEXT,
                  '', optional=True),
               _f('trim_wires', 'Trim with curve loops (outer first, then '
                                'holes)', INTEGER, '', optional=True,
                  help='Loop numbers from "Curve loop", separated by '
                       'commas. Blank: the whole patch.'),
               _f('wire_3d', 'The trimming curves are 3D curves projected '
                              'on the surface', BOOLEAN, False)), 'G-04'),
    Operation('bezier_surface', 'Bezier surface from control points',
              'Surfaces and volumes',
              (_f('points', 'Control points, row by row', ENTITIES, (),
                  dim=0),
               _f('num_points_u', 'Control points along U (one row)',
                  INTEGER, 3),
               _f('trim_wires', 'Trim with curve loops (outer first, then '
                                'holes)', INTEGER, '', optional=True,
                  help='Loop numbers from "Curve loop", separated by '
                       'commas. Blank: the whole patch.'),
               _f('wire_3d', 'The trimming curves are 3D curves projected '
                              'on the surface', BOOLEAN, False)), 'G-04'),
    Operation('trimmed_surface', 'Trim a surface', 'Surfaces and volumes',
              (_f('surface', 'Surface', ENTITIES, (), dim=2),
               _f('trim_wires', 'Trim with curve loops (outer first, then '
                                'holes)', INTEGER, ''),
               _f('wire_3d', 'The trimming curves are 3D curves projected '
                              'on the surface', BOOLEAN, False)), 'G-04'),
    Operation('surface_loop', 'Surface loop', 'Surfaces and volumes',
              (_f('surfaces', 'Surfaces', ENTITIES, (), dim=2),), 'G-04'),
    Operation('volume', 'Volume', 'Surfaces and volumes',
              (_f('shells', 'Surface loops', INTEGER, '',
                  help='Loop numbers from "Surface loop", separated by '
                       'commas.'),), 'G-04'),
    Operation('surface_from_curves', 'Surface from curves', 'Surfaces and '
                                                              'volumes',
              (_f('curves', 'Curves around the surface, in order', ENTITIES,
                  (), dim=1),), 'G-04'),
    Operation('volume_from_surfaces', 'Volume from surfaces',
              'Surfaces and volumes',
              (_f('surfaces', 'Surfaces around the volume', ENTITIES, (),
                  dim=2),), 'G-04'),
    Operation('rectangle', 'Rectangle', 'Primitives',
              _XYZ + (_f('dx', 'Width', length=True),
                      _f('dy', 'Height', length=True),
                      _f('rounded', 'Corner radius', default=0.0,
                         optional=True, length=True)), 'G-05'),
    Operation('disk', 'Disk', 'Primitives',
              _XYZ + (_f('rx', 'Radius X', length=True),
                      _f('ry', 'Radius Y (blank = circle)', default=0.0,
                         optional=True, length=True)), 'G-05'),
    Operation('box', 'Box', 'Primitives',
              _XYZ + (_f('dx', 'Length X', length=True),
                      _f('dy', 'Length Y', length=True),
                      _f('dz', 'Length Z', length=True)), 'G-05'),
    Operation('sphere', 'Sphere', 'Primitives',
              _XYZ + (_f('r', 'Radius', length=True),
                      _f('lat1', 'South latitude cut (degrees)',
                         default=-90.0, optional=True),
                      _f('lat2', 'North latitude cut (degrees)',
                         default=90.0, optional=True), _ANGLE), 'G-05'),
    Operation('cylinder', 'Cylinder', 'Primitives',
              _XYZ + _VEC + (_f('r', 'Radius', length=True), _ANGLE), 'G-05'),
    Operation('cone', 'Cone', 'Primitives',
              _XYZ + _VEC + (_f('r1', 'Start radius', length=True),
                             _f('r2', 'End radius', length=True), _ANGLE), 'G-05'),
    Operation('torus', 'Torus', 'Primitives',
              _XYZ + (_f('r1', 'Major radius', length=True),
                      _f('r2', 'Minor radius', length=True), _ANGLE), 'G-05'),
    Operation('wedge', 'Wedge', 'Primitives',
              _XYZ + _VEC + (_f('ltx', 'Top length X', default=0.0,
                                optional=True, length=True),), 'G-05'),
    Operation('pipe', 'Pipe (sweep a profile along a path)', 'Shapes',
              (_ENTITIES(name='profile', title='Profile (curve or surface)'),
               _f('path', 'Path curves, in order', ENTITIES, (), dim=1),
               _f('trihedron', 'Trihedron', CHOICE, '',
                  choices=('', 'DiscreteTrihedron', 'CorrectedFrenet',
                           'Fixed', 'Frenet', 'ConstantNormal', 'Darboux',
                           'GuideAC', 'GuidePlan', 'GuideACWithContact',
                           'GuidePlanWithContact'),
                  help='Blank: the Gmsh default (DiscreteTrihedron).')),
              'G-07'),
    Operation('thrusections', 'Loft through sections', 'Shapes',
              (_f('section1', 'Section 1 curves', ENTITIES, (), dim=1),
               _f('section2', 'Section 2 curves', ENTITIES, (), dim=1),
               _f('section3', 'Section 3 curves', ENTITIES, (), dim=1,
                  optional=True),
               _f('section4', 'Section 4 curves', ENTITIES, (), dim=1,
                  optional=True),
               _f('make_solid', 'Make a solid', BOOLEAN, True),
               _f('make_ruled', 'Ruled surfaces', BOOLEAN, False),
               _f('max_degree', 'Maximum degree (-1 = Gmsh default)',
                  INTEGER, -1),
               _f('continuity', 'Continuity', CHOICE, '',
                  choices=('', 'C0', 'G1', 'C1', 'G2', 'C2', 'C3', 'CN')),
               _f('parametrization', 'Parametrization', CHOICE, '',
                  choices=('', 'ChordLength', 'Centripetal',
                           'IsoParametric')),
               _f('smoothing', 'Smoothing', BOOLEAN, False)), 'G-07'),
    Operation('thick_solid', 'Thick solid (shell a volume)', 'Shapes',
              (_f('volume', 'Volume', ENTITIES, (), dim=3),
               _f('exclude', 'Surfaces to leave open', ENTITIES, (), dim=2,
                  optional=True),
               _f('offset', 'Thickness (negative = inwards)', default=-0.1,
                  length=True)), 'G-05'),
    Operation('offset_curve', 'Offset a curve loop', 'Shapes',
              (_f('curves', 'Curves of the loop, in order', ENTITIES, (),
                  dim=1),
               _f('offset', 'Offset', default=0.1, length=True)), 'G-03'),
    Operation('fillet', 'Fillet edges of a volume', 'Shapes',
              (_f('volumes', 'Volumes', ENTITIES, (), dim=3),
               _f('curves', 'Edges to round', ENTITIES, (), dim=1),
               _f('radii', 'Radius (one, or one per edge, with commas)',
                  TEXT, '0.1'),
               _f('remove_volume', 'Remove the original volume', BOOLEAN,
                  True)), 'G-05'),
    Operation('chamfer', 'Chamfer edges of a volume', 'Shapes',
              (_f('volumes', 'Volumes', ENTITIES, (), dim=3),
               _f('curves', 'Edges to cut', ENTITIES, (), dim=1),
               _f('surfaces', 'A face next to each edge (same count)',
                  ENTITIES, (), dim=2),
               _f('distances', 'Distance (one, or one per edge, with commas)',
                  TEXT, '0.1'),
               _f('remove_volume', 'Remove the original volume', BOOLEAN,
                  True)), 'G-05'),
    Operation('defeature', 'Defeature (remove faces)', 'Shapes',
              (_f('volumes', 'Volumes', ENTITIES, (), dim=3),
               _f('surfaces', 'Faces to remove', ENTITIES, (), dim=2),
               _f('remove_volume', 'Remove the original volume', BOOLEAN,
                  True)), 'G-05'),
    Operation('to_nurbs', 'Convert to NURBS', 'Shapes',
              (_ENTITIES(),), 'G-05'),
    Operation('heal', 'Heal the picked shapes', 'Shapes',
              (_ENTITIES(),
               _f('tolerance', 'Tolerance', default=1e-8, length=True),
               _f('fix_degenerated', 'Fix degenerated edges and faces',
                  BOOLEAN, True),
               _f('fix_small_edges', 'Fix small edges', BOOLEAN, True),
               _f('fix_small_faces', 'Fix small faces', BOOLEAN, True),
               _f('sew_faces', 'Sew faces', BOOLEAN, True),
               _f('make_solids', 'Make solids', BOOLEAN, True)), 'G-05'),
    Operation('translate', 'Translate', 'Transform',
              (_ENTITIES(),) + _VEC + (
                  _f('copy', 'Copy (keep the original)', BOOLEAN, False),),
              'G-06'),
    Operation('rotate', 'Rotate', 'Transform',
              (_ENTITIES(),) + _XYZ + _AXIS + (
                  _f('angle', 'Angle (degrees)', default=90.0),
                  _f('copy', 'Copy (keep the original)', BOOLEAN, False)),
              'G-06'),
    Operation('scale', 'Scale', 'Transform',
              (_ENTITIES(),) + _XYZ + (
                  _f('a', 'Factor X', default=1.0),
                  _f('b', 'Factor Y', default=1.0),
                  _f('c', 'Factor Z', default=1.0),
                  _f('copy', 'Copy (keep the original)', BOOLEAN, False)),
              'G-06'),
    Operation('mirror', 'Symmetry (mirror)', 'Transform',
              (_ENTITIES(),
               _f('a', 'Plane A (a x + b y + c z + d = 0)', default=1.0),
               _f('b', 'Plane B', default=0.0),
               _f('c', 'Plane C', default=0.0),
               _f('d', 'Plane D', default=0.0, length=True),
               _f('copy', 'Copy (keep the original)', BOOLEAN, True)),
              'G-06'),
    Operation('copy', 'Copy', 'Transform', (_ENTITIES(),), 'G-06'),
    Operation('affine_transform', 'Affine transformation (matrix)',
              'Transform',
              (_ENTITIES(),
               _f('matrix', '12 or 16 numbers of the 4 × 4 matrix, row by row',
                  TEXT, '1,0,0,0, 0,1,0,0, 0,0,1,0'),
               _f('copy', 'Copy (keep the original)', BOOLEAN, False)),
              'G-06'),
    Operation('extrude_translate', 'Extrude along a vector', 'Extrude',
              (_ENTITIES(),) + _VEC + _PICK_DIRECTION + (
                  _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
                  _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                     False)) + _QUADTRI, 'G-07', 'extrude',
              (('mode', 'translate'),)),
    Operation('extrude_rotate', 'Extrude by rotation (revolve)', 'Extrude',
              (_ENTITIES(),) + _XYZ + _AXIS + _PICK_AXIS + (
                  _f('angle', 'Angle (degrees)', default=360.0),
                  _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
                  _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                     False)) + _QUADTRI, 'G-07', 'extrude',
              (('mode', 'rotate'),)),
    Operation('twist', 'Twisting extrusion (built-in kernel)', 'Extrude',
              (_ENTITIES(),) + _XYZ + _VEC + _AXIS + _PICK_DIRECTION
              + _PICK_AXIS + (
                  _f('angle', 'Twist angle (degrees)', default=30.0),
                  _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
                  _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                     False)), 'G-07'),
    Operation('extrude_normal', 'Extrude along the normal', 'Extrude',
              (_ENTITIES(2, title='Surfaces'),
               _f('distance', 'Distance', default=1.0, length=True),
               _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
               _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                  False)) + _QUADTRI, 'G-07', 'extrude',
              (('mode', 'normal'),)),
    Operation('extrude_boundary_layer',
              'Extrude along the normals (boundary layer, built-in kernel)',
              'Extrude',
              (_ENTITIES(title='Curves or surfaces'),
               _f('thickness', 'Total thickness (0 = from a view)',
                  default=0.1, length=True),
               _f('layers', 'Layers (0 = one)', INTEGER, 0),
               _f('recombine', 'Recombine (hexes / quads)', BOOLEAN, True),
               _f('second', 'Second order layers', BOOLEAN, False),
               _f('view_index', 'View carrying the thickness (-1 = none)',
                  INTEGER, -1)), 'G-07'),
    Operation('fuse', 'Union (fuse)', 'Boolean',
              (_ENTITIES(name='objects', title='Objects'),
               _ENTITIES(name='tools', title='Tools'),
               _f('remove_object', 'Remove the objects', BOOLEAN, True),
               _f('remove_tool', 'Remove the tools', BOOLEAN, True)),
              'G-08', 'boolean', (('operation', 'fuse'),)),
    Operation('intersect', 'Intersection', 'Boolean',
              (_ENTITIES(name='objects', title='Objects'),
               _ENTITIES(name='tools', title='Tools'),
               _f('remove_object', 'Remove the objects', BOOLEAN, True),
               _f('remove_tool', 'Remove the tools', BOOLEAN, True)),
              'G-08', 'boolean', (('operation', 'intersect'),)),
    Operation('cut', 'Difference (cut)', 'Boolean',
              (_ENTITIES(name='objects', title='Objects'),
               _ENTITIES(name='tools', title='Tools'),
               _f('remove_object', 'Remove the objects', BOOLEAN, True),
               _f('remove_tool', 'Remove the tools', BOOLEAN, True)),
              'G-08', 'boolean', (('operation', 'cut'),)),
    Operation('fragment', 'Fragments', 'Boolean',
              (_ENTITIES(name='objects', title='Objects'),
               _f('tools', 'Tools (blank = the objects fragment each '
                            'other)', ENTITIES, (), optional=True),
               _f('remove_object', 'Remove the objects', BOOLEAN, True),
               _f('remove_tool', 'Remove the tools', BOOLEAN, True)),
              'G-08', 'boolean', (('operation', 'fragment'),)),
    Operation('delete', 'Delete', 'Delete',
              (_ENTITIES(), _f('recursive', 'Recursive (also the entities '
                                            'it is made of)', BOOLEAN,
                               False)), 'G-09'),
    Operation('case_2d', '2D case: extrude one cell', 'Presets',
              (_ENTITIES(2, title='Surfaces'),
               _f('thickness', 'Thickness', default=0.1, length=True),
               _f('nx', 'Direction X', default=0.0),
               _f('ny', 'Direction Y', default=0.0),
               _f('nz', 'Direction Z', default=1.0),
               _f('naming', 'Front and back patches', CHOICE, 'one_group',
                  choices=('one_group', 'pair'),
                  help='one_group: both faces in one patch (the OpenFOAM '
                       'empty pair); pair: front and back apart.'),
               _f('front_back_name', 'Patch name (both faces)', TEXT,
                  'frontAndBack', optional=True),
               _f('front_name', 'Front patch name (pair)', TEXT, 'front',
                  optional=True),
               _f('back_name', 'Back patch name (pair)', TEXT, 'back',
                  optional=True),
               _f('sides_name', 'Side patch name (blank: leave unnamed)',
                  TEXT, '', optional=True),
               _f('fluid_name', 'Volume name (blank: leave unnamed)', TEXT,
                  '', optional=True)), 'G-07'),
    Operation('axisymmetric_wedge', 'Axisymmetric wedge', 'Presets',
              (_ENTITIES(2, title='Surfaces'),
               _f('angle', 'Wedge angle (degrees)', default=5.0),
               _f('symmetric', 'Symmetric about the picked plane', BOOLEAN,
                  True,
                  help='Half the angle on each side of the picked plane, '
                       'which is what an OpenFOAM wedge expects. Off: the '
                       'wedge is revolved from the picked plane on one '
                       'side only.')) + _XYZ + (
                  _f('ax', 'Axis X', default=1.0),
                  _f('ay', 'Axis Y', default=0.0),
                  _f('az', 'Axis Z', default=0.0)) + _PICK_AXIS + (
                  _f('front_name', 'Front patch name', TEXT, 'wedgeFront',
                     optional=True),
                  _f('back_name', 'Back patch name', TEXT, 'wedgeBack',
                     optional=True),
                  _f('sides_name', 'Side patch name (blank: leave unnamed)',
                     TEXT, '', optional=True),
                  _f('fluid_name', 'Volume name (blank: leave unnamed)',
                     TEXT, '', optional=True)), 'G-07'),
    Operation('farfield', 'Farfield (primitive and cut)', 'Presets',
              (_f('shape', 'Shape', CHOICE, 'box',
                  choices=('box', 'sphere', 'cylinder')),
               _f('padding', 'Padding (box: model diagonals)', default=2.0),
               _f('radius', 'Radius (sphere, cylinder)', default=0.0,
                  optional=True, length=True),
               _f('length', 'Length (cylinder)', default=0.0,
                  optional=True, length=True)), 'G-05, G-08'),
)

_BY_ID = {op.id: op for op in OPERATIONS}


def operation(op_id: str) -> Operation:
    try:
        return _BY_ID[op_id]
    except KeyError:
        raise BuildRefusal('there is no geometry operation %r' % op_id
                           ) from None


def operations_in(group: str) -> tuple:
    return tuple(op for op in OPERATIONS if op.group == group)


def unavailable(op_id: str, kernel: str) -> str:
    """Why *op_id* cannot be used on *kernel* (the panel greys it with this),
    or ``''`` when it can."""
    if kernel not in KERNELS:
        raise BuildRefusal('the geometry kernel must be one of %s'
                           % ', '.join(KERNELS))
    op = operation(op_id)
    if kernel in op.kernels:
        return ''
    if kernel == 'geo':
        if op.kind in _OCC_PRIMITIVES:
            return _WHY_NOT_BUILTIN['primitive']
        if op.kind in _OCC_BOOLEANS:
            return _WHY_NOT_BUILTIN['boolean']
        return _WHY_NOT_BUILTIN['shape']
    return _WHY_NOT_OCC.get(op.kind, 'this operation exists only on the '
                                     'built-in kernel')


def available(kernel: str) -> tuple:
    """The operations that exist on *kernel*."""
    return tuple(op for op in OPERATIONS if not unavailable(op.id, kernel))


# --------------------------------------------------------------------------- #
# From a typed form to the session call
# --------------------------------------------------------------------------- #

def _entity_list(op, spec, raw) -> list:
    items = [tuple(e) for e in (raw or ())]
    if not items:
        raise BuildRefusal('%s: pick at least one entity in the viewport'
                           % spec.title)
    out = []
    for dim, tag in items:
        if spec.dim is not None and dim != spec.dim:
            raise BuildRefusal('%s takes %s, but a %s was picked'
                               % (spec.title, _dim_word(spec.dim, True),
                                  _dim_word(dim)))
        out.append((int(dim), int(tag)))
    return out


def _dim_word(dim, plural=False):
    word = ('point', 'curve', 'surface', 'volume')[dim]
    return word + 's' if plural else word


def _tag_list(spec, raw) -> list:
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = [p for p in str(raw).replace(';', ',').split(',')
                 if p.strip()]
    try:
        tags = [int(float(p)) for p in parts]
    except (TypeError, ValueError):
        raise BuildRefusal('%s: give whole numbers separated by commas'
                           % spec.title) from None
    if not tags:
        raise BuildRefusal('%s: give at least one number' % spec.title)
    return tags


def plan(op_id: str, values: dict, parameters: Parameters | None = None
         ) -> tuple:
    """``(kind, kwargs)`` for ``session.build``, or a :class:`BuildRefusal`.

    *values* holds what the form holds: numbers or expressions for number
    fields, ``(dim, tag)`` pairs for entity fields.  Entities that are
    points, curves or surfaces *as tags* (a line from two points) are sent as
    plain tag lists; transforms, extrusions, booleans and delete are sent as
    dimension-tag pairs.
    """
    op = operation(op_id)
    names = parameters.values() if parameters else {}
    sent: dict = {}
    for spec in op.fields:
        raw = values.get(spec.name, spec.default)
        if spec.kind == ENTITIES and spec.optional and not raw:
            continue
        if spec.kind == ENTITIES:
            picked = _entity_list(op, spec, raw)
            if spec.dim is not None and spec.name != 'entities':
                sent[spec.name] = [tag for _dim, tag in picked]
                if spec.name in ('start', 'end', 'center', 'major'):
                    if len(picked) != 1:
                        raise BuildRefusal('%s: pick exactly one point'
                                           % spec.title)
                    sent[spec.name] = picked[0][1]
            else:
                sent[spec.name] = [list(e) for e in picked]
        elif spec.kind == BOOLEAN:
            sent[spec.name] = bool(raw)
        elif spec.kind == CHOICE:
            if raw not in spec.choices and not (raw is None
                                                and '' in spec.choices):
                raise BuildRefusal('%s must be one of %s'
                                   % (spec.title, ', '.join(spec.choices)))
            sent[spec.name] = raw if raw is not None else ''
        elif spec.kind == TEXT:
            text = str(raw if raw is not None else '').strip()
            if not text and spec.optional:
                continue
            if not text:
                raise BuildRefusal('%s is empty' % spec.title)
            sent[spec.name] = text
        elif spec.kind == INTEGER and spec.name in TAG_LISTS:
            if spec.optional and (raw is None or str(raw).strip() == ''):
                continue
            sent[spec.name] = _tag_list(spec, raw)
        elif spec.kind == INTEGER:
            number = evaluate(raw if str(raw).strip() != '' else 0, names)
            if number != int(number):
                raise BuildRefusal('%s must be a whole number' % spec.title)
            sent[spec.name] = int(number)
        else:
            if spec.optional and (raw is None or str(raw).strip() == ''):
                continue
            try:
                sent[spec.name] = evaluate(raw, names)
            except BuildRefusal as error:
                raise BuildRefusal('%s: %s' % (spec.title, error)) from None
    for name, value in op.fixed:
        sent[name] = value
    if op.id in LAYER_TABLE_OPS and str(
            values.get('layer_table') or '').strip():
        sent['layer_table'] = str(values['layer_table'])
    if op.id == 'parameter':
        # Evaluated against the table: the table keeps the expression.
        pass
    _check(op, sent)
    return op.kind, sent


def _check(op, sent) -> None:
    if 'layer_table' in sent:
        from foammesh.core.gmsh import layer_preview
        try:
            rows = layer_preview.parse_table(sent.pop('layer_table'))
        except layer_preview.LayerTableRefusal as error:
            raise BuildRefusal('Layer table: %s' % error) from None
        if rows:
            sent['layer_counts'], sent['layer_heights'] = (
                layer_preview.counts_heights(rows))
    for name in ('direction_curve', 'axis_curve'):
        if name in sent:
            if len(sent[name]) != 1:
                raise BuildRefusal('pick exactly one curve for %s'
                                   % name.replace('_', ' '))
            sent[name] = sent[name][0]
    if op.id == 'case_2d' and not (sent.get('thickness', 0) > 0):
        raise BuildRefusal('the thickness must be greater than zero')
    if op.id == 'axisymmetric_wedge':
        if not 0 < sent.get('angle', 0) < 180:
            raise BuildRefusal('the wedge angle must be between 0 and 180 '
                               'degrees')
        if 'axis_curve' not in sent and not any(
                sent.get(k) for k in ('ax', 'ay', 'az')):
            raise BuildRefusal('the wedge axis is the zero vector')
    if op.id == 'extrude_boundary_layer' and not (
            sent.get('thickness', 0) > 0 or sent.get('view_index', -1) >= 0):
        raise BuildRefusal('give a thickness greater than zero, or the '
                           'index of a view that carries it')
    if op.id == 'thrusections':
        sections = [sent.pop(n) for n in ('section1', 'section2', 'section3',
                                          'section4') if n in sent]
        if len(sections) < 2:
            raise BuildRefusal('a loft needs at least two sections')
        sent['sections'] = sections
    if op.id == 'split_curve' and len(sent['curve']) != 1:
        raise BuildRefusal('split one curve at a time')
    if op.id == 'trimmed_surface' and len(sent['surface']) != 1:
        raise BuildRefusal('trim one surface at a time')
    if op.id in ('compound_spline', 'compound_bspline') \
            and sent['intervals'] < 1:
        raise BuildRefusal('sample each curve at least once')
    if op.id == 'parametric_geometry':
        if sent['shape'] == 'ParametricSurface':
            if not all(sent.get(k) for k in ('fx', 'fy', 'fz')):
                raise BuildRefusal('a parametric surface needs x(u, v), '
                                   'y(u, v) and z(u, v)')
            for k in ('x', 'y', 'z', 'r'):
                sent.pop(k, None)
        else:
            if not sent.get('r', 0) > 0:
                raise BuildRefusal('the radius must be greater than zero')
            for k in ('fx', 'fy', 'fz'):
                sent.pop(k, None)
    if op.id == 'fragment' and not sent.get('tools'):
        sent.pop('tools', None)
    if op.id == 'circle' and not (sent.get('r', 0) > 0):
        raise BuildRefusal('the radius must be greater than zero')
    if op.id == 'extrude_translate' and 'direction_curve' not in sent \
            and not any(sent.get(k) for k in ('dx', 'dy', 'dz')):
        raise BuildRefusal('the extrusion vector is zero')
    if op.id == 'extrude_rotate' and 'axis_curve' not in sent \
            and not any(sent.get(k) for k in ('ax', 'ay', 'az')):
        raise BuildRefusal('the rotation axis is the zero vector')
    if op.id == 'rotate' and not any(sent.get(k) for k in ('ax', 'ay',
                                                           'az')):
        raise BuildRefusal('the rotation axis is the zero vector')
    if op.id in ('fuse', 'intersect', 'cut'):
        if not sent['objects'] or not sent['tools']:
            raise BuildRefusal('pick objects and tools')
    if op.id == 'fragment' and not sent['objects']:
        raise BuildRefusal('pick the objects to fragment')
    if op.id == 'farfield' and sent.get('shape') != 'box':
        if not sent.get('radius') and not sent.get('length'):
            # the shared specification derives them from the model
            sent.pop('radius', None)
            sent.pop('length', None)
    if op.id in ('farfield',):
        for key in ('radius', 'length'):
            if sent.get(key) == 0.0:
                sent.pop(key, None)
    if op.id == 'disk' and not sent.get('ry'):
        sent.pop('ry', None)


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Boundary roles of the 2D-case and wedge presets (Plan 48 WG, DP-4215)
# --------------------------------------------------------------------------- #
#
# The presets make physical groups and say which of them are the front and
# back faces of a one-cell-thick mesh. That fact belongs to the project, not
# to the Gmsh session: the export, the batch run and the publisher all need
# it, and none of them can read it off the group's name (a model called
# ``wedge`` is not an axisymmetric case). The rows live in
# ``<case>/foammesh/gmsh/boundary_roles.json`` keyed by the group's tag, so a
# rename in the physical-group table keeps the role.

#: The categories a role may be given, by the category the preset chose.
ROLE_CATEGORIES = {
    'empty': ('empty', 'symmetry', 'wall'),
    'wedge': ('wedge', 'symmetry', 'wall'),
    'wall': ('wall', 'inlet', 'outlet', 'symmetry', 'far_field', 'patch'),
}
#: Categories that describe a mesh, not a surface (see
#: ``layer_targets.MESH_CONSTRAINED_CATEGORIES``).
CONSTRAINED = ('empty', 'wedge')
ROLES_FILE = 'boundary_roles.json'


def roles_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / ROLES_FILE


def load_roles(case_path) -> list:
    """The saved role rows; unreadable is none."""
    try:
        document = json.loads(roles_path(case_path).read_text(
            encoding='utf-8'))
    except (OSError, ValueError):
        return []
    rows = document.get('roles') if isinstance(document, dict) else None
    return [dict(r) for r in (rows or ()) if isinstance(r, dict)
            and isinstance(r.get('tag'), int) and r.get('name')]


def save_roles(case_path, rows) -> None:
    target = roles_path(case_path)
    if not rows:
        try:
            target.unlink()
        except OSError:
            pass
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix('.json.tmp')
    scratch.write_text(json.dumps({'schema_version': 1, 'roles': list(rows)},
                                  indent=2), encoding='utf-8')
    os.replace(scratch, target)


def role_categories(rows) -> dict:
    """``{patch name: category}`` for every declared role."""
    return {str(r['name']): str(r.get('category') or r.get('default')
                                or 'wall') for r in rows or ()}


def constrained_roles(rows) -> dict:
    """``{patch name: category}`` for the roles that stand for the front or
    back face of a one-cell-thick mesh (``empty`` or ``wedge``)."""
    return {name: category for name, category in role_categories(rows).items()
            if category in CONSTRAINED}


def _group_rows(groups) -> list:
    """``(dim, tag, name, surfaces)`` for *groups* given as ``(dim, tag,
    name)`` or ``(dim, tag, name, entities)``."""
    out = []
    for group in groups:
        dim, tag, name = int(group[0]), int(group[1]), str(group[2])
        held = (frozenset(int(t) for t in group[3])
                if len(group) > 3 and group[3] is not None else None)
        out.append((dim, tag, name, held))
    return out


def follow_rows(rows, groups) -> tuple:
    """``(rows, changed)`` with each row on the name its group has now.

    *groups* is ``(dim, tag, name[, entities])`` for every group of the
    model. A row follows the group that holds its ``surfaces`` (DP-5725: a
    re-import renumbers the groups, so a tag alone names another group or
    none), else the group of its name, else, for a row without surfaces,
    the group of its tag. A row whose group is gone is dropped."""
    table = _group_rows(groups)
    kept, changed = [], False
    for row in rows:
        dim = int(row.get('dim', 2))
        surfaces = frozenset(int(t) for t in row.get('surfaces') or ())
        found = None
        if surfaces:
            found = next((g for g in table if g[0] == dim and g[3] is not None
                          and g[3] == surfaces), None)
        if found is None:
            found = next((g for g in table if g[0] == dim
                          and g[2] == row['name']), None)
        if found is None and not surfaces:
            found = next((g for g in table if g[0] == dim
                          and g[1] == int(row['tag'])), None)
        if found is None:
            changed = True
            continue
        row = dict(row)
        if found[2] != row['name'] or found[1] != int(row['tag']):
            row['name'], row['tag'] = found[2], found[1]
            changed = True
        kept.append(row)
    return kept, changed


def publish_roles(case_path, session=None) -> dict:
    """What the publisher is told about the declared patches of a project.

    ``{'rows', 'categories', 'declared'}``: *categories* maps every declared
    patch to the category it publishes with, *declared* only the ``empty`` and
    ``wedge`` ones (what ``publish.publish(declared=...)`` takes).  With a
    *session* (anything with ``call``) the rows first follow a rename made in
    the physical-group table, and the file is updated.
    """
    rows = load_roles(case_path)
    if rows and session is not None:
        try:
            reply = session.call('session.physical_groups')
            groups = [(g['dim'], g['tag'], g['name'],
                       g.get('entities'))
                      for g in reply['groups']]
        except Exception:                                  # noqa: BLE001
            groups = None
        if groups is not None:
            rows, changed = follow_rows(rows, groups)
            if changed:
                save_roles(case_path, rows)
    return {'rows': rows, 'categories': role_categories(rows),
            'declared': constrained_roles(rows)}


# -- what a build took off the imported model (DP-5716) -------------------- #
#
# The session follows every build step with the batch run's own stage code
# (``stages.build_follow``): a prepared surface the build took away loses its
# name. The names it replaced are kept in
# ``<case>/foammesh/gmsh/build_replaced.json`` so the pages and the job
# writer can say so without a Gmsh session of their own.
REPLACED_FILE = 'build_replaced.json'


def replaced_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / REPLACED_FILE


def _load_build_model(case_path) -> dict:
    try:
        document = json.loads(replaced_path(case_path).read_text(
            encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def load_replaced(case_path) -> list:
    """The prepared patch names the project's geometry build replaced."""
    names = _load_build_model(case_path).get('replaced')
    return sorted({str(n) for n in names or () if str(n).strip()})


def load_built_surfaces(case_path) -> list:
    """DP-5717. ``[{'tag', 'name', 'group'}]``: the surfaces of the model
    the geometry build left, named as the run names them
    (``stages.build_follow.built_surface_name``). Empty when the build
    replaced nothing: the prepared patches are then still the model's."""
    document = _load_build_model(case_path)
    if not document.get('replaced'):
        return []
    rows = []
    for row in document.get('surfaces') or ():
        if not isinstance(row, dict) or not str(row.get('name') or '').strip():
            continue
        try:
            tag = int(row['tag'])
        except (KeyError, TypeError, ValueError):
            continue
        group = row.get('group')
        rows.append({'tag': tag, 'name': str(row['name']).strip(),
                     'group': int(group) if isinstance(group, int) else None})
    return rows


def built_patches(case_path) -> list:
    """DP-5717. ``(patch name, category)`` of the built model, one per name,
    in tag order; a name a preset declared publishes with its role's
    category (front/back ``empty``), the rest are walls. Empty when the
    build replaced nothing."""
    categories = role_categories(load_roles(case_path))
    out, seen = [], set()
    for row in sorted(load_built_surfaces(case_path), key=lambda r: r['tag']):
        if row['name'] in seen:
            continue
        seen.add(row['name'])
        out.append((row['name'], categories.get(row['name'], 'wall')))
    return out


def built_model(case_path) -> dict:
    """DP-5717. What the window lists in place of the prepared patches once
    the project's geometry build replaced them, or ``{}``.

    ``{'replaced': [...], 'patches': [{'name', 'category', 'surfaces'}]}``:
    one row per patch name of the built model, as the run names it, with
    the category it publishes with and how many surfaces it covers. Only
    while the op log still holds build steps: a project whose build was
    taken back is on its prepared patches again.
    """
    try:
        if not replaced_path(case_path).is_file():
            return {}
    except (OSError, TypeError, ValueError):
        return {}
    from .execution import geometry_build_steps

    try:
        if not geometry_build_steps(case_path):
            return {}
    except Exception:                                    # noqa: BLE001
        return {}
    surfaces = load_built_surfaces(case_path)
    if not surfaces:
        return {}
    counts: dict[str, int] = {}
    for row in surfaces:
        counts[row['name']] = counts.get(row['name'], 0) + 1
    return {'replaced': load_replaced(case_path),
            'patches': [{'name': name, 'category': category,
                         'surfaces': counts.get(name, 1)}
                        for name, category in built_patches(case_path)]}


def save_replaced(case_path, names, surfaces=None) -> None:
    """Keep what the build replaced and, with it, the surfaces it left."""
    target = replaced_path(case_path)
    names = sorted({str(n) for n in names or () if str(n).strip()})
    if not names and not surfaces:
        try:
            target.unlink()
        except OSError:
            pass
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix('.json.tmp')
    document = {'schema_version': 2, 'replaced': names}
    if surfaces:
        document['surfaces'] = [dict(row) for row in surfaces]
    scratch.write_text(json.dumps(document, indent=2), encoding='utf-8')
    os.replace(scratch, target)


def follow_built_names(case_path, groups) -> bool:
    """A rename in the physical-group table renames the built surface the
    group holds. *groups* is ``(dim, tag, name[, entities])``; a surface
    follows the group that holds it (DP-5725), or, when *groups* carry no
    entities, the group of its recorded tag. True when it changed."""
    document = _load_build_model(case_path)
    rows = document.get('surfaces') or []
    table = [g for g in _group_rows(groups) if g[0] == 2]
    holders = {}
    for _dim, tag, name, held in table:
        for surface in held or ():
            holders.setdefault(surface, (tag, name))
    by_tag = {tag: name for _d, tag, name, held in table if held is None}
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        found = holders.get(int(row.get('tag', -1)))
        if found is not None:
            group, name = found
        else:
            group = row.get('group')
            name = by_tag.get(group) if isinstance(group, int) else None
            if name is None:
                continue
        if name != row.get('name') or group != row.get('group'):
            row['name'], row['group'] = name, group
            changed = True
    if changed:
        save_replaced(case_path, document.get('replaced'), rows)
    return changed


def built_surface_names(case_path) -> dict:
    """DP-5725. ``{surface tag (str): name}`` of the built model as the
    window last named it (a group-table rename included), for the run's
    job; empty without a build."""
    out = {}
    for row in _load_build_model(case_path).get('surfaces') or ():
        if not isinstance(row, dict) or not str(row.get('name') or '').strip():
            continue
        try:
            out[str(int(row['tag']))] = str(row['name']).strip()
        except (KeyError, TypeError, ValueError):
            continue
    return out


def replaced_layer_note(patches, replaced) -> str:
    """What a person is told when ticked layer walls were replaced."""
    lost = [name for name in patches or () if name in set(replaced or ())]
    if not lost:
        return ''
    return ('The geometry build replaced %s, so no boundary layer grows '
            'there: the run drops %s.' % (
                ', '.join(lost), 'it' if len(lost) == 1 else 'them'))


@dataclass
class BuildStep:
    op_id: str
    title: str
    created: tuple
    entities: dict = field(default_factory=dict)
    loop: int | None = None          # the loop a curve/surface loop made
    kernel: str = DEFAULT_KERNEL
    parameter: str = ''              # the name a Parameter step set
    roles: dict = field(default_factory=dict)   # front / back / sides (WG)
    #: The form as it was typed (picked entities as ``(dim, tag)``), so a
    #: kernel change can build the step again (Plan 48 GB).
    values: dict = field(default_factory=dict)
    #: The number a Parametric geometry step made (DP-5558); a point on a
    #: geometry names it.
    geometry: int | None = None


class GeometryBuilder:
    """Runs build operations on a session and remembers what was built."""

    def __init__(self, session_provider, parameters: Parameters | None = None):
        self._session_provider = session_provider
        self.parameters = parameters or Parameters()
        self.steps: list = []
        self._kernel = DEFAULT_KERNEL
        #: Scripts set aside when the kernel was changed: (kernel, steps).
        self.archive: list = []
        #: What the last kernel change built again and refused (Plan 48 GB,
        #: DP-5550): ``{'from', 'kernel', 'reimported', 'refused', ...}``.
        self.last_reimport: dict | None = None
        #: The declared boundary roles (see :func:`load_roles`); loaded from
        #: the project on first use.
        self._roles: list | None = None
        #: Called when a kernel change has emptied the model (DP-5730), so
        #: the owner can bring the prepared geometry in again before the
        #: steps are built again, in the batch run's order.
        self.on_restart = None

    @property
    def kernel(self) -> str:
        return self._kernel

    def reimport_preview(self, kernel: str) -> dict:
        """What a change to *kernel* would do with the built steps.

        ``{'kept': [...], 'refused': [...]}``: every built step is in one of
        the two lists, with its number, operation and (for a refused one) the
        reason the other kernel gives.  Nothing is changed."""
        if kernel not in KERNELS:
            raise BuildRefusal('the geometry kernel must be one of %s'
                               % ', '.join(KERNELS))
        kept, refused = [], []
        for number, step in enumerate(self.steps, 1):
            row = {'step': number, 'operation_id': step.op_id,
                   'title': step.title}
            reason = unavailable(step.op_id, kernel)
            if reason:
                refused.append(dict(row, reason=reason))
            else:
                kept.append(row)
        return {'kernel': kernel, 'kept': kept, 'refused': refused}

    def set_kernel(self, kernel: str, confirm: bool = False,
                   reimport: bool = True) -> dict:
        """Choose the geometry kernel of the project.

        Gmsh cannot mix the two kernels in one model.  With nothing built the
        change is free; with geometry built it is refused until *confirm*.
        Then the model is emptied, the built steps are kept in
        :attr:`archive` (their script stays exportable) and, with *reimport*,
        every step the new kernel has is built again in order, the entities
        it refers to carried over from the first run.  A step the new kernel
        lacks, or one that needs an entity a refused step made, is refused
        by name in ``refused``; nothing is dropped silently.
        """
        if kernel not in KERNELS:
            raise BuildRefusal('the geometry kernel must be one of %s'
                               % ', '.join(KERNELS))
        if kernel == self._kernel:
            return {'kernel': kernel, 'cleared': False}
        built = [s for s in self.steps if s.op_id != 'parameter']
        if built and not confirm:
            preview = self.reimport_preview(kernel)
            raise BuildRefusal(
                'changing the geometry kernel to %s empties the model (%d '
                'steps built with %s cannot be mixed with it); %d of the '
                'steps are built again, %d have no equivalent and are '
                'refused by name; confirm to continue'
                % (KERNEL_TITLES[kernel], len(built),
                   KERNEL_TITLES[self._kernel], len(preview['kept']),
                   len(preview['refused'])))
        if not built:
            self._kernel = kernel
            return {'kernel': kernel, 'cleared': False}
        session = self._session_provider()
        old = list(self.steps)
        before = dict(old[-1].entities) if old else None
        try:
            emptied = session.call('session.restart_model')
        except Exception as error:                       # noqa: BLE001
            raise BuildRefusal(_clean(error)) from error
        if self.on_restart is not None:
            self.on_restart()
        self.archive.append((self._kernel, tuple(old)))
        origin = self._kernel
        self.steps = []
        self._set_roles([])
        self._kernel = kernel
        result = {'kernel': kernel, 'cleared': True}
        if reimport:
            result.update(self._reimport(old))
        else:
            result.update({'reimported': [], 'refused': []})
        result['from'] = origin
        result['entities_before'] = before
        result['entities'] = (
            dict(self.steps[-1].entities) if self.steps else
            dict((emptied or {}).get('entities') or {}))
        self.last_reimport = dict(result)
        return result

    # -- the kernel change builds the steps again (Plan 48 GB) ------------ #

    _SURFACE_LOOPS = ('surface_loop', 'volume_from_surfaces')

    def _reimport(self, old) -> dict:
        maps = {'entity': {}, 'curve_loop': {}, 'surface_loop': {}}
        reimported, refused = [], []
        for number, step in enumerate(old, 1):
            row = {'step': number, 'operation_id': step.op_id,
                   'title': step.title}
            reason = unavailable(step.op_id, self._kernel)
            if reason:
                refused.append(dict(row, reason=reason))
                continue
            try:
                values = self._carried_values(step, maps)
                new = self.run(step.op_id, values)
            except BuildRefusal as error:
                refused.append(dict(row, reason=str(error)))
                continue
            note = self._carry(step, new, maps)
            row['created'] = [list(c) for c in new.created]
            if note:
                row['note'] = note
            reimported.append(row)
        return {'reimported': reimported, 'refused': refused}

    def _carried_values(self, step, maps) -> dict:
        """The form of *step* with every entity it names on the new
        kernel's tags; a reference that cannot be carried over refuses."""
        out = copy.deepcopy(dict(step.values))
        for spec in operation(step.op_id).fields:
            if spec.name not in out:
                continue
            raw = out[spec.name]
            if spec.kind == ENTITIES:
                items = []
                for entity in raw or ():
                    dim, tag = int(entity[0]), int(entity[1])
                    new = maps['entity'].get((dim, tag))
                    if new is None:
                        raise BuildRefusal(
                            '%s uses %s %d, which no step built again on '
                            'the %s kernel has made'
                            % (spec.title, _dim_word(dim), tag,
                               KERNEL_TITLES[self._kernel]))
                    items.append((dim, new))
                out[spec.name] = items
            elif spec.kind == INTEGER and spec.name in TAG_LISTS:
                if raw is None or str(raw).strip() == '':
                    continue
                space = ('surface_loop' if spec.name == 'shells'
                         else 'curve_loop')
                tags = []
                for tag in _tag_list(spec, raw):
                    new = maps[space].get(tag)
                    if new is None:
                        raise BuildRefusal(
                            '%s uses loop %d, which no step built again on '
                            'the %s kernel has made'
                            % (spec.title, tag, KERNEL_TITLES[self._kernel]))
                    tags.append(new)
                out[spec.name] = ','.join(str(t) for t in tags)
        return out

    def _carry(self, old, new, maps) -> str:
        """Remember which new entity stands for each one *old* made."""
        notes = []
        for dim in range(4):
            before = [t for d, t in old.created if d == dim]
            after = [t for d, t in new.created if d == dim]
            for was, now in zip(before, after):
                maps['entity'][(dim, int(was))] = int(now)
            if len(before) != len(after):
                notes.append('%s made %d %s, %s now makes %d; the ones '
                             'beyond %d are not carried over'
                             % (KERNEL_TITLES[old.kernel], len(before),
                                _dim_word(dim, True),
                                KERNEL_TITLES[new.kernel], len(after),
                                min(len(before), len(after))))
        if old.loop is not None and new.loop is not None:
            space = ('surface_loop' if old.op_id in self._SURFACE_LOOPS
                     else 'curve_loop')
            maps[space][int(old.loop)] = int(new.loop)
        return '; '.join(notes)

    def remove_last(self, count: int = 1) -> dict:
        """"Remove last script command": take back the last *count* steps.

        The session empties the model and runs the rest of its log again; the
        result says what it removed and the entity counts after.
        """
        geometry = [i for i, s in enumerate(self.steps)]
        if not geometry:
            raise BuildRefusal('there is no command to remove')
        if count > len(geometry):
            raise BuildRefusal('only %d commands have been built'
                               % len(geometry))
        session = self._session_provider()
        try:
            result = session.call('session.script_trim', count=int(count))
        except Exception as error:                       # noqa: BLE001
            raise BuildRefusal(_clean(error)) from error
        removed = self.steps[-count:]
        del self.steps[-count:]
        self.prune_roles()
        # DP-5716. The session rebuilt the model from the rest of its log and
        # followed those builds again; the trim reply says what they replaced.
        followed = {}
        if self.steps and isinstance(result, dict):
            followed = result.get('followed') or {}
        self._set_replaced(followed.get('replaced'),
                           followed.get('surfaces'))
        for step in removed:
            if step.parameter:
                self.parameters.remove(step.parameter)
        return {'removed': [s.title for s in removed],
                'entities': dict(result.get('entities') or {}),
                'session': result}

    SWEEPS = ('extrude', 'twist', 'case_2d', 'axisymmetric_wedge',
              'extrude_boundary_layer')

    def preview(self, op_id: str, values: dict) -> dict:
        """Where the layers of a sweep will fall, before it is built (Plan 48
        GM6): the session answers from the picked entities and the numbers on
        the form without changing the model.  ``segments`` are the lines the
        viewport draws."""
        from foammesh.core.gmsh import layer_preview
        kind, kwargs = plan(op_id, values, self.parameters)
        if kind not in self.SWEEPS:
            raise BuildRefusal('%s is not a sweep, so it has no layer '
                               'preview' % operation(op_id).title)
        reason = unavailable(op_id, self._kernel)
        if reason:
            raise BuildRefusal(reason)
        if self._kernel != DEFAULT_KERNEL:
            kwargs['kernel'] = self._kernel
        try:
            result = self._session_provider().call(
                'session.sweep_preview', kind, **kwargs)
        except Exception as error:                       # noqa: BLE001
            raise BuildRefusal(_clean(error)) from error
        result = dict(result)
        result['segments'] = layer_preview.preview_segments(result)
        return result

    def run(self, op_id: str, values: dict) -> BuildStep:
        kind, kwargs = plan(op_id, values, self.parameters)
        op = operation(op_id)
        reason = unavailable(op_id, self._kernel)
        if reason:
            raise BuildRefusal(reason)
        if self._kernel != DEFAULT_KERNEL:
            kwargs['kernel'] = self._kernel
        session = self._session_provider()
        if op_id == 'parameter':
            value = self.parameters.set(kwargs['name'], values.get('value'))
            kwargs['value'] = value
        try:
            result = session.call('session.build', kind, **kwargs)
        except Exception as error:                       # noqa: BLE001
            raise BuildRefusal(_clean(error)) from error
        step = BuildStep(op_id, op.title,
                         tuple(tuple(c) for c in result.get('created', ())),
                         dict(result.get('entities') or {}),
                         result.get('loop'), self._kernel,
                         kwargs['name'] if op_id == 'parameter' else '',
                         values=copy.deepcopy(dict(values)),
                         geometry=result.get('geometry'))
        self.steps.append(step)
        if 'followed' in result:
            followed = result.get('followed') or {}
            self._set_replaced(followed.get('replaced'),
                               followed.get('surfaces'))
        declared = (result.get('roles') or {}).get('declared')
        if declared:
            step.roles = dict(result['roles'])
            self._add_roles(op_id, len(self.steps), declared)
        return step

    # -- what the build replaced (DP-5716) ---------------------------------- #

    def _set_replaced(self, names, surfaces=None) -> None:
        self._replaced = sorted({str(n) for n in names or ()})
        case = self._roles_case()
        if case is not None:
            save_replaced(case, self._replaced, surfaces)

    def replaced_names(self) -> list:
        """The prepared patch names the build has taken off the model."""
        held = getattr(self, '_replaced', None)
        if held is None:
            case = self._roles_case()
            held = load_replaced(case) if case is not None else []
            self._replaced = held
        return list(held)

    # -- declared boundary roles (Plan 48 WG, DP-4215) ------------------- #

    def _roles_case(self):
        return getattr(self, '_kernel_case', None)

    def _set_roles(self, rows) -> None:
        self._roles = [dict(r) for r in rows]
        case = self._roles_case()
        if case is not None:
            save_roles(case, self._roles)

    def roles(self) -> list:
        """The declared role rows, in the order the presets made them."""
        if self._roles is None:
            case = self._roles_case()
            self._roles = load_roles(case) if case is not None else []
        return [dict(r) for r in self._roles]

    def _add_roles(self, op_id: str, step: int, declared) -> None:
        rows = [r for r in self.roles()
                if not any(r['tag'] == d['tag'] for d in declared)]
        for item in declared:
            row = {'step': step, 'source': op_id, 'dim': 2,
                   'tag': int(item['tag']), 'name': str(item['name']),
                   'role': str(item['role']),
                   'default': str(item['category']),
                   'category': str(item['category'])}
            if item.get('surfaces'):              # DP-5725
                row['surfaces'] = sorted(int(t) for t in item['surfaces'])
            rows.append(row)
        self._set_roles(rows)

    def prune_roles(self) -> None:
        """Forget the roles of steps that are no longer built."""
        rows = self.roles()
        kept = [r for r in rows if int(r.get('step', 0)) <= len(self.steps)]
        if len(kept) != len(rows):
            self._set_roles(kept)

    def follow_names(self, groups) -> list:
        """Make the rows follow a rename in the physical-group table.

        *groups* is ``(dim, tag, name[, entities])`` for every group of the
        model. A row whose group is gone is dropped; one whose group has
        another name now takes that name. A row saved before rows carried
        their surfaces takes them from the built surfaces of its name."""
        case = self._roles_case()
        rows = self.roles()
        if case is not None and any(not r.get('surfaces') for r in rows):
            built = _load_build_model(case).get('surfaces') or ()
            for row in rows:
                if not row.get('surfaces'):
                    held = sorted(int(b['tag']) for b in built
                                  if isinstance(b, dict)
                                  and b.get('name') == row['name'])
                    if held:
                        row['surfaces'] = held
        rows, changed = follow_rows(rows, groups)
        if changed:
            self._set_roles(rows)
        if case is not None:
            follow_built_names(case, groups)
        return rows

    def set_role_category(self, name: str, category: str) -> dict:
        """Choose the category a declared patch publishes with."""
        rows = self.roles()
        row = next((r for r in rows if r['name'] == name), None)
        if row is None:
            raise BuildRefusal(
                '%r is not a patch the 2D-case or wedge preset declared; '
                'declared: %s' % (name, ', '.join(r['name'] for r in rows)
                                  or 'none'))
        allowed = ROLE_CATEGORIES.get(row['default'], (row['default'],))
        if category not in allowed:
            raise BuildRefusal(
                '%s can publish as %s, not %r' % (
                    name, ', '.join(allowed), category))
        row['category'] = category
        self._set_roles(rows)
        return row


def _clean(error) -> str:
    text = str(error)
    for marker in ('ValueError: ', 'BuildError: '):
        if marker in text:
            text = text.rsplit(marker, 1)[-1]
    return text.strip().splitlines()[-1] if text.strip() else 'failed'
