"""The background mesh's own topology: blocks, vertices, curved edges, patches.

Plan 31 CP-07 items 1-3. Until this module existed the background domain was
six f-strings inside :meth:`CaseBuilder.block_mesh_dict` -- one hard-coded
``hex (0 1 2 3 4 5 6 7)``, ``edges ()``, ``mergePatchPairs ()`` and six patch
names that came from a module constant. There was no model, so there was
nothing to validate and nothing a second block could be added to.

The topology is deliberately **separate from snappy refinement**. A refinement
level says how finely a surface is resolved; a block says what the domain *is*.
They fail differently: a bad refinement level makes a slow mesh, a bad block
makes one ``blockMesh`` refuses (or, worse, one it accepts with inside-out
cells). Keeping them in one place had already produced the second kind of
error silently, because nothing checked the block at all.

Three validations exist because each corresponds to a failure ``blockMesh``
either does not catch or reports in terms a user cannot act on:

* **Positive Jacobians.** ``blockMesh`` will happily build a hex whose vertex
  order is inside out; the damage appears much later as negative volumes in
  ``checkMesh``, attributed to snappy. The corner Jacobian determinant of the
  trilinear map is computed here, at the eight corners, before anything runs.
* **Compatible face subdivisions.** Two blocks sharing a face must agree on
  the cell counts along that face's edges. ``blockMesh`` reports the mismatch
  as an unmatched-face error naming vertex numbers.
* **Every external face owned.** An external face left out of ``boundary``
  lands in ``defaultFaces``, a generated name with a generated type. That is
  exactly the substitution CP-07 item 3 forbids, so it is refused here.

Numbers are passed through unchanged: the writer is byte-stable and its
goldens compare exact text, so a value the project stored as ``1`` must not
come back as ``1.0``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from foammesh.core.quantities import agreeing

#: The six faces of one hexahedral block, as *local* vertex indices, in
#: ``blockMeshDict`` vertex order. Same tuple the single-block writer has
#: always used; it now names block-local corners rather than the only block.
BLOCK_FACES: tuple[tuple[str, tuple[int, int, int, int]], ...] = (
    ('xMin', (0, 3, 7, 4)),
    ('xMax', (1, 5, 6, 2)),
    ('yMin', (0, 4, 5, 1)),
    ('yMax', (3, 2, 6, 7)),
    ('zMin', (0, 1, 2, 3)),
    ('zMax', (4, 7, 6, 5)),
)

#: Which two of the block's three cell counts a face is divided into, so a
#: shared face can be checked for agreement.
FACE_AXES: dict[str, tuple[int, int]] = {
    'xMin': (1, 2), 'xMax': (1, 2),
    'yMin': (0, 2), 'yMax': (0, 2),
    'zMin': (0, 1), 'zMax': (0, 1),
}

#: The twelve edges of a block, grouped by the local axis they run along.
#: Index 0 is the x axis, 1 the y axis, 2 the z axis -- which is also the
#: index of the cell count and grading entry that governs them.
BLOCK_EDGES: tuple[tuple[tuple[int, int], ...], ...] = (
    ((0, 1), (3, 2), (4, 5), (7, 6)),
    ((0, 3), (1, 2), (4, 7), (5, 6)),
    ((0, 4), (1, 5), (2, 6), (3, 7)),
)

#: The local corner coordinates of the trilinear hex map, in vertex order.
CORNERS: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
    (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
)

#: Curved-edge kinds ``blockMesh`` reads, and how many interior points each
#: one requires. ``arc`` takes exactly one; the others take at least one.
EDGE_KINDS: dict[str, str] = {
    'arc': 'exactly one point',
    'spline': 'at least one point',
    'polyLine': 'at least one point',
    'BSpline': 'at least one point',
}


class BackgroundMeshError(ValueError):
    """The authored background topology is one blockMesh would not accept."""


def _as_float(value, what: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise BackgroundMeshError(f'{what} must be a number') from error
    if result != result or result in (float('inf'), float('-inf')):
        raise BackgroundMeshError(f'{what} must be a finite number')
    return result


@dataclass(frozen=True)
class GradingSpec:
    """One edge direction's expansion, either a ratio or a segmented profile.

    ``ratio`` is blockMesh's own convention: the size of the last cell divided
    by the size of the first, along the block's local axis direction. A value
    below one therefore packs cells at the *start* of that axis and above one
    at the end -- which is why :attr:`toward_start` exists: "fine at the wall"
    is the request a user actually has, and expressing it as a reciprocal by
    hand is where the direction gets inverted.
    """

    ratio: float = 1.0
    #: ``(lengthFraction, cellFraction, ratio)`` triples. Empty means a plain
    #: single-segment grading.
    segments: tuple[tuple[float, float, float], ...] = ()
    toward_start: bool = False
    #: The text the project stored, when it stored one. Rendered verbatim so
    #: a byte-stable writer stays byte-stable.
    literal: str | None = None

    def validate(self, what: str) -> tuple[str, ...]:
        problems: list[str] = []
        if self.segments:
            length = sum(item[0] for item in self.segments)
            cells = sum(item[1] for item in self.segments)
            if abs(length - 1.0) > 1e-9:
                problems.append(
                    f'{what}: the segment length fractions add up to '
                    f'{length:g}, not 1')
            if abs(cells - 1.0) > 1e-9:
                problems.append(
                    f'{what}: the segment cell fractions add up to '
                    f'{cells:g}, not 1')
            for index, item in enumerate(self.segments):
                if item[0] <= 0 or item[1] <= 0:
                    problems.append(
                        f'{what}: segment {index + 1} has a length or cell '
                        'fraction that is not positive')
                if item[2] <= 0:
                    problems.append(
                        f'{what}: segment {index + 1} has expansion ratio '
                        f'{item[2]:g}; blockMesh needs a positive ratio')
        elif self.ratio <= 0:
            problems.append(
                f'{what}: expansion ratio {self.ratio:g} is not positive, so '
                'blockMesh cannot grade this direction')
        return tuple(problems)

    def render(self) -> str:
        """One direction of a ``simpleGrading``, as blockMesh reads a direction.

        MEASURED against OpenFOAM 13: a direction is *one* token -- either a
        scalar ratio, or a bracketed list of segment triples. Emitting the
        triples bare turned a three-direction grading into four tokens

            simpleGrading ((0.5 0.5 4) (0.5 0.5 0.25) 3 1)

        which blockMesh reads as a graded direction, another graded
        direction, and then two more it has no axes for. The list has to
        close around the segments of the direction they belong to.
        """
        if self.literal is not None:
            return self.literal
        if self.segments:
            segments = ' '.join(
                f'({_g(a)} {_g(b)} {_g(c)})' for a, b, c in self.segments)
            return f'({segments})'
        ratio = 1.0 / self.ratio if self.toward_start else self.ratio
        return _g(ratio)


def _g(value: float) -> str:
    """A number rendered the way the dictionary writer renders one."""
    text = f'{float(value):g}'
    return text


@dataclass(frozen=True)
class Block:
    """One hexahedron of the background domain."""

    vertices: tuple[int, ...]
    counts: tuple[object, object, object]
    grading: tuple[GradingSpec, GradingSpec, GradingSpec] | tuple[GradingSpec, ...] = ()
    zone: str = ''
    #: Rendered verbatim when the caller already has the exact text -- the
    #: single-block default path does, and its golden compares bytes.
    grading_literal: str | None = None

    def render(self) -> str:
        order = ' '.join(str(item) for item in self.vertices)
        counts = ' '.join(str(item) for item in self.counts)
        if self.grading_literal is not None:
            grading = self.grading_literal
            keyword = 'simpleGrading'
        elif len(self.grading) == 12:
            grading = ' '.join(item.render() for item in self.grading)
            keyword = 'edgeGrading'
        else:
            specs = self.grading or (GradingSpec(), GradingSpec(), GradingSpec())
            grading = ' '.join(item.render() for item in specs)
            keyword = 'simpleGrading'
        zone = f' {self.zone}' if self.zone else ''
        return (f'hex ({order}){zone} ({counts}) '
                f'{keyword} ({grading})')

    def count(self, axis: int) -> int:
        return int(self.counts[axis])


@dataclass(frozen=True)
class CurvedEdge:
    """One ``edges`` entry: the shape the block edge really follows."""

    kind: str
    start: int
    end: int
    points: tuple[tuple[float, float, float], ...]

    def render(self) -> str:
        """The ``edges`` line blockMesh reads, in blockMesh's own two shapes.

        MEASURED against OpenFOAM 13: ``arc`` takes its interpolation point
        bare -- ``arc 3 8 (0.71 0.71 0)`` -- while the multi-point kinds take
        a list -- ``spline 3 8 ((..) (..))``. Wrapping the arc's single point
        in a list as well produced

            wrong token type - expected Scalar, found on line 32 the
            punctuation token '('

        and blockMesh exited, so every curved background mesh this writer
        produced was unbuildable.
        """
        points = ' '.join(
            f'({_g(x)} {_g(y)} {_g(z)})' for x, y, z in self.points)
        if self.kind == 'arc':
            # One point, no list around it: the arc's grammar, not ours.
            return f'{self.kind} {self.start} {self.end} {points}'
        return f'{self.kind} {self.start} {self.end} ({points})'


@dataclass(frozen=True)
class BoundaryFace:
    """One named patch of the background domain, and who named it.

    ``role`` is the engine-neutral category the user meant -- inlet, outlet,
    wall, symmetry -- or the empty string when they never said. ``authored``
    records whether the *name* is theirs. Both travel to the group manifest,
    because a generated name standing in for an intended category is the
    failure CP-07 item 3 names, and the only way to refuse it downstream is
    to know which one this is.
    """

    name: str
    patch_type: str
    faces: tuple[tuple[int, ...], ...]
    group: str = ''
    role: str = ''
    authored: bool = False
    #: The block-local face label this came from, for the manifest.
    origin_label: str = ''

    def render(self) -> str:
        faces = ' '.join(
            '(' + ' '.join(str(item) for item in face) + ')'
            for face in self.faces)
        group = f'inGroups ({self.group}); ' if self.group else ''
        return (f'{self.name} {{ type {self.patch_type}; '
                f'{group}faces ({faces}); }}')


#: Which OpenFOAM patch types can honestly carry which role. A role is what
#: the user *meant*; the type is what the solver reads. ``empty`` and
#: ``symmetry`` do not carry flow, so declaring one an inlet is a claim the
#: mesh cannot keep.
ROLE_TYPES: dict[str, frozenset[str]] = {
    'inlet': frozenset({'patch'}),
    'outlet': frozenset({'patch'}),
    'wall': frozenset({'wall'}),
    'symmetry': frozenset({'symmetry', 'symmetryPlane'}),
}


@dataclass
class BackgroundTopology:
    """The whole background domain, validated before anything writes it."""

    vertices: list[Sequence] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    edges: list[CurvedEdge] = field(default_factory=list)
    boundary: list[BoundaryFace] = field(default_factory=list)
    merge_pairs: list[tuple[str, str]] = field(default_factory=list)
    scale: object = 1

    # -- geometry ---------------------------------------------------------- #

    def _point(self, index: int) -> tuple[float, float, float]:
        raw = self.vertices[index]
        return (_as_float(raw[0], f'vertex {index} x'),
                _as_float(raw[1], f'vertex {index} y'),
                _as_float(raw[2], f'vertex {index} z'))

    def corner_jacobians(self, block: Block) -> tuple[float, ...]:
        """The determinant of the trilinear map at each of the eight corners.

        Every one must be positive. A negative determinant is a corner whose
        three outgoing edges form a left-handed frame -- an inside-out cell,
        which ``blockMesh`` builds without complaint and ``checkMesh`` later
        reports as a negative volume with no hint of where it came from.
        """
        points = [self._point(index) for index in block.vertices]
        results = []
        for corner, (a, b, c) in enumerate(CORNERS):
            axes = []
            for axis, value in enumerate((a, b, c)):
                # The neighbour along this local axis: the corner whose
                # coordinate on that axis is flipped.
                target = list(CORNERS[corner])
                target[axis] = 1 - value
                neighbour = CORNERS.index(tuple(target))
                step = tuple(
                    points[neighbour][k] - points[corner][k] for k in range(3))
                # Walking against the axis direction: the tangent still points
                # the way the axis increases.
                axes.append(step if value == 0 else
                            tuple(-item for item in step))
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = axes
            results.append(
                ax * (by * cz - bz * cy)
                - ay * (bx * cz - bz * cx)
                + az * (bx * cy - by * cx))
        return tuple(results)

    # -- validation -------------------------------------------------------- #

    def problems(self) -> tuple[str, ...]:
        """Everything wrong with this topology, in the order a user meets it."""
        found: list[str] = []
        count = len(self.vertices)
        for number, block in enumerate(self.blocks, start=1):
            if len(block.vertices) != 8:
                found.append(
                    f'block {number} lists {len(block.vertices)} vertices; a '
                    'hex needs exactly eight')
                continue
            if len(set(block.vertices)) != 8:
                found.append(f'block {number} uses the same vertex twice')
                continue
            if any(index < 0 or index >= count for index in block.vertices):
                found.append(
                    f'block {number} refers to a vertex the topology does '
                    f'not define (it has {count})')
                continue
            for axis, label in enumerate('xyz'):
                try:
                    n = block.count(axis)
                except (TypeError, ValueError):
                    found.append(
                        f'block {number} has a non-numeric {label} cell count')
                    continue
                if n < 1:
                    found.append(
                        f'block {number} asks for {n} cells along {label}; '
                        'every direction needs at least one')
            for index, spec in enumerate(block.grading):
                found.extend(spec.validate(f'block {number} grading {index + 1}'))
            jacobians = self.corner_jacobians(block)
            bad = [i for i, value in enumerate(jacobians) if value <= 0]
            if bad:
                found.append(
                    f'block {number} is inside out at '
                    f'{agreeing(len(bad), "corner")} '
                    f'{", ".join(str(i) for i in bad)}: the hex vertex order '
                    'gives a non-positive Jacobian, so blockMesh would build '
                    'cells with negative volume')

        found.extend(self._interface_problems())
        found.extend(self._edge_problems())
        found.extend(self._boundary_problems())
        return tuple(found)

    def _interface_problems(self) -> tuple[str, ...]:
        """Shared edges must agree on how many cells they carry."""
        found: list[str] = []
        seen: dict[frozenset, tuple[int, int]] = {}
        for number, block in enumerate(self.blocks, start=1):
            if len(block.vertices) != 8:
                continue
            for axis, pairs in enumerate(BLOCK_EDGES):
                try:
                    n = block.count(axis)
                except (TypeError, ValueError):
                    continue
                for local_a, local_b in pairs:
                    key = frozenset(
                        (block.vertices[local_a], block.vertices[local_b]))
                    if len(key) != 2:
                        continue
                    previous = seen.get(key)
                    if previous is None:
                        seen[key] = (number, n)
                    elif previous[1] != n:
                        a, b = sorted(key)
                        found.append(
                            f'blocks {previous[0]} and {number} share the edge '
                            f'between vertices {a} and {b} but divide it into '
                            f'{previous[1]} and {n} cells; blockMesh needs '
                            'the subdivisions on a shared face to match')
        return tuple(dict.fromkeys(found))

    def face_owners(self) -> dict[frozenset, list[tuple[int, str]]]:
        """Which blocks own each face, keyed by its set of global vertices."""
        owners: dict[frozenset, list[tuple[int, str]]] = {}
        for number, block in enumerate(self.blocks, start=1):
            if len(block.vertices) != 8:
                continue
            for label, locals_ in BLOCK_FACES:
                key = frozenset(block.vertices[index] for index in locals_)
                owners.setdefault(key, []).append((number, label))
        return owners

    def _edge_problems(self) -> tuple[str, ...]:
        found: list[str] = []
        count = len(self.vertices)
        block_edges = set()
        for block in self.blocks:
            if len(block.vertices) != 8:
                continue
            for pairs in BLOCK_EDGES:
                for local_a, local_b in pairs:
                    block_edges.add(frozenset(
                        (block.vertices[local_a], block.vertices[local_b])))
        for number, edge in enumerate(self.edges, start=1):
            if edge.kind not in EDGE_KINDS:
                found.append(
                    f'curved edge {number} asks for {edge.kind!r}; blockMesh '
                    f'reads {", ".join(sorted(EDGE_KINDS))}')
                continue
            if (edge.start < 0 or edge.end < 0
                    or edge.start >= count or edge.end >= count):
                found.append(
                    f'curved edge {number} names a vertex the topology does '
                    'not define')
                continue
            if frozenset((edge.start, edge.end)) not in block_edges:
                found.append(
                    f'curved edge {number} curves vertices {edge.start} and '
                    f'{edge.end}, which are not the ends of any block edge, '
                    'so blockMesh would ignore it')
            if not edge.points:
                found.append(f'curved edge {number} carries no points')
            elif edge.kind == 'arc' and len(edge.points) != 1:
                found.append(
                    f'curved edge {number} is an arc with {len(edge.points)} '
                    'points; an arc takes exactly one')
        return tuple(found)

    def _boundary_problems(self) -> tuple[str, ...]:
        found: list[str] = []
        owners = self.face_owners()
        for key, holders in owners.items():
            if len(holders) > 2:
                names = ', '.join(str(number) for number, _ in holders)
                found.append(
                    f'blocks {names} all claim the same face; a face belongs '
                    'to at most two blocks')
        external = {key for key, holders in owners.items() if len(holders) == 1}
        assigned: dict[frozenset, str] = {}
        names: set[str] = set()
        for patch in self.boundary:
            if not patch.name:
                found.append('a background patch has no name')
            elif patch.name in names:
                found.append(
                    f'two background patches are both called {patch.name!r}')
            names.add(patch.name)
            if patch.role and patch.role in ROLE_TYPES:
                allowed = ROLE_TYPES[patch.role]
                if patch.patch_type not in allowed:
                    found.append(
                        f'background patch {patch.name!r} is declared '
                        f'{patch.role} but written as an OpenFOAM '
                        f'{patch.patch_type!r} patch; {patch.role} needs '
                        f'{" or ".join(sorted(allowed))}')
            for face in patch.faces:
                key = frozenset(face)
                if key not in owners:
                    found.append(
                        f'background patch {patch.name!r} names a face no '
                        'block has')
                    continue
                if len(owners[key]) == 2:
                    found.append(
                        f'background patch {patch.name!r} claims a face that '
                        'is shared between two blocks, so it is interior')
                    continue
                if key in assigned:
                    found.append(
                        f'background patches {assigned[key]!r} and '
                        f'{patch.name!r} both claim the same face')
                    continue
                assigned[key] = patch.name
        missing = external - set(assigned)
        if missing:
            labels = []
            for key in missing:
                for number, label in owners[key]:
                    labels.append(f'block {number} {label}')
            found.append(
                'these outer faces belong to no named patch, so blockMesh '
                'would put them in a generated defaultFaces patch: '
                + ', '.join(sorted(labels)))
        return tuple(found)

    def validate(self) -> 'BackgroundTopology':
        problems = self.problems()
        if problems:
            raise BackgroundMeshError(
                'the background mesh cannot be built: ' + '; '.join(problems))
        return self

    # -- rendering --------------------------------------------------------- #

    def render(self) -> dict:
        """The ``blockMeshDict`` body for this topology."""
        return {
            'scale': self.scale,
            'vertices': [list(item) for item in self.vertices],
            'blocks': [block.render() for block in self.blocks],
            'edges': [edge.render() for edge in self.edges],
            'boundary': [patch.render() for patch in self.boundary],
            'mergePatchPairs': [f'({a} {b})' for a, b in self.merge_pairs],
        }

    def ownership(self) -> tuple[dict, ...]:
        """What each background patch is, and whether a human named it.

        CP-07 item 3. The report that reads this has to be able to say "this
        is the block's own ``xMin`` face, nobody assigned it a role" rather
        than quietly presenting a generated label as an intended category.
        """
        return tuple(
            {
                'name': patch.name,
                'type': patch.patch_type,
                'group': patch.group,
                'role': patch.role or 'unassigned',
                'naming': 'authored' if patch.authored else 'generated',
                'origin': 'background',
                'block_face': patch.origin_label,
            }
            for patch in self.boundary
        )


# --------------------------------------------------------------------------- #
# Authoring
# --------------------------------------------------------------------------- #

def parse_grading(text, *, toward_start: bool = False) -> GradingSpec:
    """One grading direction, from what a project stored.

    Accepts a plain ratio (``4``), or a segmented profile written the way
    ``blockMeshDict`` writes one -- ``(0.2 0.3 4) (0.6 0.4 1) (0.2 0.3 0.25)``
    -- so the richer form does not need a second field to live in.
    """
    raw = '' if text is None else str(text).strip()
    if not raw:
        return GradingSpec(1.0, toward_start=toward_start)
    if '(' not in raw:
        return GradingSpec(_as_float(raw, 'grading ratio'),
                           toward_start=toward_start)
    segments: list[tuple[float, float, float]] = []
    depth = 0
    current: list[str] = []
    for character in raw:
        if character == '(':
            depth += 1
            if depth == 1:
                current = []
                continue
        if character == ')':
            depth -= 1
            if depth == 0:
                values = ''.join(current).replace(',', ' ').split()
                if len(values) != 3:
                    raise BackgroundMeshError(
                        'each grading segment needs three values: length '
                        'fraction, cell fraction and expansion ratio')
                segments.append(tuple(  # type: ignore[arg-type]
                    _as_float(item, 'grading segment value') for item in values))
                continue
            if depth < 0:
                raise BackgroundMeshError('unbalanced brackets in the grading')
        if depth >= 1:
            current.append(character)
    if depth != 0:
        raise BackgroundMeshError('unbalanced brackets in the grading')
    if not segments:
        raise BackgroundMeshError('the grading names no segments')
    return GradingSpec(1.0, tuple(segments), toward_start=toward_start)


def parse_points(text) -> tuple[tuple[float, float, float], ...]:
    """``(0 1 2) (3 4 5)`` or a bare ``0 1 2`` -- a curved edge's points."""
    raw = '' if text is None else str(text).strip()
    if not raw:
        return ()
    if '(' not in raw:
        values = raw.replace(',', ' ').split()
        if len(values) % 3:
            raise BackgroundMeshError(
                'a curved edge point needs three coordinates')
        return tuple(
            tuple(_as_float(item, 'edge point coordinate')      # type: ignore
                  for item in values[index:index + 3])
            for index in range(0, len(values), 3))
    points: list[tuple[float, float, float]] = []
    for chunk in raw.split(')'):
        chunk = chunk.strip().lstrip('(').strip()
        if not chunk:
            continue
        values = chunk.replace(',', ' ').split()
        if len(values) != 3:
            raise BackgroundMeshError(
                'a curved edge point needs three coordinates')
        points.append(tuple(  # type: ignore[arg-type]
            _as_float(item, 'edge point coordinate') for item in values))
    return tuple(points)


def parse_indices(text, *, expected: int | None = None) -> tuple[int, ...]:
    values = str(text or '').replace(',', ' ').replace('(', ' ').replace(
        ')', ' ').split()
    try:
        indices = tuple(int(item) for item in values)
    except ValueError as error:
        raise BackgroundMeshError(
            'vertex indices must be whole numbers') from error
    if expected is not None and len(indices) != expected:
        raise BackgroundMeshError(
            f'expected {expected} vertex indices, found {len(indices)}')
    return indices


def single_block(bbox_vertices: Sequence[Sequence], counts: Sequence,
                 grading_literal: str, patches: Iterable[BoundaryFace],
                 scale) -> BackgroundTopology:
    """The derived box the product has always written, as a topology.

    Kept as its own constructor so the default path renders the exact bytes
    it did before this module existed: same vertex objects, same count
    objects, same grading text.
    """
    return BackgroundTopology(
        vertices=list(bbox_vertices),
        blocks=[Block(tuple(range(8)), tuple(counts),
                      grading_literal=grading_literal)],
        boundary=list(patches),
        scale=scale)


def _records(collection) -> list[dict]:
    """Collection items as plain dicts, in the order a user added them.

    The project's collections are ``IntKeyList``s whose items answer
    ``value(name)``; tests and callers that already have dicts pass those.
    Sorting by numeric key keeps the written dictionary stable across runs.
    """
    if not collection:
        return []
    items = []
    for key, value in collection.items():
        try:
            order = (0, int(key))
        except (TypeError, ValueError):
            order = (1, 0)
        items.append((order, str(key), value))
    items.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in items]


def _read(item, name, default=None):
    if isinstance(item, Mapping):
        value = item.get(name, default)
    else:
        try:
            value = item.value(name)
        except Exception:
            return default
    if value is None:
        return default
    return getattr(value, 'value', value)


def from_records(base_grid, *, scale=1) -> 'BackgroundTopology | None':
    """Build an authored multiblock topology, or ``None`` if none was authored.

    *base_grid* answers ``collection(name)`` for each of the authoring
    collections. Returning ``None`` rather than an empty topology is
    deliberate: a project that never touches these fields must keep getting
    the derived single box, byte for byte.
    """
    blocks_raw = _records(base_grid('blocks'))
    if not blocks_raw:
        return None
    vertices = [
        [_read(item, 'x', 0), _read(item, 'y', 0), _read(item, 'z', 0)]
        for item in _records(base_grid('vertices'))]
    if not vertices:
        raise BackgroundMeshError(
            'the background mesh names blocks but no vertices')

    blocks = []
    for item in blocks_raw:
        indices = parse_indices(_read(item, 'vertices'), expected=8)
        counts = (_read(item, 'numCellsX', 1), _read(item, 'numCellsY', 1),
                  _read(item, 'numCellsZ', 1))
        grading = tuple(
            parse_grading(
                _read(item, f'grading{axis}'),
                toward_start=bool(_read(item, f'grading{axis}TowardStart',
                                        False)))
            for axis in 'XYZ')
        blocks.append(Block(indices, counts, grading,
                            zone=str(_read(item, 'zone', '') or '').strip()))

    edges = []
    for item in _records(base_grid('edges')):
        kind = str(_read(item, 'kind', 'arc') or 'arc')
        edges.append(CurvedEdge(
            kind, int(_read(item, 'start', 0)), int(_read(item, 'end', 0)),
            parse_points(_read(item, 'points'))))

    boundary = []
    for item in _records(base_grid('patches')):
        faces = tuple(
            tuple(parse_indices(chunk, expected=4))
            for chunk in _face_chunks(_read(item, 'faces')))
        role = str(_read(item, 'category', '') or '')
        name = str(_read(item, 'name', '') or '').strip()
        boundary.append(BoundaryFace(
            name=name,
            patch_type=str(_read(item, 'type', 'patch') or 'patch'),
            faces=faces,
            group=str(_read(item, 'group', '') or '').strip(),
            role='' if role in ('', 'unclassified', 'unassigned') else role,
            authored=bool(name)))

    merge = []
    for item in _records(base_grid('mergePairs')):
        first = str(_read(item, 'master', '') or '').strip()
        second = str(_read(item, 'slave', '') or '').strip()
        if first and second:
            merge.append((first, second))

    return BackgroundTopology(vertices, blocks, edges, boundary, merge,
                              scale=scale)


def _face_chunks(text) -> list[str]:
    raw = str(text or '').strip()
    if not raw:
        return []
    if '(' not in raw:
        return [raw]
    return [chunk for chunk in
            (part.strip().lstrip('(').strip() for part in raw.split(')'))
            if chunk]

