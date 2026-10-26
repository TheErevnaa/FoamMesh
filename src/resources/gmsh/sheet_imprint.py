"""DP-6064. Make an imported sheet's rim part of the surfaces it meets.

An internal face or a blocking baffle drawn as an STL is a sheet whose rim
lies on the walls of the region holding it: a cross-section of a duct touches
all four walls, a plate standing on the floor touches the floor. Gmsh can embed
such a sheet in the volume only if the volume's boundary mesh conforms to the
rim, and two triangulations exported apart do not: MEASURED on the SR9 duct, a
section on a 1/8 m grid in a duct on a 1/6 m grid, embedding the classified
sheet stops the 3D pass with `PLC Error: A segment and a facet intersect at
point`, because the rim crosses wall triangles rather than running along their
edges.

:func:`imprint_rims` makes the two conform before classification, in the
triangulation itself:

1. every rim point that lies on a wall becomes a wall node (the wall triangle
   or edge it lies in is split, or it is joined to the wall node it sits on);
2. every wall node that lies on a rim edge becomes a rim node (the sheet
   triangle on that edge is split);
3. every rim edge whose two ends are now wall nodes is walked across the wall
   triangles between them, and each wall edge it crosses is split at the
   crossing, on the wall and on the rim alike,

so each such rim edge ends up as a wall edge. A rim edge that is not on a wall
(a baffle's free edge in the flow) is left alone. Classification then sees the
rim as a line shared by the sheet and the wall triangles on both sides of it,
and the curve it makes there is one curve of both.

The functions here take plain node tables and triangle lists, so they are
tested without Gmsh; :func:`imprint_model` is the Gmsh side.
"""
from __future__ import annotations

import math

#: How far a rim edge may lean out of the plane of the wall triangle it is
#: walked across, as the sine of the angle. A chord of a curved wall leans by
#: half the angle it subtends; a rim edge on the next face round a corner
#: leans by the corner angle, and must not be walked across this face.
LEAN = math.sin(math.radians(20.0))


def _sub(left, right):
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _dot(left, right):
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _cross(left, right):
    return (left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0])


def _norm(vector):
    return math.sqrt(_dot(vector, vector))


def _along(start, end, fraction):
    return tuple(start[axis] + fraction * (end[axis] - start[axis])
                 for axis in range(3))


def _key(first, second):
    return (first, second) if first < second else (second, first)


class ImprintError(RuntimeError):
    """A rim edge on a wall could not be made a wall edge."""


class _Triangulation:
    """Triangles with the indexes a local edit needs."""

    def __init__(self, triangles):
        #: ``[a, b, c]`` or ``None`` once replaced; ``owner`` per triangle.
        self.triangles: list = []
        self.owners: list = []
        self.edges: dict = {}
        self.around: dict = {}
        for triangle, owner in triangles:
            self.add(triangle, owner)

    def add(self, triangle, owner):
        index = len(self.triangles)
        self.triangles.append(tuple(int(node) for node in triangle))
        self.owners.append(owner)
        self._index(index)
        return index

    def _index(self, index):
        triangle = self.triangles[index]
        for corner in range(3):
            first, second = triangle[corner], triangle[(corner + 1) % 3]
            self.edges.setdefault(_key(first, second), set()).add(index)
            self.around.setdefault(first, set()).add(index)

    def _unindex(self, index):
        triangle = self.triangles[index]
        for corner in range(3):
            first, second = triangle[corner], triangle[(corner + 1) % 3]
            users = self.edges.get(_key(first, second))
            if users is not None:
                users.discard(index)
                if not users:
                    del self.edges[_key(first, second)]
            self.around.get(first, set()).discard(index)

    def replace(self, index, triangles):
        owner = self.owners[index]
        self._unindex(index)
        self.triangles[index] = None
        return [self.add(triangle, owner) for triangle in triangles]

    def has_edge(self, first, second):
        return bool(self.edges.get(_key(first, second)))

    def split_edge(self, first, second, node):
        """Split every triangle on edge (*first*, *second*) at *node*."""
        made = []
        for index in sorted(self.edges.get(_key(first, second), ())):
            triangle = self.triangles[index]
            for corner in range(3):
                start, end = triangle[corner], triangle[(corner + 1) % 3]
                if {start, end} == {first, second}:
                    apex = triangle[(corner + 2) % 3]
                    made.extend(self.replace(
                        index, [(start, node, apex), (node, end, apex)]))
                    break
        return made

    def split_triangle(self, index, node):
        first, second, third = self.triangles[index]
        return self.replace(index, [(first, second, node),
                                    (second, third, node),
                                    (third, first, node)])

    def rename(self, old, new):
        for index in sorted(self.around.get(old, ())):
            triangle = self.triangles[index]
            self.replace(index, [tuple(new if node == old else node
                                       for node in triangle)])

    def rim(self):
        """Edges used by exactly one triangle, as sorted pairs."""
        return sorted(edge for edge, users in self.edges.items()
                      if len(users) == 1)

    def live(self):
        return [(triangle, self.owners[index])
                for index, triangle in enumerate(self.triangles)
                if triangle is not None]


class _Grid:
    """A uniform grid of triangle boxes, for finding what is near a point."""

    def __init__(self, cell):
        self.cell = float(cell)
        self.cells: dict = {}

    def _span(self, low, high):
        return [range(int(math.floor(low[axis] / self.cell)),
                      int(math.floor(high[axis] / self.cell)) + 1)
                for axis in range(3)]

    def insert(self, index, points, margin):
        low = [min(point[axis] for point in points) - margin
               for axis in range(3)]
        high = [max(point[axis] for point in points) + margin
                for axis in range(3)]
        spans = self._span(low, high)
        for i in spans[0]:
            for j in spans[1]:
                for k in spans[2]:
                    self.cells.setdefault((i, j, k), []).append(index)

    def near(self, point):
        key = tuple(int(math.floor(point[axis] / self.cell))
                    for axis in range(3))
        return self.cells.get(key, ())


def _closest_on_segment(point, start, end):
    edge = _sub(end, start)
    length = _dot(edge, edge)
    if length <= 0.0:
        return 0.0, _norm(_sub(point, start))
    fraction = max(0.0, min(1.0, _dot(_sub(point, start), edge) / length))
    return fraction, _norm(_sub(point, _along(start, end, fraction)))


def _distance_to_plane(point, corners):
    normal = _cross(_sub(corners[1], corners[0]), _sub(corners[2], corners[0]))
    length = _norm(normal)
    if length <= 0.0:
        return math.inf, None
    return abs(_dot(_sub(point, corners[0]), normal)) / length, normal


def _inside(point, corners, normal):
    for corner in range(3):
        start, end = corners[corner], corners[(corner + 1) % 3]
        if _dot(_cross(_sub(end, start), _sub(point, start)), normal) < 0.0:
            return False
    return True


class _Imprint:
    def __init__(self, coordinates, walls, sheets, tolerance, next_node):
        self.coordinates = dict(coordinates)
        self.walls = _Triangulation(walls)
        self.sheets = _Triangulation(sheets)
        self.tolerance = float(tolerance)
        self.next_node = int(next_node)
        #: New node -> the wall owner it was made on.
        self.created: dict = {}
        #: Sheet node -> the wall node it was found to be.
        self.joined: dict = {}
        self.on_wall: set = set()
        self.split_walls = 0
        self.split_rims = 0
        lengths = [_norm(_sub(self.coordinates[first],
                              self.coordinates[second]))
                   for first, second in self.walls.edges]
        mean = sum(lengths) / len(lengths) if lengths else 1.0
        self.grid = _Grid(max(mean, 1e3 * self.tolerance, 1e-12))
        for index, triangle in enumerate(self.walls.triangles):
            self._grid(index)

    def _grid(self, index):
        self.grid.insert(index, [self.coordinates[node] for node
                                 in self.walls.triangles[index]],
                         self.tolerance)

    def _wall_split_triangle(self, index, node):
        for made in self.walls.split_triangle(index, node):
            self._grid(made)
        self.split_walls += 1

    def _wall_split_edge(self, first, second, node):
        for made in self.walls.split_edge(first, second, node):
            self._grid(made)
        self.split_walls += 1

    def _new_node(self, point, owner):
        self.next_node += 1
        self.coordinates[self.next_node] = tuple(point)
        self.created[self.next_node] = owner
        return self.next_node

    # -- step 1: rim points onto the walls ------------------------------ #

    def place(self, node):
        """Make the rim point *node* a wall node if it lies on a wall."""
        point = self.coordinates[node]
        if node in self.walls.around and self.walls.around[node]:
            self.on_wall.add(node)
            return node
        # A node before an edge before a face, whatever the distances: a
        # point on an edge is also in the plane of both its triangles, and
        # rounding must not make it split one of them into a sliver.
        found = {}
        for index in self.grid.near(point):
            triangle = self.walls.triangles[index]
            if triangle is None:
                continue
            corners = [self.coordinates[corner] for corner in triangle]
            for corner, other in zip(triangle, corners):
                distance = _norm(_sub(point, other))
                if distance <= self.tolerance:
                    found.setdefault('node', []).append((distance, corner))
            for edge in range(3):
                fraction, distance = _closest_on_segment(
                    point, corners[edge], corners[(edge + 1) % 3])
                if distance <= self.tolerance and 0.0 < fraction < 1.0:
                    found.setdefault('edge', []).append(
                        (distance, (triangle[edge], triangle[(edge + 1) % 3])))
            distance, normal = _distance_to_plane(point, corners)
            if distance <= self.tolerance and normal is not None \
                    and _inside(point, corners, normal):
                found.setdefault('face', []).append((distance, index))
        kind = next((kind for kind in ('node', 'edge', 'face')
                     if kind in found), None)
        if kind is None:
            return node
        _distance, where = min(found[kind])
        if kind == 'node':
            if where != node:
                self.sheets.rename(node, where)
                self.joined[node] = where
            self.on_wall.add(where)
            return where
        if kind == 'edge':
            self._wall_split_edge(where[0], where[1], node)
        else:
            self._wall_split_triangle(where, node)
        self.on_wall.add(node)
        return node

    # -- steps 2 and 3: rim edges onto wall edges ------------------------ #

    def _wall_nodes_on(self, first, second):
        """Wall nodes strictly inside rim edge (*first*, *second*)."""
        start, end = self.coordinates[first], self.coordinates[second]
        found = {}
        seen = set()
        steps = max(1, int(_norm(_sub(end, start)) / self.grid.cell) + 1)
        for step in range(steps + 1):
            probe = _along(start, end, step / steps)
            for index in self.grid.near(probe):
                triangle = self.walls.triangles[index]
                if triangle is None:
                    continue
                for node in triangle:
                    if node in (first, second) or node in seen:
                        continue
                    seen.add(node)
                    fraction, distance = _closest_on_segment(
                        self.coordinates[node], start, end)
                    if distance <= self.tolerance and 0.0 < fraction < 1.0:
                        found[node] = fraction
        return sorted(found, key=found.get)

    def _crossing(self, first, second):
        """The first wall edge rim edge (*first*, *second*) crosses.

        Returns ``(u, v, fraction along u->v)`` or ``None``.
        """
        start, end = self.coordinates[first], self.coordinates[second]
        direction = _sub(end, start)
        length = _norm(direction)
        best = None
        for index in sorted(self.walls.around.get(first, ())):
            triangle = self.walls.triangles[index]
            corner = triangle.index(first)
            u = triangle[(corner + 1) % 3]
            v = triangle[(corner + 2) % 3]
            to_u = _sub(self.coordinates[u], start)
            to_v = _sub(self.coordinates[v], start)
            normal = _cross(to_u, to_v)
            area = _norm(normal)
            if area <= 0.0:
                continue
            lean = abs(_dot(direction, normal)) / (area * length)
            if lean > LEAN:
                continue
            # The direction in the plane, as alpha*to_u + beta*to_v.
            flat = _sub(direction, tuple(
                component * _dot(direction, normal) / (area * area)
                for component in normal))
            uu, uv, vv = _dot(to_u, to_u), _dot(to_u, to_v), _dot(to_v, to_v)
            fu, fv = _dot(flat, to_u), _dot(flat, to_v)
            determinant = uu * vv - uv * uv
            if determinant <= 0.0:
                continue
            alpha = (fu * vv - fv * uv) / determinant
            beta = (fv * uu - fu * uv) / determinant
            scale = 1e-9 * (abs(alpha) + abs(beta))
            if alpha < -scale or beta < -scale or alpha + beta <= 0.0:
                continue
            fraction = max(0.0, min(1.0, beta / (alpha + beta)))
            if best is None or lean < best[0]:
                best = (lean, u, v, fraction)
        if best is None:
            return None
        return best[1:]

    def conform(self, first, second, budget):
        """Make rim edge (*first*, *second*) a chain of wall edges."""
        pending = [(first, second)]
        while pending:
            if budget[0] <= 0:
                raise ImprintError('the rim walk did not finish')
            budget[0] -= 1
            first, second = pending.pop()
            if self.walls.has_edge(first, second):
                continue
            inside = self._wall_nodes_on(first, second)
            if inside:
                node = inside[0]
                self.sheets.split_edge(first, second, node)
                self.split_rims += 1
                pending.append((node, second))
                pending.append((first, node))
                continue
            crossing = self._crossing(first, second)
            if crossing is None:
                start = self.coordinates[first]
                raise ImprintError(
                    'a rim edge from ({:.6g}, {:.6g}, {:.6g}) could not be '
                    'followed across the wall it lies on'.format(*start))
            u, v, fraction = crossing
            point = _along(self.coordinates[u], self.coordinates[v], fraction)
            near_u = _norm(_sub(point, self.coordinates[u])) <= self.tolerance
            near_v = _norm(_sub(point, self.coordinates[v])) <= self.tolerance
            if near_u or near_v:
                # The rim runs through a wall node that the tolerance missed
                # as lying on it: take that node as the rim's.
                node = u if near_u else v
            else:
                owner = self.walls.owners[next(iter(
                    self.walls.edges[_key(u, v)]))]
                node = self._new_node(point, owner)
                self._wall_split_edge(u, v, node)
            self.sheets.split_edge(first, second, node)
            self.split_rims += 1
            self.on_wall.add(node)
            pending.append((node, second))
            pending.append((first, node))

    def run(self):
        rim = self.sheets.rim()
        for node in sorted({node for edge in rim for node in edge}):
            self.place(node)
        # The rim again: joining a rim point to a wall node renames it.
        rim = [edge for edge in self.sheets.rim()
               if edge[0] in self.on_wall and edge[1] in self.on_wall]
        budget = [64 * (len(self.walls.triangles) + len(rim)) + 1024]
        conformed = 0
        for first, second in rim:
            if not self.walls.has_edge(first, second):
                self.conform(first, second, budget)
            conformed += 1
        return conformed


def imprint_rims(coordinates, walls, sheets, tolerance, next_node):
    """Imprint each sheet's rim on the walls it lies on.

    *coordinates* is ``{node: (x, y, z)}``; *walls* and *sheets* are lists of
    ``((a, b, c), owner)``; new nodes are numbered from ``next_node + 1``.
    Returns a dict: ``walls`` and ``sheets`` (the triangle lists after),
    ``created`` (``{node: (point, owner)}``), ``joined`` (sheet node -> wall
    node), ``rimOnWall`` (the rim edges that now run along wall edges),
    ``wallSplits`` and ``rimSplits``.
    """
    work = _Imprint(coordinates, walls, sheets, tolerance, next_node)
    conformed = work.run()
    return {
        'walls': work.walls.live(),
        'sheets': work.sheets.live(),
        'created': {node: (work.coordinates[node], owner)
                    for node, owner in work.created.items()},
        'joined': dict(work.joined),
        'rimOnWall': conformed,
        'wallSplits': work.split_walls,
        'rimSplits': work.split_rims,
    }


def imprint_model(gmsh, sheet_tags, tolerance):
    """Imprint the rims of the discrete surfaces *sheet_tags* on the others.

    Rewrites the triangles of every surface the imprint changed, and adds the
    nodes it made to the surface each was made on. Returns the summary of
    :func:`imprint_rims`, without the triangle lists.
    """
    sheet_tags = {int(tag) for tag in sheet_tags}
    tags, flat, _parametric = gmsh.model.mesh.getNodes()
    coordinates = {int(tag): (float(flat[3 * index]),
                              float(flat[3 * index + 1]),
                              float(flat[3 * index + 2]))
                   for index, tag in enumerate(tags)}
    walls, sheets, before = [], [], {}
    for _dim, tag in gmsh.model.getEntities(2):
        kept = []
        types, _elements, nodes = gmsh.model.mesh.getElements(2, tag)
        for element_type, node_tags in zip(types, nodes):
            if int(element_type) != 2:
                continue
            values = [int(value) for value in node_tags]
            kept.extend(tuple(values[start:start + 3])
                        for start in range(0, len(values) - 2, 3))
        before[int(tag)] = kept
        target = sheets if int(tag) in sheet_tags else walls
        target.extend((triangle, int(tag)) for triangle in kept)
    result = imprint_rims(coordinates, walls, sheets, tolerance,
                          int(gmsh.model.mesh.getMaxNodeTag()))
    by_owner: dict = {}
    for triangle, owner in result['walls'] + result['sheets']:
        by_owner.setdefault(owner, []).append(triangle)
    for node, (point, owner) in sorted(result['created'].items()):
        gmsh.model.mesh.addNodes(2, int(owner), [int(node)], list(point))
    changed = []
    for tag, triangles in sorted(by_owner.items()):
        if sorted(triangles) == sorted(before.get(tag, ())):
            continue
        changed.append(tag)
        gmsh.model.mesh.removeElements(2, int(tag))
        gmsh.model.mesh.addElementsByType(
            int(tag), 2, [], [node for triangle in triangles
                              for node in triangle])
    if changed or result['created']:
        gmsh.model.mesh.reclassifyNodes()
    summary = {key: value for key, value in result.items()
               if key not in ('walls', 'sheets')}
    summary['created'] = len(result['created'])
    summary['joined'] = len(result['joined'])
    summary['changedSurfaces'] = changed
    return summary


__all__ = ('ImprintError', 'imprint_model', 'imprint_rims')
