"""Deterministic OpenFOAM polyMesh writer from the canonical mixed mesh."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil

import numpy as np

from foammesh.core.mesh.connectivity import _FACES
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
        }


@dataclass(frozen=True)
class _FoamTopology:
    faces: np.ndarray
    face_sizes: np.ndarray
    owner: np.ndarray
    neighbour: np.ndarray
    patch_ids: np.ndarray
    internal_count: int


class FoamPolyMeshWriter:
    """Write one-region linear meshes without an OpenFOAM runtime dependency."""

    def write(self, destination: str | Path, mesh: CanonicalMesh) -> PolyMeshWriteReport:
        report = validate_mesh(mesh)
        if not report.valid:
            codes = ', '.join(issue.code for issue in report.issues)
            raise PolyMeshWriteError(f'canonical mesh is invalid: {codes}')
        _validate_interface_pairs(mesh)
        destination = Path(destination).resolve()
        if destination.exists():
            raise PolyMeshWriteError('polyMesh destination already exists')
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
                temporary / 'cellZones', mesh)
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
            str(destination), mesh.point_count, mesh.cell_count,
            len(topology.owner), topology.internal_count,
            len(topology.owner) - topology.internal_count,
            tuple(patches), checksums, tuple(regions), tuple(zones))


def _foam_topology(mesh: CanonicalMesh) -> _FoamTopology:
    keys = []
    oriented = []
    sizes = []
    global_cells = []
    offset = 0
    for block in mesh.cell_blocks:
        cell_ids = offset + np.arange(block.count, dtype=np.int64)
        for face in _FACES[block.cell_type]:
            selected = np.ascontiguousarray(block.connectivity[:, face])
            padded = np.full((block.count, 4), -1, dtype=np.int64)
            padded[:, :len(face)] = selected
            key = np.full((block.count, 4), -1, dtype=np.int64)
            key[:, :len(face)] = np.sort(selected, axis=1)
            oriented.append(padded)
            keys.append(key)
            sizes.append(np.full(block.count, len(face), dtype=np.int8))
            global_cells.append(cell_ids)
        offset += block.count
    keys = np.ascontiguousarray(np.concatenate(keys))
    oriented = np.ascontiguousarray(np.concatenate(oriented))
    sizes = np.concatenate(sizes)
    global_cells = np.concatenate(global_cells)
    unique, inverse, counts = np.unique(
        keys, axis=0, return_inverse=True, return_counts=True)
    if np.any(counts > 2):
        raise PolyMeshWriteError('non-manifold canonical faces cannot be exported')
    occurrence = np.arange(len(inverse), dtype=np.int64)
    order = np.lexsort((occurrence, global_cells, inverse))
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    owner_occurrence = order[starts]
    owners = global_cells[owner_occurrence]
    neighbour_occurrence = order[starts[counts == 2] + 1]
    neighbours_by_unique = np.full(len(unique), -1, dtype=np.int64)
    neighbours_by_unique[np.flatnonzero(counts == 2)] = global_cells[neighbour_occurrence]

    internal_ids = np.flatnonzero(counts == 2)
    # OpenFOAM requires internal faces in upper-triangular order: ascending
    # owner, then ascending neighbour. Face-key order does not satisfy this and
    # makes checkMesh report "Faces not in upper triangular order".
    internal_ids = internal_ids[np.lexsort((
        neighbours_by_unique[internal_ids], owners[internal_ids]))]
    boundary_ids = np.flatnonzero(counts == 1)
    boundary_patch_ids = _match_boundary_patches(
        unique[boundary_ids], mesh)
    # Patch ids are engine-assigned (MED family numbers, for example) and their
    # numeric order depends on group declaration order, not on the mesh. Rank
    # patches by a stable identity so the same logical mesh always yields the
    # same boundary ordering and checksums (S-D9, §4.6).
    ranks = _patch_ranks(boundary_patch_ids, mesh.patches)
    boundary_order = np.lexsort((
        unique[boundary_ids, 3], unique[boundary_ids, 2],
        unique[boundary_ids, 1], unique[boundary_ids, 0], ranks))
    ordered_unique = np.concatenate((internal_ids, boundary_ids[boundary_order]))
    faces = oriented[owner_occurrence[ordered_unique]]
    face_sizes = sizes[owner_occurrence[ordered_unique]]
    owner = owners[ordered_unique]
    neighbour = neighbours_by_unique[internal_ids]
    patches = boundary_patch_ids[boundary_order]
    _align_cyclic_face_starts(
        faces[len(internal_ids):], face_sizes[len(internal_ids):],
        owner[len(internal_ids):], patches, mesh)
    return _FoamTopology(faces, face_sizes, owner, neighbour, patches, len(internal_ids))


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
    return np.array([rank[int(value)] for value in patch_ids], dtype=np.int64)


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


def _write_points(path, points):
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header('vectorField', 'points'))
        stream.write(f'{len(points)}\n(\n')
        for x, y, z in points:
            stream.write(f'({x:.17g} {y:.17g} {z:.17g})\n')
        stream.write(')\n')


def _write_faces(path, faces, sizes):
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header('faceList', 'faces'))
        stream.write(f'{len(faces)}\n(\n')
        for row, size in zip(faces, sizes):
            nodes = ' '.join(str(int(value)) for value in row[:int(size)])
            stream.write(f'{int(size)}({nodes})\n')
        stream.write(')\n')


def _write_labels(path, values, class_name):
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(_header(class_name, path.name))
        stream.write(f'{len(values)}\n(\n')
        for value in values:
            stream.write(f'{int(value)}\n')
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


def _foam_patch_type(category):
    value = str(category or '').lower()
    # Plan 30 WP12. `wedge` is not a synonym for `symmetryPlane`: OpenFOAM
    # applies the axisymmetric transform across a wedge pair and treats a
    # symmetry plane as a mirror, so publishing an axisymmetric sector as two
    # symmetry planes solves a different problem without saying so.
    return {'wall': 'wall', 'symmetry': 'symmetryPlane', 'cyclic': 'cyclic',
            'symmetryplane': 'symmetryPlane', 'empty': 'empty',
            'wedge': 'wedge'}.get(value, 'patch')


def _write_cell_zones(path, mesh):
    region_ids = np.concatenate([
        block.region_ids for block in mesh.cell_blocks])
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
            for label in labels:
                stream.write(f'        {int(label)}\n')
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
    regions = _cell_regions(mesh)
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
            for label in labels:
                stream.write(f'        {int(label)}\n')
            stream.write('    );\n    flipMap List<bool>\n'
                         f'    {len(labels)}\n    (\n')
            # DP-565: 0 and 1, not the words. OpenFOAM reads a bool either
            # way, but vtkOpenFOAMReader wants a number and drops the whole
            # faceZones block on the word `false`.
            for value in flipped:
                stream.write('        %d\n' % (1 if value else 0))
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
        master_keys = _quantized_rows(transformed, tolerance)
        slave_keys = _quantized_rows(slave_centres, tolerance)
        if not np.array_equal(master_keys, slave_keys):
            raise PolyMeshWriteError(
                f'cyclic pair {pair_id!r} face centres do not match within '
                f'tolerance {tolerance:g}')
        if not np.allclose(
                np.sort(master_areas), np.sort(slave_areas),
                rtol=tolerance, atol=tolerance * tolerance):
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


def _quantized_rows(points, tolerance):
    values = np.rint(np.asarray(points) / tolerance).astype(np.int64)
    order = np.lexsort((values[:, 2], values[:, 1], values[:, 0]))
    return values[order]


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
