"""The cell zones ``splitMeshRegions -cellZonesOnly`` needs, from the regions.

DP-538 (MA24-02). MEASURED on S6_two_cubes: two closed cubes 1 m apart, two
Fluid region points, and snappyHexMesh produced exactly what was asked -- one
9,728-cell mesh in two disconnected 4,864-cell pieces -- with no
``cellZones`` file at all. The export then ran ``splitMeshRegions
-cellZonesOnly``, which died on ``Cell 0 ... is not in a cellZone``.

The mesher cannot be asked for those zones in OpenFOAM Foundation 13.
``refinementParameters.C`` reads ``insidePoints`` as a bare ``List<point>``
-- the named ``locationsInMesh`` form is ESI's and is not in
``libsnappyHexMesh.so`` -- so every region snappy keeps is kept unnamed. And
``surfaceZonesInfo.C:66`` reads the zone keys once per *surface*, so when both
cubes are solids of one STL, one surface entry cannot make one zone of each.
The v13 tutorial that splits a snappy mesh (``multiRegion/CHT/heatedDuct``)
leans on exactly that: its unzoned cells are the region it names with
``-defaultRegionName``.

What the export does know is each region's name and the point that seeded it.
Every piece snappy kept holds one of those points, so a cell zone per region is
the connected piece around its point -- the same answer ``regionToCell`` would
give, computed from the mesh files the export is about to hand over, and named
after the region so that the region directories ``splitMeshRegions`` writes
are the ones the rest of the export moves.

Anything that is not that clean case is refused *before* OpenFOAM runs, with
the reason, rather than passed on to fail with a line number.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from foammesh.core.mesh.poly_mesh_boundary import (
    PolyMeshReadError, read_poly_mesh)


class RegionZoneError(RuntimeError):
    """The mesh cannot be split into the authored regions; the text says why."""


#: What the file is called, and what v13 reads it as (``poly_mesh_writer``).
CELL_ZONES = 'cellZones'


def _connected_pieces(cell_count: int, owner, neighbour) -> np.ndarray:
    """A piece id per cell: cells joined through internal faces share one.

    Minimum-label propagation with pointer jumping, in numpy, so a mesh of
    millions of cells needs a handful of passes rather than a Python loop per
    face. After every pass each label is a root (``labels[labels] ==
    labels``), so a face whose two cells still disagree hooks the larger root
    under the smaller and the loop cannot stop until no face disagrees.
    """
    labels = np.arange(cell_count, dtype=np.int64)
    first = np.asarray(owner[:len(neighbour)], dtype=np.int64)
    second = np.asarray(neighbour, dtype=np.int64)
    while True:
        a = labels[first]
        b = labels[second]
        low = np.minimum(a, b)
        hooked = labels.copy()
        np.minimum.at(hooked, a, low)
        np.minimum.at(hooked, b, low)
        while True:
            jumped = hooked[hooked]
            if np.array_equal(jumped, hooked):
                break
            hooked = jumped
        if np.array_equal(hooked, labels):
            break
        labels = hooked
    _roots, pieces = np.unique(labels, return_inverse=True)
    return pieces.astype(np.int64)


def _cell_centres(mesh) -> np.ndarray:
    """The mean of each cell's face centres -- enough to find the nearest."""
    offsets = mesh.face_offsets
    sizes = np.diff(offsets)
    corner = mesh.points[mesh.face_vertices]
    face_centres = np.add.reduceat(corner, offsets[:-1], axis=0) / sizes[:, None]
    cells = mesh.cell_count
    owner = mesh.owner
    neighbour = mesh.neighbour
    totals = np.zeros((cells, 3))
    counts = np.bincount(owner, minlength=cells).astype(float)
    counts += np.bincount(neighbour, minlength=cells)
    for axis in range(3):
        totals[:, axis] = (
            np.bincount(owner, face_centres[:, axis], minlength=cells)
            + np.bincount(neighbour, face_centres[:len(neighbour), axis],
                          minlength=cells))
    return totals / np.maximum(counts, 1.0)[:, None]


def _where(mesh, cell: int) -> str:
    centre = _cell_centres(mesh)[int(cell)]
    return '(' + ' '.join(f'{value:.6g}' for value in centre) + ')'


def _check_existing(mesh) -> None:
    """Zones the mesher wrote must hold every cell exactly once."""
    cells = mesh.cell_count
    hits = np.zeros(cells, dtype=np.int64)
    for zone in mesh.cell_zones:
        labels = np.asarray(zone.labels, dtype=np.int64)
        labels = labels[(labels >= 0) & (labels < cells)]
        np.add.at(hits, labels, 1)
    unzoned = np.flatnonzero(hits == 0)
    twice = np.flatnonzero(hits > 1)
    names = ', '.join(zone.name for zone in mesh.cell_zones)
    if unzoned.size:
        raise RegionZoneError(
            f'the mesh has cell zones ({names}) but {unzoned.size} of its '
            f'{cells} cells are in none of them, starting with cell '
            f'{int(unzoned[0])} at {_where(mesh, unzoned[0])}. Splitting the '
            f'mesh into regions by cell zone needs every cell in exactly one '
            f'zone, so nothing was exported; give that part of the domain a '
            f'region point or a cell zone and mesh again')
    if twice.size:
        raise RegionZoneError(
            f'{twice.size} cells are in more than one of the cell zones '
            f'({names}), starting with cell {int(twice[0])} at '
            f'{_where(mesh, twice[0])}. Splitting the mesh into regions by '
            f'cell zone needs every cell in exactly one zone, so nothing was '
            f'exported')


def region_zones(mesh, seeds) -> dict[str, np.ndarray]:
    """``{region name: cell ids}`` for a mesh that carries no cell zones.

    *seeds* is ``[(name, point), ...]`` in Region page order. Refused unless
    the mesh falls into exactly one connected piece per region and each
    region's point lies in a different piece.
    """
    seeds = [(str(name), tuple(float(value) for value in point))
             for name, point in seeds]
    pieces = _connected_pieces(mesh.cell_count, mesh.owner, mesh.neighbour)
    count = int(pieces.max()) + 1 if pieces.size else 0
    names = ', '.join(name for name, _point in seeds)
    if count != len(seeds):
        raise RegionZoneError(
            f'the mesh carries no cell zones and falls into {count} '
            f'disconnected piece{"" if count == 1 else "s"}, but '
            f'{len(seeds)} regions are defined ({names}). The export names '
            f'each region after the piece around its region point, so it '
            f'needs one piece per region; nothing was exported. Regions that '
            f'touch need a cell zone or an interface to separate them')
    centres = _cell_centres(mesh)
    owners: dict[int, str] = {}
    zones: dict[str, np.ndarray] = {}
    for name, point in seeds:
        nearest = int(np.argmin(
            np.sum((centres - np.asarray(point)) ** 2, axis=1)))
        piece = int(pieces[nearest])
        if piece in owners:
            raise RegionZoneError(
                f'the region points of {owners[piece]} and {name} lie in the '
                f'same connected piece of the mesh, so the export cannot tell '
                f'which cells belong to which region; nothing was exported')
        owners[piece] = name
        zones[name] = np.flatnonzero(pieces == piece)
    return zones


def write_cell_zones(poly_mesh: Path, zones: dict[str, np.ndarray]) -> Path:
    """Write *zones* as the ``cellZoneList`` v13 reads; return the file."""
    path = Path(poly_mesh) / CELL_ZONES
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(
            'FoamFile\n{\n    format      ascii;\n    class       '
            'cellZoneList;\n    location    "constant/polyMesh";\n'
            '    object      cellZones;\n}\n'
            '// Generated by FoamMesh from the region points for '
            'splitMeshRegions\n\n')
        stream.write(f'{len(zones)}\n(\n')
        for name, labels in zones.items():
            stream.write(
                f'{name}\n{{\n    type cellZone;\n'
                f'    cellLabels List<label>\n    {len(labels)}\n    (\n')
            stream.write(''.join(f'        {int(label)}\n' for label in labels))
            stream.write('    );\n}\n')
        stream.write(')\n')
    return path


def prepare_region_zones(poly_mesh: Path, seeds) -> Path | None:
    """Make the mesh at *poly_mesh* splittable by cell zone, or say why not.

    Returns the ``cellZones`` file written for the split -- the caller removes
    it afterwards, so the native mesh is left as the mesher wrote it -- or
    ``None`` when the mesher's own zones already cover every cell once, or
    when the mesh is in a form this cannot read (binary): that case is left to
    OpenFOAM, whose refusal the export reports with its cause.
    """
    poly_mesh = Path(poly_mesh)
    try:
        mesh = read_poly_mesh(poly_mesh, layout_expectation='any')
    except PolyMeshReadError:
        return None
    if mesh.cell_count == 0:
        return None
    if mesh.cell_zones:
        _check_existing(mesh)
        return None
    return write_cell_zones(poly_mesh, region_zones(mesh, seeds))
