#!/usr/bin/env python3
"""Closed-shell topology for a tessellated import (Plan 30 WP-06a, F-38).

The runner used to hand every classified surface of a tessellated import to
one ``addVolume`` call: the largest shell became the outer boundary and every
other shell became a void inside it. Three consequences were measured:

* two separate closed bodies produced one volume with the second cut out of
  the first, and the mesher ended in ``no volume elements``;
* one file holding several shells was never split at all, because the grouping
  ran only when more than one file was given, so a two-cube STL was a single
  surface loop of both cubes;
* a body sitting outside the largest shell became a hole in nothing.

Everything here is deliberately free of Gmsh. It takes triangles and node
coordinates, and it answers three questions with plain geometry, so the
classification can be tested without a Gmsh runtime and behaves the same
whichever way the geometry arrived:

* **connectivity** -- which surfaces form one shell (they share a triangle
  edge), which is a property of the triangulation and not of the file layout,
  so a one-file and a two-file form of the same geometry classify alike;
* **closure** -- a shell is closed when every edge is used by exactly two
  triangles. An open shell cannot bound a volume and is refused by name;
* **containment** -- bounding box first, then a ray cast from a point on the
  inner shell through the outer one. A parity that is not unanimous across
  independent ray directions is *undecided*, and undecided is refused rather
  than guessed.

Roles decide what each shell becomes. The defaults alternate with nesting
depth, which is the answer for the ordinary cases: an outermost shell is a
meshed volume, a shell directly inside it is a void in that volume, a shell
inside the void is a volume again. ``fluid`` and ``solid`` both mesh; the
difference is only that ``solid`` names a body the user declared rather than
one the nesting implied. Every direct child of a shell is a hole in that
shell's volume whatever its own role, so two volumes never claim the same
region.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CALCULATION_VERSION = 'gmsh.shellTopology.v1'

#: A shell that meshes as the domain.
FLUID = 'fluid'
#: A shell that meshes as a body in its own right, and is still a hole in
#: whatever encloses it.
SOLID = 'solid'
#: A shell that is a hole and nothing else.
VOID = 'void'

ROLES = (FLUID, SOLID, VOID)

#: Directions for the containment ray cast. Deliberately not axis-aligned and
#: mutually far apart: an axis-aligned ray is coplanar with half the triangles
#: of an axis-aligned box, which is exactly the case these fixtures are.
RAY_DIRECTIONS = (
    (0.5773502691896258, 0.5773502691896258, 0.5773502691896258),
    (-0.4629100498862757, 0.8017837257372732, 0.37796447300922725),
    (0.7071067811865476, -0.3162277660168379, 0.6324555320336759),
    (-0.2672612419124244, -0.5345224838248488, 0.8017837257372732),
    (0.8944271909999159, 0.4472135954999579, -0.0),
    (0.24253562503633297, -0.9701425001453319, -0.0),
    (-0.6666666666666666, 0.3333333333333333, -0.6666666666666666),
)

#: How many independent ray directions must agree before containment is a fact.
REQUIRED_VOTES = 3


class ShellTopologyError(RuntimeError):
    """The shells cannot be resolved into volumes, and the reason names one."""


# --------------------------------------------------------------------------- #
# Small vector helpers
# --------------------------------------------------------------------------- #

def _subtract(left, right):
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _cross(left, right):
    return (left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0])


def _dot(left, right):
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


# --------------------------------------------------------------------------- #
# Triangulation facts
# --------------------------------------------------------------------------- #

def free_edges(triangles):
    """Edges used by other than exactly two triangles.

    An empty result is the definition of a closed shell used throughout this
    module: every edge shared by two faces, no boundary and no fin.
    """
    counts: dict = {}
    for triangle in triangles:
        for index in range(3):
            first, second = triangle[index], triangle[(index + 1) % 3]
            key = (first, second) if first <= second else (second, first)
            counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(edge for edge, used in counts.items() if used != 2))


def bounding_box(triangles, coordinates):
    low = [float('inf')] * 3
    high = [float('-inf')] * 3
    for triangle in triangles:
        for node in triangle:
            point = coordinates[node]
            for axis in range(3):
                low[axis] = min(low[axis], point[axis])
                high[axis] = max(high[axis], point[axis])
    return tuple(low) + tuple(high)


def signed_volume(triangles, coordinates):
    """Enclosed volume by the divergence theorem; negative means inward normals.

    The magnitude orders the nesting tree and the sign records the orientation,
    which is worth saying out loud in the manifest even though Gmsh reorients a
    discrete surface itself when it bounds a volume.
    """
    total = 0.0
    for first, second, third in triangles:
        one, two, three = (coordinates[first], coordinates[second],
                           coordinates[third])
        total += _dot(one, _cross(two, three))
    return total / 6.0


def group_by_connectivity(surfaces, triangles):
    """Partition surfaces into shells: two surfaces sharing an edge are one.

    Connectivity is read off the welded triangulation rather than off the
    classified curves, so the answer does not depend on how many files the
    triangles arrived in -- which is the whole point of the fix.
    """
    parent = {tag: tag for tag in surfaces}

    def find(tag):
        while parent[tag] != tag:
            parent[tag] = parent[parent[tag]]
            tag = parent[tag]
        return tag

    owners: dict = {}
    for tag in surfaces:
        for triangle in triangles.get(tag, ()):
            for index in range(3):
                first, second = triangle[index], triangle[(index + 1) % 3]
                key = (first, second) if first <= second else (second, first)
                owners.setdefault(key, set()).add(tag)
    for shared in owners.values():
        ordered = sorted(shared)
        for other in ordered[1:]:
            parent[find(other)] = find(ordered[0])

    groups: dict = {}
    for tag in surfaces:
        groups.setdefault(find(tag), []).append(tag)
    return [tuple(sorted(members)) for members in groups.values()]


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #

def _cast(point, direction, triangles, coordinates, length_scale):
    """Parity of crossings along one ray, or ``None`` when the ray is unsafe.

    A ray that grazes an edge or a vertex counts a crossing once or twice
    depending on floating-point luck, so such a ray is discarded rather than
    trusted. The caller tries another direction.
    """
    edge_tolerance = 1e-9
    distance_tolerance = 1e-9 * length_scale
    crossings = 0
    for first, second, third in triangles:
        origin_vertex = coordinates[first]
        edge_one = _subtract(coordinates[second], origin_vertex)
        edge_two = _subtract(coordinates[third], origin_vertex)
        helper = _cross(direction, edge_two)
        determinant = _dot(edge_one, helper)
        if -1e-14 < determinant < 1e-14:
            # The ray is parallel to this triangle's plane. With a direction
            # that is not axis-aligned this is a miss, not a crossing.
            continue
        inverse = 1.0 / determinant
        offset = _subtract(point, origin_vertex)
        u = _dot(offset, helper) * inverse
        if u < -edge_tolerance or u > 1.0 + edge_tolerance:
            continue
        second_helper = _cross(offset, edge_one)
        v = _dot(direction, second_helper) * inverse
        if v < -edge_tolerance or u + v > 1.0 + edge_tolerance:
            continue
        distance = _dot(edge_two, second_helper) * inverse
        if distance < -distance_tolerance:
            continue
        on_edge = (abs(u) < edge_tolerance or abs(v) < edge_tolerance
                   or abs(u + v - 1.0) < edge_tolerance)
        if on_edge or abs(distance) < distance_tolerance:
            return None
        crossings += 1
    return bool(crossings % 2)


def point_in_shell(point, triangles, coordinates, box=None, length_scale=1.0):
    """``True`` inside, ``False`` outside, ``None`` when it cannot be decided.

    ``None`` is a real answer: it is what the caller turns into a refusal, so
    a geometry the classifier cannot read never silently meshes as something
    else.
    """
    if box is None:
        box = bounding_box(triangles, coordinates)
    margin = 1e-9 * length_scale
    for axis in range(3):
        if point[axis] < box[axis] - margin or point[axis] > box[axis + 3] + margin:
            return False
    votes = []
    for direction in RAY_DIRECTIONS:
        parity = _cast(point, direction, triangles, coordinates, length_scale)
        if parity is None:
            continue
        votes.append(parity)
        if len(votes) >= REQUIRED_VOTES:
            break
    if len(votes) < REQUIRED_VOTES:
        return None
    if all(votes):
        return True
    if not any(votes):
        return False
    return None


def _box_within(inner, outer, margin):
    for axis in range(3):
        if inner[axis] < outer[axis] - margin:
            return False
        if inner[axis + 3] > outer[axis + 3] + margin:
            return False
    return True


# --------------------------------------------------------------------------- #
# Shells and the volumes they make
# --------------------------------------------------------------------------- #

@dataclass
class Shell:
    """One closed (or refused) shell of a tessellated import."""

    name: str
    source: str
    surfaces: tuple
    closed: bool
    free_edge_count: int
    box: tuple
    volume: float
    triangle_count: int
    reference_point: tuple
    parent: str = ''
    depth: int = 0
    role: str = FLUID
    role_source: str = 'nesting'
    volume_tag: int = 0

    @property
    def orientation(self) -> str:
        return 'outward' if self.volume >= 0.0 else 'inward'

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'source': self.source,
            'closed': self.closed,
            'parent': self.parent or None,
            'role': self.role,
            'roleSource': self.role_source,
            'volume': self.volume_tag or None,
            'depth': self.depth,
            'surfaces': list(self.surfaces),
            'orientation': self.orientation,
            'enclosedVolume': self.volume,
            'triangles': self.triangle_count,
            'boundingBox': list(self.box),
        }


@dataclass
class VolumePlan:
    """One volume to build: an outer shell and the shells that hole it."""

    name: str
    role: str
    outer: tuple
    voids: tuple = ()
    void_names: tuple = ()


@dataclass
class Topology:
    shells: tuple = ()
    volumes: tuple = ()
    domain: str = ''
    warnings: tuple = ()
    calculation_version: str = field(default=CALCULATION_VERSION)

    def shell(self, name):
        for item in self.shells:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_list(self) -> list:
        return [item.to_dict() for item in self.shells]


def _shell_names(groups, sources):
    """Stable names: the source stem, numbered when one file holds several."""
    labels = []
    for surfaces in groups:
        owned = sorted({str(sources.get(tag) or '') for tag in surfaces})
        stems = []
        for item in owned:
            if not item:
                continue
            stem = item.replace('\\', '/').rsplit('/', 1)[-1]
            stems.append(stem.rsplit('.', 1)[0] if '.' in stem else stem)
        labels.append(('+'.join(stems) or 'shell',
                       '+'.join(item for item in owned if item)))
    counts: dict = {}
    for label, _source in labels:
        counts[label] = counts.get(label, 0) + 1
    seen: dict = {}
    names = []
    for label, _source in labels:
        if counts[label] == 1:
            names.append(label)
            continue
        seen[label] = seen.get(label, 0) + 1
        names.append(f'{label}#{seen[label]}')
    return names, [source for _label, source in labels]


def build_shells(surfaces, triangles, coordinates, sources=None):
    """Split the triangulation into shells and measure each one."""
    sources = dict(sources or {})
    surfaces = [tag for tag in surfaces]
    groups = group_by_connectivity(surfaces, triangles)

    measured = []
    for members in groups:
        owned = [triangle for tag in members for triangle in triangles.get(tag, ())]
        if not owned:
            continue
        box = bounding_box(owned, coordinates)
        span = max(box[axis + 3] - box[axis] for axis in range(3)) or 1.0
        loose = free_edges(owned)
        first = owned[0]
        centre = tuple(
            sum(coordinates[node][axis] for node in first) / 3.0
            for axis in range(3))
        measured.append({
            'surfaces': members, 'triangles': owned, 'box': box, 'span': span,
            'free': loose, 'volume': signed_volume(owned, coordinates),
            'reference': centre,
        })

    # Largest first, then by corner, so the numbering does not depend on the
    # order Gmsh happened to hand back its surfaces.
    measured.sort(key=lambda item: (-abs(item['volume']), item['box']))
    names, labels = _shell_names([item['surfaces'] for item in measured],
                                 sources)
    shells = []
    for index, item in enumerate(measured):
        shells.append(Shell(
            name=names[index], source=labels[index],
            surfaces=item['surfaces'], closed=not item['free'],
            free_edge_count=len(item['free']), box=item['box'],
            volume=item['volume'], triangle_count=len(item['triangles']),
            reference_point=item['reference']))
    return shells, {name: item['triangles']
                    for name, item in zip(names, measured)}


def nest_shells(shells, owned_triangles, coordinates):
    """Set ``parent`` and ``depth`` from containment, refusing what it cannot read."""
    for shell in shells:
        if not shell.closed:
            raise ShellTopologyError(
                f'shell {shell.name!r} from {shell.source or "the import"} is '
                f'not closed: {shell.free_edge_count} edge(s) belong to one '
                'triangle instead of two, so it cannot bound a volume. Repair '
                'the surface, or remove it from the geometry.')

    containers: dict = {shell.name: [] for shell in shells}
    for inner in shells:
        for outer in shells:
            if inner is outer:
                continue
            scale = max(outer.box[axis + 3] - outer.box[axis]
                        for axis in range(3)) or 1.0
            if not _box_within(inner.box, outer.box, 1e-9 * scale):
                continue
            verdict = point_in_shell(
                inner.reference_point, owned_triangles[outer.name],
                coordinates, box=outer.box, length_scale=scale)
            if verdict is None:
                raise ShellTopologyError(
                    f'cannot decide whether shell {inner.name!r} from '
                    f'{inner.source or "the import"} lies inside shell '
                    f'{outer.name!r} from {outer.source or "the import"}: the '
                    'two surfaces touch or intersect. Separate them, or merge '
                    'them into one shell.')
            if verdict:
                containers[inner.name].append(outer)

    by_name = {shell.name: shell for shell in shells}
    for shell in shells:
        enclosing = containers[shell.name]
        shell.depth = len(enclosing)
        if enclosing:
            innermost = min(enclosing, key=lambda item: abs(item.volume))
            shell.parent = innermost.name
        else:
            shell.parent = ''
    # A parent must itself be one level shallower; anything else means the
    # shells interleave rather than nest, and guessing a tree from that is how
    # a body becomes a hole in nothing.
    for shell in shells:
        if shell.parent and by_name[shell.parent].depth != shell.depth - 1:
            raise ShellTopologyError(
                f'shell {shell.name!r} from {shell.source or "the import"} '
                'does not nest: it is enclosed by shells that do not enclose '
                'one another. Separate the bodies, or supply them as one '
                'closed shell each.')
    return shells


def apply_roles(shells, roles=None, seed=None, owned_triangles=None,
                coordinates=None):
    """Give every shell a role, and say where the role came from.

    Without any job input the roles alternate with depth, which reproduces the
    behaviour a single closed shell always had. A seed names the domain, and a
    declared role overrules both.
    """
    warnings = []
    for shell in shells:
        shell.role = FLUID if shell.depth % 2 == 0 else VOID
        shell.role_source = 'nesting'

    domain = ''
    if seed is not None:
        candidates = []
        for shell in shells:
            scale = max(shell.box[axis + 3] - shell.box[axis]
                        for axis in range(3)) or 1.0
            verdict = point_in_shell(
                tuple(float(value) for value in seed),
                owned_triangles[shell.name], coordinates, box=shell.box,
                length_scale=scale)
            if verdict is None:
                raise ShellTopologyError(
                    f'cannot decide whether the seed point {tuple(seed)!r} '
                    f'lies inside shell {shell.name!r} from '
                    f'{shell.source or "the import"}: the point sits on the '
                    'surface. Move the seed into open space.')
            if verdict:
                candidates.append(shell)
        if not candidates:
            raise ShellTopologyError(
                f'the seed point {tuple(seed)!r} is outside every shell of '
                'the geometry, so it names no volume to mesh.')
        innermost = min(candidates, key=lambda item: abs(item.volume))
        domain = innermost.name
        if innermost.role != FLUID:
            innermost.role = FLUID
        innermost.role_source = 'seed'

    for name, role in dict(roles or {}).items():
        role = str(role).strip().lower()
        if role not in ROLES:
            raise ShellTopologyError(
                f'shell role {role!r} for {name!r} is not one of '
                + ', '.join(ROLES))
        for shell in shells:
            if shell.name == name:
                shell.role = role
                shell.role_source = 'declared'
                break
        else:
            warnings.append(
                f'shellRoles names {name!r}, which is not a shell of this '
                'geometry; the shells are '
                + ', '.join(repr(item.name) for item in shells))
    return tuple(warnings), domain


def plan_volumes(shells):
    """One volume per shell that is not purely a void, holed by its children."""
    children: dict = {shell.name: [] for shell in shells}
    for shell in shells:
        if shell.parent:
            children[shell.parent].append(shell)
    plans = []
    for shell in shells:
        if shell.role == VOID:
            continue
        inner = sorted(children[shell.name], key=lambda item: item.name)
        plans.append(VolumePlan(
            name=shell.name, role=shell.role, outer=tuple(shell.surfaces),
            voids=tuple(tuple(item.surfaces) for item in inner),
            void_names=tuple(item.name for item in inner)))
    if not plans:
        raise ShellTopologyError(
            'every shell of the geometry is declared a void, so there is '
            'nothing to mesh')
    return plans


def resolve_topology(surfaces, triangles, coordinates, sources=None,
                     roles=None, seed=None) -> Topology:
    """Shells, their nesting, their roles, and the volumes to build.

    ``triangles`` maps a surface tag to its oriented node triples and
    ``coordinates`` maps a node to its position; both are exactly what a
    discrete Gmsh model holds after ``classifySurfaces``/``createGeometry``,
    and both are equally easy to read straight out of an STL, which is how the
    classification is tested without a Gmsh runtime.
    """
    shells, owned = build_shells(surfaces, triangles, coordinates, sources)
    if not shells:
        raise ShellTopologyError(
            'the tessellated import produced no triangles to classify')
    nest_shells(shells, owned, coordinates)
    warnings, domain = apply_roles(shells, roles, seed, owned, coordinates)
    plans = plan_volumes(shells)
    return Topology(shells=tuple(shells), volumes=tuple(plans), domain=domain,
                    warnings=warnings)
