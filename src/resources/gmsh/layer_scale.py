"""A scalar field that scales a boundary-layer stack node by node.

Plan 50 IP28 (DP-6436, DP-6438). ``geo.extrudeBoundaryLayer`` takes a view:
a scalar view there multiplies every node's extrusion by the view's value at
that node. MEASURED on Gmsh 4.15.2 (``scripts/probe_gmsh_layers_local_thickness.py``,
``pipe.step``, three layers): a view that is 1 on one half and 0.3 on the
other grew 10.0 on one half and 3.0 on the other, with no bad cell. A view
that holds no cell over half the model scaled that half too (3.0 where 10.0
was asked), so the field built here always covers the whole bounding box.

The field is the smaller of two parts:

- a *patch* part, the scale of the nearest sampled base node: how a call
  that carries patches with different total thicknesses (unlike stacks that
  touch, ``scripts/probe_gmsh_layers_unlike_stacks.py``) gives each its own;
- a *cap* part from the places a previous attempt folded: at each spot the
  stack is cut to the thickness the wall carried there, and the cut blends
  back to the full stack over ``radius``.

It is written as an octree of hexahedra (``SH`` list data) refined only where
the field changes, so its size follows the spots and patch borders rather
than the model. Pure Python, so it is tested without Gmsh.
"""
from __future__ import annotations

import math

#: Refinement stops once the field changes by less than this over a cell.
TOLERANCE = 0.02
#: An octree never gets deeper than this.
MAX_DEPTH = 14
#: Nor holds more leaves than this; past it the field is still whole, coarser.
MAX_LEAVES = 60000


def smoothstep(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3.0 - 2.0 * value)


class _Grid:
    """Points hashed into cubes of side *size*, for nearest and range asks."""

    def __init__(self, points, size):
        self.size = max(float(size), 1e-300)
        self.cells = {}
        for index, point in enumerate(points):
            self.cells.setdefault(self.key(point), []).append(index)
        self.points = points

    def key(self, point):
        return tuple(int(math.floor(point[axis] / self.size))
                     for axis in range(3))

    def near(self, point, reach):
        rings = int(math.ceil(reach / self.size))
        base = self.key(point)
        for i in range(-rings, rings + 1):
            for j in range(-rings, rings + 1):
                for k in range(-rings, rings + 1):
                    yield from self.cells.get(
                        (base[0] + i, base[1] + j, base[2] + k), ())

    def any_within(self, point, reach):
        """True when some point lies within *reach* of *point*."""
        rings = int(math.ceil(reach / self.size))
        base = self.key(point)
        if (2 * rings + 1) ** 3 > len(self.cells):
            keys = [key for key in self.cells
                    if all(abs(key[axis] - base[axis]) <= rings
                           for axis in range(3))]
        else:
            keys = [(base[0] + i, base[1] + j, base[2] + k)
                    for i in range(-rings, rings + 1)
                    for j in range(-rings, rings + 1)
                    for k in range(-rings, rings + 1)]
        for key in keys:
            for index in self.cells.get(key, ()):
                if _distance(point, self.points[index]) <= reach:
                    return True
        return False


def _distance(one, other):
    return math.sqrt(sum((one[axis] - other[axis]) ** 2 for axis in range(3)))


class ScaleField:
    """The scale at a point: min(patch part, cap part).

    *samples*: ``(x, y, z, scale)`` base nodes with the scale of the patch
    they belong to; with none, the patch part is 1.
    *spots*: ``(x, y, z, scale)`` places where the stack is cut to *scale*,
    the full cut over half of *radius*, blending back to 1 by *radius*.
    *spacing*: the grid the samples are bucketed on.
    """

    def __init__(self, samples=(), spots=(), radius=0.0, spacing=None):
        self.samples = [tuple(float(v) for v in item[:4]) for item in samples]
        self.spots = [tuple(float(v) for v in item[:4]) for item in spots]
        self.radius = float(radius)
        scales = {round(item[3], 12) for item in self.samples}
        # One scale everywhere needs no lookup at all.
        self.uniform = (scales.pop() if len(scales) == 1 else
                        (1.0 if not scales else None))
        self.spacing = float(spacing or self.radius or 1.0)
        self._patch = (_Grid([item[:3] for item in self.samples],
                             self.spacing)
                       if self.uniform is None else None)
        # Per bucket of the sample grid, the scales of the samples in it.
        self.buckets = {}
        if self._patch is not None:
            for key, members in self._patch.cells.items():
                self.buckets[key] = {round(self.samples[index][3], 12)
                                     for index in members}
        self._spots = (_Grid([item[:3] for item in self.spots],
                             max(self.radius, 1e-300))
                       if self.spots else None)

    def scales_in(self, keys):
        found = set()
        for key in keys:
            found |= self.buckets[key]
        return found

    def cap_near(self, centre, half_diagonal):
        return self._spots is not None and self._spots.any_within(
            centre, self.radius + half_diagonal)

    def patch_part(self, point, reach=None):
        """The scale of the nearest sample within *reach* (1 past it)."""
        if self._patch is None:
            return self.uniform
        best, value = None, 1.0
        for index in self._patch.near(point, reach or self.spacing):
            distance = _distance(point, self.samples[index])
            if best is None or distance < best:
                best, value = distance, self.samples[index][3]
        return value

    def cap_part(self, point):
        if self._spots is None or self.radius <= 0.0:
            return float('inf')
        best = float('inf')
        for index in self._spots.near(point, self.radius):
            x, y, z, scale = self.spots[index]
            distance = _distance(point, (x, y, z))
            if distance >= self.radius:
                continue
            # The full cut holds over the inner half of the radius, so a
            # trilinear cell of a quarter radius around the spot carries it
            # whole; it blends back to the full stack over the outer half.
            value = scale + (1.0 - scale) * smoothstep(
                2.0 * distance / self.radius - 1.0)
            best = min(best, value)
        return best

    def __call__(self, point):
        return min(self.patch_part(point), self.cap_part(point))

    @property
    def trivial(self):
        """True when the field is 1 everywhere: no view is needed."""
        return not self.spots and self.uniform is not None             and abs(self.uniform - 1.0) < 1e-12


def _corners(low, size):
    return [(low[0] + size * i, low[1] + size * j, low[2] + size * k)
            for k in (0, 1) for j in (0, 1) for i in (0, 1)]


# Gmsh's hexahedron node order: the bottom face counter-clockwise, then the top.
_HEX_ORDER = (0, 1, 3, 2, 4, 5, 7, 6)


def octree_leaves(field, box, finest):
    """The leaves of an octree over *box*, refined where *field* changes.

    Each leaf is ``(low_corner, size, corner_values)`` with the eight values
    in Gmsh's hexahedron order. The root is the cube around *box*, a little
    larger, so every node of the model lies inside a leaf.

    A cell is split while it is coarser than *finest* and either holds base
    nodes of more than one patch scale or lies within reach of a spot. A
    cell that holds nodes of one scale takes that scale at all eight
    corners, exactly, however large it is; a cell that holds none takes 1
    (no base node is there to read it); a cell still mixed at *finest* takes
    the nearest node's scale at each corner.
    """
    span = max(box[3] - box[0], box[4] - box[1], box[5] - box[2], 1e-12)
    span *= 1.02
    centre = [(box[axis] + box[axis + 3]) / 2.0 for axis in range(3)]
    root = (tuple(centre[axis] - span / 2.0 for axis in range(3)), span)
    finest = max(float(finest), span / (2 ** MAX_DEPTH))
    grid = field._patch
    leaves, pending = [], [(root, 0, list(field.buckets))]
    while pending:
        (low, size), depth, keys = pending.pop()
        high = tuple(low[axis] + size for axis in range(3))
        if grid is not None:
            # The sample buckets that overlap this cell.
            lo = grid.key(low)
            hi = grid.key(high)
            keys = [key for key in keys
                    if all(lo[axis] <= key[axis] <= hi[axis]
                           for axis in range(3))]
        scales = field.scales_in(keys) if grid is not None else set()
        centre = tuple(low[axis] + size / 2.0 for axis in range(3))
        capped = field.cap_near(centre, size * math.sqrt(3.0) / 2.0)
        if ((len(scales) > 1 or capped) and size > finest
                and depth < MAX_DEPTH
                and len(leaves) + len(pending) + 8 <= MAX_LEAVES):
            half = size / 2.0
            for corner in _corners(low, half):
                pending.append(((corner, half), depth + 1, keys))
            continue
        corners = _corners(low, size)
        if grid is None:
            base = [field.uniform] * 8
        elif len(scales) == 1:
            base = [next(iter(scales))] * 8
        elif not scales:
            base = [1.0] * 8
        else:
            base = [field.patch_part(point, reach=2.0 * size)
                    for point in corners]
        if capped:
            base = [min(value, field.cap_part(point))
                    for value, point in zip(base, corners)]
        leaves.append((low, size, [base[index] for index in _HEX_ORDER]))
    return leaves


def list_data(leaves):
    """``SH`` list data for ``gmsh.view.addListData``: the count and values."""
    data = []
    for low, size, values in leaves:
        points = [_corners(low, size)[index] for index in _HEX_ORDER]
        data.extend(point[0] for point in points)
        data.extend(point[1] for point in points)
        data.extend(point[2] for point in points)
        data.extend(values)
    return len(leaves), data


def merge_spots(old, new, radius):
    """Spots of two attempts as one list, the lower scale kept where close.

    A spot of the new attempt within half a radius of an old one replaces it
    when it cuts deeper and is dropped when it does not, so the list grows
    with the places that folded rather than with the attempts.
    """
    kept = [list(item) for item in old]
    for spot in new:
        spot = list(spot)
        for item in kept:
            if _distance(item[:3], spot[:3]) < radius / 2.0:
                if spot[3] < item[3]:
                    item[:] = spot
                break
        else:
            kept.append(spot)
    return kept


def thin_spots(spots, radius, limit=2000):
    """At most *limit* spots, one per half-radius cube, the deepest kept."""
    cells = {}
    size = max(radius / 2.0, 1e-300)
    for spot in spots:
        key = tuple(int(math.floor(spot[axis] / size)) for axis in range(3))
        if key not in cells or spot[3] < cells[key][3]:
            cells[key] = list(spot)
    kept = sorted(cells.values(), key=lambda item: item[3])
    return kept[:limit]
