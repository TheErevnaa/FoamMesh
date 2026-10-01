"""The farfield as snappyHexMesh builds it: one outer boundary surface.

Plan 37 UF14 (section 4.4). The farfield is authored once, in the UF13
specification (``core/mesh/farfield_spec.py``, store ``gmsh/farfield``), and
this module is the snappy adapter for it. snappy reads the same record Gmsh
does -- there is no second copy on the Geometry page to drift from it.

How snappy builds it
--------------------

* The primitive the specification resolves to around the model is written to
  ``geometry{}`` as a closed searchable surface under the key ``far_field``
  (``searchableBox`` / ``searchableSphere`` / ``searchableCylinder``) and to
  ``refinementSurfaces`` with ``patchInfo { type patch; }`` and no
  ``faceZone``: a real boundary. snappy keeps the cells on the seed's side of
  every boundary surface, so everything between the farfield and the
  background block is discarded with the bodies' interiors.
* The background block must hold the farfield with room to spare. When the
  domain is derived from the geometry the block grows to hold it; an authored
  Hex6 or block set is never moved -- it is checked, and a block that does
  not hold the farfield is refused with the box that would.
* Every other box, sphere or cylinder keeps the DP-668 meaning (an internal
  faceZone): only the specification carries the outer-boundary role.

Patch names, and why they are not Gmsh's
----------------------------------------

OpenFOAM 13's ``box``, ``sphere`` and ``cylinder`` searchable surfaces each
have a single region, ``region0``, and ``searchableSurfaceList`` names a
single-region surface's patch after its geometry key. So on snappy every
farfield shape publishes exactly one patch, ``far_field``. Gmsh names the
box's six faces ``far_field_xMin`` .. ``far_field_zMax`` and the cylinder's
``far_field_side`` / ``_inlet`` / ``_outlet``; snappy cannot, and says so
(:func:`naming_note`) instead of claiming parity. The sphere is ``far_field``
on both engines.

What the fluid is
-----------------

With a farfield the fluid is the space inside the farfield and outside every
body. A fluid seed must lie there; a seed outside the farfield (in the ring
the block adds around it) would keep only that ring, and a seed inside a body
would mesh the body. Both are refused here before snappy runs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import farfield_spec
from .exclude_points import ERROR, WARNING, Finding, Report, triangles

#: The ``geometry{}`` key, and therefore the patch name, on every shape.
PATCH = 'far_field'
GEOMETRY_KEY = PATCH

#: The boundary category the patch identity report records for it.
CATEGORY = 'far_field'

#: The block holds the farfield with at least this fraction of the farfield's
#: largest span on every side...
CLEARANCE_FRACTION = 0.05
#: ... and at least this many background cells, so castellation has cells on
#: both sides of the farfield to cut between.
CLEARANCE_CELLS = 1.5

SEMANTICS = (
    'The fluid is the space inside the farfield and outside every body; the '
    'background block beyond the farfield is discarded, and every region '
    'seed must lie inside the farfield.')

#: Finding codes.
FLUID_OUTSIDE = 'farfield.seed_outside'
FLUID_ON_WALL = 'farfield.seed_on_farfield'
FLUID_IN_BODY = 'farfield.seed_inside_body'
FLUID_MAYBE_IN_BODY = 'farfield.seed_maybe_inside_body'
FLUID_IN_CAVITY = 'farfield.seed_in_cavity'
SOLID_OUTSIDE_BODIES = 'farfield.solid_seed_outside_bodies'
EXCLUDE_OUTSIDE = 'farfield.exclude_outside'
NOT_ENCLOSED = 'farfield.block_does_not_hold'
INVALID = 'farfield.invalid'
NAME_TAKEN = 'farfield.name_taken'
COARSE = 'farfield.coarse'

#: Fewer background cells than this across the farfield's smallest span is
#: too coarse to resolve it at all.
FEW_CELLS_ACROSS = 8


# -- the specification ------------------------------------------------------ #

def active(db):
    """The enabled specification, or ``None`` when there is no farfield."""
    spec = farfield_spec.read(db)
    return spec if spec.enabled else None


def gmsh_bounds(bounds) -> list[float]:
    """``(xmin, ymin, zmin, xmax, ymax, zmax)`` from a `BBox` or six numbers
    in the case builder's order ``(xmin, xmax, ymin, ymax, zmin, zmax)``."""
    if hasattr(bounds, 'to_tuple'):
        bounds = bounds.to_tuple()
    values = [float(value) for value in bounds]
    return [values[0], values[2], values[4], values[1], values[3], values[5]]


def geometry_revision(bounds) -> str:
    """What an auto centre and a padding follow: the model's extent."""
    if bounds is None:
        return ''
    if hasattr(bounds, 'to_tuple'):
        bounds = bounds.to_tuple()
    return ','.join(f'{float(value):.12g}' for value in bounds)


@dataclass(frozen=True)
class Farfield:
    """One resolved farfield: the spec, its primitive and its surface."""

    spec: object
    #: ``farfield_primitives.resolve``: the runner's own description.
    primitive: dict
    #: ``FarfieldSpec.geometry_primitive``: ``shape``/``point1``/``point2``/
    #: ``radius`` in the Geometry page's vocabulary.
    surface: dict

    @property
    def shape(self) -> str:
        return str(self.primitive['shape'])

    def bounds(self) -> tuple[float, ...]:
        return primitive_bounds(self.primitive)

    def geometry_entry(self) -> dict:
        return geometry_entry(self.surface)

    def contains(self, point) -> str:
        return classify(self.primitive, point)


def resolve(spec, model_bounds) -> Farfield:
    """The farfield around geometry spanning *model_bounds* (builder order).

    Raises ``ValueError`` -- the UF13 ``FarfieldError`` -- for a spec that
    cannot be built or a primitive that does not hold every body with
    clearance, with the dimensions that would.
    """
    if model_bounds is None:
        raise ValueError(
            'the farfield is placed around the geometry, and there is no '
            'geometry to place it around; import a surface first')
    ordered = gmsh_bounds(model_bounds)
    primitive = spec.resolve(ordered)
    return Farfield(spec, primitive, spec.geometry_primitive(ordered))


# -- geometry ---------------------------------------------------------------- #

def _number(value) -> float:
    return float(f'{float(value):.12g}')


def geometry_entry(surface) -> dict:
    """The ``geometry{}`` entry snappy reads for the farfield surface."""
    point1 = [_number(value) for value in surface['point1']]
    point2 = [_number(value) for value in surface['point2']]
    if surface['shape'] == 'hex':
        return {'type': 'searchableBox', 'min': point1, 'max': point2}
    if surface['shape'] == 'sphere':
        return {'type': 'searchableSphere', 'centre': point1,
                'radius': _number(surface['radius'])}
    return {'type': 'searchableCylinder', 'point1': point1, 'point2': point2,
            'radius': _number(surface['radius'])}


def refinement_entry() -> dict:
    """Its ``refinementSurfaces`` entry: a boundary patch at the base level.

    ``patchInfo`` and no ``faceZone`` is what makes it a boundary: snappy
    keeps only the seeded side of it (DP-668's internal faceZone is the
    opposite, and stays the meaning of every other primitive).
    """
    return {'level': [0, 0], 'patchInfo': {'type': 'patch'}}


def primitive_bounds(primitive) -> tuple[float, ...]:
    """The primitive's exact extent, ``(xmin, xmax, ymin, ymax, zmin, zmax)``."""
    shape = primitive['shape']
    if shape == 'box':
        origin, span = primitive['origin'], primitive['span']
        return tuple(value for axis in range(3) for value in (
            float(origin[axis]), float(origin[axis]) + float(span[axis])))
    if shape == 'sphere':
        centre, radius = primitive['centre'], float(primitive['radius'])
        return tuple(value for axis in range(3) for value in (
            float(centre[axis]) - radius, float(centre[axis]) + radius))
    base = [float(value) for value in primitive['base']]
    axis_vector = [float(value) for value in primitive['axis']]
    length, radius = float(primitive['length']), float(primitive['radius'])
    top = [base[axis] + axis_vector[axis] * length for axis in range(3)]
    result = []
    for axis in range(3):
        # A disc of radius r normal to a reaches r * sqrt(1 - a_i^2) along i.
        reach = radius * math.sqrt(max(0.0, 1.0 - axis_vector[axis] ** 2))
        result.extend((min(base[axis], top[axis]) - reach,
                       max(base[axis], top[axis]) + reach))
    return tuple(result)


def clearance(primitive, cell=None) -> float:
    """The room the block leaves around the farfield on every side."""
    bounds = primitive_bounds(primitive)
    span = max(bounds[2 * axis + 1] - bounds[2 * axis] for axis in range(3))
    room = CLEARANCE_FRACTION * span
    if cell and cell > 0 and math.isfinite(cell):
        room = max(room, CLEARANCE_CELLS * float(cell))
    return room


def enclosing_bounds(base, primitive, cell=None) -> tuple[float, ...]:
    """*base* grown to hold the farfield with :func:`clearance` around it."""
    room = clearance(primitive, cell)
    held = primitive_bounds(primitive)
    base = tuple(float(value) for value in (
        base.to_tuple() if hasattr(base, 'to_tuple') else base))
    return tuple(value for axis in range(3) for value in (
        min(base[2 * axis], held[2 * axis] - room),
        max(base[2 * axis + 1], held[2 * axis + 1] + room)))


def enclosure_problem(primitive, domain_bounds, cell=None) -> str:
    """Why a fixed block does not hold the farfield, or ``''`` when it does.

    The block has to reach past the farfield by at least one background cell
    so castellation has cells on its far side to discard; the message names
    the box that would.
    """
    held = primitive_bounds(primitive)
    room = CLEARANCE_CELLS * float(cell) if cell and cell > 0 else 0.0
    short = [axis for axis in range(3)
             if domain_bounds[2 * axis] > held[2 * axis] - room
             or domain_bounds[2 * axis + 1] < held[2 * axis + 1] + room]
    if not short:
        return ''
    wanted = enclosing_bounds(domain_bounds, primitive, cell)
    names = ', '.join('xyz'[axis] for axis in short)
    return (
        f'the background block does not hold the farfield {primitive["shape"]} '
        f'along {names}: the farfield spans '
        f'{_fmt_bounds(held)} and the block must reach at least '
        f'{_fmt_bounds(wanted)}. The block is authored, so it is not moved; '
        'enlarge it (or let the domain follow the geometry), or shrink the '
        'farfield')


def cells_across(primitive, cell) -> float:
    """How many background cells span the farfield's smallest dimension."""
    if not cell or cell <= 0:
        return math.inf
    bounds = primitive_bounds(primitive)
    return min(bounds[2 * axis + 1] - bounds[2 * axis]
               for axis in range(3)) / float(cell)


def _fmt_bounds(bounds) -> str:
    return '[' + ', '.join(
        f'{bounds[2 * axis]:.6g}..{bounds[2 * axis + 1]:.6g}'
        for axis in range(3)) + ']'


def _fmt(point) -> str:
    return '(' + ' '.join(f'{float(value):.6g}' for value in point) + ')'


# -- points ------------------------------------------------------------------ #

INSIDE = 'inside'
OUTSIDE = 'outside'
ON = 'on'


def _axial(primitive, point):
    centre = [float(value) for value in primitive['centre']]
    axis = [float(value) for value in primitive['axis']]
    offset = [float(point[index]) - centre[index] for index in range(3)]
    along = sum(offset[index] * axis[index] for index in range(3))
    radial = math.sqrt(max(sum(value * value for value in offset)
                           - along * along, 0.0))
    return along, radial


def signed_distance(primitive, point) -> float:
    """Distance to the farfield wall: negative inside, positive outside.

    Exact for the sphere; for the box and the cylinder the inside value is
    the distance to the nearest face and the outside value an upper bound on
    the distance -- the sign, which is what is asked, is exact.
    """
    point = [float(value) for value in point]
    shape = primitive['shape']
    if shape == 'sphere':
        return math.dist(point, primitive['centre']) - float(primitive['radius'])
    if shape == 'box':
        origin, span = primitive['origin'], primitive['span']
        gaps = []
        for axis in range(3):
            gaps.append(float(origin[axis]) - point[axis])
            gaps.append(point[axis] - float(origin[axis]) - float(span[axis]))
        return max(gaps)
    along, radial = _axial(primitive, point)
    return max(abs(along) - float(primitive['length']) / 2.0,
               radial - float(primitive['radius']))


def classify(primitive, point, within=None) -> str:
    """``inside``, ``outside`` or ``on`` the farfield wall."""
    within = (farfield_spec.primitives().tolerance(primitive)
              if within is None else float(within))
    distance = signed_distance(primitive, point)
    if abs(distance) <= within:
        return ON
    return INSIDE if distance < 0 else OUTSIDE


def signed_distances(primitive, points) -> np.ndarray:
    """`signed_distance` for an ``(n, 3)`` array of points at once."""
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    shape = primitive['shape']
    if shape == 'sphere':
        centre = np.asarray(primitive['centre'], dtype=float)
        return (np.linalg.norm(points - centre, axis=1)
                - float(primitive['radius']))
    if shape == 'box':
        origin = np.asarray(primitive['origin'], dtype=float)
        span = np.asarray(primitive['span'], dtype=float)
        return np.maximum((origin - points).max(axis=1),
                          (points - origin - span).max(axis=1))
    centre = np.asarray(primitive['centre'], dtype=float)
    axis = np.asarray(primitive['axis'], dtype=float)
    offset = points - centre
    along = offset @ axis
    radial = np.sqrt(np.maximum((offset * offset).sum(axis=1)
                                - along * along, 0.0))
    return np.maximum(np.abs(along) - float(primitive['length']) / 2.0,
                      radial - float(primitive['radius']))


def seed_inside(primitive, field, space_id, geometry_bounds=None):
    """A point of space *space_id* inside the farfield, or ``None``.

    DP-1111. The outside space of a labelled domain is seeded at its deepest
    voxel, which with a farfield is a corner of the background block -- in
    the ring snappy discards, so the launch gate refused the region detection
    had just proposed. The point returned is a voxel centre of the same space
    inside the farfield, as far as the field allows from both the farfield
    wall and the geometry's bounding box (the bodies are inside that box, so
    the point is clear of them too). ``None`` when no voxel of the space is
    inside the farfield with clearance.
    """
    labels = np.asarray(field.labels)
    flat = np.flatnonzero(labels.ravel() == int(space_id))
    if not len(flat):
        return None
    nz, ny, nx = labels.shape
    origin = np.asarray([float(field.box[2 * axis]) for axis in range(3)])
    spacing = np.asarray(field.spacing, dtype=float)
    ijk = np.stack((flat % nx, (flat // nx) % ny, flat // (nx * ny)), axis=1)
    centres = origin + (ijk + 0.5) * spacing
    depth = -signed_distances(primitive, centres)
    margin = (farfield_spec.primitives().tolerance(primitive)
              + float(spacing.max()))
    keep = depth > margin
    if not keep.any():
        return None
    centres, depth = centres[keep], depth[keep]
    score = depth
    if geometry_bounds is not None:
        low = np.asarray([float(geometry_bounds[2 * a]) for a in range(3)])
        high = np.asarray([float(geometry_bounds[2 * a + 1])
                           for a in range(3)])
        gap = np.maximum(np.maximum(low - centres, centres - high), 0.0)
        clear = np.linalg.norm(gap, axis=1)
        if (clear > 0).any():
            score = np.minimum(depth, clear)
    best = int(np.argmax(score))
    return [float(value) for value in centres[best]]


# -- inside a body ------------------------------------------------------------ #

#: Ray directions for the parity test: skewed off every axis and every face
#: diagonal so a ray does not run along an edge of an axis-aligned model.
_RAYS = ((0.5773, 0.5774, 0.5773), (-0.2673, 0.5345, 0.8018),
         (0.8729, -0.2182, 0.4364))


def _crossings(point, tris, direction) -> int:
    """How many of *tris* the ray from *point* along *direction* crosses."""
    origin = np.asarray(point, dtype=float)
    ray = np.asarray(direction, dtype=float)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    edge1, edge2 = b - a, c - a
    pvec = np.cross(ray, edge2)
    det = np.einsum('ij,ij->i', edge1, pvec)
    scale = max(float(np.max(np.abs(tris))), 1.0)
    ok = np.abs(det) > 1e-14 * scale * scale
    with np.errstate(divide='ignore', invalid='ignore'):
        inverse = np.where(ok, 1.0 / det, 0.0)
        tvec = origin - a
        u = np.einsum('ij,ij->i', tvec, pvec) * inverse
        qvec = np.cross(tvec, edge1)
        v = np.einsum('j,ij->i', ray, qvec) * inverse
        t = np.einsum('ij,ij->i', edge2, qvec) * inverse
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0)
    return int(np.count_nonzero(hit))


def closed(tris) -> bool:
    """Whether every edge of *tris* is shared by an even number of facets.

    Three rays can all miss a hole, so agreement alone does not make parity
    exact: a surface with a boundary edge has no inside, and a point "inside"
    it is only a guess. Vertices are matched to a millionth of the model's
    size, which joins the copies an STL writes for every facet.
    """
    if tris is None or not len(tris):
        return True
    points = np.asarray(tris, dtype=float).reshape(-1, 3)
    size = max(float(np.ptp(points, axis=0).max()), 1e-300)
    keys = np.round(points / (size * 1e-6)).astype(np.int64)
    _, ids = np.unique(keys, axis=0, return_inverse=True)
    ids = np.asarray(ids).reshape(-1, 3)
    edges = np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    edges = edges[edges[:, 0] != edges[:, 1]]
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return bool(np.all(counts % 2 == 0))


def inside_bodies(point, tris, *, is_closed=None) -> tuple[bool, bool]:
    """``(inside, certain)``: whether *point* is inside the closed surfaces.

    Odd-parity ray casting along three skewed rays. On a closed surface all
    three agree and the answer is exact; on an open one (a boundary edge,
    :func:`closed`) or when the rays disagree (one grazed an edge) the answer
    is the majority, marked uncertain. *is_closed* saves recomputing
    :func:`closed` for many points on the same triangles.
    """
    if tris is None or not len(tris):
        return False, True
    votes = [_crossings(point, tris, ray) % 2 == 1 for ray in _RAYS]
    inside = sum(votes) >= 2
    if is_closed is None:
        is_closed = closed(tris)
    return inside, bool(is_closed) and len(set(votes)) == 1


# -- the preflight ------------------------------------------------------------ #

def _seed_rows(seeds):
    rows = []
    for index, entry in enumerate(seeds or ()):
        if (isinstance(entry, (tuple, list)) and len(entry) == 3
                and not isinstance(entry[0], (int, float))):
            rows.append((str(entry[0]), str(entry[1] or 'fluid'),
                         tuple(float(value) for value in entry[2])))
        else:
            rows.append((f'region {index + 1}', 'fluid',
                         tuple(float(value) for value in entry)))
    return rows


def _exclude_rows(points):
    rows = []
    for index, entry in enumerate(points or ()):
        if (isinstance(entry, (tuple, list)) and len(entry) == 2
                and isinstance(entry[0], str)):
            rows.append((entry[0] or f'exclude {index + 1}',
                         tuple(float(value) for value in entry[1])))
        else:
            rows.append((f'exclude {index + 1}',
                         tuple(float(value) for value in entry)))
    return rows


def exact_findings(farfield: Farfield, *, seeds=(), excludes=(),
                   surfaces=None) -> list[Finding]:
    """What is certain about the seeds against the farfield and the bodies.

    *seeds* are ``(label, type, xyz)`` or bare points; *excludes* ``(name,
    xyz)`` or bare points; *surfaces* the model surfaces (vtkPolyData or
    triangle arrays), without the farfield. A seed outside or on the
    farfield, and a fluid seed inside a closed body, are errors; the rest
    are warnings.
    """
    primitive = farfield.primitive
    tris = None
    if surfaces:
        parts = [triangles(surface) for surface in surfaces]
        parts = [part for part in parts if len(part)]
        tris = np.concatenate(parts) if parts else None
    is_closed = closed(tris)
    findings: list[Finding] = []
    for label, kind, point in _seed_rows(seeds):
        where = classify(primitive, point)
        if where == OUTSIDE:
            findings.append(Finding(
                FLUID_OUTSIDE, ERROR,
                f'region {label} {_fmt(point)} is outside the farfield '
                f'{farfield.shape}; snappy would keep the ring between the '
                'farfield and the background block instead of the flow. Move '
                'the seed inside the farfield', label))
            continue
        if where == ON:
            findings.append(Finding(
                FLUID_ON_WALL, ERROR,
                f'region {label} {_fmt(point)} is on the farfield '
                f'{farfield.shape}; snappy cannot tell which side is meant',
                label))
            continue
        if tris is None:
            continue
        inside, certain = inside_bodies(point, tris, is_closed=is_closed)
        if kind == 'solid':
            if not inside:
                findings.append(Finding(
                    SOLID_OUTSIDE_BODIES, WARNING,
                    f'solid region {label} {_fmt(point)} is not inside any '
                    'body; with a farfield the space outside the bodies is '
                    'the fluid', label,
                    'exact' if certain else 'approximate'))
            continue
        if inside and certain:
            findings.append(Finding(
                FLUID_IN_BODY, ERROR,
                f'fluid region {label} {_fmt(point)} is inside a body; with '
                'a farfield the fluid is the space outside the bodies, so '
                'snappy would mesh the body instead. Move the seed into the '
                'flow', label))
        elif inside:
            findings.append(Finding(
                FLUID_MAYBE_IN_BODY, WARNING,
                f'fluid region {label} {_fmt(point)} may be inside a body '
                '(the surface is not closed, so inside and outside are not '
                'certain); check the seed is in the flow', label,
                'approximate'))
    for name, point in _exclude_rows(excludes):
        if classify(primitive, point) != INSIDE:
            findings.append(Finding(
                EXCLUDE_OUTSIDE, WARNING,
                f'exclude point {name} {_fmt(point)} is not inside the '
                'farfield; the space beyond the farfield is discarded '
                'anyway, so it removes nothing more', name))
    return findings


def preflight(farfield: Farfield, *, seeds=(), excludes=(), surfaces=None,
              field=None) -> Report:
    """Every farfield check on the seeds, with the voxel cavity check.

    *field* is the Plan 36 labelled domain (``fluid_spaces.FluidSpaces``),
    labelled against the model surfaces alone. A fluid seed whose space does
    not reach the domain boundary is sealed off from the flow around the
    bodies -- a cavity or an internal passage -- and snappy would mesh only
    that; from voxels, so a warning.
    """
    report = Report(exact_findings(farfield, seeds=seeds, excludes=excludes,
                                   surfaces=surfaces))
    if field is None:
        return report
    from .fluid_regions import APPROXIMATE

    refused = {finding.point for finding in report.findings}
    for label, kind, point in _seed_rows(seeds):
        if kind != 'fluid' or label in refused:
            continue
        found = field.space_at(point)
        if found is None or found.label == 0 or found.outside:
            continue
        report.findings.append(Finding(
            FLUID_IN_CAVITY, WARNING,
            f'fluid region {label} {_fmt(point)} is in a space sealed off from '
            'the flow around the bodies (a cavity or an internal passage); '
            'with a farfield snappy meshes only that space. Move the seed '
            'outside the bodies if the external flow is meant (from the voxel '
            'labelling, which may miss a thin opening)', label, APPROXIMATE))
    return report


# -- the patch name ---------------------------------------------------------- #

def patch_names(shape) -> tuple[str, ...]:
    """Every patch the farfield publishes on snappy: one, on every shape."""
    return (PATCH,)


def naming_note(shape) -> str:
    """How the snappy patch names differ from Gmsh's for *shape*, or ``''``."""
    gmsh = farfield_spec.primitives().patch_names(shape)
    if tuple(gmsh) == patch_names(shape):
        return ''
    return (
        f'On snappy the farfield {shape} is one patch, {PATCH}: OpenFOAM 13 '
        f'gives a searchable {shape} a single region. Gmsh names its faces '
        f'{", ".join(gmsh)}.')


def support_text(db, engine='snappy') -> str:
    """One line for a page: whether this engine builds the farfield, and how."""
    spec = farfield_spec.read(db)
    if not spec.enabled:
        return ''
    answer = farfield_spec.support(spec, engine, farfield_spec.TESSELLATED)
    if not answer.supported:
        return answer.reason
    note = naming_note(spec.shape)
    return ' '.join(part for part in (
        f'Outer boundary: farfield {spec.shape}, patch {PATCH}.',
        SEMANTICS, note) if part)


# -- tessellation ------------------------------------------------------------ #

def tessellate(primitive, resolution: int = 48):
    """A closed triangulated vtkPolyData of the farfield, for drawing and
    for labelling the domain with the farfield as a wall."""
    from vtkmodules.vtkCommonTransforms import vtkTransform
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData, vtkTriangleFilter
    from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter
    from vtkmodules.vtkFiltersSources import (
        vtkCubeSource, vtkCylinderSource, vtkSphereSource,
    )

    shape = primitive['shape']
    transform = vtkTransform()
    transform.PostMultiply()
    if shape == 'box':
        source = vtkCubeSource()
        low = primitive_bounds(primitive)
        source.SetBounds(*low)
    elif shape == 'sphere':
        source = vtkSphereSource()
        source.SetCenter(*(float(value) for value in primitive['centre']))
        source.SetRadius(float(primitive['radius']))
        source.SetThetaResolution(int(resolution))
        source.SetPhiResolution(max(8, int(resolution) // 2))
    else:
        source = vtkCylinderSource()
        source.SetRadius(float(primitive['radius']))
        source.SetHeight(float(primitive['length']))
        source.SetResolution(int(resolution))
        source.CappingOn()
        # vtkCylinderSource stands on +y; turn +y onto the axis, then move
        # its middle onto the centre.
        axis = np.asarray(primitive['axis'], dtype=float)
        axis /= max(np.linalg.norm(axis), 1e-300)
        up = np.array([0.0, 1.0, 0.0])
        cross = np.cross(up, axis)
        sine = float(np.linalg.norm(cross))
        cosine = float(np.dot(up, axis))
        if sine > 1e-12:
            transform.RotateWXYZ(math.degrees(math.atan2(sine, cosine)),
                                 *(cross / sine))
        elif cosine < 0:
            transform.RotateWXYZ(180.0, 1.0, 0.0, 0.0)
        transform.Translate(*(float(value) for value in primitive['centre']))
    moved = vtkTransformPolyDataFilter()
    moved.SetInputConnection(source.GetOutputPort())
    moved.SetTransform(transform)
    tri = vtkTriangleFilter()
    tri.SetInputConnection(moved.GetOutputPort())
    clean = vtkCleanPolyData()
    clean.SetInputConnection(tri.GetOutputPort())
    clean.Update()
    output = clean.GetOutput()
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    result = vtkPolyData()
    result.DeepCopy(output)
    return result
