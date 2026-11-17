"""Deterministic OpenFOAM polyMesh writer from the canonical mixed mesh."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil

import numpy as np

from foammesh.core.mesh.connectivity import _FACES, row_groups
from foammesh.core.mesh.model import CanonicalMesh
from foammesh.core.mesh.validate import validate_mesh


class PolyMeshWriteError(ValueError):
    pass


@dataclass(frozen=True)
class PolyMeshWriteReport:
    destination: str
    points: int
    cells: int
    faces: int
    internal_faces: int
    boundary_faces: int
    patches: tuple[dict, ...]
    checksums: dict
    regions: tuple[dict, ...] = ()
    # DP-420. One row per interface between two cell zones, so a caller that
    # wants to know whether the conformal interface was labelled can read it
    # here instead of parsing the mesh back.
    face_zones: tuple[dict, ...] = ()
    # DP-6131. Empty unless cells were merged to make `checkMesh` pass.
    agglomeration: dict | None = None
    schema_version: int = 1

    def to_dict(self):
        return {
            'schema_version': self.schema_version, 'destination': self.destination,
            'points': self.points, 'cells': self.cells, 'faces': self.faces,
            'internal_faces': self.internal_faces,
            'boundary_faces': self.boundary_faces,
            'patches': list(self.patches), 'checksums': self.checksums,
            'regions': list(self.regions),
            'face_zones': list(self.face_zones),
            **({'agglomeration': dict(self.agglomeration)}
               if self.agglomeration else {}),
        }


@dataclass(frozen=True)
class _FoamTopology:
    faces: np.ndarray
    face_sizes: np.ndarray
    owner: np.ndarray
    neighbour: np.ndarray
    patch_ids: np.ndarray
    internal_count: int
    # DP-6131. The cells the faces refer to. Equal to the canonical mesh's
    # unless cells were merged, when `cell_regions` is the zone of each merged
    # cell and `agglomeration` says what was done.
    cell_count: int = 0
    cell_regions: np.ndarray | None = None
    agglomeration: dict | None = None


class FoamPolyMeshWriter:
    """Write one-region linear meshes without an OpenFOAM runtime dependency."""

    def write(self, destination: str | Path, mesh: CanonicalMesh, *,
              topology: _FoamTopology | None = None) -> PolyMeshWriteReport:
        report = validate_mesh(mesh)
        if not report.valid:
            codes = ', '.join(issue.code for issue in report.issues)
            raise PolyMeshWriteError(f'canonical mesh is invalid: {codes}')
        _validate_interface_pairs(mesh)
        destination = Path(destination).resolve()
        if destination.exists():
            raise PolyMeshWriteError('polyMesh destination already exists')
        if topology is None:
            topology = _foam_topology(mesh)
        temporary = destination.with_name(destination.name + '.tmp')
        temporary.mkdir(parents=True)
        try:
            _write_points(temporary / 'points', mesh.points)
            _write_faces(temporary / 'faces', topology.faces, topology.face_sizes)
            _write_labels(temporary / 'owner', topology.owner, 'labelList')
            _write_labels(temporary / 'neighbour', topology.neighbour, 'labelList')
            patches = _write_boundary(
                temporary / 'boundary', topology.patch_ids,
                topology.internal_count, mesh.patches)
            regions = _write_cell_zones(
                temporary / 'cellZones', mesh, topology)
            # DP-420. The interface, where there is one. Written after the
            # cell zones because a face zone is only meaningful between two of
            # them, and skipped entirely when the mesh has one region -- there
            # is nothing for an interface to be between.
            zones = _write_face_zones(
                temporary / 'faceZones', topology, mesh) if regions else []
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        checksums = {
            name: _sha256(destination / name)
            for name in ('points', 'faces', 'owner', 'neighbour', 'boundary',
                         'cellZones', 'faceZones')
            if (destination / name).is_file()}
        return PolyMeshWriteReport(
            str(destination), mesh.point_count, topology.cell_count,
            len(topology.owner), topology.internal_count,
            len(topology.owner) - topology.internal_count,
            tuple(patches), checksums, tuple(regions), tuple(zones),
            topology.agglomeration)


def _foam_topology(mesh: CanonicalMesh) -> _FoamTopology:
    """Faces, owners and neighbours in the order OpenFOAM and the hand-off need.

    The order is a contract (Plan 50 IP14, §10 last risk): internal faces by
    ascending owner then neighbour, ties in face-key order; then boundary
    faces patch by patch in the stable patch order, each patch in face-key
    order; a face is written as its owner cell has it, the owner being the
    lower cell number (then the earlier occurrence). Patch ``startFace``,
    the face zones and their flip maps, the cell agglomeration (DP-6131) and
    the hand-off manifest all read it.

    Plan 50 IP14 (GM-1). The same order, computed from one integer sort of
    the face keys (:func:`row_groups`) instead of ``np.unique(axis=0)`` and
    a three-key ``lexsort``, with the oriented face rows gathered for the
    written faces only rather than kept for every cell face.
    """
    blocks = [block for block in mesh.cell_blocks]
    segments = []          # (block, local face, first occurrence, cell offset)
    total = 0
    offset = 0
    for block in blocks:
        for face in _FACES[block.cell_type]:
            segments.append((block, face, total, offset))
            total += block.count
        offset += block.count
    key_type = np.int32 if mesh.point_count < np.iinfo(np.int32).max else np.int64
    keys = np.full((total, 4), -1, dtype=key_type)
    sizes = np.empty(total, dtype=np.int8)
    global_cells = np.empty(total, dtype=np.int64)
    for block, face, start, cell_offset in segments:
        end = start + block.count
        keys[start:end, :len(face)] = np.sort(block.connectivity[:, face], axis=1)
        sizes[start:end] = len(face)
        global_cells[start:end] = np.arange(
            cell_offset, cell_offset + block.count, dtype=np.int64)
    groups = row_groups(keys)
    counts = groups.counts
    if np.any(counts > 2):
        raise PolyMeshWriteError('non-manifold canonical faces cannot be exported')
    first_position = groups.starts[:-1]
    owner_occurrence = groups.order[first_position].copy()
    internal_ids = np.flatnonzero(counts == 2)
    # The owner of a face met twice is the lower cell number, then the earlier
    # occurrence; the other one is the neighbour.
    one = owner_occurrence[internal_ids]
    two = groups.order[first_position[internal_ids] + 1]
    second_first = ((global_cells[two] < global_cells[one])
                    | ((global_cells[two] == global_cells[one]) & (two < one)))
    owner_occurrence[internal_ids] = np.where(second_first, two, one)
    neighbour_occurrence = np.where(second_first, one, two)
    del one, two, second_first, groups
    owners = global_cells[owner_occurrence]
    neighbours_of_internal = global_cells[neighbour_occurrence]
    del neighbour_occurrence

    # OpenFOAM requires internal faces in upper-triangular order: ascending
    # owner, then ascending neighbour. Face-key order does not satisfy this and
    # makes checkMesh report "Faces not in upper triangular order".
    internal_order = _stable_pair_order(
        owners[internal_ids], neighbours_of_internal, mesh.cell_count)
    internal_ids = internal_ids[internal_order]
    neighbour = neighbours_of_internal[internal_order]
    del neighbours_of_internal, internal_order
    boundary_ids = np.flatnonzero(counts == 1)
    boundary_keys = keys[owner_occurrence[boundary_ids]].astype(np.int64)
    del keys
    boundary_patch_ids = _match_boundary_patches(boundary_keys, mesh)
    del boundary_keys
    # Patch ids are engine-assigned (MED family numbers, for example) and their
    # numeric order depends on group declaration order, not on the mesh. Rank
    # patches by a stable identity so the same logical mesh always yields the
    # same boundary ordering and checksums (S-D9, §4.6). Within a patch the
    # faces stay in face-key order, which is the order boundary_ids has.
    ranks = _patch_ranks(boundary_patch_ids, mesh.patches)
    boundary_order = np.argsort(ranks, kind='stable')
    ordered_unique = np.concatenate((internal_ids, boundary_ids[boundary_order]))
    chosen = owner_occurrence[ordered_unique]
    faces = _oriented_rows(chosen, segments)
    face_sizes = sizes[chosen]
    owner = owners[ordered_unique]
    patches = boundary_patch_ids[boundary_order]
    _align_cyclic_face_starts(
        faces[len(internal_ids):], face_sizes[len(internal_ids):],
        owner[len(internal_ids):], patches, mesh)
    return _FoamTopology(
        faces, face_sizes, owner, neighbour, patches, len(internal_ids),
        mesh.cell_count, _cell_regions(mesh))


def _oriented_rows(occurrences, segments):
    """The cell-face rows of *occurrences*, as their cells have them.

    ``-1`` pads a triangle to the four columns of a quadrilateral.
    """
    faces = np.full((len(occurrences), 4), -1, dtype=np.int64)
    if not len(occurrences):
        return faces
    starts = np.array([start for _b, _f, start, _o in segments], dtype=np.int64)
    segment = np.searchsorted(starts, occurrences, side='right') - 1
    by_segment = np.argsort(segment, kind='stable')
    bounds = np.searchsorted(segment[by_segment],
                             np.arange(len(segments) + 1))
    for index, (block, face, start, _offset) in enumerate(segments):
        rows = by_segment[bounds[index]:bounds[index + 1]]
        if not rows.size:
            continue
        cells = occurrences[rows] - start
        faces[rows, :len(face)] = block.connectivity[cells][:, face]
    return faces


def _stable_pair_order(first, second, limit):
    """``np.lexsort((second, first))``: by *first*, then *second*, then index.

    Both are cell numbers below *limit*, so the pair packs into one integer
    and sorts with one unstable sort; pairs that come twice (two cells that
    share two faces) are put back in index order afterwards.
    """
    if not len(first):
        return np.empty(0, dtype=np.int64)
    span = int(limit) + 1
    if span * span >= 2 ** 63:
        return np.lexsort((second, first))
    packed = first.astype(np.int64) * span + second.astype(np.int64)
    order = np.argsort(packed)
    ordered = packed[order]
    tied = np.flatnonzero(ordered[1:] == ordered[:-1])
    if tied.size:
        # re-sort only the runs of equal pairs, by index
        run = np.zeros(len(order), dtype=bool)
        run[tied] = True
        run[tied + 1] = True
        members = np.flatnonzero(run)
        fixed = np.lexsort((order[members], ordered[members]))
        order[members] = order[members][fixed]
    return order


def agglomerated_topology(mesh, topology=None, *, protected_keys=()):
    """The writer's topology with its edge cells merged (DP-6131).

    ``protected_keys`` are sorted vertex tuples of internal faces a merge may
    not remove, the faces an Internal-face surface role labels. Returns the
    topology unchanged when no cell needs a merge, or when the mesh is a
    constrained (empty or wedge) one, whose determinant is taken in its
    solution directions only.
    """
    from foammesh.core.export.cell_agglomeration import agglomerate

    if topology is None:
        topology = _foam_topology(mesh)
    types = {_patch_type_of(item) for item in mesh.patches.values()}
    if types & {'empty', 'wedge'}:
        return topology
    coupled = [int(patch_id) for patch_id, item in mesh.patches.items()
               if (item.get('interface') or {}).get('coupling') == 'cyclic']
    protected = _internal_labels(topology, protected_keys)
    result = agglomerate(
        mesh.points, topology.faces, topology.face_sizes, topology.owner,
        topology.neighbour, topology.patch_ids, topology.internal_count,
        topology.cell_count, topology.cell_regions,
        coupled_patches=coupled, protected_faces=protected)
    if result is None or not result.merged_cells:
        return topology
    regions = np.empty(result.cell_count, dtype=topology.cell_regions.dtype)
    regions[result.cell_map] = topology.cell_regions
    return _FoamTopology(
        result.faces, result.face_sizes, result.owner, result.neighbour,
        result.patch_ids, result.internal_count, result.cell_count, regions,
        result.to_dict())


def _patch_type_of(item):
    if str(item.get('patch_type') or '') in _FREE_PATCH_TYPES:
        return str(item['patch_type'])
    return _foam_patch_type(item.get('category'))


def _internal_labels(topology, keys):
    """Labels of the internal faces whose sorted vertices are in *keys*."""
    keys = {tuple(sorted(int(v) for v in key)) for key in keys}
    if not keys:
        return []
    found = []
    vertices = sorted({value for key in keys for value in key})
    near = np.flatnonzero(np.isin(
        topology.faces[:int(topology.internal_count)], vertices).any(axis=1))
    for label in near.tolist():
        size = int(topology.face_sizes[label])
        if tuple(sorted(int(v) for v in topology.faces[label, :size])) in keys:
            found.append(label)
    return found


def _patch_sort_key(patch_id, metadata):
    item = metadata.get(int(patch_id), {}) if metadata else {}
    identity = (item.get('stable_id') or item.get('solver_name') or
                item.get('name') or '')
    # Fall back to the numeric id only when no stable identity is available, so
    # unmanifested meshes keep a defined (if engine-dependent) order.
    return (str(identity), int(patch_id))


def _patch_ranks(patch_ids, metadata):
    order = sorted({int(value) for value in patch_ids},
                   key=lambda value: _patch_sort_key(value, metadata))
    rank = {value: index for index, value in enumerate(order)}
    # Plan 50 IP14: one lookup per patch, not one per face.
    patch_ids = np.asarray(patch_ids)
    if not patch_ids.size:
        return np.array([], dtype=np.int64)
    present, where = np.unique(patch_ids, return_inverse=True)
    return np.array([rank[int(value)] for value in present],
                    dtype=np.int64)[np.asarray(where).reshape(-1)]


def _match_boundary_patches(keys, mesh):
    supplied_keys = []
    supplied_patches = []
    for block in mesh.boundary_blocks:
        padded = np.full((block.count, 4), -1, dtype=np.int64)
        padded[:, :block.cell_type.node_count] = np.sort(block.connectivity, axis=1)
        supplied_keys.append(padded)
        supplied_patches.append(block.patch_ids)
    supplied_keys = np.ascontiguousarray(np.concatenate(supplied_keys))
    supplied_patches = np.concatenate(supplied_patches)
    key_view = _row_view(keys)
    supplied_view = _row_view(supplied_keys)
    order = np.argsort(supplied_view)
    sorted_view = supplied_view[order]
    indices = np.searchsorted(sorted_view, key_view)
    if np.any(indices >= len(sorted_view)) or np.any(sorted_view[indices] != key_view):
        raise PolyMeshWriteError('boundary patch mapping is incomplete')
    return supplied_patches[order[indices]]


def _row_view(value):
    contiguous = np.ascontiguousarray(value)
    return contiguous.view(np.dtype((np.void, contiguous.dtype.itemsize *
                                     contiguous.shape[1]))).ravel()


def _header(class_name, object_name):
    return (
        'FoamFile\n{\n    format      ascii;\n    class       ' + class_name +
        ';\n    location    "constant/polyMesh";\n    object      ' + object_name +
        ';\n}\n// Generated by FoamMesh canonical writer v1\n\n')


#: Plan 50 IP14 (GM-1). Rows formatted per write: a block of rows goes
#: through one ``%`` of a repeated row format, in C, instead of an f-string a
#: row. The text is the same, character for character (``'%.17g' % x`` and
#: ``f'{x:.17g}'`` are one formatter, ``'%d'`` and ``str`` one integer
#: spelling); the block bounds the memory the text takes.
_ROWS_PER_WRITE = 1 << 16


def _write_rows(stream, row_format, values, width):
    """Write *values* (``(n, width)`` or flat) a block of rows at a time."""
    flat = np.asarray(values).reshape(-1)
    step = _ROWS_PER_WRITE * width
    for start in range(0, flat.size, step):
        part = flat[start:start + step].tolist()
        stream.write((row_format * (len(part) // width)) % tuple(part))


def _write_points(path, points):
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header('vectorField', 'points'))
        stream.write(f'{len(points)}\n(\n')
        _write_rows(stream, '(%.17g %.17g %.17g)\n',
                    np.asarray(points, dtype=np.float64), 3)
        stream.write(')\n')


def _face_format(size):
    return f'{size}(' + ' '.join(['%d'] * size) + ')\n'


def _write_faces(path, faces, sizes):
    faces = np.asarray(faces)
    sizes = np.asarray(sizes).astype(np.int64)
    width = faces.shape[1] if faces.ndim == 2 else 0
    formats = {}
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header('faceList', 'faces'))
        stream.write(f'{len(faces)}\n(\n')
        for start in range(0, len(faces), _ROWS_PER_WRITE):
            rows = faces[start:start + _ROWS_PER_WRITE]
            counts = sizes[start:start + _ROWS_PER_WRITE]
            # row-major: each row's first `size` nodes, in order
            values = rows[np.arange(width)[None, :] < counts[:, None]]
            text = ''.join([
                formats.get(size) or formats.setdefault(size, _face_format(size))
                for size in counts.tolist()])
            stream.write(text % tuple(values.tolist()))
        stream.write(')\n')


def _write_labels(path, values, class_name):
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header(class_name, path.name))
        stream.write(f'{len(values)}\n(\n')
        _write_rows(stream, '%d\n', np.asarray(values, dtype=np.int64), 1)
        stream.write(')\n')


def _write_boundary(path, patch_ids, start_face, metadata):
    # Faces are already grouped contiguously in the deterministic patch order
    # chosen by _patch_ranks, so emit patches in order of first appearance
    # rather than by ascending numeric id.
    ids, first, counts = np.unique(
        patch_ids, return_index=True, return_counts=True)
    appearance = np.argsort(first)
    ids, counts = ids[appearance], counts[appearance]
    rows = []
    current = int(start_face)
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header('polyBoundaryMesh', 'boundary'))
        stream.write(f'{len(ids)}\n(\n')
        stable_names = {
            str(item.get('stable_id') or ''):
                _foam_name(item.get('solver_name') or item.get('name') or
                           f'patch_{int(item_id)}')
            for item_id, item in metadata.items()}
        for patch_id, count in zip(ids, counts):
            item = metadata[int(patch_id)]
            name = _foam_name(item.get('solver_name') or item.get('name') or
                              f'patch_{int(patch_id)}')
            patch_type = _foam_patch_type(item.get('category'))
            # Plan 49 SR5. A free mesh patch type the user chose on the
            # surface (``symmetry`` has no category of its own: the
            # ``symmetry`` category has always written ``symmetryPlane``).
            if str(item.get('patch_type') or '') in _FREE_PATCH_TYPES:
                patch_type = str(item['patch_type'])
            stream.write(
                f'{name}\n{{\n    type {patch_type};\n')
            interface = item.get('interface') or {}
            if patch_type == 'cyclic':
                neighbour = stable_names.get(
                    str(interface.get('neighbour_stable_id') or ''),
                    _foam_name(interface.get(
                        'neighbour_solver_name') or 'neighbour'))
                stream.write(f'    neighbourPatch {neighbour};\n')
                stream.write(
                    f"    matchTolerance "
                    f"{float(interface.get('match_tolerance', 1e-6)):.17g};\n")
                transform = str(interface.get('transform') or 'coincident')
                if transform == 'translational':
                    vector = np.asarray(
                        interface.get('translation') or (0, 0, 0),
                        dtype=float)
                    # Geometry intent stores the master -> slave transform.
                    # A cyclic patch dictionary stores neighbour -> current,
                    # so the master's transform is the inverse and the
                    # slave's is the authored direction.
                    if str(interface.get('role')) == 'master':
                        vector = -vector
                    stream.write('    transformType translational;\n')
                    stream.write(
                        '    separation '
                        f'({_foam_vector(vector)});\n')
                elif transform == 'rotational':
                    axis = interface.get('rotation_axis') or (0, 0, 1)
                    centre = interface.get('rotation_centre') or (0, 0, 0)
                    angle = float(
                        interface.get('rotation_angle_degrees', 0))
                    if str(interface.get('role')) == 'master':
                        angle = -angle
                    stream.write('    transformType rotational;\n')
                    stream.write(
                        f'    rotationAxis ({_foam_vector(axis)});\n')
                    stream.write(
                        f'    rotationCentre ({_foam_vector(centre)});\n')
                    stream.write(
                        '    rotationAngle '
                        f"{angle:.17g};\n")
                else:
                    stream.write('    transformType none;\n')
            stream.write(
                f'    nFaces {int(count)};\n'
                f'    startFace {current};\n}}\n')
            rows.append({'patch_id': int(patch_id), 'name': name, 'type': patch_type,
                         'nFaces': int(count), 'startFace': current})
            current += int(count)
        stream.write(')\n')
    return rows


#: Plan 49 SR5. The mesh patch types a surface may be given directly.
_FREE_PATCH_TYPES = ('patch', 'wall', 'symmetry', 'symmetryPlane', 'empty',
                     'wedge')


def _foam_patch_type(category):
    value = str(category or '').lower()
    # Plan 30 WP12. `wedge` is not a synonym for `symmetryPlane`: OpenFOAM
    # applies the axisymmetric transform across a wedge pair and treats a
    # symmetry plane as a mirror, so publishing an axisymmetric sector as two
    # symmetry planes solves a different problem without saying so.
    return {'wall': 'wall', 'symmetry': 'symmetryPlane', 'cyclic': 'cyclic',
            'symmetryplane': 'symmetryPlane', 'empty': 'empty',
            'wedge': 'wedge'}.get(value, 'patch')


def _write_cell_zones(path, mesh, topology=None):
    region_ids = (topology.cell_regions if topology is not None
                  and topology.cell_regions is not None
                  else np.concatenate([
                      block.region_ids for block in mesh.cell_blocks]))
    unique = sorted(
        {int(value) for value in region_ids},
        key=lambda value: _patch_sort_key(value, mesh.regions))
    if len(unique) <= 1:
        return []
    rows = []
    with path.open('w', encoding='ascii', newline='\n') as stream:
        # Foundation v13 reads this object as a cellZoneList.  The historical
        # generic regIOobject header is tolerated but emits a checkMesh warning.
        stream.write(_header('cellZoneList', 'cellZones'))
        stream.write(f'{len(unique)}\n(\n')
        for region_id in unique:
            item = mesh.regions[int(region_id)]
            name = _foam_name(
                item.get('solver_name') or item.get('name')
                or f'region_{region_id}')
            labels = np.flatnonzero(region_ids == region_id)
            stream.write(
                f'{name}\n{{\n    type cellZone;\n'
                f'    cellLabels List<label>\n    {len(labels)}\n    (\n')
            _write_rows(stream, '        %d\n', labels, 1)
            stream.write('    );\n}\n')
            rows.append({
                'region_id': region_id, 'name': name,
                'cell_count': int(len(labels)),
                'region_type': item.get('region_type', item.get('category')),
            })
        stream.write(')\n')
    return rows


def _cell_regions(mesh):
    """The region of every cell, in the global cell order the writer uses."""
    return np.concatenate([block.region_ids for block in mesh.cell_blocks])


def _write_face_zones(path, topology, mesh):
    """Label the faces where one cell zone meets another.

    DP-420. MEASURED on all four models this campaign meshed on both engines:
    every Gmsh multiregion mesh had a `cellZones` file and no `faceZones` file
    at all, where every snappy one had both. The interface was there and
    unlabelled -- 9,152 internal faces of refined `coaxial_ducts` have their
    owner in one zone and their neighbour in the other, out of 1,181,006
    internal faces -- so the conformal interface these eighty meshes exist to
    test was built by both engines and named by one. Nothing that reaches for
    it by name found it on a Gmsh mesh, the viewport included.

    It never had to be inferred: a face is on the interface exactly when its
    two cells belong to different zones, which is the same definition the
    audit counted with.
    """
    internal = int(topology.internal_count)
    if internal <= 0:
        return []
    regions = (topology.cell_regions if topology.cell_regions is not None
               else _cell_regions(mesh))
    owner = regions[topology.owner[:internal]]
    neighbour = regions[topology.neighbour[:internal]]
    crossing = np.flatnonzero(owner != neighbour)
    if not len(crossing):
        return []

    order = sorted({int(value) for value in regions},
                   key=lambda value: _patch_sort_key(value, mesh.regions))
    names = {}
    for region_id in order:
        item = mesh.regions[int(region_id)]
        names[int(region_id)] = _foam_name(
            item.get('solver_name') or item.get('name')
            or f'region_{region_id}')
    rank = {value: index for index, value in enumerate(order)}

    low = np.minimum(owner[crossing], neighbour[crossing])
    high = np.maximum(owner[crossing], neighbour[crossing])
    # The zone's normal points out of the region written first, so a face
    # whose owner is the other one is flipped. `flipMap` is what says so, and
    # a faceZone without it is not readable by OpenFOAM.
    pairs = sorted(
        {(int(a), int(b)) for a, b in zip(low, high)},
        key=lambda pair: sorted((rank[pair[0]], rank[pair[1]])))

    rows = []
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header('faceZoneList', 'faceZones'))
        stream.write(f'{len(pairs)}\n(\n')
        for pair in pairs:
            first, second = sorted(pair, key=lambda value: rank[value])
            selected = np.flatnonzero((low == pair[0]) & (high == pair[1]))
            labels = crossing[selected]
            flipped = owner[labels] != first
            name = _foam_name(f'{names[first]}_to_{names[second]}')
            stream.write(
                f'{name}\n{{\n    type faceZone;\n'
                f'    faceLabels List<label>\n    {len(labels)}\n    (\n')
            _write_rows(stream, '        %d\n', labels, 1)
            stream.write('    );\n    flipMap List<bool>\n'
                         f'    {len(labels)}\n    (\n')
            # DP-565: 0 and 1, not the words. OpenFOAM reads a bool either
            # way, but vtkOpenFOAMReader wants a number and drops the whole
            # faceZones block on the word `false`.
            _write_rows(stream, '        %d\n',
                        np.asarray(flipped, dtype=bool).astype(np.int64), 1)
            stream.write('    );\n}\n')
            rows.append({
                'name': name, 'face_count': int(len(labels)),
                'regions': [names[int(first)], names[int(second)]],
            })
        stream.write(')\n')
    return rows


def _validate_interface_pairs(mesh):
    """Fail closed when a conformal cyclic pair cannot map one-to-one."""
    by_pair = {}
    for patch_id, item in mesh.patches.items():
        interface = item.get('interface') or {}
        if interface.get('coupling') != 'cyclic':
            continue
        by_pair.setdefault(str(interface.get('pair_id')), {})[
            str(interface.get('role'))] = int(patch_id)
    faces_by_patch = {}
    for block in mesh.boundary_blocks:
        for patch_id in np.unique(block.patch_ids):
            selected = block.connectivity[block.patch_ids == patch_id]
            points = mesh.points[selected]
            centres = points.mean(axis=1)
            if block.cell_type.node_count == 3:
                areas = np.linalg.norm(
                    np.cross(points[:, 1] - points[:, 0],
                             points[:, 2] - points[:, 0]), axis=1) * 0.5
            else:
                areas = (
                    np.linalg.norm(
                        np.cross(points[:, 1] - points[:, 0],
                                 points[:, 2] - points[:, 0]), axis=1)
                    + np.linalg.norm(
                        np.cross(points[:, 2] - points[:, 0],
                                 points[:, 3] - points[:, 0]), axis=1)
                ) * 0.5
            existing = faces_by_patch.get(int(patch_id))
            if existing is None:
                faces_by_patch[int(patch_id)] = (centres, areas)
            else:
                faces_by_patch[int(patch_id)] = (
                    np.concatenate((existing[0], centres)),
                    np.concatenate((existing[1], areas)))
    for pair_id, roles in by_pair.items():
        if set(roles) != {'master', 'slave'}:
            raise PolyMeshWriteError(
                f'cyclic pair {pair_id!r} is missing master or slave patch')
        master_id, slave_id = roles['master'], roles['slave']
        if master_id not in faces_by_patch or slave_id not in faces_by_patch:
            raise PolyMeshWriteError(
                f'cyclic pair {pair_id!r} has an empty boundary patch')
        master_centres, master_areas = faces_by_patch[master_id]
        slave_centres, slave_areas = faces_by_patch[slave_id]
        if len(master_centres) != len(slave_centres):
            raise PolyMeshWriteError(
                f'cyclic pair {pair_id!r} face counts do not match')
        interface = mesh.patches[master_id]['interface']
        tolerance = float(interface.get('match_tolerance', 1e-6))
        transformed = _transform_points(master_centres, interface)
        if not _centres_correspond(transformed, slave_centres, tolerance):
            raise PolyMeshWriteError(
                f'cyclic pair {pair_id!r} face centres do not match within '
                f'tolerance {tolerance:g}')
        if not np.allclose(
                np.sort(master_areas), np.sort(slave_areas),
                rtol=_CYCLIC_AREA_RTOL, atol=tolerance * tolerance):
            raise PolyMeshWriteError(
                f'cyclic pair {pair_id!r} face areas do not match')


def _align_cyclic_face_starts(faces, face_sizes, owners, patch_ids, mesh):
    """Align coupled face order and point order for Foundation-v13 cyclic."""
    by_pair = {}
    for patch_id, item in mesh.patches.items():
        interface = item.get('interface') or {}
        if interface.get('coupling') != 'cyclic':
            continue
        by_pair.setdefault(str(interface.get('pair_id')), {})[
            str(interface.get('role'))] = int(patch_id)
    for pair_id, roles in by_pair.items():
        if set(roles) != {'master', 'slave'}:
            continue
        master_rows = np.flatnonzero(patch_ids == roles['master'])
        slave_rows = np.flatnonzero(patch_ids == roles['slave'])
        if len(master_rows) != len(slave_rows):
            continue
        interface = mesh.patches[roles['master']]['interface']
        remaining = set(int(value) for value in slave_rows)
        matched = []
        for master_row in master_rows:
            master_size = int(face_sizes[master_row])
            master_nodes = faces[master_row, :master_size]
            master_points = _transform_points(
                mesh.points[master_nodes], interface)
            master_centre = master_points.mean(axis=0)
            slave_row = min(
                remaining,
                key=lambda row: float(np.linalg.norm(
                    mesh.points[
                        faces[row, :int(face_sizes[row])]].mean(axis=0)
                    - master_centre)))
            remaining.remove(slave_row)
            matched.append((int(master_row), int(slave_row), master_points))
        source_faces = faces.copy()
        source_sizes = face_sizes.copy()
        source_owners = owners.copy()
        for destination_row, (master_row, source_row, master_points) in zip(
                slave_rows, matched):
            master_size = int(face_sizes[master_row])
            slave_size = int(source_sizes[source_row])
            if master_size != slave_size:
                raise PolyMeshWriteError(
                    f'cyclic pair {pair_id!r} has incompatible face vertices')
            slave_nodes = source_faces[source_row, :slave_size].copy()
            slave_points = mesh.points[slave_nodes]
            desired = master_points[
                [0, *range(master_size - 1, 0, -1)]]
            available = set(range(slave_size))
            ordering = []
            for point in desired:
                selected = min(
                    available,
                    key=lambda index: float(np.linalg.norm(
                        slave_points[index] - point)))
                available.remove(selected)
                ordering.append(selected)
            faces[destination_row] = -1
            faces[destination_row, :slave_size] = slave_nodes[ordering]
            face_sizes[destination_row] = source_sizes[source_row]
            owners[destination_row] = source_owners[source_row]


def _transform_points(points, interface):
    transform = str(interface.get('transform') or 'coincident')
    if transform == 'translational':
        return points + np.asarray(
            interface.get('translation') or (0, 0, 0), dtype=float)
    if transform != 'rotational':
        return points
    axis = np.asarray(
        interface.get('rotation_axis') or (0, 0, 1), dtype=float)
    axis /= np.linalg.norm(axis)
    centre = np.asarray(
        interface.get('rotation_centre') or (0, 0, 0), dtype=float)
    angle = np.deg2rad(float(interface.get('rotation_angle_degrees', 0)))
    shifted = points - centre
    cosine, sine = np.cos(angle), np.sin(angle)
    return (
        shifted * cosine
        + np.cross(axis, shifted) * sine
        + np.outer(shifted @ axis, axis) * (1 - cosine)
        + centre)


# DP-6128: a vertex 9e-9 m off on a 0.5 mm face changes its area by 4e-5 of
# itself; the centres already pin the geometry, so the areas only catch a
# gross mismatch.
_CYCLIC_AREA_RTOL = 1e-3


def _centres_correspond(moved, targets, tolerance):
    """True when each moved centre has its own target within *tolerance*.

    DP-6127. The pair was compared by rounding both sets of centres onto a
    grid of *tolerance* and testing the grids for equality. Two centres 9e-9 m
    apart (the single-precision rounding of a rotated STL face, measured on a
    30 degree sector) fall on either side of a grid line for about 1 in 100
    coordinates, so a mesh of 236,000 cells was refused at export for faces
    that correspond. A nearest-neighbour match is the same test without the
    grid line.
    """
    from scipy.spatial import cKDTree
    distance, index = cKDTree(np.asarray(targets, dtype=float)).query(
        np.asarray(moved, dtype=float))
    return bool(np.all(distance <= tolerance)
                and len(np.unique(index)) == len(index))


def _foam_vector(values):
    return ' '.join(f'{float(value):.17g}' for value in values)


def _foam_name(value):
    safe = ''.join(character if character.isalnum() or character == '_'
                   else '_' for character in str(value)).strip('_')
    if not safe:
        safe = 'patch'
    if safe[0].isdigit():
        safe = 'patch_' + safe
    return safe


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()
