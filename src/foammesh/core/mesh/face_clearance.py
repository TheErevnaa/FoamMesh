"""How far a seed is from the nearest face of the mesh snappy will cut.

Plan 36 RP13 #2. ``locationInMesh`` (and every ``insidePoints`` entry) has to
be *inside* a cell: snappyHexMesh finds the cell that contains it, and a point
exactly on a face is claimed by two cells or by none, which stops the mesher
with "Point ... is not inside the mesh or on a face or edge". The seed gizmo
snapped to whole base-cell steps from the box corner -- which, for the derived
uniform block, are exactly the blockMesh faces.

The faces a seed can land on are:

* the base grid's own faces, along each block axis, where blockMesh puts them
  for that block's cell count and grading (``simpleGrading`` ratios and
  segmented profiles, and ``toward_start``);
* every face castellation adds, at any refinement level up to the case's
  highest surface or region level: a level-``L`` cell splits its parent in
  half along each axis, so its faces sit at ``m / 2**L`` of a base cell.

`seed_face_clearance` answers the distance to the nearest of those, in model
units and as a fraction of the finest cell there (the base cell divided by
``2**max_level``). A point a third of a finest cell from every face is as far
as it can be at every level at once -- ``1/3`` is never a dyadic fraction --
which is why the snap goes to ``origin + (k + 1/3) * step`` and a nudge moves a
third of a finest cell.

Everything here is in block-vertex units, the frame the geometry is drawn in
and the frame `domain_box.DomainBox.bounds` uses; ``blockMesh`` scales both
the same way, so the fractions do not change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

#: A seed nearer a face than this fraction of the finest cell is moved off it.
MIN_CLEARANCE = 1e-3

#: The fraction of a step a snapped seed sits at: never a dyadic fraction, so
#: never on a face at any refinement level.
SNAP_FRACTION = 1.0 / 3.0

#: The parametric slack a point on a block's boundary is still "in" it.
_INSIDE_TOLERANCE = 1e-9


# -- where blockMesh puts the faces along one block axis -------------------- #

def _geometric(count: int, ratio: float) -> list[float]:
    """Face fractions ``0..1`` of *count* cells, last/first size = *ratio*."""
    count = max(1, int(count))
    if count == 1:
        return [0.0, 1.0]
    if ratio <= 0 or not math.isfinite(ratio):
        ratio = 1.0
    per_cell = ratio ** (1.0 / (count - 1))
    if abs(per_cell - 1.0) < 1e-12:
        return [index / count for index in range(count + 1)]
    total = (per_cell ** count - 1.0) / (per_cell - 1.0)
    faces = [0.0]
    size = 1.0 / total
    for _index in range(count):
        faces.append(faces[-1] + size)
        size *= per_cell
    faces[-1] = 1.0
    return faces


def axis_faces(count: int, grading=None) -> tuple[float, ...]:
    """The face fractions blockMesh uses along one axis of one block.

    *grading* is a `background_mesh.GradingSpec`, a plain ratio, or ``None``
    for uniform. Segment cell counts follow blockMesh's rounding: each
    segment takes ``round(cellFraction * count)`` and the last the remainder.
    """
    count = max(1, int(count))
    if grading is None:
        return tuple(_geometric(count, 1.0))
    if isinstance(grading, (int, float)):
        return tuple(_geometric(count, float(grading)))
    segments = tuple(getattr(grading, 'segments', ()) or ())
    toward_start = bool(getattr(grading, 'toward_start', False))
    if not segments:
        ratio = float(getattr(grading, 'ratio', 1.0) or 1.0)
        if toward_start and ratio > 0:
            ratio = 1.0 / ratio
        return tuple(_geometric(count, ratio))
    length_total = sum(float(item[0]) for item in segments) or 1.0
    faces = [0.0]
    used = 0
    start = 0.0
    for index, (length, cells, ratio) in enumerate(segments):
        if index == len(segments) - 1:
            divisions = count - used
        else:
            divisions = int(float(cells) * count + 0.5)
            divisions = max(0, min(divisions, count - used))
        used += divisions
        span = float(length) / length_total
        if divisions > 0:
            for fraction in _geometric(divisions, float(ratio))[1:]:
                faces.append(start + span * fraction)
        start += span
    faces[-1] = 1.0
    return tuple(sorted(set(faces)))


def _literal_gradings(literal) -> list:
    """The three directions of a ``simpleGrading`` text such as ``2 1 (..)``."""
    from foammesh.openfoam.background_mesh import parse_grading

    text = str(literal or '').strip()
    if text.startswith('(') and text.endswith(')') and text.count('(') == 1:
        text = text[1:-1]
    tokens: list[str] = []
    depth = 0
    current = ''
    for character in text:
        if character == '(':
            depth += 1
        if character == ')':
            depth -= 1
        if character.isspace() and depth == 0:
            if current:
                tokens.append(current)
                current = ''
            continue
        current += character
    if current:
        tokens.append(current)
    if len(tokens) != 3:
        return [None, None, None]
    gradings = []
    for token in tokens:
        if token.startswith('(') and token.endswith(')') and token.count('(') > 1:
            token = token[1:-1]
        try:
            gradings.append(parse_grading(token))
        except Exception:  # noqa: BLE001 - an unreadable direction is uniform
            gradings.append(None)
    return gradings


# -- the grid --------------------------------------------------------------- #

@dataclass(frozen=True)
class GridBlock:
    """One background block: its corners and each axis' face fractions."""

    corners: tuple[tuple[float, float, float], ...]
    faces: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]

    @property
    def bounds(self) -> tuple[float, ...]:
        return tuple(bound for axis in range(3) for bound in (
            min(corner[axis] for corner in self.corners),
            max(corner[axis] for corner in self.corners)))

    def is_axis_box(self) -> bool:
        """True when the block is an axis-aligned box in blockMesh's order."""
        from foammesh.openfoam.background_mesh import CORNERS

        box = self.bounds
        tolerance = 1e-12 * max(1.0, max(abs(value) for value in box))
        for corner, local in zip(self.corners, CORNERS):
            for axis in range(3):
                expected = box[2 * axis + local[axis]]
                if abs(corner[axis] - expected) > tolerance:
                    return False
        return True

    def point(self, local) -> tuple[float, float, float]:
        """The trilinear map from ``(u, v, w)`` in ``[0, 1]`` to space."""
        from foammesh.openfoam.background_mesh import CORNERS

        u, v, w = local
        result = [0.0, 0.0, 0.0]
        for corner, (a, b, c) in zip(self.corners, CORNERS):
            weight = ((u if a else 1 - u) * (v if b else 1 - v)
                      * (w if c else 1 - w))
            for axis in range(3):
                result[axis] += weight * corner[axis]
        return tuple(result)

    def _jacobian(self, local) -> list[list[float]]:
        from foammesh.openfoam.background_mesh import CORNERS

        u, v, w = local
        columns = [[0.0, 0.0, 0.0] for _ in range(3)]
        for corner, (a, b, c) in zip(self.corners, CORNERS):
            fu = (u if a else 1 - u)
            fv = (v if b else 1 - v)
            fw = (w if c else 1 - w)
            du = (1 if a else -1) * fv * fw
            dv = fu * (1 if b else -1) * fw
            dw = fu * fv * (1 if c else -1)
            for axis in range(3):
                columns[0][axis] += du * corner[axis]
                columns[1][axis] += dv * corner[axis]
                columns[2][axis] += dw * corner[axis]
        return columns

    def local(self, point) -> tuple[float, float, float] | None:
        """``(u, v, w)`` of *point*, by the inverse trilinear map."""
        if self.is_axis_box():
            box = self.bounds
            result = []
            for axis in range(3):
                span = box[2 * axis + 1] - box[2 * axis]
                if span <= 0:
                    return None
                result.append((point[axis] - box[2 * axis]) / span)
            return tuple(result)
        local = [0.5, 0.5, 0.5]
        for _iteration in range(40):
            mapped = self.point(local)
            residual = [point[axis] - mapped[axis] for axis in range(3)]
            columns = self._jacobian(local)
            step = _solve3(columns, residual)
            if step is None:
                return None
            local = [local[axis] + step[axis] for axis in range(3)]
            if max(abs(item) for item in step) < 1e-13:
                break
        return tuple(local)

    def edge_length(self, axis: int, local) -> float:
        """The block's length along *axis* at *local* (the Jacobian column)."""
        column = self._jacobian(local)[axis]
        return math.sqrt(sum(item * item for item in column))


def _solve3(columns, rhs) -> list[float] | None:
    """Solve ``[c0 c1 c2] x = rhs`` by Cramer's rule."""
    def det(a, b, c):
        return (a[0] * (b[1] * c[2] - b[2] * c[1])
                - b[0] * (a[1] * c[2] - a[2] * c[1])
                + c[0] * (a[1] * b[2] - a[2] * b[1]))

    c0, c1, c2 = columns
    determinant = det(c0, c1, c2)
    if abs(determinant) < 1e-300:
        return None
    return [det(rhs, c1, c2) / determinant,
            det(c0, rhs, c2) / determinant,
            det(c0, c1, rhs) / determinant]


@dataclass(frozen=True)
class BackgroundGrid:
    """Every background block, and the origin a snap is measured from."""

    blocks: tuple[GridBlock, ...]

    @property
    def origin(self) -> tuple[float, float, float]:
        """blockMesh's origin: the lowest corner of every block together."""
        return tuple(min(block.bounds[2 * axis] for block in self.blocks)
                     for axis in range(3))

    @property
    def bounds(self) -> tuple[float, ...]:
        return tuple(bound for axis in range(3) for bound in (
            min(block.bounds[2 * axis] for block in self.blocks),
            max(block.bounds[2 * axis + 1] for block in self.blocks)))

    def locate(self, point) -> tuple[int, tuple[float, float, float]] | None:
        """The first block holding *point* and the point's ``(u, v, w)``."""
        for index, block in enumerate(self.blocks):
            box = block.bounds
            slack = 1e-9 * max(1.0, max(abs(value) for value in box))
            if any(point[axis] < box[2 * axis] - slack
                   or point[axis] > box[2 * axis + 1] + slack
                   for axis in range(3)):
                continue
            local = block.local(point)
            if local is None:
                continue
            if all(-_INSIDE_TOLERANCE <= item <= 1 + _INSIDE_TOLERANCE
                   for item in local):
                return index, local
        return None


def uniform_grid(bounds: Sequence[float], counts: Sequence[int],
                 gradings=(None, None, None)) -> BackgroundGrid:
    """One axis-aligned block, as the derived background box is written."""
    from foammesh.openfoam.background_mesh import CORNERS

    corners = tuple(tuple(float(bounds[2 * axis + local[axis]])
                          for axis in range(3)) for local in CORNERS)
    faces = tuple(axis_faces(counts[axis], gradings[axis]) for axis in range(3))
    return BackgroundGrid((GridBlock(corners, faces),))


def grid_from_topology(topology) -> BackgroundGrid | None:
    """The grid a `background_mesh.BackgroundTopology` describes."""
    if topology is None:
        return None
    blocks = []
    try:
        for block in topology.blocks:
            corners = tuple(
                tuple(float(value) for value in topology.vertices[index][:3])
                for index in block.vertices)
            if block.grading_literal is not None:
                gradings = _literal_gradings(block.grading_literal)
            elif len(block.grading) == 3:
                gradings = list(block.grading)
            elif len(block.grading) == 12:
                # edgeGrading: the first edge of each direction stands for
                # all four; a guard, not a writer, so that is close enough.
                gradings = [block.grading[0], block.grading[4],
                            block.grading[8]]
            else:
                gradings = [None, None, None]
            faces = tuple(axis_faces(block.count(axis), gradings[axis])
                          for axis in range(3))
            blocks.append(GridBlock(corners, faces))
    except (TypeError, ValueError, IndexError, AttributeError):
        return None
    return BackgroundGrid(tuple(blocks)) if blocks else None


def background_grid(db, geometry_bounds=None) -> BackgroundGrid | None:
    """The grid the case writer would build, or ``None`` with none to build."""
    if db is None:
        return None
    try:
        from foammesh.core.geometry import BBox
        from foammesh.openfoam.case_builder import CaseBuilder

        bbox = None if geometry_bounds is None else BBox(
            *(float(value) for value in geometry_bounds))
        topology = CaseBuilder(db, bbox).background_topology()
    except Exception:  # noqa: BLE001 - no configuration to build a grid from
        return None
    return grid_from_topology(topology)


def max_refinement_level(db) -> int:
    """The highest surface or volume-region refinement level in the case."""
    if db is None:
        return 0
    try:
        from foammesh.openfoam.case_builder import CaseBuilder

        builder = CaseBuilder(db, None)
    except Exception:  # noqa: BLE001
        return 0
    levels = [0]
    for refinement in builder._elements(
            'castellation/refinementSurfaces').values():
        element = builder._item_element(refinement, 'surfaceRefinement')
        for name in ('maximumLevel', 'minimumLevel'):
            try:
                levels.append(int(builder._item_value(element, name, 0) or 0))
            except (TypeError, ValueError):
                continue
        try:
            levels.append(int(builder._item_value(
                refinement, 'featureEdgeRefinementLevel', 0) or 0))
        except (TypeError, ValueError):
            pass
    for refinement in builder._elements(
            'castellation/refinementVolumes').values():
        try:
            bands = builder._refinement_bands(refinement, 'region', 'distance')
            levels.extend(int(level) for _distance, level in bands)
        except Exception:  # noqa: BLE001 - an unreadable region adds nothing
            pass
        try:
            levels.append(int(builder._item_value(
                refinement, 'volumeRefinementLevel', 0) or 0))
        except (TypeError, ValueError):
            pass
    return max(0, max(levels))


# -- the answer ------------------------------------------------------------- #

@dataclass(frozen=True)
class Clearance:
    """How far a point is from the nearest face, and along which axis."""

    #: Distance to the nearest face, in model units.
    distance: float
    #: The same distance as a fraction of the finest cell there.
    relative: float
    #: The block axis (0, 1, 2) of the nearest face.
    axis: int
    #: The block holding the point.
    block: int
    #: The finest cell's size along that axis, in model units.
    cell: float


def _dyadic_distance(t: float, level: int) -> float:
    """Distance from *t* in ``[0, 1]`` to the nearest ``m / 2**level``."""
    scaled = t * (2 ** level)
    return abs(scaled - round(scaled)) / (2 ** level)


def _axis_clearance(block: GridBlock, axis: int, local, max_level: int):
    """``(distance, relative, finest)`` along one block axis."""
    faces = block.faces[axis]
    t = min(1.0, max(0.0, local[axis]))
    index = 0
    while index < len(faces) - 2 and t > faces[index + 1]:
        index += 1
    lower, upper = faces[index], faces[index + 1]
    width = upper - lower
    if width <= 0:
        return 0.0, 0.0, 0.0
    within = (t - lower) / width
    fraction = _dyadic_distance(within, max_level)
    length = block.edge_length(axis, local)
    base = width * length
    finest = base / (2 ** max_level)
    distance = fraction * base
    relative = distance / finest if finest > 0 else 0.0
    return distance, relative, finest


def seed_face_clearance(point, grid: BackgroundGrid | None,
                        max_level: int = 0) -> Clearance | None:
    """The nearest face to *point* at any level up to *max_level*.

    ``None`` when there is no grid or the point is outside every block.
    """
    if grid is None:
        return None
    point = tuple(float(value) for value in point[:3])
    found = grid.locate(point)
    if found is None:
        return None
    index, local = found
    block = grid.blocks[index]
    best = None
    for axis in range(3):
        distance, relative, finest = _axis_clearance(
            block, axis, local, max(0, int(max_level)))
        if best is None or relative < best.relative:
            best = Clearance(distance, relative, axis, index, finest)
    return best


def nudge_off_faces(point, grid: BackgroundGrid | None, max_level: int = 0,
                    *, minimum: float = MIN_CLEARANCE) -> tuple:
    """*point*, moved a third of a finest cell off any face nearer than
    *minimum* of that cell, along the axis of each such face.

    The move is inward, away from the block's nearer end, so it never leaves
    the block. Unchanged when the point is clear, or outside every block.
    """
    point = tuple(float(value) for value in point[:3])
    if grid is None:
        return point
    found = grid.locate(point)
    if found is None:
        return point
    index, local = found
    block = grid.blocks[index]
    level = max(0, int(max_level))
    moved = list(local)
    changed = False
    for axis in range(3):
        _distance, relative, _finest = _axis_clearance(block, axis, moved, level)
        if relative >= minimum:
            continue
        faces = block.faces[axis]
        t = min(1.0, max(0.0, moved[axis]))
        position = 0
        while position < len(faces) - 2 and t > faces[position + 1]:
            position += 1
        width = faces[position + 1] - faces[position]
        step = width / (2 ** level) * SNAP_FRACTION
        # Land a third of a finest cell from the face the point is on, on
        # the side that stays in this base cell -- so the cell size the
        # third was taken from is the one the point ends up in.
        within = (t - faces[position]) / width if width > 0 else 0.0
        scaled = within * (2 ** level)
        nearest = round(scaled) / (2 ** level)
        if nearest <= 0.0:
            direction = 1.0
        elif nearest >= 1.0:
            direction = -1.0
        else:
            direction = 1.0 if t < 0.5 else -1.0
        target = faces[position] + nearest * width + direction * step
        moved[axis] = min(1.0, max(0.0, target))
        changed = True
    if not changed:
        return point
    return block.point(moved)


def snap_value(value: float, origin: float, step: float) -> float:
    """*value* snapped to ``origin + (k + 1/3) * step``, the nearest such."""
    if not step or step <= 0 or not math.isfinite(step):
        return value
    k = round((value - origin) / step - SNAP_FRACTION)
    return origin + (k + SNAP_FRACTION) * step
