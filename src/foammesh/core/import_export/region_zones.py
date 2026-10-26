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

#: DP-5751. Where the mesher's own ``cellZones`` waits while the export's
#: completed list is split by; ``restore_cell_zones`` puts it back.
ORIGINAL_CELL_ZONES = CELL_ZONES + '.foammesh-original'


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


def _point_text(point) -> str:
    return '(' + ' '.join(f'{float(value):.6g}' for value in point) + ')'


def _point_outside_its_zone(name, point, *, cell_distance, zone_cells,
                            zone_distance) -> str:
    """Why a region's point missed the cell zone carved for it, and the fix.

    DP-6065. MEASURED on run sr9h, snappy CHT tube: every surface was meshed
    at level (0 0), the 10 mm steel wall came out a ragged zone of 1,406
    cells about one cell thick, and the wall's region point fell in an
    unzoned cell beside it. The refusal said only that the region "lies
    outside every cell zone, but a cell zone ... is already called" by its
    name, which told the person nothing they could act on. It now says what
    was measured -- where the point is, how far the nearest cell and the
    nearest cell of the zone are from it, how many cells the zone has -- and
    what to change.
    """
    if zone_cells:
        measured = (
            f'cell zone {name} holds {zone_cells} cells, and the nearest of '
            f'them is {zone_distance:.3g} m from the point, while the cell '
            f'nearest the point ({cell_distance:.3g} m away) belongs to no '
            f'zone')
    else:
        measured = f'cell zone {name} holds no cells'
    return (
        f'The region point of {name} at {_point_text(point)} is not inside '
        f'cell zone {name}, the zone the mesher made for that body: '
        f'{measured}. This usually means the body is only a cell or two '
        f'thick at this mesh size, so its zone is too coarse or too ragged '
        f'to hold the point. Nothing was exported. Refine the surfaces that '
        f'bound {name} (raise their surface refinement level, or choose a '
        f'finer Mesh size) so the body is several cells thick, or move the '
        f'region point of {name} well inside the body, then mesh again.')


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
        completed = complete_region_zones(mesh, seeds)
        if completed is None:
            return None
        # The mesher's file is kept aside and put back after the split.
        (poly_mesh / CELL_ZONES).replace(poly_mesh / ORIGINAL_CELL_ZONES)
        return write_cell_zones(poly_mesh, completed)
    return write_cell_zones(poly_mesh, region_zones(mesh, seeds))


def restore_cell_zones(path: Path) -> None:
    """Remove a ``cellZones`` written for the split; put the mesher's back."""
    path = Path(path)
    path.unlink(missing_ok=True)
    original = path.with_name(ORIGINAL_CELL_ZONES)
    if original.is_file():
        original.replace(path)


def _zone_of_cell(mesh) -> np.ndarray:
    """The index of the zone each cell is in, ``-1`` for none."""
    owner = np.full(mesh.cell_count, -1, dtype=np.int64)
    for index, zone in enumerate(mesh.cell_zones):
        labels = np.asarray(zone.labels, dtype=np.int64)
        labels = labels[(labels >= 0) & (labels < mesh.cell_count)]
        owner[labels] = index
    return owner


def complete_region_zones(mesh, seeds) -> dict[str, np.ndarray] | None:
    """The mesher's zones completed and named by region, or ``None``.

    DP-5751. MEASURED on the CHT tube (cold core, steel wall, hot annulus)
    after Split interfaces: snappyHexMesh carves the solid as the cell zone
    of the body the interfaces bound (``zone_02_steel_wall_r50to60mm``) --
    the v13 ``heatedDuct`` layout, whose fluid is the unzoned rest -- so the
    export refused it: zones that miss cells. A region whose point lies in a
    zone takes that zone, under the region's name, and each region whose
    point lies outside every zone takes the unzoned piece around it, pieces
    being joined only through faces between unzoned cells. ``None`` when the
    zones already are the regions, each named after its region and covering
    every cell once; anything else this cannot settle is refused, saying why.
    """
    seeds = [(str(name), tuple(float(value) for value in point))
             for name, point in seeds]
    _check_overlaps(mesh)
    zone_of = _zone_of_cell(mesh)
    names = [zone.name for zone in mesh.cell_zones]
    centres = _cell_centres(mesh)
    claimed: dict[int, str] = {}
    loose: list[tuple[str, int]] = []
    for name, point in seeds:
        nearest = int(np.argmin(
            np.sum((centres - np.asarray(point)) ** 2, axis=1)))
        zone = int(zone_of[nearest])
        if zone < 0:
            loose.append((name, nearest))
        elif zone in claimed:
            raise RegionZoneError(
                f'the region points of {claimed[zone]} and {name} both lie in '
                f'cell zone {names[zone]}, so the export cannot tell which '
                f'cells belong to which region; nothing was exported')
        else:
            claimed[zone] = name
    if not loose and all(names[zone] == name
                         for zone, name in claimed.items()):
        _check_existing(mesh)
        return None
    zones: dict[str, np.ndarray] = {}
    for index, zone in enumerate(mesh.cell_zones):
        zones[claimed.get(index, zone.name)] = np.flatnonzero(zone_of == index)
    if loose:
        unzoned = zone_of < 0
        internal = len(mesh.neighbour)
        first = np.asarray(mesh.owner[:internal], dtype=np.int64)
        second = np.asarray(mesh.neighbour, dtype=np.int64)
        joined = unzoned[first] & unzoned[second]
        pieces = _connected_pieces(
            mesh.cell_count, first[joined], second[joined])
        owners: dict[int, str] = {}
        for name, cell in loose:
            piece = int(pieces[cell])
            if piece in owners:
                raise RegionZoneError(
                    f'the region points of {owners[piece]} and {name} lie in '
                    f'the same connected piece of the mesh, so the export '
                    f'cannot tell which cells belong to which region; '
                    f'nothing was exported')
            if name in zones:
                point = dict(seeds)[name]
                distances = np.sqrt(np.sum(
                    (centres - np.asarray(point)) ** 2, axis=1))
                own = zones[name]
                raise RegionZoneError(_point_outside_its_zone(
                    name, point, cell_distance=float(distances[cell]),
                    zone_cells=int(own.size),
                    zone_distance=(float(distances[own].min())
                                   if own.size else 0.0)))
            owners[piece] = name
            zones[name] = np.flatnonzero(unzoned & (pieces == piece))
    covered = np.zeros(mesh.cell_count, dtype=bool)
    for labels in zones.values():
        covered[labels] = True
    missing = np.flatnonzero(~covered)
    if missing.size:
        raise RegionZoneError(
            f'{missing.size} of the mesh\'s {mesh.cell_count} cells are in no '
            f'cell zone and hold no region point, starting with cell '
            f'{int(missing[0])} at {_where(mesh, missing[0])}. Splitting the '
            f'mesh into regions by cell zone needs every cell in exactly one '
            f'zone, so nothing was exported; give that part of the domain a '
            f'region point and mesh again')
    return zones


def require_solid_zones(poly_mesh: Path, names) -> None:
    """DP-6058. Refuse a Gmsh mesh whose cell zones are not its solids.

    A Gmsh region is a typed solid and its cell zone is the mesher's,
    published under the region's name, so there is nothing to make: the
    zones must be exactly the regions, each cell in one. A mesh this cannot
    read (binary) is left to ``splitMeshRegions``, whose refusal the export
    reports with its cause.
    """
    try:
        mesh = read_poly_mesh(Path(poly_mesh), layout_expectation='any')
    except PolyMeshReadError:
        return
    if mesh.cell_count == 0:
        return
    zones = [zone.name for zone in mesh.cell_zones]
    wanted = [str(name) for name in names]
    missing = [name for name in wanted if name not in zones]
    extra = [name for name in zones if name not in wanted]
    if missing or extra:
        said = []
        if missing:
            said.append('no cell zone for ' + ', '.join(missing))
        if extra:
            said.append('cell zones ' + ', '.join(extra)
                        + ' that are no typed region')
        raise RegionZoneError(
            f'the regions are the typed solids ({", ".join(wanted)}), but the '
            f'mesh has {" and ".join(said)}. Each solid the mesh holds must '
            f'be typed fluid or solid, or excluded, before it is meshed; '
            f'type them on "How many regions?" and mesh again')
    _check_existing(mesh)


#: DP-5751. The cell set of every cell outside the mesher's zones, which
#: ``regionToCell`` is held inside when a decomposed mesh is completed.
UNZONED_SET = 'foammesh_unzoned'


def decomposed_region_plan(poly_meshes, seeds):
    """What ``topoSet`` must do to a decomposed mesh, or ``None``.

    DP-5749/DP-5751. The processor meshes are read only to answer, for each
    region point, which zone -- or none -- the cell nearest it lies in; the
    zones themselves are made by OpenFOAM, whose ``regionToCell`` walks
    across processors. Returns ``(zone_names, claimed, loose)``: the mesher's
    zones, ``{zone: region}`` for each zone holding a region point, and the
    ``(region, point)`` of every point outside all zones. ``None`` when the
    zones already are the regions, or the mesh cannot be read here (binary);
    OpenFOAM's split then says whether they fall short.
    """
    seeds = [(str(name), tuple(float(value) for value in point))
             for name, point in seeds]
    best: dict[str, tuple[float, str | None]] = {
        name: (float('inf'), None) for name, _point in seeds}
    zone_names: list[str] = []
    # DP-6065: per region, the cells of the zone named after it and the
    # nearest of them, for the refusal when its point misses that zone.
    own_cells = {name: 0 for name, _point in seeds}
    own_nearest = {name: float('inf') for name, _point in seeds}
    for poly_mesh in poly_meshes:
        try:
            mesh = read_poly_mesh(Path(poly_mesh), layout_expectation='any')
        except PolyMeshReadError:
            return None
        if mesh.cell_count == 0:
            continue
        for zone in mesh.cell_zones:
            if zone.name not in zone_names:
                zone_names.append(zone.name)
        zone_of = _zone_of_cell(mesh)
        centres = _cell_centres(mesh)
        for name, point in seeds:
            distance = np.sum((centres - np.asarray(point)) ** 2, axis=1)
            nearest = int(np.argmin(distance))
            if distance[nearest] < best[name][0]:
                index = int(zone_of[nearest])
                best[name] = (float(distance[nearest]),
                              mesh.cell_zones[index].name if index >= 0 else '')
            for index, zone in enumerate(mesh.cell_zones):
                if zone.name != name:
                    continue
                inside = np.flatnonzero(zone_of == index)
                own_cells[name] += int(inside.size)
                if inside.size:
                    own_nearest[name] = min(own_nearest[name],
                                            float(distance[inside].min()))
    claimed: dict[str, str] = {}
    loose: list[tuple[str, tuple]] = []
    for name, point in seeds:
        zone = best[name][1]
        if zone is None:
            return None
        if not zone:
            loose.append((name, point))
        elif zone in claimed:
            raise RegionZoneError(
                f'the region points of {claimed[zone]} and {name} both lie in '
                f'cell zone {zone}, so the export cannot tell which cells '
                f'belong to which region; nothing was exported')
        else:
            claimed[zone] = name
    if not loose and all(zone == name for zone, name in claimed.items()):
        return None
    for name, point in loose:
        if name in zone_names:
            raise RegionZoneError(_point_outside_its_zone(
                name, point, cell_distance=float(np.sqrt(best[name][0])),
                zone_cells=own_cells[name],
                zone_distance=(float(np.sqrt(own_nearest[name]))
                               if own_cells[name] else 0.0)))
    return zone_names, claimed, loose


def region_zones_dict(zone_names, claimed, loose) -> str:
    """The ``topoSet`` dictionary that carries out ``decomposed_region_plan``."""
    actions = []
    if loose and zone_names:
        for number, zone in enumerate(zone_names):
            verb = 'new' if number == 0 else 'add'
            actions.append(
                f'    {{ name {UNZONED_SET}; type cellSet; action {verb}; '
                f'source zoneToCell; zone {zone}; }}\n')
        actions.append(
            f'    {{ name {UNZONED_SET}; type cellSet; action invert; }}\n')
    held = f' set {UNZONED_SET};' if zone_names else ''
    for name, point in loose:
        where = ' '.join(f'{float(value):g}' for value in point)
        actions.append(
            f'    {{ name {name}; type cellSet; action new; '
            f'source regionToCell;{held} insidePoints (({where})); '
            f'nErode 0; }}\n'
            f'    {{ name {name}; type cellZoneSet; action new; '
            f'source setToCellZone; set {name}; }}\n')
    for zone, name in claimed.items():
        if zone == name:
            continue
        actions.append(
            f'    {{ name {name}; type cellSet; action new; '
            f'source zoneToCell; zone {zone}; }}\n'
            f'    {{ name {zone}; type cellZoneSet; action remove; }}\n'
            f'    {{ name {name}; type cellZoneSet; action new; '
            f'source setToCellZone; set {name}; }}\n')
    return ('FoamFile\n{\n    format      ascii;\n    class       '
            'dictionary;\n    object      topoSetDict;\n}\n'
            '// Generated by FoamMesh from the region points for '
            'splitMeshRegions\n\nactions\n(\n' + ''.join(actions) + ');\n')


def _check_overlaps(mesh) -> None:
    """Refuse cells the mesher put in two zones, as ``_check_existing`` does."""
    hits = np.zeros(mesh.cell_count, dtype=np.int64)
    for zone in mesh.cell_zones:
        labels = np.asarray(zone.labels, dtype=np.int64)
        labels = labels[(labels >= 0) & (labels < mesh.cell_count)]
        np.add.at(hits, labels, 1)
    if (hits > 1).any():
        _check_existing(mesh)
