"""Per-entity meshing controls of Gmsh (Plan 41 GN9, M-28..M-32, A-17).

The native GUI's *Mesh > Define* entries that act on one model entity or a few:

==================  =============================================  =========
kind                Gmsh call                                      entities
==================  =============================================  =========
embed               ``mesh.embed(dim, tags, inDim, inTag)``        points, curves, surfaces into a surface or a volume
recombine           ``mesh.setRecombine(2, tag, angle)``           surfaces
algorithm           ``mesh.setAlgorithm(2, tag, value)``           surfaces
smoothing           ``mesh.setSmoothing(2, tag, steps)``           surfaces
sizeFromBoundary    ``mesh.setSizeFromBoundary(2, tag, 0|1)``     surfaces
reverse             ``mesh.setReverse(dim, tag, True)``            curves, surfaces
outwardOrientation  ``mesh.setOutwardOrientation(tag)``            volumes
compound            ``mesh.setCompound(dim, tags)``                curves or surfaces
==================  =============================================  =========

The dimension limits are Gmsh 4.15.2's own (read from the docstrings of the
installed API on 2026-10-10), not ours: a row outside them is refused here,
with the reason, before any run.  Nothing depends on the target solver.

The rows are one JSON list stored as text (``gmsh.entityControls.rows``), like
the size fields: the set of kinds is Gmsh's.  The host validates a row and
derives it into the job's ``entityControls`` list; ``resources/gmsh/
entity_controls_v1.py`` applies it, in the runner and in the resident session.
"""
from __future__ import annotations

import json
import math
import importlib.util
import sys
from dataclasses import dataclass

FIELD_ID = 'gmsh.entityControls.rows'
CALCULATION_VERSION = 'gmsh-entity-controls-v1'

#: ``Mesh.Algorithm`` values Gmsh accepts for one surface, with Gmsh's names.
SURFACE_ALGORITHMS = (
    (1, 'MeshAdapt'), (2, 'Automatic'), (3, 'Initial mesh only'),
    (5, 'Delaunay'), (6, 'Frontal-Delaunay'), (7, 'BAMG'),
    (8, 'Frontal-Delaunay for quads'), (9, 'Packing of parallelograms'),
    (11, 'Quasi-structured quad'),
)

#: Gmsh's ``Mesh.CompoundClassify`` and ``Mesh.CompoundMeshSizeFactor`` read
#: back 1 and 0.5 on 4.15.2.
DEFAULT_COMPOUND_CLASSIFY = 1
DEFAULT_COMPOUND_SIZE_FACTOR = 0.5


class EntityRefusal(ValueError):
    """A stored row Gmsh cannot take; the message names the row and why."""


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                 # 'float' | 'int' | 'bool' | 'enum'
    default: object
    help: str
    low: float | None = None
    high: float | None = None
    choices: tuple = ()


@dataclass(frozen=True)
class Kind:
    id: str
    title: str
    gmsh: str                 # the API call, shown in the panel
    dims: tuple               # dimensions of the entities picked
    help: str
    params: tuple = ()
    #: several tags at once (embed and compound take a list; the others are
    #: applied to each tag)
    min_tags: int = 1

    def param(self, name):
        for item in self.params:
            if item.name == name:
                return item
        return None


KINDS = {k.id: k for k in (
    Kind('embed', 'Embed', 'mesh.embed', (0, 1, 2),
         'Force the mesh of a surface or a volume to conform to points, '
         'curves or surfaces inside it (a baffle, a wire, a sizing point). '
         'The embedded entities must not touch the boundary of the entity '
         'they are embedded in.',
         (Param('in_dim', 'enum', 3,
                'Dimension of the entity that receives them.',
                choices=((2, 'surface'), (3, 'volume'))),
          Param('in_tag', 'int', 1, 'Tag of the surface or volume that '
                'receives them.', low=1))),
    Kind('recombine', 'Recombine surface', 'mesh.setRecombine', (2,),
         'Recombine the triangles of these surfaces into quadrangles; other '
         'surfaces keep their triangles.',
         (Param('angle', 'float', 45.0,
                'Threshold angle of the simple recombination algorithm, '
                'degrees.', low=0.0, high=180.0),)),
    Kind('algorithm', 'Surface algorithm', 'mesh.setAlgorithm', (2,),
         'Mesh these surfaces with their own 2D algorithm instead of the '
         'global one.',
         (Param('value', 'enum', 6, 'The 2D algorithm of these surfaces.',
                choices=SURFACE_ALGORITHMS),)),
    Kind('smoothing', 'Surface smoothing', 'mesh.setSmoothing', (2,),
         'Laplace smoothing passes applied to these surfaces after they are '
         'meshed.',
         (Param('steps', 'int', 1, 'Number of smoothing iterations.',
                low=0, high=1000),)),
    Kind('sizeFromBoundary', 'Size from boundary', 'mesh.setSizeFromBoundary',
         (2,),
         'Extend the mesh size of the boundary curves into these surfaces, '
         'or stop doing so.',
         (Param('on', 'bool', True, 'Extend the boundary size into the '
                'surface.'),)),
    Kind('reverse', 'Reverse', 'mesh.setReverse', (1, 2),
         'Reverse the orientation of the mesh of these curves or surfaces '
         'with respect to the natural orientation of the geometry.'),
    Kind('outwardOrientation', 'Outward orientation',
         'mesh.setOutwardOrientation', (3,),
         'Orient every bounding surface of these volumes with outward '
         'pointing normals (OpenCASCADE kernel only), and reorient an '
         'existing mesh.'),
    Kind('compound', 'Compound', 'mesh.setCompound', (1, 2),
         'Mesh these curves or surfaces as one discrete entity, '
         'reparametrised automatically.',
         (Param('classify', 'bool', True,
                'Mesh.CompoundClassify: classify the compound before '
                'meshing it.'),
          Param('size_factor', 'float', DEFAULT_COMPOUND_SIZE_FACTOR,
                'Mesh.CompoundMeshSizeFactor: size factor of the compound '
                'mesh.', low=1e-6, high=1000.0)),
         min_tags=2),
)}


def kind_choices() -> tuple:
    return tuple((k.id, k.title) for k in KINDS.values())


@dataclass(frozen=True)
class EntityOp:
    """One validated row, in the shape the job carries."""
    name: str
    kind: str
    dim: int
    tags: tuple
    params: tuple = ()          # ((name, number), ...)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {'name': self.name, 'kind': self.kind, 'dim': self.dim,
                'tags': list(self.tags), 'enabled': self.enabled,
                'params': [[n, v] for n, v in self.params]}


@dataclass(frozen=True)
class EntityControls:
    ops: tuple = ()
    warnings: tuple = ()
    calculation_version: str = CALCULATION_VERSION

    def to_list(self) -> list:
        return [op.to_dict() for op in self.ops]


def new_row(kind: str, name: str, dim: int | None = None) -> dict:
    spec = KINDS[kind]
    return {'name': name, 'kind': kind, 'enabled': True,
            'dim': spec.dims[-1] if dim is None else int(dim), 'tags': [],
            'params': {p.name: p.default for p in spec.params}}


def next_row_name(kind: str, rows) -> str:
    """The name Add control gives a new row: ``<kind>1``, ``<kind>2``, ..."""
    names = {r.get('name') for r in (rows or ())}
    number = 1
    while f'{kind}{number}' in names:
        number += 1
    return f'{kind}{number}'


def catalogue() -> list[dict]:
    """The kinds Add control offers, with every parameter's kind, default,
    range and choices (Plan 48 PA2: what the panel draws, as data)."""
    out = []
    for kind in KINDS.values():
        out.append({
            'id': kind.id, 'title': kind.title, 'gmsh': kind.gmsh,
            'dims': list(kind.dims), 'help': kind.help,
            'min_tags': kind.min_tags,
            'default_dim': kind.dims[-1],
            'params': [{'name': p.name, 'kind': p.kind,
                        'default': p.default, 'help': p.help,
                        'low': p.low, 'high': p.high,
                        'choices': [{'value': c[0], 'title': c[1]}
                                    for c in p.choices]}
                       for p in kind.params]})
    return out


def rows_from_text(text) -> list[dict]:
    text = str(getattr(text, 'value', text) or '').strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as error:
        raise EntityRefusal(
            f'the stored entity controls are not valid JSON: {error}'
        ) from None
    if isinstance(data, dict):
        data = data.get('rows', [])
    if not isinstance(data, list):
        raise EntityRefusal('the stored entity controls are not a list')
    return [dict(item) for item in data if isinstance(item, dict)]


def rows_to_text(rows) -> str:
    rows = [dict(item) for item in (rows or ())]
    return json.dumps(rows, separators=(',', ':'), sort_keys=True) \
        if rows else ''


def parse_tags(value) -> list[int]:
    """``'1, 2 5'`` or ``[1, 2]`` to ``[1, 2, 5]``; a bad entry is refused."""
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = str(value or '').replace(',', ' ').replace(';', ' ').split()
    tags = []
    for item in items:
        try:
            number = float(item)
        except (TypeError, ValueError):
            raise EntityRefusal(
                f'{item!r} is not an entity tag (a whole number)') from None
        if number != int(number) or number < 1:
            raise EntityRefusal(
                f'{item!r} is not an entity tag (a whole number from 1)')
        if int(number) not in tags:
            tags.append(int(number))
    return tags


def _number(spec: Param, value, owner: str) -> float:
    if isinstance(value, bool):
        number = 1.0 if value else 0.0
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise EntityRefusal(
                f'{owner}: {spec.name} needs a number, not {value!r}'
            ) from None
    if math.isnan(number) or math.isinf(number):
        raise EntityRefusal(f'{owner}: {spec.name} is not a finite number')
    if spec.kind == 'int' and number != int(number):
        raise EntityRefusal(
            f'{owner}: {spec.name} needs a whole number, not {value!r}')
    if spec.low is not None and number < spec.low:
        raise EntityRefusal(
            f'{owner}: {spec.name} {value!r} is below the minimum {spec.low:g}')
    if spec.high is not None and number > spec.high:
        raise EntityRefusal(
            f'{owner}: {spec.name} {value!r} is above the maximum '
            f'{spec.high:g}')
    if spec.kind == 'enum' and int(number) not in [c[0] for c in spec.choices]:
        raise EntityRefusal(
            f'{owner}: {spec.name} {value!r} is not one of '
            + ', '.join(f'{c[0]} ({c[1]})' for c in spec.choices))
    return number


def validate(rows) -> EntityControls:
    """Check every row; the first problem is raised with the row's name."""
    ops, names, warnings = [], set(), []
    for index, row in enumerate(rows or ()):
        name = str(row.get('name') or f'control{index + 1}').strip()
        owner = f'entity control {name!r}'
        if name in names:
            raise EntityRefusal(f'{owner}: two rows have the name {name!r}')
        names.add(name)
        kind = KINDS.get(str(row.get('kind') or ''))
        if kind is None:
            raise EntityRefusal(
                f'{owner}: unknown kind {row.get("kind")!r}; the kinds are '
                + ', '.join(KINDS))
        try:
            dim = int(row.get('dim', kind.dims[-1]))
        except (TypeError, ValueError):
            raise EntityRefusal(f'{owner}: dim is not a number') from None
        if dim not in kind.dims:
            raise EntityRefusal(
                f'{owner}: {kind.title} ({kind.gmsh}) acts on '
                + ' or '.join(('points', 'curves', 'surfaces', 'volumes')[d]
                              for d in kind.dims)
                + f', not on dimension {dim} (Gmsh 4.15.2)')
        tags = parse_tags(row.get('tags'))
        if len(tags) < kind.min_tags:
            what = 'at least two entities' if kind.min_tags > 1 \
                else 'an entity'
            raise EntityRefusal(f'{owner}: {kind.title} needs {what}')
        given = dict(row.get('params') or {})
        for key in given:
            if kind.param(key) is None:
                raise EntityRefusal(
                    f'{owner}: {kind.title} has no parameter {key!r}')
        params = []
        for spec in kind.params:
            params.append((spec.name, _number(
                spec, given.get(spec.name, spec.default), owner)))
        values = dict(params)
        if kind.id == 'embed':
            if int(values['in_dim']) <= dim:
                raise EntityRefusal(
                    f'{owner}: a {("point", "curve", "surface")[dim]} can '
                    'only be embedded in a larger entity: the surface or '
                    'volume that receives it must be of a higher dimension')
            if int(values['in_tag']) in tags and int(values['in_dim']) == dim:
                raise EntityRefusal(f'{owner}: an entity cannot be embedded '
                                    'in itself')
        enabled = bool(row.get('enabled', True))
        ops.append(EntityOp(name, kind.id, dim, tuple(tags), tuple(params),
                            enabled))
    claims: dict = {}
    for op in ops:
        if not op.enabled:
            continue
        # Two rows deciding the same single-valued thing on one entity: the
        # later one would silently win in Gmsh.
        if op.kind in ('recombine', 'algorithm', 'smoothing',
                       'sizeFromBoundary', 'reverse', 'outwardOrientation'):
            for tag in op.tags:
                other = claims.setdefault((op.kind, op.dim, tag), op.name)
                if other != op.name:
                    raise EntityRefusal(
                        f'entity controls {other!r} and {op.name!r} both set '
                        f'{KINDS[op.kind].title.lower()} on '
                        f'{("point", "curve", "surface", "volume")[op.dim]} '
                        f'{tag}; keep one')
    return EntityControls(ops=tuple(ops), warnings=tuple(warnings))


def describe(row: dict) -> str:
    """One line for a row, as the table shows it."""
    kind = KINDS.get(row.get('kind'))
    tags = ', '.join(str(t) for t in parse_tags(row.get('tags') or []))
    if kind is None:
        return str(row.get('kind') or '')
    dimension = ('point', 'curve', 'surface', 'volume')[
        int(row.get('dim', kind.dims[-1]))]
    return f'{kind.title}: {dimension} {tags or "none picked"}'


# -- applying them on the resident session ------------------------------------ #

def runtime_module():
    """``resources/gmsh/entity_controls_v1.py``, loaded once per process."""
    module = sys.modules.get('foammesh._entity_controls_v1')
    if module is not None:
        return module
    from resources import resource

    location = resource.file('gmsh/entity_controls_v1.py')
    spec = importlib.util.spec_from_file_location(
        'foammesh._entity_controls_v1', str(location))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RemoteGmsh:
    """``gmsh.model.mesh.embed(...)`` spelled as a session call.

    The resident session runs any ``model.*`` / ``option.*`` call by name and
    logs the ones that change the model, so a crash replays them.  The shared
    applier therefore runs here unchanged, against this stand-in.
    """

    def __init__(self, call, path=()):
        self._call = call
        self._path = path

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return RemoteGmsh(self._call, self._path + (name,))

    def __call__(self, *args, **kwargs):
        return self._call('.'.join(self._path), *args, **kwargs)


def apply_to_session(call, ops) -> dict:
    """Apply derived *ops* through ``call(op, *args)``; raises ValueError."""
    runtime = runtime_module()
    try:
        result = runtime.apply(RemoteGmsh(call), ops or [])
    except runtime.BuildError as error:
        raise ValueError(str(error)) from error
    return {'count': result['count'],
            'applied': [[a, b, str(c), str(d)] for a, b, c, d
                        in result['applied']]}
