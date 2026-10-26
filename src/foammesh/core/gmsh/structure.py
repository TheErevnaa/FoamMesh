"""Structured meshing of Gmsh as rows (Plan 41 GN8, M-24..M-27).

The native GUI's *Mesh > Define > Transfinite* entries, with the corner
picking and the arrangement the API takes:

====================  ==========================================  =========
kind                  Gmsh call                                   entities
====================  ==========================================  =========
transfiniteSurface    ``mesh.setTransfiniteSurface(tag, arr, c)``  surfaces
transfiniteVolume     ``mesh.setTransfiniteVolume(tag, c)``        volumes
transfiniteAutomatic  ``mesh.setTransfiniteAutomatic(d, a, r)``    surfaces / volumes / all
====================  ==========================================  =========

The arrangement values are Gmsh 4.15.2's own (``Left``, ``Right``,
``AlternateLeft``, ``AlternateRight``, read from the docstring of the installed
API on 2026-10-10).  The other structured settings of the row are catalogue
options and are set from the Options dock: ``Mesh.FlexibleTransfinite``,
``Mesh.TransfiniteTri``, ``Mesh.QuasiTransfinite`` (M-27), ``Mesh.Recombine3D*``
and ``Mesh.RecombineAll`` (A-07), ``Mesh.Quadqs*`` and algorithm 11 (O-04).

Nothing depends on the target solver.  The rows are one JSON list stored as
text (``gmsh.structure.rows``); the host validates a row and derives it into
the job's ``structure`` list; ``resources/gmsh/structure_v1.py`` applies it, in
the runner and in the resident session.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import dataclass

FIELD_ID = 'gmsh.structure.rows'
STORED_ID = 'gmsh/structure/rows'
CALCULATION_VERSION = 'gmsh.structure.v1'

#: ``setTransfiniteSurface`` arrangements, in the order the parameter counts.
ARRANGEMENTS = (
    (0, 'Left'), (1, 'Right'), (2, 'Alternate (left first)'),
    (3, 'Alternate (right first)'))
ARRANGEMENT_NAMES = ('Left', 'Right', 'AlternateLeft', 'AlternateRight')

#: Gmsh's default ``cornerAngle`` of ``setTransfiniteAutomatic`` (radians).
DEFAULT_CORNER_ANGLE = 2.35


class StructureRefusal(ValueError):
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
    min_tags: int = 1
    #: how many corner points may be named (0: none; the others are the
    #: counts Gmsh accepts)
    corners: tuple = (0,)

    def param(self, name):
        for item in self.params:
            if item.name == name:
                return item
        return None


_CURVE_ELEMENTS = Param(
    'curve_elements', 'int', 0,
    'Elements along every edge of the block that no other control has set '
    '(setTransfiniteCurve). 0: the edges are set elsewhere, on the Curve '
    'controls step.', low=0, high=10000)

KINDS = {k.id: k for k in (
    Kind('transfiniteSurface', 'Transfinite surface',
         'mesh.setTransfiniteSurface', (2,),
         'Mesh these surfaces as a structured grid between their corner '
         'points. The edges need a transfinite count each and opposite edges '
         'the same count. Name the corners (3 or 4 points) when the surface '
         'has more than four points on its boundary; leave them blank '
         'otherwise. The arrangement is how the triangles are cut when the '
         'surface is not recombined.',
         (Param('arrangement', 'enum', 0,
                'Arrangement of the triangles of a surface that is not '
                'recombined.', choices=ARRANGEMENTS),
          Param('recombine', 'bool', False,
                'Recombine the triangles of these surfaces into '
                'quadrangles (setRecombine); the arrangement then no '
                'longer matters.'),
          _CURVE_ELEMENTS),
         corners=(0, 3, 4)),
    Kind('transfiniteVolume', 'Transfinite volume',
         'mesh.setTransfiniteVolume', (3,),
         'Mesh these volumes as a structured block between their corner '
         'points (6 or 8 points; blank to let Gmsh choose). Gmsh builds the '
         'block from structured faces, so by default every bounding face is '
         'made transfinite first.',
         (Param('faces', 'bool', True,
                'Also make every bounding face of the volume transfinite '
                '(a transfinite volume on unstructured faces is ignored by '
                'Gmsh).'),
          Param('recombine', 'bool', True,
                'Recombine the bounding faces into quadrangles, so the '
                'block is hexahedra and not prisms or tetrahedra.'),
          _CURVE_ELEMENTS),
         corners=(0, 6, 8)),
    Kind('transfiniteAutomatic', 'Automatic transfinite',
         'mesh.setTransfiniteAutomatic', (2, 3),
         'Let Gmsh find the surfaces and volumes it can structure and set '
         'the transfinite constraints itself, the number of points coming '
         'from the sizing. Entities left blank: the whole model. It '
         'succeeds volume by volume, so the mesh is measured, not assumed.',
         (Param('corner_angle', 'float', DEFAULT_CORNER_ANGLE,
                'Quadrangular faces with a corner angle above this are '
                'ignored, radians (2.35 is 134.6 degrees).',
                low=0.0, high=math.pi),
          Param('recombine', 'bool', True,
                'Set the recombine flag on the transfinite surfaces, so '
                'they come out as quadrangles and the volumes as '
                'hexahedra.')),
         min_tags=0),
)}


def kind_choices() -> tuple:
    return tuple((k.id, k.title) for k in KINDS.values())


@dataclass(frozen=True)
class StructureOp:
    """One validated row, in the shape the job carries."""
    name: str
    kind: str
    dim: int
    tags: tuple
    corners: tuple = ()
    params: tuple = ()          # ((name, number), ...)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {'name': self.name, 'kind': self.kind, 'dim': self.dim,
                'tags': list(self.tags), 'corners': list(self.corners),
                'enabled': self.enabled,
                'params': [[n, v] for n, v in self.params]}


@dataclass(frozen=True)
class Structure:
    ops: tuple = ()
    warnings: tuple = ()
    calculation_version: str = CALCULATION_VERSION

    def to_list(self) -> list:
        return [op.to_dict() for op in self.ops]

    def __bool__(self) -> bool:
        return bool(self.ops)


def new_row(kind: str, name: str, dim: int | None = None) -> dict:
    spec = KINDS[kind]
    return {'name': name, 'kind': kind, 'enabled': True,
            'dim': spec.dims[-1] if dim is None else int(dim), 'tags': [],
            'corners': [],
            'params': {p.name: p.default for p in spec.params}}


def with_automatic_row(rows, dim=3, tags=(),
                       corner_angle=DEFAULT_CORNER_ANGLE, recombine=True,
                       name: str = '') -> tuple[list, dict, bool]:
    """DP-5738 (DP-4216). The rows with the Automatic transfinite row that
    "Make structured where possible" sets: ``(rows, row, added)``.

    The action sets the constraints on the session's model, which a
    re-import (any changed stage input) starts again and the batch run never
    sees, so the press is kept as the row it is a one-press form of. An
    enabled row already asking for the same is reused, not repeated.
    """
    tags = sorted(int(t) for t in (tags or ()))
    for row in rows:
        params = row.get('params') or {}
        if (row.get('kind') == 'transfiniteAutomatic'
                and row.get('enabled', True)
                and int(row.get('dim', 3)) == int(dim)
                and sorted(parse_tags(row.get('tags'))) == tags
                and abs(float(params.get('corner_angle', DEFAULT_CORNER_ANGLE))
                        - float(corner_angle)) < 1e-9
                and bool(params.get('recombine', True)) == bool(recombine)):
            return list(rows), row, False
    row = new_row('transfiniteAutomatic', name or 'transfiniteAutomatic1',
                  dim)
    row['tags'] = tags
    row['params'] = dict(row['params'], corner_angle=float(corner_angle),
                         recombine=bool(recombine))
    return [*rows, row], row, True


def rows_from_text(text) -> list[dict]:
    text = str(getattr(text, 'value', text) or '').strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as error:
        raise StructureRefusal(
            f'the stored structure rows are not valid JSON: {error}'
        ) from None
    if isinstance(data, dict):
        data = data.get('rows', [])
    if not isinstance(data, list):
        raise StructureRefusal('the stored structure rows are not a list')
    return [dict(item) for item in data if isinstance(item, dict)]


def rows_to_text(rows) -> str:
    rows = [dict(item) for item in (rows or ())]
    return json.dumps(rows, separators=(',', ':'), sort_keys=True) \
        if rows else ''


def parse_tags(value, unique: bool = True) -> list[int]:
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
            raise StructureRefusal(
                f'{item!r} is not an entity tag (a whole number)') from None
        if number != int(number) or number < 1:
            raise StructureRefusal(
                f'{item!r} is not an entity tag (a whole number from 1)')
        if not unique or int(number) not in tags:
            tags.append(int(number))
    return tags


def _number(spec: Param, value, owner: str) -> float:
    if isinstance(value, bool):
        number = 1.0 if value else 0.0
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise StructureRefusal(
                f'{owner}: {spec.name} needs a number, not {value!r}'
            ) from None
    if math.isnan(number) or math.isinf(number):
        raise StructureRefusal(f'{owner}: {spec.name} is not a finite number')
    if spec.kind == 'int' and number != int(number):
        raise StructureRefusal(
            f'{owner}: {spec.name} needs a whole number, not {value!r}')
    if spec.low is not None and number < spec.low:
        raise StructureRefusal(
            f'{owner}: {spec.name} {value!r} is below the minimum {spec.low:g}')
    if spec.high is not None and number > spec.high:
        raise StructureRefusal(
            f'{owner}: {spec.name} {value!r} is above the maximum '
            f'{spec.high:g}')
    if spec.kind == 'enum' and int(number) not in [c[0] for c in spec.choices]:
        raise StructureRefusal(
            f'{owner}: {spec.name} {value!r} is not one of '
            + ', '.join(f'{c[0]} ({c[1]})' for c in spec.choices))
    return number


def validate(rows) -> Structure:
    """Check every row; the first problem is raised with the row's name."""
    ops, names, warnings = [], set(), []
    for index, row in enumerate(rows or ()):
        name = str(row.get('name') or f'structure{index + 1}').strip()
        owner = f'structure {name!r}'
        if name in names:
            raise StructureRefusal(f'{owner}: two rows have the name {name!r}')
        names.add(name)
        kind = KINDS.get(str(row.get('kind') or ''))
        if kind is None:
            raise StructureRefusal(
                f'{owner}: unknown kind {row.get("kind")!r}; the kinds are '
                + ', '.join(KINDS))
        try:
            dim = int(row.get('dim', kind.dims[-1]))
        except (TypeError, ValueError):
            raise StructureRefusal(f'{owner}: dim is not a number') from None
        if dim not in kind.dims:
            raise StructureRefusal(
                f'{owner}: {kind.title} ({kind.gmsh}) acts on '
                + ' or '.join(('points', 'curves', 'surfaces', 'volumes')[d]
                              for d in kind.dims)
                + f', not on dimension {dim} (Gmsh 4.15.2)')
        tags = parse_tags(row.get('tags'))
        if len(tags) < kind.min_tags:
            raise StructureRefusal(f'{owner}: {kind.title} needs an entity')
        corners = parse_tags(row.get('corners'), unique=False)
        if len(corners) not in kind.corners:
            allowed = [str(n) for n in kind.corners if n]
            raise StructureRefusal(
                f'{owner}: {kind.title} takes '
                + (' or '.join(allowed) + ' corner points, or none'
                   if allowed else 'no corner points')
                + f', not {len(corners)}')
        if corners and len(tags) != 1:
            raise StructureRefusal(
                f'{owner}: the corners belong to one entity; name one '
                f'{"surface" if dim == 2 else "volume"}, or one row per '
                'entity')
        if len(set(corners)) != len(corners):
            raise StructureRefusal(f'{owner}: a corner point is named twice')
        given = dict(row.get('params') or {})
        for key in given:
            if kind.param(key) is None:
                raise StructureRefusal(
                    f'{owner}: {kind.title} has no parameter {key!r}')
        params = [(spec.name, _number(spec, given.get(spec.name, spec.default),
                                      owner)) for spec in kind.params]
        ops.append(StructureOp(name, kind.id, dim, tuple(tags),
                               tuple(corners), tuple(params),
                               bool(row.get('enabled', True))))
    claims: dict = {}
    for op in ops:
        if not op.enabled or op.kind == 'transfiniteAutomatic':
            continue
        for tag in op.tags:
            other = claims.setdefault((op.kind, tag), op.name)
            if other != op.name:
                raise StructureRefusal(
                    f'structure {other!r} and {op.name!r} both make '
                    f'{("surface", "volume")[op.dim - 2]} {tag} transfinite; '
                    'keep one')
    return Structure(ops=tuple(ops), warnings=tuple(warnings))


def derive(text) -> Structure:
    """The stored rows to the ops the job carries."""
    return validate(rows_from_text(text))


def describe(row: dict) -> str:
    """One line for a row, as the table shows it."""
    kind = KINDS.get(row.get('kind'))
    if kind is None:
        return str(row.get('kind') or '')
    tags = ', '.join(str(t) for t in parse_tags(row.get('tags') or []))
    dimension = ('point', 'curve', 'surface', 'volume')[
        int(row.get('dim', kind.dims[-1]))]
    where = f'{dimension} {tags}' if tags else 'every entity'
    return f'{kind.title}: {where}'


def block_surfaces(ops, boundary_faces) -> list:
    """The surfaces the block grid is drawn on: the surfaces of enabled
    transfinite-surface rows and the faces of enabled transfinite-volume rows
    (``boundary_faces(volume) -> [surface]`` is asked of the model)."""
    out = []
    for op in ops or ():
        op = op.to_dict() if hasattr(op, 'to_dict') else dict(op)
        if not op.get('enabled', True):
            continue
        if op['kind'] == 'transfiniteSurface':
            out.extend(int(t) for t in op['tags'])
        elif op['kind'] == 'transfiniteVolume':
            for volume in op['tags']:
                out.extend(boundary_faces(volume))
    seen = []
    for tag in out:
        if tag not in seen:
            seen.append(tag)
    return seen


# -- applying them on the resident session ------------------------------------ #

def runtime_module():
    """``resources/gmsh/structure_v1.py``, loaded once per process."""
    module = sys.modules.get('foammesh._structure_v1')
    if module is not None:
        return module
    from resources import resource

    location = resource.file('gmsh/structure_v1.py')
    spec = importlib.util.spec_from_file_location(
        'foammesh._structure_v1', str(location))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RemoteGmsh:
    """``gmsh.model.mesh.setTransfiniteSurface(...)`` spelled as a session
    call.  The resident session runs any ``model.*`` / ``option.*`` call by
    name and logs the ones that change the model, so a crash replays them."""

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


def preview_on_session(call, ops) -> dict:
    """The block grid of the enabled rows, drawn from the 1D mesh of the
    session; the mesh is cleared again.  Refused when a mesh is present."""
    runtime = runtime_module()
    remote = RemoteGmsh(call)
    nodes = remote.model.mesh.getNodes()
    if nodes and len(nodes[0]):
        raise ValueError(
            'the model already has a mesh; delete it (Generate > Delete '
            'mesh) before previewing the block grid, so the preview does '
            'not throw away a mesh you meant to keep')
    surfaces = block_surfaces(
        ops, lambda volume: runtime.boundary_faces(remote, int(volume)))
    if not surfaces:
        return {'surfaces': [], 'skipped': []}
    return runtime.block_grid(remote, surfaces)


# -- Plan 48 GM6: pre-check, make structured, hex-dominant -------------------- #

def precheck_on_session(call, dim, tags, corners=()) -> dict:
    """Can each of *tags* be transfinite?  ``{'entities': [{'tag',
    'qualifies', 'reason', 'corner_candidates', ...}]}``: the reason is
    Gmsh's own topology rule, named (more than four sides, a hole, a face of
    seven sides); the corner candidates are the points the person may pick."""
    return call('session.structure_precheck', dim=int(dim),
                tags=[int(t) for t in tags],
                corners=[int(c) for c in corners or ()])


def precheck_row_on_session(call, row: dict) -> dict:
    """The pre-check of one stored row (its tags and picked corners)."""
    kind = KINDS.get(row.get('kind'))
    if kind is None or row.get('kind') == 'transfiniteAutomatic':
        raise StructureRefusal('only a transfinite surface or volume row '
                               'has a corner pre-check')
    dim = 2 if row['kind'] == 'transfiniteSurface' else 3
    tags = parse_tags(row.get('tags'))
    if not tags:
        raise StructureRefusal('pick the %s to check'
                               % ('surfaces' if dim == 2 else 'volumes'))
    return precheck_on_session(call, dim, tags, parse_tags(
        row.get('corners'), unique=False))


def make_structured_on_session(call, dim=3, tags=(),
                               corner_angle=DEFAULT_CORNER_ANGLE,
                               recombine=True, apply=True) -> dict:
    """"Make structured where possible": which volumes (``dim`` 3) or
    surfaces (``dim`` 2) can be transfinite, set on those that can
    (``apply``), the others named with the reason.  ``colours`` maps an
    entity tag to the RGB the preview paints it."""
    runtime = runtime_module()
    try:
        return runtime.make_structured(RemoteGmsh(call), int(dim), tags,
                                       float(corner_angle), bool(recombine),
                                       bool(apply))
    except runtime.BuildError as error:
        raise ValueError(str(error)) from error


def hex_dominant_on_session(call, level=1, conformity=0,
                            time_limit=None) -> dict:
    """The "Hex-dominant (experimental)" action: 3D recombination on, mesh,
    and the count of tetrahedra, hexahedra, prisms and pyramids.

    Plan 48 TL: the 3D meshing runs under the Recombine3D time limit
    (*time_limit* seconds, else the project's, else the default; 0 for
    none). A mesh that does not return is refused with the Gmsh 4.15.2 limit
    named, and the session is restarted from its log.
    """
    from . import generate_actions as actions

    runtime = runtime_module()
    owner = getattr(call, '__self__', None)
    if time_limit is None:
        time_limit = actions.limit_for(
            'recombine3d', getattr(owner, 'time_limits', None))
    seconds = float(time_limit or 0)

    def limited(op, *args, **kwargs):
        if op == 'model.mesh.generate' and seconds:
            kwargs.setdefault('timeout', seconds)
        return call(op, *args, **kwargs)

    try:
        algorithm = call('option.getNumber', 'Mesh.Algorithm3D')
    except Exception:                                  # noqa: BLE001
        algorithm = None

    def refuse():
        restarted = actions.restart_after_limit(owner)
        # DP-4217: the action meshes with Mesh.Algorithm3D = 9; the restarted
        # session replays that from its log, so the person's choice is put
        # back here
        if algorithm is not None:
            try:
                call('option.setNumber', 'Mesh.Algorithm3D', algorithm)
            except Exception:                          # noqa: BLE001
                pass
        return actions.TimeLimitRefusal(
            actions.LIMIT_WHAT['recombine3d'], seconds, restarted)

    try:
        return runtime.hex_dominant(RemoteGmsh(limited), int(level),
                                    int(conformity))
    except runtime.BuildError as error:
        # BuildError wraps the crash that ended the 3D mesh.
        cause = error.__cause__
        if cause is not None and seconds and actions.is_time_limit(cause):
            raise refuse() from None
        raise ValueError(str(error)) from error
    except Exception as error:                         # noqa: BLE001
        if seconds and actions.is_time_limit(error):
            raise refuse() from None
        raise
