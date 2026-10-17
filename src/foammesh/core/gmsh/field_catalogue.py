"""Every size-field type of Gmsh 4.15.2, with every parameter (Plan 41 GN6).

The Gmsh route used to offer seven hand-wired field shapes.  Gmsh's own GUI
offers every type it was built with, each with its full parameter list, and
lets one field read another.  This module is the data for that: the 29 types
(M-02..M-20, M-22), their parameters with kind, default and manual text, and
the checks a set of rows must pass before a run.

A *row* is one field the user made::

    {"name": "wall", "type": "Distance", "enabled": True, "background": False,
     "params": {"SurfacesList": [1, 2], "Sampling": 40}}

``params`` holds only what the user set; everything else stays at Gmsh's
default.  A reference to another field (``InField``, ``FieldsList``) is the
*name* of another row, so a graph of any depth can be built and the page can
draw it.  The rows are stored as JSON text in ``gmsh.nativeFields.rows``.

Names and kinds were measured on the installed build by
``scripts/gmsh_field_probe.py`` (``plans/evidence/plan41/gn6/field-probe.json``);
the manual text is Gmsh's reference manual, paraphrased.  The anisotropic types
(M-19) are offered only while the 2D algorithm is BAMG or the 3D algorithm is
MMG3D (Plan 41 Q3).  ``AutomaticMeshSizeField`` (M-20) needs a Gmsh built with
the optional p4est library: on a build without it the row stays in the table,
switched off with the measured reason, and the run goes on without it.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field, replace
import json
import math

#: Measured on the shipped Gmsh 4.15.2 (``General.BuildOptions``, 2026-10-16);
#: a runtime with another build replaces it with :func:`set_build_options`.
_BUILD_OPTIONS: str | None = None


def build_options() -> str:
    """The ``General.BuildOptions`` text the checks are made against."""
    if _BUILD_OPTIONS is not None:
        return _BUILD_OPTIONS
    from .option_catalogue_data import ROWS
    for row in ROWS:
        if row[0] == 'General.BuildOptions':
            return str(row[2])
    return ''


def set_build_options(text) -> None:
    """Use the build options a live Gmsh reported (``None``: the measured)."""
    global _BUILD_OPTIONS
    _BUILD_OPTIONS = None if text is None else str(text)


def build_has(name: str) -> bool:
    return name.lower() in {word.lower().split('[')[0]
                            for word in build_options().split()}


#: Where the rows live in the project.
FIELD_ID = 'gmsh.native_fields.rows'
#: The stored name, as the registry spells it.
STORED_ID = 'gmsh/nativeFields/rows'

CALCULATION_VERSION = 'gmsh.native_fields.v1'

#: The 2D and 3D algorithm enum values under which Gmsh honours a metric.
ANISOTROPIC_SURFACE = ('bamg',)
ANISOTROPIC_VOLUME = ('mmg3d',)

ENTITY_DIMENSIONS = {
    'PointsList': 0, 'CurvesList': 1, 'SurfacesList': 2, 'VolumesList': 3,
    'ExcludedSurfacesList': 2, 'FanPointsList': 0, 'FanNodesList': 0,
}


class FieldRefusal(ValueError):
    """A set of field rows Gmsh cannot be given, with the reason."""


@dataclass(frozen=True)
class FieldOption:
    name: str
    #: float | int | bool | enum | string | expression | file | entities |
    #: field | fields
    kind: str
    default: object = None
    help: str = ''
    unit: str = ''
    choices: tuple = ()
    required: bool = False

    @property
    def dimension(self):
        return ENTITY_DIMENSIONS.get(self.name)

    def to_dict(self) -> dict:
        return {'name': self.name, 'kind': self.kind, 'default': self.default,
                'help': self.help, 'unit': self.unit,
                'choices': [list(c) for c in self.choices],
                'required': self.required}


@dataclass(frozen=True)
class FieldType:
    gmsh: str
    title: str
    #: shape | distance | evaluate | operator | anisotropic | layer | other
    family: str
    help: str
    options: tuple = ()
    anisotropic: bool = False
    #: M-20. The Gmsh build option this type needs (``P4est``), and what to
    #: say when the build lacks it. Empty: the type works on every build.
    needs_build: str = ''
    needs_reason: str = ''

    @property
    def refusal(self) -> str:
        """Why this type cannot run on the installed Gmsh, else ''."""
        if self.needs_build and not build_has(self.needs_build):
            return self.needs_reason
        return ''

    def option(self, name: str):
        for item in self.options:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict:
        return {'gmsh': self.gmsh, 'title': self.title, 'family': self.family,
                'help': self.help, 'anisotropic': self.anisotropic,
                'refusal': self.refusal,
                'options': [o.to_dict() for o in self.options]}


def _f(name, default, help, unit='', required=False):
    return FieldOption(name, 'float', default, help, unit, (), required)


def _i(name, default, help, required=False):
    return FieldOption(name, 'int', default, help, '', (), required)


def _b(name, default, help):
    return FieldOption(name, 'bool', default, help)


def _s(name, help, kind='string', required=False):
    return FieldOption(name, kind, None, help, '', (), required)


def _e(name, default, help, choices):
    return FieldOption(name, 'enum', default, help, '', tuple(choices))


def _ent(name, help):
    return FieldOption(name, 'entities', None, help)


def _in(help='The field this one reads.'):
    return FieldOption('InField', 'field', None, help, required=True)


def _fields(help='The fields this one combines.'):
    return FieldOption('FieldsList', 'fields', None, help, required=True)


_NONE = None   # Gmsh's default is 1e22, i.e. "no value"; the row leaves it out.

_TYPES = (
    FieldType(
        'Ball', 'Ball', 'shape',
        'VIn inside a sphere, VOut outside it. With a Thickness the size '
        'ramps linearly from VIn to VOut over that distance outside the '
        'sphere.',
        (_f('VIn', _NONE, 'Size inside the ball.', 'm'),
         _f('VOut', _NONE, 'Size outside the ball.', 'm'),
         _f('XCenter', 0.0, 'X of the centre.', 'm'),
         _f('YCenter', 0.0, 'Y of the centre.', 'm'),
         _f('ZCenter', 0.0, 'Z of the centre.', 'm'),
         _f('Radius', 0.0, 'Radius of the ball.', 'm', True),
         _f('Thickness', 0.0, 'Distance over which the size ramps from VIn '
            'to VOut outside the ball (0 = a step).', 'm'))),
    FieldType(
        'Box', 'Box', 'shape',
        'VIn inside an axis-aligned box, VOut outside it. With a Thickness '
        'the size ramps linearly over that distance outside the box.',
        (_f('VIn', _NONE, 'Size inside the box.', 'm'),
         _f('VOut', _NONE, 'Size outside the box.', 'm'),
         _f('XMin', 0.0, 'Minimum X.', 'm'), _f('XMax', 0.0, 'Maximum X.', 'm'),
         _f('YMin', 0.0, 'Minimum Y.', 'm'), _f('YMax', 0.0, 'Maximum Y.', 'm'),
         _f('ZMin', 0.0, 'Minimum Z.', 'm'), _f('ZMax', 0.0, 'Maximum Z.', 'm'),
         _f('Thickness', 0.0, 'Distance over which the size ramps from VIn '
            'to VOut outside the box (0 = a step).', 'm'))),
    FieldType(
        'Cylinder', 'Cylinder', 'shape',
        'VIn inside a cylinder, VOut outside it. The cylinder is centred on '
        'the given point, with the given axis (its length is the half-length '
        'on each side) and radius.',
        (_f('VIn', _NONE, 'Size inside the cylinder.', 'm'),
         _f('VOut', _NONE, 'Size outside the cylinder.', 'm'),
         _f('XCenter', 0.0, 'X of the centre.', 'm'),
         _f('YCenter', 0.0, 'Y of the centre.', 'm'),
         _f('ZCenter', 0.0, 'Z of the centre.', 'm'),
         _f('XAxis', 0.0, 'X component of the axis.', 'm'),
         _f('YAxis', 0.0, 'Y component of the axis.', 'm'),
         _f('ZAxis', 1.0, 'Z component of the axis.', 'm'),
         _f('Radius', 0.0, 'Radius of the cylinder.', 'm', True))),
    FieldType(
        'Frustum', 'Frustum', 'shape',
        'A size that varies linearly along the segment from endpoint 1 to '
        'endpoint 2 of a conical shell: the inner and outer radius and the '
        'size inside the inner radius and outside the outer one are given at '
        'each end.',
        (_f('X1', 0.0, 'X of endpoint 1.', 'm'),
         _f('Y1', 0.0, 'Y of endpoint 1.', 'm'),
         _f('Z1', 1.0, 'Z of endpoint 1.', 'm'),
         _f('X2', 0.0, 'X of endpoint 2.', 'm'),
         _f('Y2', 0.0, 'Y of endpoint 2.', 'm'),
         _f('Z2', 0.0, 'Z of endpoint 2.', 'm'),
         _f('InnerR1', 0.0, 'Inner radius at endpoint 1.', 'm'),
         _f('InnerR2', 0.0, 'Inner radius at endpoint 2.', 'm'),
         _f('InnerV1', 0.1, 'Size inside the inner radius, endpoint 1.', 'm'),
         _f('InnerV2', 0.1, 'Size inside the inner radius, endpoint 2.', 'm'),
         _f('OuterR1', 1.0, 'Outer radius at endpoint 1.', 'm'),
         _f('OuterR2', 1.0, 'Outer radius at endpoint 2.', 'm'),
         _f('OuterV1', 1.0, 'Size outside the outer radius, endpoint 1.', 'm'),
         _f('OuterV2', 1.0, 'Size outside the outer radius, endpoint 2.',
            'm'))),
    FieldType(
        'Constant', 'Constant', 'shape',
        'VIn on the listed entities (with or without their boundary), VOut '
        'everywhere else. With no entity listed it returns VOut everywhere.',
        (_f('VIn', _NONE, 'Size on the listed entities.', 'm'),
         _f('VOut', _NONE, 'Size elsewhere.', 'm'),
         _b('IncludeBoundary', True, 'Also count the boundary of the listed '
            'entities as inside.'),
         _ent('PointsList', 'Points.'), _ent('CurvesList', 'Curves.'),
         _ent('SurfacesList', 'Surfaces.'), _ent('VolumesList', 'Volumes.'))),
    FieldType(
        'Distance', 'Distance', 'distance',
        'The distance to a set of points, curves and surfaces, sampled at a '
        'number of points on each curve and surface. It is not a size by '
        'itself: a Threshold turns it into one.',
        (_ent('PointsList', 'Points the distance is measured from.'),
         _ent('CurvesList', 'Curves the distance is measured from.'),
         _ent('SurfacesList', 'Surfaces the distance is measured from.'),
         _i('Sampling', 20, 'Points sampled on each curve or surface. Too '
            'few and the ramp is blocky on a curved face.'))),
    FieldType(
        'Threshold', 'Threshold', 'distance',
        'A size that ramps from SizeMin at DistMin to SizeMax at DistMax of '
        'the field it reads (usually a Distance). Sigmoid replaces the linear '
        'ramp by a smooth one; StopAtDistMax returns no size beyond DistMax '
        'so other fields decide there.',
        (_in('Usually a Distance field.'),
         _f('SizeMin', 0.1, 'Size at and below DistMin.', 'm'),
         _f('SizeMax', 1.0, 'Size at and beyond DistMax.', 'm'),
         _f('DistMin', 1.0, 'Distance up to which SizeMin holds.', 'm'),
         _f('DistMax', 10.0, 'Distance from which SizeMax holds.', 'm'),
         _b('Sigmoid', False, 'Ramp with a smooth sigmoid instead of '
            'linearly.'),
         _b('StopAtDistMax', False, 'Do not return SizeMax beyond DistMax; '
            'return no size there.'))),
    FieldType(
        'Extend', 'Extend', 'distance',
        'Extends the mesh size of the listed curves and surfaces into the '
        'domain: the size at a point is the size at the closest listed '
        'entity, raised by a power law up to SizeMax at DistMax.',
        (_ent('CurvesList', 'Curves whose size is extended.'),
         _ent('SurfacesList', 'Surfaces whose size is extended.'),
         _f('DistMax', 1.0, 'Distance at which the size reaches SizeMax.',
            'm'),
         _f('SizeMax', 1.0, 'Size at DistMax.', 'm'),
         _f('Power', 1.0, 'Exponent of the growth between the entity and '
            'DistMax.'))),
    FieldType(
        'Curvature', 'Curvature', 'operator',
        'The curvature of the field it reads, div(grad f / |grad f|), by '
        'finite differences of step Delta.',
        (_in('Usually a Distance field.'),
         _f('Delta', 0.00034641016151377546, 'Finite-difference step.',
            'm'))),
    FieldType(
        'Mean', 'Mean', 'operator',
        'The mean of the field it reads over a cube of half-side Delta.',
        (_in(), _f('Delta', 0.00034641016151377546,
                   'Half-side of the averaging cube.', 'm'))),
    FieldType(
        'Gradient', 'Gradient', 'operator',
        'The finite-difference gradient of the field it reads.',
        (_in(), _f('Delta', 0.00034641016151377546, 'Finite-difference step.',
                   'm'),
         _e('Kind', 3, 'Which component of the gradient to return.',
            ((0, 'X'), (1, 'Y'), (2, 'Z'), (3, 'Norm'))))),
    FieldType(
        'Laplacian', 'Laplacian', 'operator',
        'The finite-difference Laplacian of the field it reads.',
        (_in(), _f('Delta', 0.00034641016151377546, 'Finite-difference step.',
                   'm'))),
    FieldType(
        'MaxEigenHessian', 'Max eigenvalue of the Hessian', 'operator',
        'The largest eigenvalue of the Hessian of the field it reads, with '
        'the gradients taken by finite differences of step Delta.',
        (_in(), _f('Delta', 0.00034641016151377546, 'Finite-difference step.',
                   'm'))),
    FieldType(
        'Min', 'Min', 'operator',
        'The smallest of the listed fields at each point.',
        (_fields(),)),
    FieldType(
        'Max', 'Max', 'operator',
        'The largest of the listed fields at each point.',
        (_fields(),)),
    FieldType(
        'Restrict', 'Restrict', 'operator',
        'Restricts the field it reads to the listed entities: elsewhere it '
        'returns no size.',
        (_in(), _b('IncludeBoundary', True, 'Also count the boundary of the '
                   'listed entities.'),
         _ent('PointsList', 'Points.'), _ent('CurvesList', 'Curves.'),
         _ent('SurfacesList', 'Surfaces.'), _ent('VolumesList', 'Volumes.'))),
    FieldType(
        'Param', 'Param', 'operator',
        'Evaluates the field it reads in parametric coordinates: '
        'f(FX(x,y,z), FY(x,y,z), FZ(x,y,z)).',
        (_in(), _s('FX', 'Expression for the first coordinate.',
                   'expression', True),
         _s('FY', 'Expression for the second coordinate.', 'expression',
            True),
         _s('FZ', 'Expression for the third coordinate.', 'expression',
            True))),
    FieldType(
        'LonLat', 'Longitude-latitude', 'operator',
        'Evaluates the field it reads in geographic coordinates (longitude, '
        'latitude) of the point; FromStereo converts from stereographic '
        'coordinates of the given sphere radius first.',
        (_in(), _b('FromStereo', False, 'The coordinates are stereographic.'),
         _f('RadiusStereo', 6371000.0, 'Radius of the sphere of the '
            'stereographic projection.', 'm'))),
    FieldType(
        'Octree', 'Octree', 'operator',
        'Pre-computes the field it reads on an octree so that it evaluates '
        'quickly. The octree is built when the mesh is generated.',
        (_in(),)),
    FieldType(
        'MathEval', 'Expression', 'evaluate',
        'A size from a mathematical expression in x, y and z. Fn(x,y,z) '
        'reads the field numbered n.',
        (_s('F', 'The expression, e.g. 0.01 + 0.05*sqrt(x*x + y*y + z*z).',
            'expression', True),)),
    FieldType(
        'ExternalProcess', 'External process', 'evaluate',
        'Calls a program that reads coordinate triplets (x, y, z) as binary '
        'doubles on its standard input and writes one binary double per '
        'triplet on its standard output. It runs in the Gmsh runtime.',
        (_s('CommandLine', 'The command line to run in the Gmsh runtime.',
            'string', True),)),
    FieldType(
        'Structured', 'Structured grid', 'evaluate',
        'Reads a size from a regular grid file: three lines (origin, step, '
        'counts) then the values, with the first index slowest and the last '
        'fastest (z). Outside the grid it '
        'returns OutsideValue if SetOutsideValue is on, else the nearest '
        'value.',
        (_s('FileName', 'Path of the grid file, as the Gmsh runtime sees it.',
            'file', True),
         _e('TextFormat', 0, 'Whether the values are binary or ASCII text.',
            ((0, 'Binary'), (1, 'ASCII'))),
         _b('SetOutsideValue', False, 'Return OutsideValue outside the grid.'),
         _f('OutsideValue', _NONE, 'Size outside the grid.', 'm'))),
    FieldType(
        'PostView', 'Size from a view', 'evaluate',
        'The size is read from a post-processing view (for instance a size '
        'map written by an earlier mesh or by the size-field tool).',
        (_i('ViewTag', -1, 'Tag of the view (-1: use ViewIndex).'),
         _i('ViewIndex', 1, 'Index of the view in the list of views.'),
         _i('IView', 0, 'Earlier spelling of ViewIndex.'),
         _b('CropNegativeValues', True, 'Return no size where the view is '
            'negative, instead of the negative value.'),
         _b('UseClosest', True, 'Outside every element of the view, use the '
            'value of the closest node.'))),
    FieldType(
        'AttractorAnisoCurve', 'Anisotropic curve attractor', 'anisotropic',
        'An anisotropic size attracted by the listed curves: sizes '
        'normal and tangent to the curve are SizeMin* at DistMin and SizeMax* '
        'at DistMax. Honoured by BAMG (2D) and MMG3D (3D) only.',
        (_ent('CurvesList', 'Curves that attract.'),
         _i('Sampling', 20, 'Points sampled on each curve.'),
         _f('DistMin', 0.1, 'Distance up to which the minimum sizes hold.',
            'm'),
         _f('DistMax', 0.5, 'Distance from which the maximum sizes hold.',
            'm'),
         _f('SizeMinNormal', 0.05, 'Size normal to the curve near it.', 'm'),
         _f('SizeMinTangent', 0.5, 'Size along the curve near it.', 'm'),
         _f('SizeMaxNormal', 0.5, 'Size normal to the curve far from it.',
            'm'),
         _f('SizeMaxTangent', 0.5, 'Size along the curve far from it.',
            'm')), anisotropic=True),
    FieldType(
        'MathEvalAniso', 'Expression (anisotropic)', 'anisotropic',
        'A metric tensor from six expressions in x, y and z (the symmetric '
        'matrix M11 M12 M13 M22 M23 M33). Honoured by BAMG (2D) and MMG3D '
        '(3D) only.',
        (_s('M11', 'Metric component 11.', 'expression', True),
         _s('M12', 'Metric component 12.', 'expression', True),
         _s('M13', 'Metric component 13.', 'expression', True),
         _s('M22', 'Metric component 22.', 'expression', True),
         _s('M23', 'Metric component 23.', 'expression', True),
         _s('M33', 'Metric component 33.', 'expression', True)),
        anisotropic=True),
    FieldType(
        'MinAniso', 'Min (anisotropic)', 'anisotropic',
        'The smallest of the listed possibly anisotropic fields. Honoured by '
        'BAMG (2D) and MMG3D (3D) only.',
        (_fields(),), anisotropic=True),
    FieldType(
        'IntersectAniso', 'Intersection (anisotropic)', 'anisotropic',
        'The metric intersection of the listed possibly anisotropic fields. '
        'Honoured by BAMG (2D) and MMG3D (3D) only.',
        (_fields(),), anisotropic=True),
    FieldType(
        'BoundaryLayer', 'Boundary layer', 'layer',
        'Gmsh\'s 2D boundary-layer field: the layer mesh grown from curves '
        'and points, used as the boundary layer rather than as a size. A fan '
        'point grows a fan of elements around it (a trailing edge); how '
        'many per half turn is Mesh.BoundaryLayerFanElements on the '
        'Boundary layers page.',
        (_ent('CurvesList', 'Curves the layer is grown from.'),
         _ent('PointsList', 'Points the layer is grown from.'),
         _ent('FanPointsList', 'Points a fan of layers is built around.'),
         _ent('FanNodesList', 'Nodes a fan of layers is built around.'),
         _ent('ExcludedSurfacesList', 'Surfaces the layer must not enter.'),
         _f('Size', 0.1, 'Size of the first layer.', 'm'),
         _f('SizeFar', 1.0, 'Size far from the layer.', 'm'),
         _f('Thickness', 0.01, 'Total thickness of the layer.', 'm'),
         _f('Ratio', 1.1, 'Growth ratio between layers.'),
         _i('NbLayers', 10, 'Number of layers.'),
         _b('Quads', False, 'Recombine the layer into quadrangles.'),
         _b('IntersectMetrics', False, 'Intersect the metrics where layers '
            'meet.'),
         _i('BetaLaw', 0, 'Law of the angle blending.'),
         _f('Beta', 1.01, 'Angle-blending parameter.'))),
    FieldType(
        'AutomaticMeshSizeField', 'Automatic mesh size', 'other',
        'Gmsh\'s curvature, proximity and gradation field, built on an '
        'octree of the surfaces. The settings of the Automatic sizing '
        'shortcut live here: the shortcut computes from this row. A size '
        'left empty is chosen from the model.',
        (_f('hMin', None, 'Smallest size. Empty: from the model.', 'm'),
         _f('hMax', None, 'Largest size. Empty: from the model.', 'm'),
         _f('hBulk', None, 'Size where nothing prescribes one. Empty: from '
            'the model.', 'm'),
         _f('gradation', 1.1, 'Maximum growth ratio between neighbouring '
            'edges.'),
         _i('nPointsPerCircle', 20, 'Elements per circle of curvature.'),
         _i('nPointsPerGap', 0, 'Layers of elements across a thin gap.'),
         _b('smoothing', True, 'Smooth the sizes (Gmsh says this should '
            'always be on).'),
         _b('features', True, 'Also find the local feature size (thin '
            'channels).'),
         _s('p4estFileToLoad', 'A saved size forest (p4est file) to read '
            'instead of computing one.', kind='file')),
        needs_build='P4est',
        needs_reason='this Gmsh was built without the optional p4est '
                     'library, which the automatic size field needs (Gmsh '
                     'says: "Gmsh has to be compiled with HXT and P4EST to '
                     'use automaticMeshSizeField"); the row is kept, '
                     'switched off, and the mesh is made without it'),
)

TYPES: dict[str, FieldType] = {item.gmsh: item for item in _TYPES}

#: How the types are grouped in the type list.
FAMILIES = (
    ('shape', 'Shapes and regions'), ('distance', 'Distance and ramps'),
    ('operator', 'Operators on fields'), ('evaluate', 'Evaluated sizes'),
    ('anisotropic', 'Anisotropic (BAMG, MMG3D)'), ('layer', 'Boundary layer'),
    ('other', 'Other'),
)


def type_names() -> tuple[str, ...]:
    return tuple(TYPES)


def visible_types(surface_algorithm: str = '', volume_algorithm: str = ''
                  ) -> tuple[FieldType, ...]:
    """The types the Size fields page lists for the chosen algorithms.

    Plan 41 Q3 (decided by the user): the four anisotropic types are listed
    only while the 2D algorithm is BAMG or the 3D algorithm is MMG3D.
    """
    shown = anisotropic_available(surface_algorithm, volume_algorithm)
    return tuple(item for item in _TYPES
                 if shown or not item.anisotropic)


def anisotropic_available(surface_algorithm: str = '',
                          volume_algorithm: str = '') -> bool:
    return (str(surface_algorithm or '').lower() in ANISOTROPIC_SURFACE
            or str(volume_algorithm or '').lower() in ANISOTROPIC_VOLUME)


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

def new_row(gmsh_type: str, name: str) -> dict:
    if gmsh_type not in TYPES:
        raise FieldRefusal(f'{gmsh_type!r} is not a Gmsh size-field type')
    return {'name': name, 'type': gmsh_type, 'enabled': True,
            'background': False, 'params': {}}


def rows_from_text(text) -> list[dict]:
    text = str(getattr(text, 'value', text) or '').strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as error:
        raise FieldRefusal(f'the stored size fields are not valid JSON: '
                           f'{error}') from None
    if isinstance(data, dict):
        data = data.get('rows', [])
    if not isinstance(data, list):
        raise FieldRefusal('the stored size fields are not a list of rows')
    return [dict(item) for item in data if isinstance(item, dict)]


def rows_to_text(rows) -> str:
    rows = [dict(item) for item in (rows or ())]
    return json.dumps(rows, separators=(',', ':'), sort_keys=True) \
        if rows else ''


def _number(option: FieldOption, value, owner: str) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FieldRefusal(
            f'{owner}: {option.name} needs a number, not {value!r}') from None
    if math.isnan(number) or math.isinf(number):
        raise FieldRefusal(f'{owner}: {option.name} must be finite')
    return number


def _entity_list(option: FieldOption, value, owner: str) -> list[int]:
    if isinstance(value, str):
        value = [part for part in value.replace(';', ',').replace(
            ' ', ',').split(',') if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise FieldRefusal(
            f'{owner}: {option.name} needs entity numbers, not {value!r}')
    tags = []
    for item in value:
        try:
            tag = int(float(item))
        except (TypeError, ValueError):
            raise FieldRefusal(
                f'{owner}: {option.name} has {item!r}, which is not an '
                'entity number') from None
        if tag < 1:
            raise FieldRefusal(
                f'{owner}: {option.name} entity numbers start at 1')
        tags.append(tag)
    return tags


@dataclass(frozen=True)
class FieldNode:
    """One validated row, split by how Gmsh takes each value."""

    name: str
    gmsh: str
    enabled: bool
    background: bool
    numbers: tuple = ()
    strings: tuple = ()
    lists: tuple = ()
    refs: tuple = ()
    ref_lists: tuple = ()

    def to_dict(self) -> dict:
        return {'name': self.name, 'type': self.gmsh,
                'enabled': self.enabled, 'background': self.background,
                'numbers': [list(i) for i in self.numbers],
                'strings': [list(i) for i in self.strings],
                'lists': [[n, list(v)] for n, v in self.lists],
                'refs': [list(i) for i in self.refs],
                'refLists': [[n, list(v)] for n, v in self.ref_lists]}


@dataclass(frozen=True)
class NativeFields:
    nodes: tuple = ()
    #: Names in an order where every reference is already built.
    order: tuple = ()
    warnings: tuple = ()
    calculation_version: str = CALCULATION_VERSION

    def to_list(self) -> list:
        return [node.to_dict() for node in self.nodes]

    def background_names(self) -> tuple:
        return tuple(n.name for n in self.nodes
                     if n.background and n.enabled)


def _split(row: dict, owner: str, strict: bool = True) -> FieldNode:
    kind = TYPES[row['type']]
    numbers, strings, lists, refs, ref_lists = [], [], [], [], []
    params = dict(row.get('params') or {})
    for name, value in params.items():
        option = kind.option(name)
        if option is None:
            raise FieldRefusal(
                f'{owner}: a {kind.gmsh} field has no parameter {name!r}; it '
                'has ' + ', '.join(o.name for o in kind.options))
        if option.kind in ('float', 'int', 'bool', 'enum'):
            number = _number(option, value, owner)
            if option.kind == 'int' and number != int(number):
                raise FieldRefusal(
                    f'{owner}: {name} needs a whole number, not {value!r}')
            if option.kind == 'enum' and int(number) not in [
                    c[0] for c in option.choices]:
                raise FieldRefusal(
                    f'{owner}: {name} {value!r} is not one of '
                    + ', '.join(f'{c[0]} ({c[1]})' for c in option.choices))
            numbers.append((name, number))
        elif option.kind in ('string', 'expression', 'file'):
            text = str(value or '').strip()
            if text:
                strings.append((name, text))
        elif option.kind == 'entities':
            lists.append((name, tuple(_entity_list(option, value, owner))))
        elif option.kind == 'field':
            target = str(value or '').strip()
            if target:
                refs.append((name, target))
        elif option.kind == 'fields':
            if isinstance(value, str):
                value = [part.strip() for part in value.split(',')]
            targets = tuple(str(item).strip() for item in (value or ())
                            if str(item).strip())
            if targets:
                ref_lists.append((name, targets))
    given = {n for n, *_ in (*numbers, *strings, *lists, *refs, *ref_lists)}
    for option in kind.options:
        if strict and option.required and option.name not in given:
            raise FieldRefusal(
                f'{owner}: a {kind.gmsh} field needs {option.name} '
                f'({option.help.rstrip(".").lower()})')
    return FieldNode(
        name=str(row['name']), gmsh=kind.gmsh,
        enabled=bool(row.get('enabled', True)),
        background=bool(row.get('background', False)),
        numbers=tuple(numbers), strings=tuple(strings), lists=tuple(lists),
        refs=tuple(refs), ref_lists=tuple(ref_lists))


def graph_edges(nodes) -> list[tuple[str, str]]:
    """``(reader, read)`` pairs: the arrows of the field graph."""
    edges = []
    for node in nodes:
        for _option, target in node.refs:
            edges.append((node.name, target))
        for _option, targets in node.ref_lists:
            edges.extend((node.name, target) for target in targets)
    return edges


def _drop_skipped(nodes, skipped: dict, warnings: list) -> list:
    """Take the skipped fields out of what the others read (M-20).

    A Min or Max stops reading a skipped member; a field whose one input is
    skipped, or whose every member is, is skipped with it.
    """
    nodes = list(nodes)
    changed = True
    while changed:
        changed = False
        for index, node in enumerate(nodes):
            if not node.enabled or node.name in skipped:
                continue
            gone = [t for _o, t in node.refs if t in skipped]
            lists, dropped = [], []
            for option, targets in node.ref_lists:
                kept = tuple(t for t in targets if t not in skipped)
                dropped += [t for t in targets if t in skipped]
                if kept:
                    lists.append((option, kept))
                elif targets:
                    gone.append(targets[0])
            if gone:
                skipped[node.name] = f'it reads {gone[0]!r}, which is skipped'
                warnings.append(f'size field {node.name!r} is skipped: '
                                f'{skipped[node.name]}')
                nodes[index] = replace(node, enabled=False)
                changed = True
            elif dropped:
                warnings.append(
                    f'size field {node.name!r} no longer reads '
                    + ', '.join(repr(t) for t in dropped)
                    + ' (skipped, see above)')
                nodes[index] = replace(node, ref_lists=tuple(lists))
                changed = True
    return nodes


def validate(rows, *, surface_algorithm: str = '',
             volume_algorithm: str = '') -> NativeFields:
    """Check the rows and return them split for Gmsh, in build order.

    Refuses, with the reason and before any run: an unknown type, a type this
    build cannot use, an anisotropic type while neither BAMG nor MMG3D is
    selected, an unknown or mistyped parameter, a missing required one, a
    reference to a field that is not there or is switched off, and a cycle.
    """
    rows = [dict(item) for item in (rows or ())]
    seen: dict[str, dict] = {}
    for index, row in enumerate(rows):
        name = str(row.get('name') or '').strip()
        if not name:
            raise FieldRefusal(f'size field {index + 1} has no name')
        row['name'] = name
        if name in seen:
            raise FieldRefusal(
                f'two size fields are both called {name!r}; each field in '
                'the graph needs its own name')
        gmsh_type = str(row.get('type') or '')
        if gmsh_type not in TYPES:
            raise FieldRefusal(
                f'size field {name!r} has unknown type {gmsh_type!r}')
        seen[name] = row

    nodes = []
    warnings: list[str] = []
    skipped: dict[str, str] = {}
    for row in rows:
        kind = TYPES[row['type']]
        owner = f'size field {row["name"]!r}'
        if not bool(row.get('enabled', True)):
            # Switched off: kept so the page can show it, never built, and
            # not held to the rules a running field is.
            nodes.append(_split(row, owner, strict=False))
            continue
        if kind.refusal:
            # M-20. The build cannot run this type: the row is kept, off, and
            # the run goes on without it (never a run that fails).
            skipped[row['name']] = kind.refusal
            warnings.append(f'{owner} ({kind.gmsh}) is skipped: '
                            f'{kind.refusal}')
            nodes.append(replace(_split(row, owner, strict=False),
                                 enabled=False))
            continue
        if kind.anisotropic and not anisotropic_available(
                surface_algorithm, volume_algorithm):
            raise FieldRefusal(
                f'{owner} is a {kind.gmsh} field, which changes the mesh only '
                'under the BAMG 2D algorithm or the MMG3D 3D algorithm; '
                'choose one of them on Sizing, or turn the field off')
        nodes.append(_split(row, owner))

    if skipped:
        nodes = _drop_skipped(nodes, skipped, warnings)
    by_name = {node.name: node for node in nodes}
    enabled = {n for n, node in by_name.items() if node.enabled}
    for node in nodes:
        if not node.enabled:
            continue
        owner = f'size field {node.name!r}'
        for _option, target in (*node.refs, *[
                (o, t) for o, ts in node.ref_lists for t in ts]):
            if target == node.name:
                raise FieldRefusal(f'{owner} reads itself')
            if target not in by_name:
                raise FieldRefusal(
                    f'{owner} reads a field {target!r} that does not exist')
            if target not in enabled:
                raise FieldRefusal(
                    f'{owner} reads {target!r}, which is switched off')
    order = _order(nodes)

    background = [n for n in nodes if n.enabled and n.background]
    layer = [n for n in background if n.gmsh == 'BoundaryLayer']
    sizes = [n for n in background if n.gmsh != 'BoundaryLayer']
    anisotropic = [n for n in sizes if TYPES[n.gmsh].anisotropic]
    if anisotropic and len(anisotropic) != len(sizes):
        raise FieldRefusal(
            'a background field cannot mix anisotropic and isotropic fields: '
            + ', '.join(repr(n.name) for n in anisotropic)
            + ' are anisotropic; combine them with a Min (anisotropic) or '
            'Intersection (anisotropic) field and make that the background')
    if len(layer) > 1:
        raise FieldRefusal('Gmsh takes one boundary-layer field; these are '
                           'all marked: ' + ', '.join(
                               repr(n.name) for n in layer))
    read = {t for e in graph_edges(nodes) for t in [e[1]]}
    for node in nodes:
        if node.enabled and not node.background and node.name not in read:
            warnings.append(
                f'size field {node.name!r} feeds nothing and is not the '
                'background, so it will not affect the mesh')
    return NativeFields(nodes=tuple(nodes), order=order,
                        warnings=tuple(warnings))


def _order(nodes) -> tuple:
    """Kahn order over the enabled nodes; a leftover is a cycle."""
    live = {n.name: n for n in nodes if n.enabled}
    needs = {name: {t for _o, t in node.refs}
             | {t for _o, ts in node.ref_lists for t in ts}
             for name, node in live.items()}
    order: list[str] = []
    ready = sorted(name for name, deps in needs.items() if not deps)
    while ready:
        name = ready.pop(0)
        order.append(name)
        needs.pop(name)
        freed = []
        for other, deps in needs.items():
            if name in deps:
                deps.discard(name)
                if not deps:
                    freed.append(other)
        ready = sorted(ready + freed)
    if needs:
        raise FieldRefusal(
            'these size fields depend on each other in a cycle, so none of '
            'them can be built: ' + ', '.join(sorted(needs)))
    return tuple(order)


def graph_layout(rows) -> dict:
    """Positions for the field graph view: layer by depth, left to right.

    Pure, so the view and its test agree.  ``nodes`` maps a name to
    ``(column, row)``; ``edges`` are ``(reader, read)``.  A reader sits one
    column to the right of the deepest field it reads.
    """
    names = [str(r.get('name') or '') for r in rows if r.get('name')]
    reads: dict[str, set] = {name: set() for name in names}
    for row in rows:
        name = str(row.get('name') or '')
        kind = TYPES.get(str(row.get('type') or ''))
        if not name or kind is None:
            continue
        for option in kind.options:
            value = (row.get('params') or {}).get(option.name)
            if option.kind == 'field' and value:
                reads[name].add(str(value))
            elif option.kind == 'fields' and value:
                targets = ([p.strip() for p in value.split(',')]
                           if isinstance(value, str) else value)
                reads[name].update(str(t) for t in targets if str(t).strip())
    depth: dict[str, int] = {}

    def column(name, trail=()):
        if name in depth:
            return depth[name]
        if name in trail or name not in reads:
            return 0
        deps = [column(t, (*trail, name)) for t in reads[name] if t in reads]
        depth[name] = (max(deps) + 1) if deps else 0
        return depth[name]

    for name in names:
        column(name)
    rows_in: dict[int, int] = {}
    nodes = {}
    for name in sorted(names, key=lambda n: (depth.get(n, 0), n)):
        col = depth.get(name, 0)
        nodes[name] = (col, rows_in.get(col, 0))
        rows_in[col] = rows_in.get(col, 0) + 1
    edges = [(name, target) for name in names
             for target in sorted(reads[name]) if target in nodes]
    return {'nodes': nodes, 'edges': edges,
            'columns': (max(depth.values()) + 1) if depth else 0}


# --------------------------------------------------------------------------- #
# The Automatic sizing shortcut (M-20): the settings live in the row
# --------------------------------------------------------------------------- #

AUTOMATIC_TYPE = 'AutomaticMeshSizeField'
AUTOMATIC_NAME = 'automatic'

#: Row parameter -> the project field the size-field tool records its inputs
#: in, and what the tool uses when the row leaves the parameter out.
AUTOMATIC_INPUTS = {
    'hMin': ('sizing.field.minimum_size', 0.0),
    'hMax': ('sizing.field.maximum_size', 0.0),
    'hBulk': ('sizing.field.bulk_size', 0.0),
    'gradation': ('sizing.field.gradation', 1.1),
    'nPointsPerCircle': ('sizing.field.nodes_per_two_pi', 20),
    'nPointsPerGap': ('sizing.field.nodes_per_gap', 4),
    'smoothing': ('sizing.field.smoothing', True),
}
#: The row's Background box is the shortcut's "use as background".
AUTOMATIC_BACKGROUND_FIELD = 'sizing.field.use_as_background'


def automatic_row(rows) -> dict | None:
    for row in rows or ():
        if row.get('type') == AUTOMATIC_TYPE:
            return row
    return None


def with_automatic_row(rows) -> tuple[list, dict, bool]:
    """``(rows, the automatic row, created)``; makes the row when absent."""
    rows = [dict(item, params=dict(item.get('params') or {}))
            for item in (rows or ())]
    found = automatic_row(rows)
    if found is not None:
        return rows, found, False
    names = {item.get('name') for item in rows}
    name, number = AUTOMATIC_NAME, 1
    while name in names:
        number += 1
        name = f'{AUTOMATIC_NAME}{number}'
    created = new_row(AUTOMATIC_TYPE, name)
    rows.append(created)
    return rows, created, True


def automatic_patch(rows) -> dict:
    """The tool's recorded inputs, read from the row (never typed twice)."""
    row = automatic_row(rows)
    params = dict((row or {}).get('params') or {})
    patch = {}
    for name, (field_id, fallback) in AUTOMATIC_INPUTS.items():
        value = params.get(name)
        patch[field_id] = fallback if value in (None, '') else value
    patch[AUTOMATIC_BACKGROUND_FIELD] = bool(
        row and row.get('enabled', True) and row.get('background'))
    return patch
