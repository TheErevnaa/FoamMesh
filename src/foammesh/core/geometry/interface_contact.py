"""Which two prepared faces actually touch, for an interface pair.

DP-556 (audit 0924, case G6 ``tee_with_plug.brep``). The Geometry page's
interface-pair editor opened on the first prepared face in both scope pickers
and accepted any two faces at all. The user saved ``tee_plug_contact`` with
the slave ``body1_face2`` -- the tee's outer end cap, a boundary of one
volume -- so the pair could never apply, and nothing said so until the Gmsh
run graded it ``not applied`` (DP-548).

The prepared group manifest already knows which faces meet. The CAD import
marks a face two solids share, and a face written into each solid as two
coincident copies, as an interface (``interface_patch_uuid``, DP-92), and a
face both solids share is listed once per solid (DP-546). This module reads
those marks to *propose* the pairs that touch, and measures the prepared
tessellation to *judge* a pair the marks do not cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = ('FaceContact', 'contacting_face_pairs', 'face_contact',
           'face_triangles', 'group_manifest_of')


@dataclass(frozen=True)
class FaceContact:
    """Whether two prepared faces touch.

    ``touching`` is ``None`` when nothing could be measured -- no
    tessellation, or a face with no triangles of its own -- which is not
    evidence either way.
    """

    touching: bool | None
    reason: str


def group_manifest_of(prepared: Mapping | None) -> dict:
    """The group manifest inside a ``geometry.prepared.current`` payload."""
    if not isinstance(prepared, Mapping):
        return {}
    manifest = prepared.get('group_manifest')
    if isinstance(manifest, Mapping) and manifest:
        return dict(manifest)
    return dict(prepared) if 'groups' in prepared else {}


def _groups(manifest: Mapping) -> dict:
    return {str(group.get('patch_uuid') or ''): group
            for group in (manifest.get('groups') or ())
            if isinstance(group, Mapping) and group.get('patch_uuid')}


def _region_of(manifest: Mapping) -> dict:
    """Patch uuid -> the region (solid) whose boundary lists it."""
    owner = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, Mapping):
            continue
        for uuid_ in region.get('boundary_patch_uuids') or ():
            owner.setdefault(str(uuid_), str(region.get('region_uuid') or ''))
    return owner


def _order(group: Mapping) -> int:
    try:
        return int(group.get('face_order'))
    except (TypeError, ValueError):
        return 1 << 30


def _same_area(first: Mapping, second: Mapping) -> bool:
    try:
        a, b = float(first.get('area')), float(second.get('area'))
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-30)


def _marked_contact(first: Mapping, second: Mapping) -> str:
    """Why the import says these two listings are one interface, or ''.

    Two marks, both written by the CAD import (``cad_importer``):

    * a twin: the file carries the interface as two coincident faces, one
      per solid, and each names the other (or one names the other);
    * one face in both solids: each listing is its own twin, and the two
      listings measure the same area and border the same faces -- the rule
      ``gmsh.execution.shared_surface_aliases`` joins them by (DP-546).
    """
    a = str(first.get('patch_uuid') or '')
    b = str(second.get('patch_uuid') or '')
    twin_a = str(first.get('interface_patch_uuid') or '')
    twin_b = str(second.get('interface_patch_uuid') or '')
    if a and b and a != b and (twin_a == b or twin_b == a):
        return 'the import measured them as coincident copies of one interface'
    if twin_a == a and twin_b == b and a != b and _same_area(first, second):
        rim_a = frozenset(map(str, first.get('adjacent_patch_uuids') or ())) | {a}
        rim_b = frozenset(map(str, second.get('adjacent_patch_uuids') or ())) | {b}
        if rim_a == rim_b:
            return 'they are one CAD face shared by both solids'
    return ''


def contacting_face_pairs(manifest: Mapping) -> list[tuple[str, str]]:
    """``(master uuid, slave uuid)`` for every pair the import marks as touching.

    Only pairs across two solids: a pair within one solid is not an interface
    between volumes. The master is the listing the mesher reads first, which
    is the one that survives a merge. Sorted by that order.
    """
    groups = _groups(manifest)
    region = _region_of(manifest)
    ordered = sorted(groups.values(), key=_order)
    pairs = []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            a, b = str(first['patch_uuid']), str(second['patch_uuid'])
            if region and region.get(a) == region.get(b):
                continue
            if _marked_contact(first, second):
                pairs.append((a, b))
    return pairs


# -- measuring the tessellation --------------------------------------------- #

def _surface_path(prepared: Mapping, geometry_id: str) -> Path | None:
    body = prepared.get('manifest') if isinstance(prepared, Mapping) else None
    body = body if isinstance(body, Mapping) else {}
    reference = prepared.get('reference') if isinstance(prepared, Mapping) else None
    root = (reference or {}).get('root') if isinstance(reference, Mapping) else None
    for source in body.get('sources') or ():
        if not isinstance(source, Mapping):
            continue
        if geometry_id and str(source.get('geometry_id') or '') != geometry_id:
            continue
        candidates = []
        if root and source.get('surface_prepared_name'):
            candidates.append(Path(root) / 'sources'
                              / str(source['surface_prepared_name']))
        if source.get('surface_source_path'):
            candidates.append(Path(str(source['surface_source_path'])))
        for path in candidates:
            if path.is_file():
                return path
    return None


def _deflection(prepared: Mapping, geometry_id: str) -> float:
    """How far the tessellation may stand off the true surface."""
    body = prepared.get('manifest') if isinstance(prepared, Mapping) else None
    for source in (body or {}).get('sources') or ():
        if not isinstance(source, Mapping):
            continue
        if geometry_id and str(source.get('geometry_id') or '') != geometry_id:
            continue
        try:
            return abs(float((source.get('tessellation') or {})
                             .get('linear_deflection') or 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _solid_name(group: Mapping) -> str:
    for ref in group.get('source_refs') or ():
        if isinstance(ref, Mapping):
            for key in ('original_name', 'solid_name'):
                if ref.get(key):
                    return str(ref[key])
    return str(group.get('display_name') or '')


def _read_solids(path: Path, wanted: set | None) -> dict:
    """Triangles of the named solids of an ASCII STL, as ``[(3,3) list]``.

    ``wanted`` of ``None`` reads every solid.
    """
    solids: dict[str, list] = {}
    current, vertices = None, []
    try:
        handle = path.open('r', encoding='utf-8', errors='replace')
    except OSError:
        return {}
    with handle:
        for line in handle:
            words = line.split()
            if not words:
                continue
            keyword = words[0].lower()
            if keyword == 'solid':
                name = ' '.join(words[1:])
                current = (name if wanted is None or name in wanted
                           else None)
                if current is not None:
                    solids.setdefault(current, [])
            elif keyword == 'endsolid':
                current = None
            elif current is not None and keyword == 'vertex':
                vertices.append(tuple(float(value) for value in words[1:4]))
                if len(vertices) == 3:
                    solids[current].append(vertices)
                    vertices = []
            elif keyword == 'endloop':
                vertices = []
    return solids


def _is_ascii(path: Path) -> bool:
    """The test ``stl_importer._is_ascii_stl`` makes, without importing VTK."""
    try:
        with path.open('rb') as stream:
            head = stream.read(1024)
    except OSError:
        return False
    if not head.lstrip().lower().startswith(b'solid'):
        return False
    try:
        text = head.decode('ascii').lower()
    except UnicodeDecodeError:
        return False
    return 'facet' in text or 'endsolid' in text


def _read_binary(path: Path):
    """Every triangle of a binary STL, as an ``(n, 3, 3)`` float array."""
    import numpy as np

    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 84:
        return None
    count = int.from_bytes(data[80:84], 'little')
    record = np.dtype([('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)),
                       ('attribute', '<u2')])
    count = min(count, (len(data) - 84) // record.itemsize)
    if count <= 0:
        return None
    return np.frombuffer(data, dtype=record, count=count,
                         offset=84)['vertices'].astype(float)


def face_triangles(prepared: Mapping, patch_uuid: str):
    """``(triangles, reason)`` of one prepared face, read from its own file.

    Plan 49 SR3 (DP-5785). Each face is read from the prepared surface of the
    geometry file it came from, so two faces of two files are measured like
    two faces of one. ``triangles`` is an ``(n, 3, 3)`` array, or ``None``
    with the reason nothing could be read. A face that is the only one its
    file holds is the whole file, whatever its solid is called or however it
    is stored (ASCII or binary).
    """
    import numpy as np

    groups = _groups(group_manifest_of(prepared))
    group = groups.get(str(patch_uuid))
    if group is None:
        return None, 'a face is not in the prepared geometry'
    geometry_id = str(group.get('geometry_id') or '')
    path = _surface_path(prepared, geometry_id)
    if path is None:
        return None, 'the prepared tessellation could not be read'
    alone = sum(1 for item in groups.values()
                if str(item.get('geometry_id') or '') == geometry_id) == 1
    if _is_ascii(path):
        name = _solid_name(group)
        triangles = _read_solids(path, {name}).get(name)
        if not triangles and alone:
            triangles = [tri for solid in _read_solids(path, None).values()
                         for tri in solid]
        if not triangles:
            return None, ('a face has no triangles of its own in the '
                          'prepared tessellation')
        return np.asarray(triangles, dtype=float), ''
    if not alone:
        return None, ('a binary surface file holds several faces and does '
                      'not say which triangles are whose')
    triangles = _read_binary(path)
    if triangles is None or not len(triangles):
        return None, 'the prepared tessellation could not be read'
    return triangles, ''


def _distances(points, triangles):
    """The distance from each point to the nearest of *triangles*.

    Plan 49 SR3. Exact either way. Small inputs are compared all against
    all; larger ones only against the triangles that can be nearest: the
    nearest triangle's centroid lies within ``d0 + R`` of the point, where
    ``d0`` is the distance to the nearest centroid and ``R`` the largest
    centroid-to-vertex distance (measured: 4,000 points against 51,200
    triangles took 121 s all against all).
    """
    import numpy as np

    points = np.asarray(points, dtype=float)
    triangles = np.asarray(triangles, dtype=float)
    if len(points) == 0:
        return np.zeros(0)
    if len(triangles) == 0:
        return np.full(len(points), np.inf)
    if len(points) * len(triangles) <= _ALL_AGAINST_ALL:
        return _all_against_all(points, triangles)
    try:
        from scipy.spatial import cKDTree
    except ImportError:                                  # pragma: no cover
        return _all_against_all(points, triangles)
    centroids = triangles.mean(axis=1)
    reach = float(np.linalg.norm(triangles - centroids[:, None, :],
                                 axis=-1).max())
    tree = cKDTree(centroids)
    nearest, _index = tree.query(points)
    candidates = tree.query_ball_point(points, nearest + reach * (1 + 1e-9)
                                       + 1e-12)
    counts = np.fromiter((len(item) for item in candidates), dtype=np.int64,
                         count=len(points))
    owner = np.repeat(np.arange(len(points)), counts)
    which = np.fromiter((index for item in candidates for index in item),
                        dtype=np.int64, count=int(counts.sum()))
    best = np.full(len(points), np.inf)
    step = _ALL_AGAINST_ALL
    for begin in range(0, len(owner), step):
        rows = owner[begin:begin + step]
        chosen = triangles[which[begin:begin + step]]
        np.minimum.at(best, rows, _point_triangle(
            points[rows], chosen[:, 0], chosen[:, 1], chosen[:, 2]))
    return best


#: Point-triangle pairs compared in one numpy pass.
_ALL_AGAINST_ALL = 2_000_000


def _point_triangle(p, a, b, c):
    """Distance from each point ``p`` to the triangle ``a b c`` beside it.

    The arrays broadcast against each other: one point against many
    triangles, or point ``i`` against triangle ``i``.
    """
    import numpy as np

    normal = np.cross(b - a, c - a)
    squared = (normal * normal).sum(-1)
    valid = squared > 0
    unit = np.where(valid[..., None], normal
                    / np.sqrt(np.where(valid, squared, 1.0))[..., None], 0.0)

    def segment(start, end):
        edge = end - start
        length = (edge * edge).sum(-1)
        t = np.clip(((p - start) * edge).sum(-1)
                    / np.where(length > 0, length, 1.0), 0.0, 1.0)
        return np.linalg.norm(p - (start + t[..., None] * edge), axis=-1)

    height = ((p - a) * unit).sum(-1)
    foot = p - height[..., None] * unit
    inside = valid & np.all([
        (np.cross(y - x, foot - x) * unit).sum(-1) >= 0
        for x, y in ((a, b), (b, c), (c, a))], axis=0)
    plane = np.where(inside, np.abs(height), np.inf)
    edges = np.minimum(np.minimum(segment(a, b), segment(b, c)),
                       segment(c, a))
    return np.minimum(plane, edges)


def _all_against_all(points, triangles):
    import numpy as np

    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    best = np.full(len(points), np.inf)
    step = max(1, _ALL_AGAINST_ALL // max(len(triangles), 1))
    for begin in range(0, len(points), step):
        p = points[begin:begin + step, None, :]
        best[begin:begin + step] = _point_triangle(p, a, b, c).min(1)
    return best


def face_contact(prepared: Mapping, master: str, slave: str,
                 tolerance: float = 0.0) -> FaceContact:
    """Whether prepared faces *master* and *slave* touch within *tolerance*.

    The import's marks are read first. Otherwise every vertex, edge midpoint
    and triangle centroid of the smaller face must lie on the other face, within the larger of *tolerance* and the
    tessellation's linear deflection: two coincident curved faces are each
    tessellated on their own, and their triangles stand off one another by
    up to that deflection although the CAD faces are one surface.
    """
    manifest = group_manifest_of(prepared)
    groups = _groups(manifest)
    first, second = groups.get(str(master)), groups.get(str(slave))
    if first is None or second is None:
        return FaceContact(None, 'a face is not in the prepared geometry')
    if str(master) == str(slave):
        return FaceContact(False, 'the master and the slave are the same face')
    marked = _marked_contact(first, second)
    if marked:
        return FaceContact(True, marked)
    # Plan 49 SR3 (DP-5785). Each face is read from its own file: a pair
    # across two files used to come back "not evidence either way" unmeasured.
    first_tri, reason = face_triangles(prepared, master)
    if first_tri is None:
        return FaceContact(None, reason)
    second_tri, reason = face_triangles(prepared, slave)
    if second_tri is None:
        return FaceContact(None, reason)
    import numpy as np

    def area(tri):
        return 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0],
                                             tri[:, 2] - tri[:, 0]),
                                    axis=1).sum()

    small, large = ((first_tri, second_tri)
                    if area(first_tri) <= area(second_tri)
                    else (second_tri, first_tri))
    # Vertices alone are not enough: a flat disc tessellated as a fan has
    # every vertex on its rim, and the rim lies on the wall of the pipe it
    # caps. The centroid and the edge midpoints of each triangle are points
    # inside the face.
    points = np.unique(np.concatenate([
        small.reshape(-1, 3), small.mean(axis=1),
        0.5 * (small + np.roll(small, 1, axis=1)).reshape(-1, 3)]), axis=0)
    gap = float(_distances(points, large).max())
    allowed = max(abs(float(tolerance or 0.0)),
                  _deflection(prepared, str(first.get('geometry_id') or '')),
                  _deflection(prepared, str(second.get('geometry_id') or '')))
    if gap <= allowed:
        return FaceContact(True, f'the faces lie within {gap:.3g} m of each '
                                 'other')
    return FaceContact(False, f'the faces stand up to {gap:.3g} m apart, more '
                              f'than the {allowed:.3g} m allowed')
