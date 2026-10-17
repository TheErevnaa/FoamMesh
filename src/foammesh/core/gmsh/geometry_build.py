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
import math
import operator
from dataclasses import dataclass, field

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

GROUPS = ('Parameter', 'Points', 'Curves', 'Surfaces and volumes',
          'Primitives', 'Transform', 'Extrude', 'Boolean', 'Delete',
          'Presets')

OPERATIONS = (
    Operation('parameter', 'Parameter', 'Parameter',
              (_f('name', 'Name', TEXT, 'R'), _f('value', 'Value', NUMBER,
                                                 1.0)), 'G-01'),
    Operation('point', 'Point', 'Points',
              _XYZ + (_f('size', 'Size at point (0 = none)', default=0.0,
                         optional=True, length=True),), 'G-02'),
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
    Operation('spline', 'Spline', 'Curves',
              (_f('points', 'Points, in order', ENTITIES, (), dim=0),),
              'G-03'),
    Operation('bspline', 'B-spline', 'Curves',
              (_f('points', 'Control points, in order', ENTITIES, (),
                  dim=0),), 'G-03'),
    Operation('bezier', 'Bezier', 'Curves',
              (_f('points', 'Control points, in order', ENTITIES, (),
                  dim=0),), 'G-03'),
    Operation('curve_loop', 'Curve loop', 'Surfaces and volumes',
              (_f('curves', 'Curves, in order', ENTITIES, (), dim=1),),
              'G-04'),
    Operation('plane_surface', 'Plane surface', 'Surfaces and volumes',
              (_f('loops', 'Curve loops (outer first, then holes)', INTEGER,
                  '', help='Loop numbers from "Curve loop", separated '
                           'by commas.'),), 'G-04'),
    Operation('surface_filling', 'Surface filling', 'Surfaces and volumes',
              (_f('loops', 'Curve loop', INTEGER, ''),), 'G-04'),
    Operation('surface_loop', 'Surface loop', 'Surfaces and volumes',
              (_f('surfaces', 'Surfaces', ENTITIES, (), dim=2),), 'G-04'),
    Operation('volume', 'Volume', 'Surfaces and volumes',
              (_f('shells', 'Surface loops', INTEGER, '',
                  help='Loop numbers from "Surface loop", separated by '
                       'commas.'),), 'G-04'),
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
              _XYZ + (_f('r', 'Radius', length=True), _ANGLE), 'G-05'),
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
    Operation('extrude_translate', 'Extrude along a vector', 'Extrude',
              (_ENTITIES(),) + _VEC + (
                  _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
                  _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                     False)), 'G-07', 'extrude', (('mode', 'translate'),)),
    Operation('extrude_rotate', 'Extrude by rotation (revolve)', 'Extrude',
              (_ENTITIES(),) + _XYZ + _AXIS + (
                  _f('angle', 'Angle (degrees)', default=360.0),
                  _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
                  _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                     False)), 'G-07', 'extrude', (('mode', 'rotate'),)),
    Operation('extrude_normal', 'Extrude along the normal', 'Extrude',
              (_ENTITIES(2, title='Surfaces'),
               _f('distance', 'Distance', default=1.0, length=True),
               _f('layers', 'Layers (0 = one, unstructured)', INTEGER, 0),
               _f('recombine', 'Recombine (hexes / quads)', BOOLEAN,
                  False)), 'G-07', 'extrude', (('mode', 'normal'),)),
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
               _ENTITIES(name='tools', title='Tools'),
               _f('remove_object', 'Remove the objects', BOOLEAN, True),
               _f('remove_tool', 'Remove the tools', BOOLEAN, True)),
              'G-08', 'boolean', (('operation', 'fragment'),)),
    Operation('delete', 'Delete', 'Delete',
              (_ENTITIES(), _f('recursive', 'Recursive (also the entities '
                                            'it is made of)', BOOLEAN,
                               False)), 'G-09'),
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
            if raw not in spec.choices:
                raise BuildRefusal('%s must be one of %s'
                                   % (spec.title, ', '.join(spec.choices)))
            sent[spec.name] = raw
        elif spec.kind == TEXT:
            text = str(raw).strip()
            if not text:
                raise BuildRefusal('%s is empty' % spec.title)
            sent[spec.name] = text
        elif spec.kind == INTEGER and spec.name in ('loops', 'shells'):
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
    if op.id == 'parameter':
        # Evaluated against the table: the table keeps the expression.
        pass
    _check(op, sent)
    return op.kind, sent


def _check(op, sent) -> None:
    if op.id == 'extrude_translate' and not any(
            sent.get(k) for k in ('dx', 'dy', 'dz')):
        raise BuildRefusal('the extrusion vector is zero')
    if op.id == 'extrude_rotate' and not any(
            sent.get(k) for k in ('ax', 'ay', 'az')):
        raise BuildRefusal('the rotation axis is the zero vector')
    if op.id == 'rotate' and not any(sent.get(k) for k in ('ax', 'ay',
                                                           'az')):
        raise BuildRefusal('the rotation axis is the zero vector')
    if op.id in ('fuse', 'intersect', 'cut', 'fragment'):
        if not sent['objects'] or not sent['tools']:
            raise BuildRefusal('pick objects and tools')
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

@dataclass
class BuildStep:
    op_id: str
    title: str
    created: tuple
    entities: dict = field(default_factory=dict)
    loop: int | None = None          # the loop a curve/surface loop made


class GeometryBuilder:
    """Runs build operations on a session and remembers what was built."""

    def __init__(self, session_provider, parameters: Parameters | None = None):
        self._session_provider = session_provider
        self.parameters = parameters or Parameters()
        self.steps: list = []

    def run(self, op_id: str, values: dict) -> BuildStep:
        kind, kwargs = plan(op_id, values, self.parameters)
        op = operation(op_id)
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
                         result.get('loop'))
        self.steps.append(step)
        return step


def _clean(error) -> str:
    text = str(error)
    for marker in ('ValueError: ', 'BuildError: '):
        if marker in text:
            text = text.rsplit(marker, 1)[-1]
    return text.strip().splitlines()[-1] if text.strip() else 'failed'
