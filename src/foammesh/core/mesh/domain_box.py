"""The box the background mesh will span, answered once for every reader.

Plan 36 RP1. snappyHexMesh keeps the cells of the background mesh that are
reachable from a region's seed, so a seed is only meaningful inside the box
``blockMesh`` builds. Three places answered "where is that box?" and each in
its own way: the legacy Base Grid page from its widgets, the launch gate from
a ``blockMeshDict`` parsed back off disk, and ``CaseBuilder`` -- the only one
that is actually written -- from the configuration. Domain & Regions, where a
seed is placed, asked none of them and drew nothing (F4).

The rules here are the writer's, in the writer's order:

1. authored blocks win, and the box is their vertex hull;
2. else a chosen bounding Hex6 (``baseGrid/boundingHex6``) is the block;
3. else the block is derived from the geometry extent, pushed out by
   ``baseGrid/standoff`` (`stand_off_bounds`);
4. else, with no configuration or geometry to derive it from, a written
   ``system/blockMeshDict`` is read back.

The plan lists the Hex6 before the authored blocks. ``CaseBuilder`` does it
the other way round -- a project with authored blocks writes them whatever the
Hex6 says -- and the box drawn has to be the box written, so the writer's
order is kept.

``bounds`` are block vertex coordinates, the frame the geometry is drawn in.
``blockMesh`` multiplies every vertex by ``scale``, so ``metres()`` is what
the mesh spans once built -- the frame the launch gate has always judged
seeds in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Where a box came from, in the order they are tried.
BLOCKS = 'blocks'
HEX6 = 'hex6'
GEOMETRY = 'geometry'
BLOCK_MESH_DICT = 'blockMeshDict'

SOURCES = (BLOCKS, HEX6, GEOMETRY, BLOCK_MESH_DICT)

#: A keyword left at this reads its value from the configuration.
_STORED = object()


#: RP13 #5. Authored blocks filling less than this fraction of their hull are
#: not a box: the rest of the hull has no background cells.
CUBOID_FRACTION = 0.999

#: Each face of a block as four corner indices in blockMesh's vertex order,
#: wound so the normal points out of a right-handed block.
BLOCK_FACES = ((0, 4, 7, 3), (1, 2, 6, 5), (0, 1, 5, 4), (3, 7, 6, 2),
               (0, 3, 2, 1), (4, 5, 6, 7))


@dataclass(frozen=True)
class DomainBox:
    """``(xmin, xmax, ymin, ymax, zmin, zmax)`` in block vertex units."""

    bounds: tuple[float, float, float, float, float, float]
    source: str
    scale: float = 1.0
    #: RP13 #5. Each authored block's eight corners, in blockMesh's order;
    #: empty for every other source, whose domain is the box itself.
    blocks: tuple = ()
    #: False when the blocks fill less than `CUBOID_FRACTION` of the hull,
    #: so part of the hull has no background cells.
    cuboid: bool = True

    def metres(self) -> tuple[float, ...]:
        """The box ``blockMesh`` builds: every vertex times ``scale``."""
        return tuple(value * self.scale for value in self.bounds)

    def outlines(self) -> tuple:
        """What to draw: each block's corners, or the hull's when a box."""
        if self.cuboid or not self.blocks:
            return (_box_corners(self.bounds),)
        return self.blocks

    def contains(self, point) -> bool:
        """True when *point* is where blockMesh makes cells (walls included)."""
        return bool(self.mask([point])[0])

    def mask(self, points):
        """Which of *points* (``N x 3``) have background cells, as booleans.

        A box answers by its bounds; a domain that is not one answers by its
        blocks, each a convex hexahedron with planar faces.
        """
        import numpy as np

        points = np.asarray(points, dtype=float).reshape(-1, 3)
        bounds = np.asarray(self.bounds, dtype=float)
        span = float(np.max(bounds[1::2] - bounds[0::2])) if len(bounds) else 0
        slack = 1e-9 * max(1.0, span)
        inside = np.all((points >= bounds[0::2] - slack)
                        & (points <= bounds[1::2] + slack), axis=1)
        if self.cuboid or not self.blocks:
            return inside
        held = np.zeros(len(points), dtype=bool)
        for corners in self.blocks:
            held |= _inside_hex(corners, points, slack)
        return inside & held


def _hull(points) -> tuple[float, ...] | None:
    points = [tuple(float(value) for value in point) for point in points]
    if not points:
        return None
    return tuple(bound for axis in range(3) for bound in (
        min(point[axis] for point in points),
        max(point[axis] for point in points)))


def _builder(db, geometry_bounds):
    from foammesh.core.geometry import BBox
    from foammesh.openfoam.case_builder import CaseBuilder

    bbox = None if geometry_bounds is None else BBox(
        *(float(value) for value in geometry_bounds))
    return CaseBuilder(db, bbox)


def _scale(builder) -> float:
    try:
        scale = float(builder._v('baseGrid/scale', 1) or 1)
    except (TypeError, ValueError):
        return 1.0
    return scale if scale > 0 else 1.0


def _box_corners(bounds) -> tuple:
    x0, x1, y0, y1, z0, z1 = (float(value) for value in bounds)
    return ((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def hex_volume(corners) -> float:
    """A block's volume: twelve tetrahedra from its centroid to its faces."""
    centre = tuple(sum(corner[axis] for corner in corners) / 8.0
                   for axis in range(3))
    volume = 0.0
    for face in BLOCK_FACES:
        a, b, c, d = (corners[index] for index in face)
        for first, second, third in ((a, b, c), (a, c, d)):
            volume += _dot(_sub(first, centre),
                           _cross(_sub(second, centre),
                                  _sub(third, centre))) / 6.0
    return abs(volume)


def _inside_hex(corners, points, slack):
    """Which *points* are on the inner side of all six face planes."""
    import numpy as np

    centre = np.mean(np.asarray(corners, dtype=float), axis=0)
    held = np.ones(len(points), dtype=bool)
    for face in BLOCK_FACES:
        quad = np.asarray([corners[index] for index in face], dtype=float)
        normal = np.cross(quad[2] - quad[0], quad[3] - quad[1])
        length = float(np.linalg.norm(normal))
        if length <= 0:
            continue
        normal /= length
        middle = quad.mean(axis=0)
        if float(np.dot(centre - middle, normal)) > 0:
            normal = -normal                  # point it out of the block
        held &= (points - middle) @ normal <= slack
    return held


def _authored(builder):
    """``(hull, blocks, cuboid)`` of the authored blocks, or ``None``."""
    from foammesh.openfoam import background_mesh

    try:
        topology = background_mesh.from_records(
            lambda name: builder._elements(f'baseGrid/{name}'))
    except Exception:  # noqa: BLE001 - blocks that cannot be read write nothing
        return None
    if topology is None:
        return None
    try:
        hull = _hull(topology.vertices)
        blocks = tuple(
            tuple(tuple(float(value) for value in topology.vertices[index][:3])
                  for index in block.vertices)
            for block in topology.blocks)
    except (TypeError, ValueError, IndexError):
        return None
    if hull is None:
        return None
    # RP13 #5. blockMesh blocks do not overlap, so their volumes add up to
    # the union's; short of the hull, the rest of the hull has no cells.
    hull_volume = 1.0
    for axis in range(3):
        hull_volume *= hull[2 * axis + 1] - hull[2 * axis]
    try:
        filled = sum(hex_volume(corners) for corners in blocks)
    except (TypeError, ValueError, IndexError):
        filled = hull_volume
    cuboid = hull_volume <= 0 or filled >= CUBOID_FRACTION * hull_volume
    return hull, blocks, cuboid


def _stored_hex6(builder):
    """The chosen Hex6's corners, by the writer's rule (DP-577), or ``None``."""
    key = builder._bounding_hex6_key()
    if key is None:
        return None
    geometry = builder._collection_item('geometry', key)
    try:
        return geometry.vector('point1'), geometry.vector('point2')
    except Exception:  # noqa: BLE001 - a row without corners is not a box
        return None


def _stored_standoff(builder) -> float:
    try:
        return float(builder._v('baseGrid/standoff', 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def domain_box(db, case_path=None, geometry_bounds=None, *,
               hex6=_STORED, standoff=_STORED) -> DomainBox | None:
    """The box the background mesh spans, and which source said so.

    *db* is a configuration (or a working copy of one) and may be ``None``;
    *geometry_bounds* is the extent of every surface in the case, as six
    numbers. *hex6* -- a ``(point1, point2)`` pair, or ``None`` for no Hex6 --
    and *standoff* override the stored values, for a page that shows its
    unsaved choices. ``None`` when nothing at all describes a box.
    """
    from foammesh.core.mesh.sizing import stand_off_bounds

    scale = 1.0
    builder = None
    if db is not None:
        builder = _builder(db, geometry_bounds)
        scale = _scale(builder)
        authored = _authored(builder)
        if authored is not None:
            hull, blocks, cuboid = authored
            return DomainBox(hull, BLOCKS, scale, blocks, cuboid)
        if hex6 is _STORED:
            hex6 = _stored_hex6(builder)
        if standoff is _STORED:
            standoff = _stored_standoff(builder)
    if hex6 not in (None, _STORED):
        corners = _hull(hex6)
        if corners is not None:
            return DomainBox(corners, HEX6, scale)
    if geometry_bounds is not None:
        margin = 0.0 if standoff in (None, _STORED) else standoff
        bounds = tuple(float(value) for value in stand_off_bounds(
            geometry_bounds, margin))
        # Plan 37 UF14. The derived block grows to hold the farfield, by the
        # writer's own rule, so the box drawn is the block written.
        enclosing = (None if builder is None
                     else builder.farfield_block(bounds))
        if enclosing is not None:
            bounds = tuple(float(value) for value in enclosing)
        return DomainBox(bounds, GEOMETRY, scale)
    if case_path is not None:
        return written_domain_box(case_path)
    return None


def written_domain_box(case_path) -> DomainBox | None:
    """The box a written ``system/blockMeshDict`` spans, or ``None``.

    The vertex hull is exact for the derived single block and a close enough
    bound for authored blocks (curved edges aside). ``None`` when no
    dictionary has been written or it cannot be read.
    """
    path = Path(case_path) / 'system' / 'blockMeshDict'
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    scale = 1.0
    match = re.search(r'(?:^|[;\s])(?:scale|convertToMeters)\s+'
                      r'([-+0-9.eE]+)\s*;', text)
    if match:
        try:
            scale = float(match.group(1))
        except ValueError:
            return None
    match = re.search(r'\bvertices\s*\((.*?)\)\s*;', text, flags=re.S)
    if not match:
        return None
    number = r'([-+0-9.eE]+)'
    points = re.findall(
        rf'\(\s*{number}\s+{number}\s+{number}\s*\)', match.group(1))
    try:
        bounds = _hull(points)
    except ValueError:
        return None
    if bounds is None:
        return None
    return DomainBox(bounds, BLOCK_MESH_DICT, scale)
