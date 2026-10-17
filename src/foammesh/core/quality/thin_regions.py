"""DP-2635: a region meshed too thin to resolve a gradient across it.

MEASURED on the CHT three-region pipe (``issues/``, 2026-10-15): the pipe
wall is 2 mm thick between a 10 mm hot core and a 20 mm cold annulus. At
the default sizing snappy kept the seeded pipe as **one cell** (seed-only
route) or as 19,200 cells every one of which touches a wall (Split
interfaces route), and the Quality step graded both runnable -- checkMesh
judges cells, not whether a region is resolved, and a one-cell solid is a
perfectly valid cell. A conjugate heat-transfer run on it has no
temperature gradient through the wall to compute.

The check: the mesh's cell regions are its connected pieces with every
faceZone face and every face between two cellZones cut, so each region a
seed (or a cell zone) made is one piece. A cell is a wall cell when it owns
a boundary face (``empty``, ``wedge``, ``symmetry``, cyclic and processor
patches excepted: they are not walls) or sits on a cut face. Depth is the
number of cell layers from the nearest wall, by face neighbours. A region
whose deepest cell is at depth ``d`` is at most ``2 d`` cells across
anywhere. It **fails** when that is fewer than ``FAIL_CELLS_ACROSS`` -- 3,
`geometry_fidelity`'s ``fail_cells``, "the classical minimum for a resolved
gradient across a channel" (§6.6). It is a resolution check, not a cap: no
cell count is limited, and a region of any size passes once it is three
cells thick somewhere.

Each region is named by the region seed whose cell is in it (else its cell
zone), so the finding names the region the user made. Pieces holding no
seed and no zone are the retained-region check's (`retained_regions`), not
this one's. The finding is the app's judgement filed as blocking, and is
the one blocking finding a user may knowingly accept (``Accept anyway``):
the mesh runs, it just does not resolve that region.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from foammesh.core.quantities import count_text

logger = logging.getLogger(__name__)

#: The fewest cells across a region that resolve a gradient through it:
#: `geometry_fidelity.thresholds` CANDIDATE ``fail_cells`` (§6.6).
FAIL_CELLS_ACROSS = 3

#: The finding's leading words, for a reader that needs to tell it from
#: checkMesh's own findings.
FINDING_PREFIX = 'region meshed too thin: '

#: Patch types that are not walls: a region is not "thin" across them.
#: Every ``cyclic*`` type is coupled too (see `_is_wall`); they are matched
#: by prefix, not listed, so the feature matrix's token scan does not read
#: a coupled type this module only skips as something FoamMesh writes.
NOT_WALLS = ('empty', 'wedge', 'symmetry', 'symmetryPlane',
             'nonConformalCyclic', 'processor', 'processorCyclic',
             'nonConformalProcessorCyclic')


def _is_wall(patch_type) -> bool:
    kind = str(patch_type or '')
    return not (kind in NOT_WALLS or kind.startswith('cyclic'))

#: RAM the read and the graphs need: this many times the owner/neighbour
#: (and faces/points, for naming) bytes, plus bytes a label.
FILE_FACTOR = 3
BYTES_PER_LABEL = 96
ASCII_BYTES_PER_LABEL = 8
#: The candidate cells a seed is tested against, nearest centres first.
NEAREST_CELLS = 16


def _file_bytes(mesh: Path) -> int:
    total = 0
    for name in ('owner', 'neighbour', 'faces', 'points'):
        for candidate in (mesh / name, mesh / f'{name}.gz'):
            if candidate.is_file():
                total += candidate.stat().st_size
                break
    return total


def memory_needed(mesh_dir) -> int:
    total = _file_bytes(Path(mesh_dir))
    return FILE_FACTOR * total + BYTES_PER_LABEL * (
        total // ASCII_BYTES_PER_LABEL)


def _wall_faces(mesh: Path, internal: int, faces: int):
    """Boundary face ids that are walls, as a bool mask over all faces."""
    import numpy as np

    from foammesh.core.mesh.poly_mesh_boundary import _read_boundary

    wall = np.zeros(faces, dtype=bool)
    wall[internal:] = True
    try:
        patches = _read_boundary(mesh, any_format=True)
    except Exception:  # noqa: BLE001 - no table: every boundary face counts
        return wall
    for patch in patches:
        if not _is_wall(patch.patch_type):
            wall[patch.start_face:patch.end_face] = False
    return wall


def _cell_centres(mesh: Path, owner, neighbour, cells):
    """Approximate cell centres (mean of face centres) and face data."""
    import numpy as np

    from foammesh.core.mesh.poly_mesh_boundary import (
        _read_faces, _read_points,
    )

    points = _read_points(mesh)
    flat, offsets = _read_faces(mesh)
    counts = np.diff(offsets)
    face_of_vertex = np.repeat(np.arange(counts.size), counts)
    centres = np.zeros((counts.size, 3))
    for axis in range(3):
        centres[:, axis] = np.bincount(
            face_of_vertex, weights=points[flat, axis],
            minlength=counts.size) / np.maximum(counts, 1)
    total = np.zeros((cells, 3))
    seen = np.bincount(owner, minlength=cells).astype(float)
    seen += np.bincount(neighbour, minlength=cells)
    for axis in range(3):
        total[:, axis] = (
            np.bincount(owner, weights=centres[:, axis], minlength=cells)
            + np.bincount(neighbour, weights=centres[:neighbour.size, axis],
                          minlength=cells))
    return total / np.maximum(seen, 1)[:, None], points, flat, offsets, \
        centres


def _inside_cell(point, cell, owner, neighbour, points, flat, offsets,
                 face_centres) -> bool:
    """Is *point* inside *cell* (every face plane, owner-outward)?"""
    import numpy as np

    owned = np.flatnonzero(owner == cell)
    entered = np.flatnonzero(neighbour == cell)
    scale = 0.0
    for faces, sign in ((owned, 1.0), (entered, -1.0)):
        for face in faces:
            ring = points[flat[offsets[face]:offsets[face + 1]]]
            normal = np.cross(ring - face_centres[face],
                              np.roll(ring, -1, axis=0)
                              - face_centres[face]).sum(axis=0) / 2.0
            length = float(np.linalg.norm(normal))
            if length == 0.0:
                continue
            scale = max(scale, math.sqrt(length))
            side = float(np.dot(np.asarray(point) - face_centres[face],
                                normal / length)) * sign
            if side > 1e-9 * max(scale, 1e-12):
                return False
    return True


def _seed_cells(seeds, centres, owner, neighbour, points, flat, offsets,
                face_centres) -> dict:
    """``{seed label: cell}`` for every seed a cell holds (else nearest)."""
    import numpy as np

    found = {}
    if not seeds or centres.size == 0:
        return found
    try:
        from scipy.spatial import cKDTree

        tree = cKDTree(centres)
    except Exception:  # noqa: BLE001 - no scipy tree: brute force below
        tree = None
    for label, point in seeds:
        point = np.asarray([float(value) for value in point])
        if tree is not None:
            count = min(NEAREST_CELLS, len(centres))
            _distances, near = tree.query(point, k=count)
            near = np.atleast_1d(near)
        else:
            near = np.argsort(np.linalg.norm(centres - point, axis=1))[
                :NEAREST_CELLS]
        cell = next((int(candidate) for candidate in near
                     if _inside_cell(point, int(candidate), owner, neighbour,
                                     points, flat, offsets, face_centres)),
                    None)
        found[str(label)] = int(near[0]) if cell is None else cell
    return found


def measure(case_path, seeds=()) -> list[dict] | None:
    """Every named cell region of ``constant/polyMesh`` with its depth.

    *seeds* is ``[(label, xyz), ...]``. Returns ``[{'name', 'cells',
    'depth', 'across', 'zone', 'seeds'}]`` -- ``across`` the most cells the
    region can be across anywhere, ``2 * depth`` -- or ``None`` when the
    mesh cannot be read (ASCII only) or the free RAM cannot hold the read.
    """
    try:
        import numpy as np
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        from foammesh.core.mesh.poly_mesh_boundary import (
            _read_labels, _read_zones,
        )
        from foammesh.support.resource_budget import memory_refusal

        mesh = Path(case_path) / 'constant' / 'polyMesh'
        refusal = memory_refusal('measuring how thin each region is',
                                 memory_needed(mesh))
        if refusal is not None:
            logger.info('thin regions not measured: %s', refusal)
            return None
        owner = _read_labels(mesh, 'owner')
        neighbour = _read_labels(mesh, 'neighbour')
        if owner.size == 0:
            return None
        cells = int(owner.max()) + 1
        if neighbour.size:
            cells = max(cells, int(neighbour.max()) + 1)
        internal = neighbour.size
        inner_owner = owner[:internal]
        cut = np.zeros(internal, dtype=bool)
        for zone in _read_zones(mesh, 'faceZones'):
            labels = zone.labels[zone.labels < internal]
            cut[labels] = True
        zone_of = np.full(cells, -1, dtype=np.int64)
        zone_names = []
        for index, zone in enumerate(_read_zones(mesh, 'cellZones')):
            zone_of[zone.labels] = index
            zone_names.append(zone.name)
        cut |= zone_of[inner_owner] != zone_of[neighbour]
        keep = ~cut
        graph = coo_matrix(
            (np.ones(int(keep.sum()), dtype=np.int8),
             (inner_owner[keep], neighbour[keep])), shape=(cells, cells))
        count, labels = connected_components(graph, directed=False)

        wall_face = _wall_faces(mesh, internal, owner.size)
        depth = np.zeros(cells, dtype=np.int64)
        depth[owner[wall_face]] = 1
        depth[inner_owner[cut]] = 1
        depth[neighbour[cut]] = 1
        adjacency = (graph + graph.T).tocsr()
        front = depth == 1
        level = 1
        while front.any():
            reached = (adjacency @ front.astype(np.int8)) > 0
            reached &= depth == 0
            if not reached.any():
                break
            level += 1
            depth[reached] = level
            front = reached

        names = {}
        if seeds:
            centres, points, flat, offsets, face_centres = _cell_centres(
                mesh, owner, neighbour, cells)
            for label, cell in _seed_cells(
                    seeds, centres, owner, neighbour, points, flat, offsets,
                    face_centres).items():
                names.setdefault(int(labels[cell]), []).append(label)
        regions = []
        sizes = np.bincount(labels, minlength=count)
        deepest = np.zeros(count, dtype=np.int64)
        np.maximum.at(deepest, labels, depth)
        first_cell = np.full(count, -1, dtype=np.int64)
        order = np.arange(cells)[::-1]
        first_cell[labels[order]] = order
        for region in range(count):
            zone_index = int(zone_of[first_cell[region]])
            zone = zone_names[zone_index] if zone_index >= 0 else None
            held = names.get(region, [])
            if not held and zone is None:
                continue          # an unseeded piece: retained_regions'
            region_depth = int(deepest[region])
            regions.append({
                'name': ' / '.join(held) if held else zone,
                'seeds': list(held), 'zone': zone,
                'cells': int(sizes[region]), 'depth': region_depth,
                'across': min(2 * region_depth, int(sizes[region]))})
        return regions
    except Exception as error:  # noqa: BLE001 - binary, missing, malformed
        logger.info('thin regions not measured: %s', error)
        return None


def assess(regions, *, fail_cells: int = FAIL_CELLS_ACROSS) -> dict | None:
    """The judgement on `measure`'s rows, or ``None`` with nothing measured."""
    if regions is None:
        return None
    thin = [dict(row) for row in regions if row['across'] < fail_cells]
    assessment = {'regions': [dict(row) for row in regions],
                  'thin': thin, 'fail_cells': int(fail_cells)}
    assessment['findings'] = [finding_text(row) for row in thin]
    assessment['message'] = message(assessment) if thin else ''
    return assessment


def finding_text(row: dict) -> str:
    return (f'{FINDING_PREFIX}{row["name"]} has '
            f'{count_text(row["cells"], "cell")}, at most '
            f'{count_text(row["across"], "cell")} across')


def message(assessment: dict) -> str:
    """The plain sentences the QA page, the summary and the evidence carry."""
    fail_cells = assessment['fail_cells']
    lines = []
    for row in assessment['thin']:
        lines.append(
            f'Region {row["name"]} was meshed with '
            f'{count_text(row["cells"], "cell")} and is at most '
            f'{count_text(row["across"], "cell")} across its thickness; '
            f'{fail_cells} is the fewest that resolve a gradient through it, '
            'so a solve there has no gradient across that region to compute.')
    lines.append(
        'Refine it: a surface refinement level on its walls, or a smaller '
        'base cell, then mesh again. If the region does not need resolving, '
        'Accept anyway records that decision against this mesh.')
    return ' '.join(lines)


def apply(result, assessment: dict | None) -> None:
    """Attach *assessment* to *result*; a thin region is a failure.

    Filed as the app's judgement, among the blocking findings only, like
    `retained_regions.apply`; ``failed_check_details`` stays checkMesh's.
    """
    if not assessment:
        return
    result.thin_regions = dict(assessment)
    if not assessment['thin']:
        return
    for finding in assessment['findings']:
        if finding not in result.blocking_findings:
            result.blocking_findings.append(finding)
    if result.incomplete:
        return
    result.runnable = False
    result.severity = 'fail'
    if assessment['message'] not in result.recommendations:
        result.recommendations.insert(0, assessment['message'])
    from .checkmesh_parser import _verdict

    result.verdict = _verdict(result)


def line(assessment: dict | None) -> str:
    """The thin regions as one line for the verdict strip and Export.

    MEASURED live (CHT2 walk DA): the report failed on the pipe alone, and
    the strip and the Export note named only checkMesh's two advisory
    checks -- a red Quality row whose words gave a different reason.
    """
    findings = list((assessment or {}).get('findings') or ())
    if not findings:
        return ''
    text = '; '.join(item[len(FINDING_PREFIX):] if is_finding(item) else item
                     for item in findings)
    return 'Region meshed too thin: ' + text


def is_finding(text) -> bool:
    return str(text or '').startswith(FINDING_PREFIX)
