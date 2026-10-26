"""Session operations for the cvt area (Plan 48 GM16b, 4.11.1).

CVT surface remeshing on the resident session. The remeshing itself never runs
in the session: Plugin(CVTRemesh) takes every surface of the model and can stop
the process, so ``cvt_run`` writes the picked triangles to a job folder and
starts ``cvt_worker_v1.py`` as a separate process (the same pattern as
``quad_layout``). ``cvt_apply`` reads the answer back into the model.

* ``cvt_measure``  area, normalisation and counts of the picked surfaces.
* ``cvt_run``      the isolated run; writes ``result.npz`` and ``report.json``.
* ``cvt_apply``    keep the result beside the originals, or replace them
                   (only when the worker's replacement checks all passed).
* ``cvt_describe`` ``.vtu`` files of the picked surfaces before and after,
                   coloured by triangle gamma, for the viewport.

``cvt_apply`` is the operation that changes the model; the window takes a
checkpoint (``session.checkpoint_save``) before it and "Undo" restores it.
The mixin must not import ``session_v1``; it uses ``self.gmsh`` and ``self.raw``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np

_TRIANGLE, _QUAD = 2, 3


def _faces(items) -> list:
    """Surface tags from ``[tag, ...]`` or ``[[2, tag], ...]``."""
    out = []
    for item in items or []:
        if isinstance(item, (list, tuple)):
            if int(item[0]) != 2:
                raise ValueError('only surfaces (dimension 2) can be remeshed')
            item = item[1]
        out.append(int(item))
    return sorted(set(out))


def _all_surface_elements(gmsh):
    """Triangle and quadrangle corner nodes of every surface, by surface."""
    tri_nodes, tri_face, quad_nodes, quad_face = [], [], [], []
    for _dim, tag in gmsh.model.getEntities(2):
        kinds, _tags, nodes = gmsh.model.mesh.getElements(2, tag)
        for kind, chunk in zip(kinds, nodes):
            props = gmsh.model.mesh.getElementProperties(int(kind))
            per, corners = int(props[3]), int(props[5])
            block = np.asarray(chunk, dtype=np.int64).reshape(-1, per)
            if corners == 3:
                tri_nodes.append(block[:, :3])
                tri_face.append(np.full(len(block), tag, dtype=np.int64))
            elif corners == 4:
                quad_nodes.append(block[:, :4])
                quad_face.append(np.full(len(block), tag, dtype=np.int64))
    def join(parts, width):
        return (np.concatenate(parts) if parts
                else np.zeros((0, width), dtype=np.int64))
    return (join(tri_nodes, 3),
            np.concatenate(tri_face) if tri_face else np.zeros(0, np.int64),
            join(quad_nodes, 4),
            np.concatenate(quad_face) if quad_face else np.zeros(0, np.int64))


def collect(gmsh, faces) -> tuple:
    """``(arrays, volumes)`` of the surface mesh, for the worker."""
    faces = _faces(faces)
    have = {t for d, t in gmsh.model.getEntities(2)}
    missing = [f for f in faces if f not in have]
    if not faces:
        raise ValueError('pick at least one surface')
    if missing:
        raise ValueError('there is no surface %s in the model'
                         % ', '.join(str(m) for m in missing))
    node_ids, xyz, _p = gmsh.model.mesh.getNodes()
    tri_nodes, tri_face, quad_nodes, quad_face = _all_surface_elements(gmsh)
    if not np.isin(tri_face, faces).any():
        raise ValueError('the picked surfaces have no triangles; run Mesh 2D '
                         'first')
    volumes = {}
    for _dim, vol in gmsh.model.getEntities(3):
        bound = gmsh.model.getBoundary([(3, vol)], False, False, False)
        volumes[int(vol)] = sorted({int(t) for d, t in bound if d == 2})
    arrays = {
        'node_ids': np.asarray(node_ids, dtype=np.int64),
        'node_xyz': np.asarray(xyz, dtype=np.float64).reshape(-1, 3),
        'tri_nodes': tri_nodes, 'tri_face': tri_face,
        'quad_nodes': quad_nodes, 'quad_face': quad_face}
    return arrays, volumes


def measure(gmsh, faces) -> dict:
    """Area, counts, the normalisation scale and the mean edge of the picked."""
    import cvt_worker_v1 as worker

    arrays, _volumes = collect(gmsh, faces)
    faces = _faces(faces)
    pick = np.isin(arrays['tri_face'], faces)
    ids = arrays['node_ids']
    order = {int(n): i for i, n in enumerate(ids)}
    used = np.unique(arrays['tri_nodes'][pick])
    local = {int(n): i for i, n in enumerate(used)}
    points = arrays['node_xyz'][[order[int(n)] for n in used]]
    tris = np.vectorize(local.__getitem__, otypes=[np.int64])(
        arrays['tri_nodes'][pick]).reshape(-1, 3)
    quads = int(np.isin(arrays['quad_face'], faces).sum())
    centre, scale = worker.normalisation(points)
    stats = worker.surface_stats(points, tris)
    return {'faces': faces, 'quadrilaterals': quads,
            'centre': centre.tolist(), 'scale': scale, **stats}


def run(gmsh, faces, directory, options=None, target_edge_length=None,
        mode='keep', tolerance=None, timeout=900.0, python=None,
        worker_path=None) -> dict:
    """Start the worker on the picked surfaces; the model is not changed."""
    arrays, volumes = collect(gmsh, faces)
    faces = _faces(faces)
    if mode not in ('keep', 'replace'):
        raise ValueError("mode must be 'keep' or 'replace'")
    os.makedirs(directory, exist_ok=True)
    for stale in ('result.npz', 'report.json'):
        path = os.path.join(directory, stale)
        if os.path.exists(path):
            os.remove(path)
    np.savez(os.path.join(directory, 'job.npz'), **arrays)
    meta = {'picked': faces, 'options': dict(options or {}),
            'target_edge_length': target_edge_length, 'mode': mode,
            'tolerance': tolerance, 'volumes': volumes}
    with open(os.path.join(directory, 'job.json'), 'w',
              encoding='utf-8') as handle:
        json.dump(meta, handle)
    worker_path = worker_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'cvt_worker_v1.py')
    try:
        done = subprocess.run(
            [python or sys.executable, worker_path, directory],
            capture_output=True, text=True, timeout=float(timeout),
            cwd=directory)
    except subprocess.TimeoutExpired:
        return {'refused': 'the CVT remesh took more than %g s and was '
                           'stopped; the model was left as it was' % timeout}
    report_path = os.path.join(directory, 'report.json')
    tail = [line for line in (done.stdout + done.stderr).splitlines()
            if line.strip()]
    last = tail[-1].strip() if tail else ''
    if done.returncode != 0 or not os.path.exists(report_path):
        return {'refused': (
            'Gmsh stopped while remeshing (exit code %s): %s. The run was '
            'isolated, so the session and the model are as they were'
            % (done.returncode, last or 'no message')),
            'exit_code': done.returncode}
    with open(report_path, encoding='utf-8') as handle:
        report = json.load(handle)
    if 'refused' in report:
        return {'refused': report['refused']}
    report['directory'] = directory
    return report


def _gamma_vtu(write_vtk, path, points, tris, name, other=None):
    """Triangles coloured by gamma (``value``); ``deviation`` is the distance
    of each centroid to the other surface (the old one, or the new one)."""
    import cvt_worker_v1 as worker

    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    gamma = worker.tri_gamma(points, tris)
    n = len(tris)
    cells = (tris.reshape(-1).copy(), 3 * np.arange(1, n + 1, dtype=np.int64),
             np.full(n, 5, dtype=np.uint8))
    data = {'value': gamma}
    if other is not None:
        centroids = np.asarray(points, dtype=np.float64)[tris].mean(axis=1)
        data['deviation'] = worker.nearest_on_mesh(
            centroids, other[0], other[1])[0]
    write_vtk(path, 'vtu', points=np.asarray(points, dtype=np.float64),
              cells=cells, cell_data=data)
    return {'tag': None, 'entity': [2, 0], 'name': name, 'path': path,
            'cells': int(n), 'min': float(gamma.min()),
            'avg': float(gamma.mean()), 'max': float(gamma.max())}


def describe(directory, out_directory, write_vtk) -> dict:
    """Before and after of the last run as ``.vtu`` coloured by gamma."""
    import cvt_worker_v1 as worker

    report_path = os.path.join(directory, 'report.json')
    if not os.path.exists(report_path):
        raise ValueError('there is no CVT result to show; run it first')
    meta, job = worker.load_job(directory)
    data = np.load(os.path.join(directory, 'result.npz'))
    os.makedirs(out_directory, exist_ok=True)
    if 'final_points' in data.files:
        points, tris = data['final_points'], data['final_tris']
    else:
        points, tris = data['raw_points'], data['raw_tris']
    old = (job['xyz_of'], job['picked_tris']['tris'])
    rows = {'before': _gamma_vtu(
        write_vtk, os.path.join(out_directory, 'cvt_before.vtu'),
        old[0], old[1], 'CVT before', (points, tris))}
    rows['after'] = _gamma_vtu(
        write_vtk, os.path.join(out_directory, 'cvt_after.vtu'),
        points, tris, 'CVT after', old)
    with open(report_path, encoding='utf-8') as handle:
        report = json.load(handle)
    return {'rows': rows, 'before': report.get('before'),
            'after': report.get('after') or report.get('raw'),
            'deviation': report.get('deviation')}


def _orphan_nodes(gmsh) -> int:
    """Nodes of the model that no element of any dimension uses."""
    used = set()
    for dim, tag in gmsh.model.getEntities():
        for chunk in gmsh.model.mesh.getElements(dim, tag)[2]:
            used.update(int(n) for n in chunk)
    return len({int(n) for n in gmsh.model.mesh.getNodes()[0]} - used)


def apply(gmsh, directory, mode='keep', name='') -> dict:
    """Put a finished run into the model.

    ``keep`` adds the result as a new surface beside the originals. ``replace``
    swaps the originals for it, one new surface mesh per original face, and is
    refused unless every check of the worker passed.
    """
    report_path = os.path.join(directory, 'report.json')
    result_path = os.path.join(directory, 'result.npz')
    if not (os.path.exists(report_path) and os.path.exists(result_path)):
        raise ValueError('there is no CVT result in %s' % directory)
    with open(report_path, encoding='utf-8') as handle:
        report = json.load(handle)
    data = np.load(result_path)
    node_max = int(max(gmsh.model.mesh.getNodes()[0], default=0))
    if mode == 'keep':
        tag = int(max([t for d, t in gmsh.model.getEntities(2)] or [0])) + 1
        points, tris = data['raw_points'], data['raw_tris']
        gmsh.model.addDiscreteEntity(2, tag)
        gmsh.model.mesh.addNodes(
            2, tag, list(range(node_max + 1, node_max + 1 + len(points))),
            np.asarray(points, dtype=np.float64).reshape(-1).tolist())
        gmsh.model.mesh.addElementsByType(
            tag, _TRIANGLE, [],
            (np.asarray(tris, dtype=np.int64) + node_max + 1)
            .reshape(-1).tolist())
        label = name or 'CVT remesh'
        gmsh.model.setEntityName(2, tag, label)
        return {'mode': 'keep', 'entity': [2, tag], 'name': label,
                'triangles': int(len(tris)), 'replaced': [],
                'orphan_nodes': 0}
    if mode != 'replace':
        raise ValueError("mode must be 'keep' or 'replace'")
    replace = report.get('replace') or {}
    if not replace.get('requested'):
        raise ValueError('this run was made to keep the result beside the '
                         'originals; run it again with Replace originals on')
    if not replace.get('ok') or 'final_points' not in data.files:
        raise ValueError(replace.get('refusal')
                         or 'Replace is refused: a check failed')
    picked = [int(f) for f in report['picked']]
    points = np.asarray(data['final_points'], dtype=np.float64)
    tris = np.asarray(data['final_tris'], dtype=np.int64)
    face = np.asarray(data['final_face'], dtype=np.int64)
    node_id = np.asarray(data['final_node_id'], dtype=np.int64)
    # Which nodes does the picked surfaces' mesh own, and does anything else
    # still use them? Gmsh can only drop a surface's nodes all together.
    owned = set()
    for f in picked:
        owned.update(int(n) for n in gmsh.model.mesh.getNodes(2, f, False)[0])
    elsewhere = set()
    for dim, tag in gmsh.model.getEntities():
        if dim == 2 and tag in picked:
            continue
        _k, _t, nodes = gmsh.model.mesh.getElements(dim, tag)
        for chunk in nodes:
            elsewhere.update(int(n) for n in chunk)
    shared = owned & elsewhere
    stray = _orphan_nodes(gmsh)
    if not shared:
        gmsh.model.mesh.clear([(2, f) for f in picked])
    else:
        for f in picked:
            gmsh.model.mesh.removeElements(2, f)
        # The nodes only the picked surfaces used stay behind, owned by an
        # entity with no element. Gmsh drops them when the nodes are
        # reclassified from the elements; what is left is counted, not hidden.
        gmsh.model.mesh.reclassifyNodes()
    base = int(max(gmsh.model.mesh.getNodes()[0], default=0))
    tags = np.where(node_id >= 0, node_id, 0)
    fresh = np.flatnonzero(node_id < 0)
    tags[fresh] = base + 1 + np.arange(len(fresh))
    # a new node belongs to the face of the first triangle that uses it
    owner = {}
    for tri, f in zip(tris, face):
        for v in tri:
            if node_id[v] < 0:
                owner.setdefault(int(v), int(f))
    for f in picked:
        mine = np.array(sorted(v for v, o in owner.items() if o == f),
                        dtype=np.int64)
        if len(mine):
            gmsh.model.mesh.addNodes(2, f, tags[mine].tolist(),
                                     points[mine].reshape(-1).tolist())
    for f in picked:
        part = tris[face == f]
        if len(part):
            gmsh.model.mesh.addElementsByType(
                f, _TRIANGLE, [], tags[part].reshape(-1).tolist())
    return {'mode': 'replace', 'entity': None, 'name': name,
            'triangles': int(len(tris)), 'replaced': picked,
            'orphan_nodes': max(0, _orphan_nodes(gmsh) - stray)}


class SessionCvtMixin:
    """Operations of the cvt area (GM16b)."""

    __slots__ = ()

    def cvt_measure(self, faces=None, **_):
        return measure(self.gmsh, faces)

    def cvt_run(self, faces=None, directory=None, options=None,
                target_edge_length=None, mode='keep', tolerance=None,
                limit=900.0, **_):
        # ``limit``, not ``timeout``: a call's own ``timeout`` is the
        # session's deadline and ends the whole process when it passes.
        return run(self.gmsh, faces, directory, options, target_edge_length,
                   mode, tolerance, limit)

    def cvt_describe(self, directory=None, out_directory=None, **_):
        write_vtk = sys.modules[type(self).__module__]._write_vtk
        return describe(directory, out_directory or directory, write_vtk)

    def cvt_apply(self, directory=None, mode='keep', name='', **_):
        return apply(self.gmsh, directory, mode, name)
