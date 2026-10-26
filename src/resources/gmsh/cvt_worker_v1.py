"""CVT surface remeshing in an isolated Gmsh process (Plan 48 GM16b, 4.11.1).

Plugin(CVTRemesh) of Gmsh 4.15.2 takes *every* surface of the model (it never
looks at visibility or selection, ``plugin/CVTRemesh.cpp:90``) and returns one
new discrete surface. So a selection can only be honoured by giving the plugin
nothing but the picked triangles: this file is the process that does that.

    python3 cvt_worker_v1.py JOB_DIR

``JOB_DIR/job.json`` and ``job.npz`` hold the options and the surface mesh;
``result.npz`` and ``report.json`` come back. The session (``session_cvt_v1``)
starts it with its own interpreter, which is the one that imports FoamMesh's
Gmsh build, and reads the answer; a crash of Gmsh therefore ends this process
and never the session.

Everything that is not a Gmsh call is plain numpy (and scipy where present) so
the host imports this file too: the Sites <-> edge length formula, the
normalised normal radius, the re-split of the result onto the original faces,
the zipper that keeps the border of the picked region fixed, and the checks of
the replacement contract. A check that fails is named in the report; nothing is
repaired silently.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

import numpy as np

#: Gmsh's own defaults of Plugin(CVTRemesh) (CVTRemesh.cpp:27-33).
CVT_DEFAULTS = {
    'Sites': 20000.0,
    'Iterations': 20.0,
    'Anisotropy': 0.03,
    'Variable density': 0.3,
    'Feature sensitivity': 5.0,
    'Normal computation radius': 0.005,
}

#: Area owned by one point of a near-equilateral mesh of edge ``h``.
POINT_AREA_FACTOR = math.sqrt(3.0) / 2.0

#: Fraction of inverted triangles tolerated among the ones the plugin made.
INVERTED_FRACTION_LIMIT = 0.01
#: Width of the collar cut away at a fixed border, in mean edge lengths.
COLLAR_EDGES = 1.0
#: Replace refuses when the surface moved by more than this many mean edges.
DEVIATION_EDGES = 0.5


class CvtRefusal(ValueError):
    """The run cannot be done; the message says which part failed."""


# -- the formulas the panel shows ------------------------------------------------- #

def sites_for_edge_length(area: float, edge: float) -> int:
    """Points needed so that a near-equilateral mesh has edge ``edge``.

    Sites are sample *points*, not triangles: each point owns about
    ``sqrt(3)/2 * h**2`` of area and the triangle count is about twice the
    point count.
    """
    if not (area > 0 and edge > 0):
        raise CvtRefusal('the area and the edge length must be positive')
    return max(4, int(round(area / (POINT_AREA_FACTOR * edge * edge))))


def edge_length_for_sites(area: float, sites: float) -> float:
    """The inverse of :func:`sites_for_edge_length` (the length, not rounded)."""
    if not (area > 0 and sites > 0):
        raise CvtRefusal('the area and the number of points must be positive')
    return math.sqrt(area / (POINT_AREA_FACTOR * float(sites)))


def normalisation(points) -> tuple:
    """``(centre, scale)`` Revoropt divides the mesh by before it works.

    ``normalize_mesh`` (measure_def.hpp:197-237) moves the vertex centroid to
    the origin and then takes the largest ``|centre - x|`` over the vertices
    *already moved*, so the radius it divides by is ``max |2c - x_i|`` for the
    original positions ``x_i`` and centroid ``c``. The same formula is used
    here, so the length equivalent of the normal radius is the one the plugin
    applies.
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if not len(p):
        raise CvtRefusal('there are no points to normalise')
    centre = p.mean(axis=0)
    scale = float(np.linalg.norm(centre - (p - centre), axis=1).max())
    if not scale > 0:
        raise CvtRefusal('the surface has no extent to normalise by')
    return centre, scale


def radius_to_length(native: float, scale: float) -> float:
    """Normal computation radius (fraction of the scale) -> model length."""
    return float(native) * float(scale)


def length_to_radius(length: float, scale: float) -> float:
    """Model length -> normal computation radius (fraction of the scale)."""
    return float(length) / float(scale)


# -- triangle measures ------------------------------------------------------------ #

def _xyz(points, tris):
    p = np.asarray(points, dtype=np.float64)
    return p[np.asarray(tris, dtype=np.int64)]


def tri_areas(points, tris) -> np.ndarray:
    t = _xyz(points, tris)
    return 0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]),
                                axis=1)


def tri_normals(points, tris) -> np.ndarray:
    t = _xyz(points, tris)
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    length = np.linalg.norm(n, axis=1)
    length[length == 0] = 1.0
    return n / length[:, None]


def tri_gamma(points, tris) -> np.ndarray:
    """Gmsh's gamma: twice the inscribed over the circumscribed radius."""
    t = _xyz(points, tris)
    a = np.linalg.norm(t[:, 1] - t[:, 2], axis=1)
    b = np.linalg.norm(t[:, 0] - t[:, 2], axis=1)
    c = np.linalg.norm(t[:, 0] - t[:, 1], axis=1)
    area = tri_areas(points, tris)
    bottom = (a + b + c) * a * b * c
    out = np.zeros(len(t))
    ok = bottom > 0
    out[ok] = 16.0 * area[ok] ** 2 / bottom[ok]
    return out


def edge_lengths(points, tris) -> np.ndarray:
    """Length of every distinct edge of the triangulation."""
    p = np.asarray(points, dtype=np.float64)
    e = _unique_edges(tris)
    return np.linalg.norm(p[e[:, 0]] - p[e[:, 1]], axis=1)


def _unique_edges(tris) -> np.ndarray:
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    e.sort(axis=1)
    return np.unique(e, axis=0)


def _edge_table(tris):
    """``(edges, counts)`` of the undirected edges of the triangles."""
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    e.sort(axis=1)
    if not len(e):
        return e.reshape(0, 2), np.zeros(0, dtype=np.int64)
    return np.unique(e, axis=0, return_counts=True)


def surface_stats(points, tris) -> dict:
    """Count, area, mean edge and gamma of a triangulation."""
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if not len(tris):
        return {'triangles': 0, 'points': 0, 'area': 0.0,
                'mean_edge': None, 'gamma_min': None, 'gamma_mean': None}
    gamma = tri_gamma(points, tris)
    return {'triangles': int(len(tris)), 'points': int(len(np.unique(tris))),
            'area': float(tri_areas(points, tris).sum()),
            'mean_edge': float(edge_lengths(points, tris).mean()),
            'gamma_min': float(gamma.min()), 'gamma_mean': float(gamma.mean())}


# -- distances to a triangulation ------------------------------------------------- #

def closest_on_triangles(p, a, b, c) -> np.ndarray:
    """Closest point to each ``p[i]`` on triangle ``(a[i], b[i], c[i])``."""
    ab, ac = b - a, c - a
    ap = p - a
    d1, d2 = (ab * ap).sum(1), (ac * ap).sum(1)
    bp = p - b
    d3, d4 = (ab * bp).sum(1), (ac * bp).sum(1)
    cp = p - c
    d5, d6 = (ab * cp).sum(1), (ac * cp).sum(1)
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    total = va + vb + vc
    total[total == 0] = 1.0
    out = a + ab * (vb / total)[:, None] + ac * (vc / total)[:, None]
    # Regions, lowest priority first so the vertex regions win.
    with np.errstate(divide='ignore', invalid='ignore'):
        m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        out[m] = (b + (c - b) * w[:, None])[m]
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        w = d2 / (d2 - d6)
        out[m] = (a + ac * w[:, None])[m]
        m = (d6 >= 0) & (d5 <= d6)
        out[m] = c[m]
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        w = d1 / (d1 - d3)
        out[m] = (a + ab * w[:, None])[m]
        m = (d3 >= 0) & (d4 <= d3)
        out[m] = b[m]
        m = (d1 <= 0) & (d2 <= 0)
        out[m] = a[m]
    return out


def _tree(points):
    try:
        from scipy.spatial import cKDTree
    except ImportError:                                # pragma: no cover
        return None
    return cKDTree(points)


def nearest_on_mesh(points, mesh_points, mesh_tris, neighbours: int = 16):
    """``(distance, triangle index, closest point)`` of *points* to a mesh.

    The candidates are the triangles whose centroids are nearest (a k-d tree);
    the distance to each is exact. With a very elongated triangle the nearest
    centroid may not be the nearest triangle, so ``neighbours`` is generous and
    a distance can only be too large, never too small.
    """
    q = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    t = _xyz(mesh_points, mesh_tris)
    count = len(t)
    if not count or not len(q):
        return (np.full(len(q), np.inf), np.full(len(q), -1, dtype=np.int64),
                q.copy())
    centroids = t.mean(axis=1)
    k = min(int(neighbours), count)
    tree = _tree(centroids)
    best = np.full(len(q), np.inf)
    where = np.full(len(q), -1, dtype=np.int64)
    near = q.copy()
    step = 20000
    for start in range(0, len(q), step):
        chunk = q[start:start + step]
        if tree is not None:
            _d, cand = tree.query(chunk, k=k)
            cand = np.asarray(cand).reshape(len(chunk), k)
        else:                                          # pragma: no cover
            dist = np.linalg.norm(chunk[:, None, :] - centroids[None], axis=2)
            cand = np.argsort(dist, axis=1)[:, :k]
        for j in range(k):
            idx = cand[:, j]
            tri = t[idx]
            c = closest_on_triangles(chunk, tri[:, 0], tri[:, 1], tri[:, 2])
            d = np.linalg.norm(chunk - c, axis=1)
            better = d < best[start:start + step]
            sl = slice(start, start + len(chunk))
            best[sl] = np.where(better, d, best[sl])
            where[sl] = np.where(better, idx, where[sl])
            near[sl][better] = c[better]
    return best, where, near


def _point_segment_distance(p, a, b):
    ab = b - a
    denom = (ab * ab).sum(1)
    denom[denom == 0] = 1.0
    w = np.clip(((p - a) * ab).sum(1) / denom, 0.0, 1.0)
    return np.linalg.norm(p - (a + ab * w[:, None]), axis=1)


def distance_to_segments(points, seg_a, seg_b, neighbours: int = 8):
    """Distance of each point to the nearest of a set of segments."""
    q = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    a = np.asarray(seg_a, dtype=np.float64).reshape(-1, 3)
    b = np.asarray(seg_b, dtype=np.float64).reshape(-1, 3)
    if not len(a) or not len(q):
        return np.full(len(q), np.inf)
    mids = 0.5 * (a + b)
    k = min(neighbours, len(a))
    tree = _tree(mids)
    best = np.full(len(q), np.inf)
    for start in range(0, len(q), 20000):
        chunk = q[start:start + 20000]
        if tree is not None:
            _d, cand = tree.query(chunk, k=k)
            cand = np.asarray(cand).reshape(len(chunk), k)
        else:                                          # pragma: no cover
            dist = np.linalg.norm(chunk[:, None, :] - mids[None], axis=2)
            cand = np.argsort(dist, axis=1)[:, :k]
        cur = np.full(len(chunk), np.inf)
        for j in range(k):
            idx = cand[:, j]
            cur = np.minimum(cur, _point_segment_distance(
                chunk, a[idx], b[idx]))
        best[start:start + len(chunk)] = cur
    return best


def deviation(old_points, old_tris, new_points, new_tris) -> dict:
    """Distance between two triangulations of the same surface, both ways.

    Vertices and triangle centroids of each are measured to the other, so a
    mesh cannot hide a gap between its vertices.
    """
    def samples(points, tris):
        t = _xyz(points, tris)
        return np.concatenate([np.asarray(points, dtype=np.float64)[
            np.unique(np.asarray(tris, dtype=np.int64))], t.mean(axis=1)])

    to_old = nearest_on_mesh(samples(new_points, new_tris), old_points,
                             old_tris)[0]
    to_new = nearest_on_mesh(samples(old_points, old_tris), new_points,
                             new_tris)[0]
    both = np.concatenate([to_old, to_new])
    return {'new_to_old_max': float(to_old.max()),
            'old_to_new_max': float(to_new.max()),
            'max': float(both.max()), 'mean': float(both.mean()),
            'p95': float(np.percentile(both, 95)), 'samples': int(len(both))}


# -- borders, loops and components ------------------------------------------------ #

def boundary_halfedges(tris) -> np.ndarray:
    """Directed edges ``(a, b)`` used by exactly one triangle, as it runs."""
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if not len(t):
        return np.zeros((0, 2), dtype=np.int64)
    half = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    key = np.sort(half, axis=1)
    _u, inverse, counts = np.unique(key, axis=0, return_inverse=True,
                                    return_counts=True)
    return half[counts[inverse.reshape(-1)] == 1]


def ordered_loops(half) -> list:
    """Closed loops of node ids from directed boundary edges, or raise.

    A border that touches itself (a node leaving twice) is not a simple loop;
    that is reported, not guessed at.
    """
    nxt = {}
    for a, b in np.asarray(half, dtype=np.int64).reshape(-1, 2):
        a, b = int(a), int(b)
        if a in nxt:
            raise CvtRefusal('a border touches itself at node %d' % a)
        nxt[a] = b
    seen, loops = set(), []
    for start in list(nxt):
        if start in seen:
            continue
        loop, cur = [], start
        while cur not in seen:
            seen.add(cur)
            loop.append(cur)
            cur = nxt.get(cur)
            if cur is None:
                raise CvtRefusal('a border is open at node %d' % loop[-1])
        if cur != start:
            raise CvtRefusal('a border does not close')
        loops.append(loop)
    return loops


def components(tris) -> int:
    """Number of edge-connected groups of triangles."""
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if not len(t):
        return 0
    parent = list(range(len(t)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first = {}
    for i, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            if key in first:
                ra, rb = find(first[key]), find(i)
                if ra != rb:
                    parent[ra] = rb
            else:
                first[key] = i
    return len({find(i) for i in range(len(t))})


def _chains(edges) -> int:
    """Number of connected chains in a set of undirected node-id edges."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return len({find(x) for x in list(parent)})


# -- the replacement contract ----------------------------------------------------- #

def consistent_orientation(tris) -> np.ndarray:
    """Turn the triangles so neighbours run opposite ways along a shared edge.

    The plugin's triangles come out in no particular winding. A pass from a
    seed triangle over edges used by exactly two triangles flips whichever
    neighbour disagrees; each connected piece is then consistent in itself.
    """
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3).copy()
    owners = {}
    for i, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            owners.setdefault((int(min(a, b)), int(max(a, b))), []).append(i)
    done = np.zeros(len(t), dtype=bool)
    for seed in range(len(t)):
        if done[seed]:
            continue
        done[seed] = True
        stack = [seed]
        while stack:
            i = stack.pop()
            tri = t[i]
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]),
                         (tri[2], tri[0])):
                group = owners[(int(min(a, b)), int(max(a, b)))]
                if len(group) != 2:
                    continue
                j = group[0] if group[1] == i else group[1]
                if done[j]:
                    continue
                other = t[j]
                same = any((int(x), int(y)) == (int(a), int(b)) for x, y in
                           ((other[0], other[1]), (other[1], other[2]),
                            (other[2], other[0])))
                if same:
                    t[j] = other[[0, 2, 1]]
                done[j] = True
                stack.append(j)
    return t


def _orient_like(points, tris, old_points, old_tris):
    """Make *tris* consistent and facing like the surface they replace.

    Each connected piece is flipped as a whole when its area-weighted normal
    runs against the nearest original triangles.
    """
    t = consistent_orientation(tris)
    if not len(t):
        return t, False
    c = _xyz(points, t).mean(axis=1)
    _d, idx, _p = nearest_on_mesh(c, old_points, old_tris)
    dots = (tri_normals(points, t) * tri_normals(old_points, old_tris)[idx]
            ).sum(axis=1) * tri_areas(points, t)
    # connected pieces: union over shared edges
    parent = list(range(len(t)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first = {}
    for i, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (int(min(a, b)), int(max(a, b)))
            if key in first:
                ra, rb = find(first[key]), find(i)
                if ra != rb:
                    parent[ra] = rb
            else:
                first[key] = i
    roots = np.array([find(i) for i in range(len(t))])
    flipped = False
    for root in np.unique(roots):
        mask = roots == root
        if dots[mask].sum() < 0:
            t[mask] = t[mask][:, [0, 2, 1]]
            flipped = True
    return t, flipped


def resplit(new_points, new_tris, old_points, old_tris, old_face) -> np.ndarray:
    """The original face each new triangle lies closest to (by centroid)."""
    c = _xyz(new_points, new_tris).mean(axis=1)
    _d, idx, _p = nearest_on_mesh(c, old_points, old_tris)
    return np.asarray(old_face)[idx]


def _loop_distance(loop_a, xyz_a, loop_b, xyz_b) -> float:
    """Mean distance between two closed loops of nodes, both ways round."""
    pa, pb = xyz_a[loop_a], xyz_b[loop_b]
    to_b = distance_to_segments(pa, pb, np.roll(pb, -1, axis=0))
    to_a = distance_to_segments(pb, pa, np.roll(pa, -1, axis=0))
    return float(0.5 * (np.mean(to_b) + np.mean(to_a)))


def _pair_loops(old_loops, old_xyz, new_loops, new_xyz) -> list:
    """Match each fixed loop with the new loop that lies along it, or raise.

    The loops are compared by their shape, not by their centres: two walls
    that share an axis (the inner and the outer wall of a jacket) have the
    same centre at each end, and pairing by centre joined a wall to the loop
    of the other one.
    """
    if len(old_loops) != len(new_loops):
        raise CvtRefusal('the border has %d loops and the new surface has %d'
                         % (len(old_loops), len(new_loops)))
    cost = np.array([[_loop_distance(o, old_xyz, n, new_xyz)
                      for n in new_loops] for o in old_loops])
    if not cost.size:
        return []
    try:
        from scipy.optimize import linear_sum_assignment
        rows, cols = linear_sum_assignment(cost)
        return [(int(i), int(j)) for i, j in zip(rows, cols)]
    except ImportError:                                # pragma: no cover
        free = list(range(len(new_loops)))
        pairs = []
        for i in range(len(old_loops)):
            j = min(free, key=lambda k: cost[i, k])
            free.remove(j)
            pairs.append((i, j))
        return pairs


def _zipper(old_loop, new_loop, xyz):
    """Strip between two loops that run the same way.

    Returns ``[(kind, a, b, c)]`` as triangle node ids, ``kind`` 'o' for a
    triangle standing on an old edge. Both loops keep the picked region on
    their left, the strip lies left of the old loop and right of the new one,
    so ``(O[i], O[i+1], N[j])`` and ``(N[j+1], N[j], O[i])`` face the same way
    as the surface.
    """
    no, nn = len(old_loop), len(new_loop)
    start = int(np.argmin(np.linalg.norm(
        xyz[new_loop] - xyz[old_loop[0]], axis=1)))
    new_loop = new_loop[start:] + new_loop[:start]
    i = j = 0
    out = []
    while i < no or j < nn:
        o0, o1 = old_loop[i % no], old_loop[(i + 1) % no]
        n0, n1 = new_loop[j % nn], new_loop[(j + 1) % nn]
        if i >= no:
            advance_old = False
        elif j >= nn:
            advance_old = True
        else:
            advance_old = (np.linalg.norm(xyz[o1] - xyz[n0])
                           <= np.linalg.norm(xyz[n1] - xyz[o0]))
        if advance_old:
            out.append(('o', o0, o1, n0, i % no))
            i += 1
        else:
            out.append(('n', n1, n0, o0, i % no))
            j += 1
    return out


#: A triangle of at least this gamma is well shaped; the stitch is held to the
#: smaller of this and the smallest gamma of the remeshed surface itself.
STITCH_GAMMA_GOOD = 0.5
#: Rings of triangles around the stitched strip that may be flipped and smoothed.
STITCH_RINGS = 3
STITCH_PASSES = 30


def _tri_gamma1(xyz, tri) -> float:
    a, b, c = (xyz[int(v)] for v in tri)
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cx = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
          ab[0] * ac[1] - ab[1] * ac[0])
    area2 = cx[0] * cx[0] + cx[1] * cx[1] + cx[2] * cx[2]     # (2 * area)^2
    la = math.dist(b, c)
    lb = math.dist(a, c)
    lc = math.dist(a, b)
    bottom = (la + lb + lc) * la * lb * lc
    return 4.0 * area2 / bottom if bottom > 0 else 0.0


def improve_stitch(points, tris, face, fixed, strip, ref_points, ref_tris,
                   rings: int = STITCH_RINGS, passes: int = STITCH_PASSES,
                   goal=None, max_stages: int = 40):
    """Flip and smooth the triangles around the stitched strip.

    The strip joins the CVT surface to the fixed border and its triangles are
    long where the two node spacings differ. Within ``rings`` triangles of it
    (the rest of the CVT surface is the plugin's and is left alone) an edge is
    flipped when that raises the smaller gamma of its two triangles, and a free
    node moves to the mean of its neighbours, projected back onto the original
    surface, when that raises the smallest gamma of its star. Nothing changes
    that would turn a triangle over, move a border node (``fixed``), or flip
    an edge between two faces. When ``goal`` is given and the smallest gamma
    of the region is still below it once nothing improves, the region grows by
    ``rings`` more triangles and the work goes on. Returns
    ``(points, tris, flips, moves)``.
    """
    tris = np.asarray(tris, dtype=np.int64).copy()
    points = np.asarray(points, dtype=np.float64).copy()
    face = np.asarray(face)
    n_tri = len(tris)
    if not len(strip):
        return points, tris, 0, 0
    region_nodes = set(int(v) for v in np.unique(tris[strip]))
    for _ in range(rings):
        touch = np.isin(tris, list(region_nodes)).any(axis=1)
        region_nodes.update(int(v) for v in np.unique(tris[touch]))
    in_region = np.isin(tris, list(region_nodes)).all(axis=1)

    def normal(pts, tri):
        a, b, c = (pts[int(v)] for v in tri)
        n = np.cross(b - a, c - a)
        norm = math.sqrt(float(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]))
        return n / norm if norm > 0 else n * 0.0

    edge_tris = {}

    def link(t):
        a, b, c = (int(v) for v in tris[t])
        for u, v in ((a, b), (b, c), (c, a)):
            edge_tris.setdefault((min(u, v), max(u, v)), set()).add(t)

    def unlink(t):
        a, b, c = (int(v) for v in tris[t])
        for u, v in ((a, b), (b, c), (c, a)):
            key = (min(u, v), max(u, v))
            edge_tris[key].discard(t)
            if not edge_tris[key]:
                del edge_tris[key]

    for t in range(n_tri):
        link(t)
    flips = moves = 0
    for _stage in range(max_stages):
        for _round in range(passes):
            changed = 0
            # --- edge flips ---------------------------------------------------
            for (u, v), pair in sorted(edge_tris.items()):
                if len(pair) != 2:
                    continue
                t1, t2 = sorted(pair)
                if not (in_region[t1] and in_region[t2]) or face[t1] != face[t2]:
                    continue
                c1 = [int(x) for x in tris[t1]]
                c2 = [int(x) for x in tris[t2]]
                o1 = [x for x in c1 if x not in (u, v)][0]
                o2 = [x for x in c2 if x not in (u, v)][0]
                if o1 == o2 or (min(o1, o2), max(o1, o2)) in edge_tris:
                    continue
                # rotate each triangle so the shared edge is (a, b) / (b, a)
                r1 = c1[c1.index(u):] + c1[:c1.index(u)]
                if r1[1] == v:
                    a, b, c, d = u, v, o1, o2
                else:
                    a, b, c, d = v, u, o1, o2
                new1, new2 = [c, a, d], [c, d, b]
                old_min = min(_tri_gamma1(points, c1), _tri_gamma1(points, c2))
                new_min = min(_tri_gamma1(points, new1), _tri_gamma1(points, new2))
                if new_min <= old_min * 1.02 + 1e-12:
                    continue
                ref = normal(points, c1) + normal(points, c2)
                ref = ref / max(np.linalg.norm(ref), 1e-300)
                if (float(np.dot(normal(points, new1), ref)) <= 0.1
                        or float(np.dot(normal(points, new2), ref)) <= 0.1):
                    continue
                unlink(t1)
                unlink(t2)
                tris[t1], tris[t2] = new1, new2
                link(t1)
                link(t2)
                flips += 1
                changed += 1
            # --- smoothing ----------------------------------------------------
            star = {}
            for t in range(n_tri):
                if in_region[t]:
                    for v in tris[t]:
                        star.setdefault(int(v), []).append(t)
            free = [v for v in sorted(star) if not fixed[v]
                    and v in region_nodes
                    and len({int(face[t]) for t in star[v]}) == 1
                    and all(len(edge_tris[(min(v, int(w)), max(v, int(w)))]) == 2
                            for t in star[v] for w in tris[t] if int(w) != v)]
            if free:
                target = np.empty((len(free), 3))
                for i, v in enumerate(free):
                    nb = {int(w) for t in star[v] for w in tris[t]} - {v}
                    target[i] = points[sorted(nb)].mean(axis=0)
                target = nearest_on_mesh(target, ref_points, ref_tris)[2]
                for i, v in enumerate(free):
                    ring = [tris[t] for t in star[v]]
                    before = min(_tri_gamma1(points, r) for r in ring)
                    was = [normal(points, r) for r in ring]
                    keep = points[v].copy()
                    points[v] = target[i]
                    after = min(_tri_gamma1(points, r) for r in ring)
                    if after <= before + 1e-12 or any(
                            float(np.dot(normal(points, r), n0)) <= 0.1
                            for r, n0 in zip(ring, was)):
                        points[v] = keep
                    else:
                        moves += 1
                        changed += 1
            if not changed:
                break
        low = np.flatnonzero(~in_region)
        inside = np.flatnonzero(
            in_region | np.isin(tris, list(region_nodes)).any(axis=1))
        reached = (goal is None
                   or float(tri_gamma(points, tris[inside]).min()) >= goal)
        if reached or not len(low):
            break
        region_nodes.update(int(v) for v in np.unique(
            tris[np.isin(tris, list(region_nodes)).any(axis=1)]))
        in_region[:] = np.isin(tris, list(region_nodes)).all(axis=1)
    return points, tris, flips, moves


def replace_picked(job: dict, raw_points, raw_tris, tolerance=None) -> dict:
    """Re-split the plugin's surface onto the original faces and stitch it.

    ``job`` holds the numpy arrays of the model surface mesh (see
    :func:`load_job`). The result carries the final triangles, the face of
    each, the id of every point (an old node id for a kept border node, -1 for
    a new one) and the list of checks with ``ok`` and a ``detail``.
    """
    picked = job['picked_tris']
    p_xyz = job['xyz_of']
    old_tris = picked['tris']
    old_face = picked['face']
    mean_edge = float(edge_lengths(raw_points, raw_tris).mean())
    tol = float(tolerance) if tolerance else DEVIATION_EDGES * mean_edge
    checks = []

    def check(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
        return ok

    raw_tris, flipped = _orient_like(raw_points, raw_tris, p_xyz, old_tris)

    # The border of the picked region is fixed: the faces left alone keep
    # their nodes, so the new surface is cut back from it and a strip of
    # triangles joins the two.
    half = boundary_halfedges(old_tris)
    new_points = np.asarray(raw_points, dtype=np.float64)
    old_loops, loops_new, trimmed, problem = [], [], raw_tris, ''
    try:
        old_loops = ordered_loops(half)
        if len(half):
            near = distance_to_segments(new_points, p_xyz[half[:, 0]],
                                        p_xyz[half[:, 1]])
            keep = (near[raw_tris] > COLLAR_EDGES * mean_edge).all(axis=1)
            trimmed = _simple_border(_largest_pieces(raw_tris[keep]))
            loops_new = ordered_loops(boundary_halfedges(trimmed))
        pairs = (_pair_loops(old_loops, p_xyz, loops_new, new_points)
                 if len(half) else [])
    except CvtRefusal as error:
        problem = str(error)
        pairs = []
    check('boundary_loops', not problem,
          problem or 'fixed border loops: %d' % len(old_loops))
    if problem:
        return {'ok': False, 'checks': checks, 'tolerance': tol,
                'flipped': flipped, 'failed': _failed(checks)}

    # one point table: the kept new points, then the old border nodes
    used = np.unique(trimmed)
    remap_new = -np.ones(len(new_points), dtype=np.int64)
    remap_new[used] = np.arange(len(used))
    border_nodes = sorted({n for loop in old_loops for n in loop})
    remap_old = {n: len(used) + k for k, n in enumerate(border_nodes)}
    final_points = np.concatenate([new_points[used], p_xyz[border_nodes]])
    final_id = np.concatenate([
        -np.ones(len(used), dtype=np.int64), job['node_id_of'][border_nodes]])
    tri_rows = [remap_new[trimmed]]
    face_rows = [resplit(new_points, trimmed, p_xyz, old_tris, old_face)]
    edge_face = _edge_faces(old_tris, old_face)
    strip_tris, strip_faces = [], []
    for oi, nj in pairs:
        o_loop = [remap_old[n] for n in old_loops[oi]]
        n_loop = [int(remap_new[v]) for v in loops_new[nj]]
        for _kind, a, b, c, at in _zipper(o_loop, n_loop, final_points):
            strip_tris.append((a, b, c))
            o_a = old_loops[oi][at]
            o_b = old_loops[oi][(at + 1) % len(old_loops[oi])]
            strip_faces.append(edge_face[(min(o_a, o_b), max(o_a, o_b))])
    inverted_strips = 0
    if strip_tris:
        st = np.asarray(strip_tris, dtype=np.int64)
        tri_rows.append(st)
        face_rows.append(np.asarray(strip_faces, dtype=np.int64))
        ref = tri_normals(p_xyz, old_tris)
        ref_idx = nearest_on_mesh(_xyz(final_points, st).mean(axis=1), p_xyz,
                                  old_tris)[1]
        inverted_strips = int(((tri_normals(final_points, st) * ref[ref_idx]
                                ).sum(axis=1) <= 0).sum())
    final_tris = np.concatenate(tri_rows)
    final_face = np.concatenate(face_rows)
    gamma_raw = (float(tri_gamma(new_points, trimmed).min())
                 if len(trimmed) else None)
    gamma_stitched = float(tri_gamma(final_points, final_tris).min())
    goal = (min(gamma_raw, STITCH_GAMMA_GOOD) if gamma_raw is not None
            else STITCH_GAMMA_GOOD)
    flips = moves = 0
    if strip_tris:
        final_points, final_tris, flips, moves = improve_stitch(
            final_points, final_tris, final_face, final_id >= 0,
            np.arange(len(trimmed), len(final_tris)), p_xyz, old_tris,
            goal=goal)
    # --- the checks of the contract ----------------------------------------
    faces = sorted({int(f) for f in np.unique(old_face)})
    have = {int(f) for f in np.unique(final_face)}
    missing = [f for f in faces if f not in have]
    check('face_kept', not missing,
          'every original face keeps triangles' if not missing else
          'no triangle would be left on faces %s' % ', '.join(
              str(f) for f in missing))

    split = []
    for f in faces:
        before = components(old_tris[old_face == f])
        after = components(final_tris[final_face == f])
        if after != before:
            split.append('face %d falls into %d pieces (was %d)'
                         % (f, after, before))
    check('face_connected', not split,
          'each face is as connected as it was' if not split
          else '; '.join(split))

    # fixed border: every old border edge is an edge of the new surface
    f_edges, f_counts = _edge_table(final_tris)
    present = {(int(a), int(b)) for a, b in f_edges}
    lost = 0
    for loop in old_loops:
        for k in range(len(loop)):
            a = remap_old[loop[k]]
            b = remap_old[loop[(k + 1) % len(loop)]]
            if (min(a, b), max(a, b)) not in present:
                lost += 1
    check('interfaces_unpicked', lost == 0,
          'the border with the faces left alone is kept node for node'
          if lost == 0 else 'border edges with no triangle: %d (a gap)' % lost)

    _interfaces_between_picked(job, final_points, final_tris, final_face,
                               tol, check)

    over = int((f_counts > 2).sum())
    check('no_overlap', over == 0,
          'no edge is shared by more than two triangles' if over == 0
          else 'edges shared by more than two triangles: %d' % over)

    inv_raw = 0
    if len(trimmed):
        ref = tri_normals(p_xyz, old_tris)
        idx = nearest_on_mesh(_xyz(new_points, trimmed).mean(axis=1), p_xyz,
                              old_tris)[1]
        inv_raw = int(((tri_normals(new_points, trimmed) * ref[idx]).sum(
            axis=1) <= 0).sum())
    fraction = inv_raw / max(1, len(trimmed))
    check('orientation', inverted_strips == 0
          and fraction <= INVERTED_FRACTION_LIMIT,
          'no triangle faces against the surface it replaces'
          if inverted_strips == 0 and fraction <= INVERTED_FRACTION_LIMIT
          else 'triangles facing the wrong way: %d stitched and %d remeshed'
          % (inverted_strips, inv_raw))

    gamma_final = float(tri_gamma(final_points, final_tris).min())
    check('stitch_quality', gamma_final >= goal - 1e-9,
          'the smallest gamma is %.3f after stitching (%.3f before the '
          'strip was improved; %.3f is the floor: the smaller of %.1f and '
          'the remeshed surface alone; %d flips, %d moves)'
          % (gamma_final, gamma_stitched, goal, STITCH_GAMMA_GOOD, flips,
             moves))

    dev = deviation(p_xyz, old_tris, final_points, final_tris)
    check('deviation', dev['max'] <= tol,
          'the surface moved by at most %.4g (limit %.4g)' % (dev['max'], tol)
          if dev['max'] <= tol else
          'the surface moved by %.4g, more than the limit %.4g'
          % (dev['max'], tol))

    return {'ok': all(c['ok'] for c in checks), 'checks': checks,
            'tolerance': tol, 'flipped': flipped,
            'points': final_points, 'tris': final_tris, 'face': final_face,
            'node_id': final_id, 'deviation': dev,
            'stitch': {'gamma_min': gamma_final, 'gamma_before': gamma_stitched,
                       'gamma_remeshed': gamma_raw, 'floor': goal, 'flips': flips,
                       'moves': moves},
            'failed': _failed(checks)}


def _failed(checks) -> list:
    return [c for c in checks if not c['ok']]


def _largest_pieces(tris):
    """Keep the edge-connected pieces holding at least 5 % of the triangles."""
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if not len(t):
        raise CvtRefusal('the collar at the fixed border removed everything')
    parent = list(range(len(t)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first = {}
    for i, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            if key in first:
                ra, rb = find(first[key]), find(i)
                if ra != rb:
                    parent[ra] = rb
            else:
                first[key] = i
    roots = np.array([find(i) for i in range(len(t))])
    uniq, counts = np.unique(roots, return_counts=True)
    big = uniq[counts >= 0.05 * counts.max()]
    return t[np.isin(roots, big)]


def _simple_border(tris, rounds: int = 50):
    """Cut pinches and islands until the border is a set of simple loops.

    A node where the border leaves twice (two fans of triangles meeting at one
    point) is cut by keeping its largest fan. The cut is local: only triangles
    of the smaller fans go.
    """
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    for _ in range(rounds):
        half = boundary_halfedges(t)
        out = {}
        for a, _b in half:
            out[int(a)] = out.get(int(a), 0) + 1
        pinches = {a for a, c in out.items() if c > 1}
        if not pinches:
            return t
        drop = np.zeros(len(t), dtype=bool)
        for v in pinches:
            idx = np.flatnonzero((t == v).any(axis=1))
            parent = {int(i): int(i) for i in idx}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            by_edge = {}
            for i in idx:
                for other in t[i]:
                    if other != v:
                        by_edge.setdefault(int(other), []).append(int(i))
            for group in by_edge.values():
                for i in group[1:]:
                    ra, rb = find(group[0]), find(i)
                    if ra != rb:
                        parent[ra] = rb
            roots = {}
            for i in idx:
                roots.setdefault(find(int(i)), []).append(int(i))
            biggest = max(roots.values(), key=len)
            for members in roots.values():
                if members is not biggest:
                    drop[members] = True
        t = t[~drop]
        t = _largest_pieces(t)
    raise CvtRefusal('the cut-back border of the new surface stays pinched')


def _edge_faces(tris, face) -> dict:
    """``{(a, b) sorted: face}`` of the picked triangles."""
    out = {}
    for tri, f in zip(np.asarray(tris), np.asarray(face)):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            out[(int(min(a, b)), int(max(a, b)))] = int(f)
    return out


def _interfaces_between_picked(job, points, tris, face, tol, check):
    """Faces that met still share one curve, and it stays where it was."""
    old_tris, old_face = job['picked_tris']['tris'], job['picked_tris']['face']
    xyz = job['xyz_of']

    def interface_edges(t, f):
        owners = {}
        for tri, fa in zip(t, f):
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                owners.setdefault((int(min(a, b)), int(max(a, b))),
                                  set()).add(int(fa))
        pairs = {}
        for edge, who in owners.items():
            if len(who) == 2:
                pairs.setdefault(tuple(sorted(who)), []).append(edge)
        return pairs

    before = interface_edges(old_tris, old_face)
    after = interface_edges(tris, face)
    problems = []
    worst = 0.0
    for pair in sorted(set(before) | set(after)):
        old_edges, new_edges = before.get(pair), after.get(pair)
        if old_edges is None:
            problems.append('faces %d and %d now touch but did not' % pair)
            continue
        if new_edges is None:
            problems.append('faces %d and %d no longer share a curve' % pair)
            continue
        if _chains(old_edges) != _chains(new_edges):
            problems.append('curves shared by faces %d and %d: %d, was %d' % (
                pair + (_chains(new_edges), _chains(old_edges))))
        a = xyz[[e[0] for e in old_edges]]
        b = xyz[[e[1] for e in old_edges]]
        nodes = np.unique(np.asarray(new_edges).reshape(-1))
        d = distance_to_segments(points[nodes], a, b)
        worst = max(worst, float(d.max()))
        if float(d.max()) > tol:
            problems.append('the curve between faces %d and %d moved by %.4g '
                            '(limit %.4g)' % (pair + (float(d.max()), tol)))
    check('interfaces_picked', not problems,
          ('faces that met share one curve per original curve (largest move '
           '%.4g)' % worst) if not problems else '; '.join(problems))


def closed_volumes(job: dict, points, tris, face, node_id) -> dict:
    """Edge balance of the model after the replacement.

    ``job['volumes']`` maps a volume to its surface faces. A volume that was
    watertight must still be: every edge of its surfaces must still be used
    exactly twice. Without volumes the whole surface mesh is read as one shell.
    """
    ctx = job['context_edges']     # {face: (E,2) array of node ids}
    picked_faces = set(int(f) for f in np.unique(job['picked_tris']['face']))
    ids = np.concatenate([node_id])

    def edges_of(face_list, with_new):
        parts = [ctx[f] for f in face_list if f in ctx and f not in picked_faces]
        if with_new:
            sel = np.isin(face, [f for f in face_list if f in picked_faces])
            t = tris[sel]
            if len(t):
                # translate to model ids: new points get ids >= big offset
                mapped = np.where(node_id[t] >= 0, node_id[t],
                                  (1 << 40) + t)
                parts.append(np.concatenate([mapped[:, [0, 1]],
                                             mapped[:, [1, 2]],
                                             mapped[:, [2, 0]]]))
        else:
            for f in face_list:
                if f in picked_faces:
                    parts.append(job['picked_edges'][f])
        if not parts:
            return np.zeros((0, 2), dtype=np.int64)
        e = np.concatenate([np.asarray(p, dtype=np.int64) for p in parts])
        e.sort(axis=1)
        return e

    def open_edges(e):
        if not len(e):
            return 0
        _u, c = np.unique(e, axis=0, return_counts=True)
        return int((c != 2).sum())

    volumes = job['volumes'] or {0: sorted(set(ctx) | picked_faces)}
    rows = []
    for vol, faces in sorted(volumes.items()):
        if not any(f in picked_faces for f in faces):
            continue
        b = open_edges(edges_of(faces, False))
        a = open_edges(edges_of(faces, True))
        rows.append({'volume': int(vol), 'open_before': b, 'open_after': a,
                     'closed_before': b == 0, 'closed_after': a == 0})
    del ids
    return {'volumes': rows,
            'ok': all(r['open_after'] <= r['open_before'] for r in rows)}


# -- the job on disk -------------------------------------------------------------- #

def load_job(job_dir: str) -> tuple:
    """``(meta, job)``: the options and the arrays of the surface mesh."""
    with open(os.path.join(job_dir, 'job.json'), encoding='utf-8') as handle:
        meta = json.load(handle)
    data = np.load(os.path.join(job_dir, 'job.npz'))
    node_ids = data['node_ids'].astype(np.int64)
    xyz = data['node_xyz'].astype(np.float64).reshape(-1, 3)
    row = {int(n): i for i, n in enumerate(node_ids)}
    tri_nodes = data['tri_nodes'].astype(np.int64).reshape(-1, 3)
    tri_face = data['tri_face'].astype(np.int64)
    quad_nodes = data['quad_nodes'].astype(np.int64).reshape(-1, 4)
    quad_face = data['quad_face'].astype(np.int64)
    picked = [int(f) for f in meta['picked']]
    if not picked:
        raise CvtRefusal('no surface was picked')
    pick_mask = np.isin(tri_face, picked)
    if not pick_mask.any():
        raise CvtRefusal('the picked surfaces hold no triangles')
    if np.isin(quad_face, picked).any():
        raise CvtRefusal('a picked surface holds quadrilaterals; the CVT '
                         'remesher reads triangles only')
    local = np.vectorize(row.__getitem__, otypes=[np.int64])
    p_tri_ids = tri_nodes[pick_mask]
    used_ids = np.unique(p_tri_ids)
    used_rows = local(used_ids)
    xyz_of = xyz[used_rows]
    index_of = {int(n): i for i, n in enumerate(used_ids)}
    p_tris = np.vectorize(index_of.__getitem__, otypes=[np.int64])(
        p_tri_ids).reshape(-1, 3)
    ctx = {}
    for source_nodes, source_face, width in ((tri_nodes, tri_face, 3),
                                             (quad_nodes, quad_face, 4)):
        for f in np.unique(source_face):
            sel = source_nodes[source_face == f]
            cols = [(i, (i + 1) % width) for i in range(width)]
            e = np.concatenate([sel[:, list(c)] for c in cols])
            ctx.setdefault(int(f), []).append(e)
    context_edges = {f: np.sort(np.concatenate(parts), axis=1)
                     for f, parts in ctx.items()}
    picked_edges = {f: context_edges[f] for f in picked if f in context_edges}
    volumes = {int(k): [int(x) for x in v]
               for k, v in (meta.get('volumes') or {}).items()}
    return meta, {
        'picked': picked,
        'picked_tris': {'tris': p_tris, 'face': tri_face[pick_mask]},
        'xyz_of': xyz_of, 'node_id_of': used_ids,
        'context_edges': context_edges, 'picked_edges': picked_edges,
        'volumes': volumes,
    }


# -- the Gmsh run ----------------------------------------------------------------- #

def run_cvt(gmsh, points, tris, options: dict, log=None) -> tuple:
    """Plugin(CVTRemesh) on exactly this triangulation; ``(points, tris)``.

    The plugin evaluates the surface, which a bare mesh only supports once it
    is classified and given a parametrisation (measured: it stops the process
    on a merged STL otherwise), so the same two calls as for an STL are made.
    """
    gmsh.option.setNumber('General.Terminal', 0)
    gmsh.model.add('cvt')
    gmsh.model.addDiscreteEntity(2, 1)
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    gmsh.model.mesh.addNodes(2, 1, list(range(1, len(p) + 1)),
                             p.reshape(-1).tolist())
    gmsh.model.mesh.addElementsByType(1, 2, [], (t + 1).reshape(-1).tolist())
    gmsh.model.mesh.classifySurfaces(math.pi, True, True)
    gmsh.model.mesh.createGeometry()
    before = set(gmsh.model.getEntities(2))
    for name, value in options.items():
        gmsh.plugin.setNumber('CVTRemesh', name, float(value))
    gmsh.plugin.run('CVTRemesh')
    new = [e for e in gmsh.model.getEntities(2) if e not in before]
    if not new:
        raise CvtRefusal('Gmsh returned no surface')
    tag = new[-1][1]
    node_tags, coords, _ = gmsh.model.mesh.getNodes(2, tag, True, False)
    kinds, _etags, enodes = gmsh.model.mesh.getElements(2, tag)
    conn = None
    for kind, nodes in zip(kinds, enodes):
        if int(kind) == 2:
            conn = np.asarray(nodes, dtype=np.int64)
    if conn is None or not len(node_tags):
        raise CvtRefusal('Gmsh returned a surface without triangles')
    node_tags = np.asarray(node_tags, dtype=np.int64)
    order = {int(n): i for i, n in enumerate(node_tags)}
    out_tris = np.vectorize(order.__getitem__, otypes=[np.int64])(
        conn).reshape(-1, 3)
    return np.asarray(coords, dtype=np.float64).reshape(-1, 3), out_tris


def component_labels(tris) -> np.ndarray:
    """Edge-connected group of each triangle, numbered from 0 in order of
    the first triangle of each group."""
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    parent = list(range(len(t)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first = {}
    for i, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            if key in first:
                ra, rb = find(first[key]), find(i)
                if ra != rb:
                    parent[ra] = rb
            else:
                first[key] = i
    number, out = {}, np.zeros(len(t), dtype=np.int64)
    for i in range(len(t)):
        out[i] = number.setdefault(find(i), len(number))
    return out


def run_cvt_parts(gmsh, points, tris, options: dict) -> tuple:
    """:func:`run_cvt` once per edge-connected piece of the picked surface.

    Plugin(CVTRemesh) sees one surface and its sites do not know which piece
    they lie on, so two walls that face each other across a gap come back
    fused (measured on the faces 1 and 4 of a jacketed pipe). Each piece is
    therefore remeshed on its own, with the same edge length and the same
    normal radius in model units, each in a model of its own (the plugin
    remeshes every surface of the current model). Returns
    ``(points, tris, pieces)``.
    """
    label = component_labels(tris)
    count = int(label.max()) + 1 if len(label) else 0
    if count <= 1:
        raw_points, raw_tris = run_cvt(gmsh, points, tris, options)
        return raw_points, raw_tris, 1
    total = float(tri_areas(points, tris).sum())
    length = radius_to_length(options['Normal computation radius'],
                              normalisation(points)[1])
    all_points, all_tris, offset = [], [], 0
    for k in range(count):
        part = tris[label == k]
        used = np.unique(part)
        local = -np.ones(len(points), dtype=np.int64)
        local[used] = np.arange(len(used))
        part_points = points[used]
        area = float(tri_areas(points, part).sum())
        mine = dict(options)
        mine['Sites'] = max(4.0, round(options['Sites'] * area / total))
        mine['Normal computation radius'] = length_to_radius(
            length, normalisation(part_points)[1])
        p, t = run_cvt(gmsh, part_points, local[part], mine)
        if gmsh is not None:
            # The plugin remeshes every surface of the current model: the
            # next piece must start in a model of its own.
            gmsh.model.remove()
        all_points.append(p)
        all_tris.append(t + offset)
        offset += len(p)
    return np.concatenate(all_points), np.concatenate(all_tris), count


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def execute(job_dir: str, gmsh=None) -> dict:
    """Run the job in *job_dir*; writes ``report.json`` and ``result.npz``."""
    started = time.time()
    meta, job = load_job(job_dir)
    options = dict(CVT_DEFAULTS)
    options.update({k: float(v) for k, v in (meta.get('options') or {}).items()
                    if k in CVT_DEFAULTS})
    points, tris = job['xyz_of'], job['picked_tris']['tris']
    area = float(tri_areas(points, tris).sum())
    edge = meta.get('target_edge_length')
    if edge:
        options['Sites'] = float(sites_for_edge_length(area, float(edge)))
    centre, scale = normalisation(points)
    report = {
        'area': area, 'sites': int(options['Sites']), 'options': options,
        'normalisation': {'centre': centre.tolist(), 'scale': scale,
                          'normal_radius_native': options[
                              'Normal computation radius'],
                          'normal_radius_length': radius_to_length(
                              options['Normal computation radius'], scale)},
        'before': surface_stats(points, tris), 'picked': job['picked'],
        'mode': meta.get('mode', 'keep'),
    }
    if gmsh is None:
        import gmsh
        gmsh.initialize()
    try:
        raw_points, raw_tris, pieces = run_cvt_parts(gmsh, points, tris,
                                                      options)
    finally:
        try:
            gmsh.finalize()
        except Exception:                              # noqa: BLE001
            pass
    raw_tris, _flipped = _orient_like(raw_points, raw_tris, points, tris)
    report['pieces'] = pieces
    report['raw'] = surface_stats(raw_points, raw_tris)
    report['deviation'] = deviation(points, tris, raw_points, raw_tris)
    tol = float(meta.get('tolerance') or 0) or (
        DEVIATION_EDGES * (report['raw']['mean_edge'] or 0.0))
    report['deviation']['tolerance'] = tol
    report['deviation']['within'] = report['deviation']['max'] <= tol
    arrays = {'raw_points': raw_points, 'raw_tris': raw_tris}
    report['replace'] = {'requested': report['mode'] == 'replace',
                         'ok': None, 'checks': [], 'failed': []}
    if report['mode'] == 'replace':
        done = replace_picked(job, raw_points, raw_tris, tol)
        shell = None
        if 'points' in done:
            shell = closed_volumes(job, done['points'], done['tris'],
                                   done['face'], done['node_id'])
            done['checks'].append({
                'name': 'closed_volumes', 'ok': bool(shell['ok']),
                'detail': ('every volume the replaced faces bound is as '
                           'closed as before' if shell['ok'] else
                           'a volume lost its closure: ' + ', '.join(
                               '%d (open edges %d, was %d)' % (
                                   r['volume'], r['open_after'],
                                   r['open_before'])
                               for r in shell['volumes']
                               if r['open_after'] > r['open_before']))})
            done['ok'] = all(c['ok'] for c in done['checks'])
            done['failed'] = _failed(done['checks'])
        report['replace'] = {
            'requested': True, 'ok': bool(done['ok']),
            'checks': done['checks'], 'failed': done['failed'],
            'volumes': (shell or {}).get('volumes', [])}
        if done['ok']:
            arrays.update(final_points=done['points'],
                          final_tris=done['tris'], final_face=done['face'],
                          final_node_id=done['node_id'])
            report['after'] = surface_stats(done['points'], done['tris'])
            report['deviation'] = dict(done['deviation'],
                                       tolerance=tol,
                                       within=done['deviation']['max'] <= tol)
        else:
            names = ', '.join(c['name'] for c in done['failed'])
            report['replace']['refusal'] = (
                'Replace is refused; failed checks: ' + names + '. '
                + '; '.join('%s: %s' % (c['name'], c['detail'])
                            for c in done['failed']))
    report['seconds'] = round(time.time() - started, 3)
    np.savez(os.path.join(job_dir, 'result.npz'), **arrays)
    with open(os.path.join(job_dir, 'report.json'), 'w',
              encoding='utf-8') as handle:
        json.dump(_jsonable(report), handle)
    return report


def main(argv) -> int:
    if len(argv) != 2:
        sys.stderr.write('usage: cvt_worker_v1.py JOB_DIR\n')
        return 2
    try:
        execute(argv[1])
    except CvtRefusal as error:
        with open(os.path.join(argv[1], 'report.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump({'refused': str(error)}, handle)
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
