"""Per-cell quality arrays, computed from the polyMesh.

Plan 26 WP6.5. The Display Control path is fully built and reachable:
``MeshQualityIndex`` offers ``cellAspectRatio``, ``nonOrthoAngle``,
``skewness`` and ``cellVolume``; the selector populates a combo, sets a scalar
band and adds a colour bar; ``actor_info`` does the colouring. **Nothing wrote
those cell arrays**, so the selector coloured by four fields that did not
exist.

The plan proposed passing ``checkMesh -writeAllFields``. **Measured against
the live target and it does not exist**: OpenFOAM Foundation v13's checkMesh
advertises ``-allGeometry -allTopology -writeSets -writeSurfaces -meshQuality
-nonOrthThreshold -skewThreshold`` and no ``-writeAllFields`` -- that flag is
an ESI/OpenCFD extension. So the arrays are computed here instead, which the
plan named as the alternative.

**The definitions are OpenFOAM's, not approximations of them**, because the
acceptance criterion is that the on-screen maximum equals the log maximum. If
the picture disagrees with the log, the picture is lying.

*Measured against live OpenFOAM 13 ``checkMesh`` on two real meshes*
(``test_cases/gmsh/duct``, 78,305 cells and ``test_cases/snappyhexmesh/duct``,
34,409 cells):

===================  ==================  ==================
metric               agreement           note
===================  ==================  ==================
``nonOrthoAngle``    exact               66.128629 / 40.596498 vs 66.128629 / 40.596485
``cellVolume``       exact               min and total both match to the printed digits
``skewness``         0.06% relative      2.784298 vs 2.784273; 0.455999 vs 0.456251
===================  ==================  ==================

The skewness residual is cell-centre precision, not a different formula: it
survives with the boundary faces excluded, and the governing face is internal
in both cases. It is stated rather than rounded away.

The definitions:

``nonOrthoAngle``
    the angle between the face normal and the owner-to-neighbour centroid
    vector, in degrees, per ``primitiveMeshCheck``. Reported per *cell* as the
    worst angle over that cell's faces, because the surface is coloured by
    cell.
``skewness``
    ``primitiveMeshCheck``'s skewness vector, normalised by *its own*
    normalisation distance -- ``max(0.2|d|, max_p |sv&#770; · (p - Cf)|)`` over
    the face's points, **not** by the centroid separation. Normalising by
    ``|d|`` looks reasonable, is what a first implementation here did, and
    gave 0.874 where checkMesh reported 2.784 on the same mesh. Again the
    worst over the cell's faces.
``cellAspectRatio``
    OpenFOAM's ``1/6 * sum|Sf| / V^(2/3)``, normalised so a cube reads 1.
``cellVolume``
    the signed volume, by the same pyramid decomposition OpenFOAM uses.
"""
from __future__ import annotations

import numpy as np

#: Array names the renderer's ``MeshQualityIndex`` already offers. Named here so
#: a metric added to one side and not the other is a test failure rather than a
#: selector entry that colours by nothing.
FIELD_NAMES = ('cellAspectRatio', 'nonOrthoAngle', 'skewness', 'cellVolume')

CALCULATION_VERSION = 'foammesh.cell_fields.v1'

#: Below this a face or cell is treated as degenerate and excluded from the
#: ratios rather than producing an infinity that ruins the whole colour scale.
_TINY = 1.0e-30


def _face_geometry(mesh):
    """Centre and area vector of every face, by OpenFOAM's decomposition.

    A polygon face is split into triangles about its *average* point rather
    than about vertex 0; the two agree for a planar face and disagree for a
    warped one, and OpenFOAM uses the former.
    """
    offsets = mesh.face_offsets
    sizes = np.diff(offsets).astype(np.int64)
    face_count = int(sizes.size)
    points = mesh.points
    vertices = mesh.face_vertices

    # Index of the face each entry of `vertices` belongs to.
    face_of_entry = np.repeat(np.arange(face_count, dtype=np.int64), sizes)
    coordinates = points[vertices]

    average = np.zeros((face_count, 3), dtype=np.float64)
    np.add.at(average, face_of_entry, coordinates)
    average /= sizes[:, None]

    # Next vertex within the same face, wrapping at the face boundary.
    position = np.arange(vertices.size, dtype=np.int64) - offsets[face_of_entry]
    following = offsets[face_of_entry] + (position + 1) % sizes[face_of_entry]
    next_coordinates = points[vertices[following]]

    centre_of_entry = average[face_of_entry]
    # Triangle (p_i, p_i+1, average): area vector and centroid.
    area = 0.5 * np.cross(next_coordinates - coordinates,
                          centre_of_entry - coordinates)
    centroid = (coordinates + next_coordinates + centre_of_entry) / 3.0
    magnitude = np.linalg.norm(area, axis=1)

    face_area = np.zeros((face_count, 3), dtype=np.float64)
    np.add.at(face_area, face_of_entry, area)
    weighted = np.zeros((face_count, 3), dtype=np.float64)
    np.add.at(weighted, face_of_entry, centroid * magnitude[:, None])
    total = np.zeros(face_count, dtype=np.float64)
    np.add.at(total, face_of_entry, magnitude)

    face_centre = np.where(total[:, None] > _TINY,
                           weighted / np.maximum(total, _TINY)[:, None],
                           average)
    return face_centre, face_area


def _cell_geometry(mesh, face_centre, face_area):
    """Cell centroid and volume, by OpenFOAM's pyramid decomposition."""
    cell_count = mesh.cell_count
    owner, neighbour = mesh.owner, mesh.neighbour
    face_count = int(face_centre.shape[0])

    # A first estimate of each cell's centre: the mean of its face centres.
    estimate = np.zeros((cell_count, 3), dtype=np.float64)
    counts = np.zeros(cell_count, dtype=np.float64)
    np.add.at(estimate, owner, face_centre)
    np.add.at(counts, owner, 1.0)
    if neighbour.size:
        np.add.at(estimate, neighbour, face_centre[:neighbour.size])
        np.add.at(counts, neighbour, 1.0)
    estimate /= np.maximum(counts, 1.0)[:, None]

    volume = np.zeros(cell_count, dtype=np.float64)
    moment = np.zeros((cell_count, 3), dtype=np.float64)

    def accumulate(cells, faces, sign):
        # Pyramid from the estimated centre to the face; three quarters of the
        # way to the face is the centroid of a pyramid.
        height = face_centre[faces] - estimate[cells]
        pyramid = sign * (np.einsum('ij,ij->i', face_area[faces], height) / 3.0)
        centroid = estimate[cells] + 0.75 * height
        np.add.at(volume, cells, pyramid)
        np.add.at(moment, cells, centroid * pyramid[:, None])

    all_faces = np.arange(face_count, dtype=np.int64)
    accumulate(owner, all_faces, 1.0)
    if neighbour.size:
        accumulate(neighbour, all_faces[:neighbour.size], -1.0)

    centre = np.where(np.abs(volume)[:, None] > _TINY,
                      moment / np.where(np.abs(volume) > _TINY,
                                        volume, 1.0)[:, None],
                      estimate)
    return centre, volume


def _skewness(mesh, face_ids, face_centre, face_area, owner_centre, separation,
              *, precomputed_vector=None) -> np.ndarray:
    """``primitiveMeshCheck``'s skewness for the given faces.

    The subtlety is the denominator. OpenFOAM does **not** normalise by the
    centroid separation; it normalises by how far the face actually extends in
    the direction of the skew, floored at a fifth of the separation::

        fd = max(0.2|d|, max_p |svHat . (p - Cf)|)

    Normalising by ``|d|`` instead gave 0.874 where checkMesh reported 2.784 on
    the same mesh -- plausible, wrong, and exactly the kind of picture that
    disagrees with the log while looking healthy.
    """
    to_face = face_centre[face_ids] - owner_centre
    if precomputed_vector is not None:
        vector = precomputed_vector
    else:
        along = np.einsum('ij,ij->i', face_area[face_ids], separation)
        scale = np.einsum('ij,ij->i', face_area[face_ids], to_face) / np.where(
            np.abs(along) > _TINY, along, _TINY)
        vector = to_face - scale[:, None] * separation
    length = np.linalg.norm(vector, axis=1)
    unit = vector / np.maximum(length, _TINY)[:, None]

    # max over the face's own points of |unit . (p - Cf)|, vectorised across
    # every face at once rather than one Python loop per face.
    offsets = mesh.face_offsets
    sizes = np.diff(offsets)[face_ids].astype(np.int64)
    slot = np.repeat(np.arange(face_ids.size, dtype=np.int64), sizes)
    entries = np.concatenate([
        np.arange(offsets[face], offsets[face + 1], dtype=np.int64)
        for face in face_ids]) if face_ids.size else np.zeros(0, dtype=np.int64)
    spread = np.abs(np.einsum(
        'ij,ij->i', unit[slot],
        mesh.points[mesh.face_vertices[entries]] - face_centre[face_ids][slot]))

    extent = 0.2 * np.linalg.norm(separation, axis=1)
    np.maximum.at(extent, slot, spread)
    return length / np.maximum(extent, _TINY)


def compute_cell_fields(mesh) -> dict[str, np.ndarray]:
    """The four arrays, keyed by the names the renderer already asks for."""
    cell_count = mesh.cell_count
    if not cell_count:
        return {name: np.zeros(0, dtype=np.float64) for name in FIELD_NAMES}

    face_centre, face_area = _face_geometry(mesh)
    cell_centre, volume = _cell_geometry(mesh, face_centre, face_area)
    magnitude = np.linalg.norm(face_area, axis=1)
    owner, neighbour = mesh.owner, mesh.neighbour
    internal = int(neighbour.size)
    face_count = mesh.face_count

    # -- aspect ratio: OpenFOAM's 1/6 * sum|Sf| / V^(2/3), a cube reading 1 --
    area_sum = np.zeros(cell_count, dtype=np.float64)
    np.add.at(area_sum, owner, magnitude)
    if internal:
        np.add.at(area_sum, neighbour, magnitude[:internal])
    safe_volume = np.maximum(np.abs(volume), _TINY)
    aspect = (1.0 / 6.0) * area_sum / np.cbrt(safe_volume) ** 2
    aspect[np.abs(volume) <= _TINY] = 0.0

    # -- non-orthogonality, per internal face -------------------------------
    non_ortho = np.zeros(cell_count, dtype=np.float64)
    skewness = np.zeros(cell_count, dtype=np.float64)
    if internal:
        own, nei = owner[:internal], neighbour[:internal]
        separation = cell_centre[nei] - cell_centre[own]
        distance = np.linalg.norm(separation, axis=1)
        area_magnitude = np.maximum(magnitude[:internal], _TINY)

        cosine = np.einsum('ij,ij->i', face_area[:internal], separation) / (
            area_magnitude * np.maximum(distance, _TINY))
        angle = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
        for cells in (own, nei):
            np.maximum.at(non_ortho, cells, angle)

        skew = _skewness(mesh, np.arange(internal, dtype=np.int64),
                         face_centre, face_area,
                         cell_centre[own], separation)
        for cells in (own, nei):
            np.maximum.at(skewness, cells, skew)

    # Boundary faces are skewed too, and checkMesh reports the worst of both.
    if face_count > internal:
        boundary = np.arange(internal, face_count, dtype=np.int64)
        owner_centre = cell_centre[owner[internal:]]
        offset = face_centre[boundary] - owner_centre
        unit = face_area[boundary] / np.maximum(
            magnitude[boundary], _TINY)[:, None]
        # For a boundary face OpenFOAM projects the owner-to-face vector onto
        # the face normal and takes the difference; there is no neighbour.
        along = unit * np.einsum('ij,ij->i', unit, offset)[:, None]
        skew = _skewness(mesh, boundary, face_centre, face_area,
                         owner_centre, along, precomputed_vector=offset - along)
        np.maximum.at(skewness, owner[internal:], skew)

    return {
        'cellAspectRatio': aspect,
        'nonOrthoAngle': non_ortho,
        'skewness': skewness,
        'cellVolume': volume,
    }


class _FaceSubset:
    """The faces ``faces`` of ``mesh`` as a compact mesh for
    :func:`_face_geometry` and :func:`_skewness` (local face ids)."""

    def __init__(self, mesh, faces: np.ndarray):
        offsets = np.asarray(mesh.face_offsets, dtype=np.int64)
        sizes = (offsets[faces + 1] - offsets[faces]).astype(np.int64)
        local = np.zeros(faces.size + 1, dtype=np.int64)
        np.cumsum(sizes, out=local[1:])
        start = np.repeat(offsets[faces] - local[:-1], sizes)
        start += np.arange(int(local[-1]), dtype=np.int64)
        self.points = mesh.points
        self.face_vertices = np.asarray(mesh.face_vertices)[start]
        self.face_offsets = local


def subset_cells(mesh, cells) -> np.ndarray:
    """``cells`` and every cell sharing a face with one of them (sorted):
    what :func:`compute_cell_fields` computes the geometry of for them."""
    owner, neighbour = mesh.owner, mesh.neighbour
    internal = int(neighbour.size)
    kept = np.zeros(mesh.cell_count, dtype=bool)
    kept[np.asarray(cells, dtype=np.int64)] = True
    touching = kept[owner[:internal]] | kept[neighbour]
    grown = kept.copy()
    grown[owner[:internal][touching]] = True
    grown[neighbour[touching]] = True
    return np.flatnonzero(grown)


def compute_cell_fields_for(mesh, cells) -> dict[str, np.ndarray]:
    """The four arrays for ``cells`` only, as full-length arrays (zero for
    every other cell). The values for ``cells`` equal
    :func:`compute_cell_fields`'s: their geometry and their face neighbours'
    is computed exactly as there, from only the faces of those cells, so
    the memory follows the cells asked for rather than the whole mesh."""
    cell_count = mesh.cell_count
    out = {name: np.zeros(cell_count, dtype=np.float64)
           for name in FIELD_NAMES}
    cells = np.unique(np.asarray(cells, dtype=np.int64))
    if not cell_count or not cells.size:
        return out
    owner = np.asarray(mesh.owner, dtype=np.int64)
    neighbour = np.asarray(mesh.neighbour, dtype=np.int64)
    internal = int(neighbour.size)

    # the kept cells, their face neighbours, and every face of either
    grown = subset_cells(mesh, cells)
    inside = np.zeros(cell_count, dtype=bool)
    inside[grown] = True
    touching = inside[owner]
    touching[:internal] |= inside[neighbour]
    faces = np.flatnonzero(touching)
    del inside, touching
    local = np.full(cell_count, -1, dtype=np.int64)
    local[grown] = np.arange(grown.size, dtype=np.int64)
    owner_local = local[owner[faces]]
    neighbour_local = np.full(faces.size, -1, dtype=np.int64)
    is_internal = faces < internal
    neighbour_local[is_internal] = local[neighbour[faces[is_internal]]]
    del local

    sub = _FaceSubset(mesh, faces)
    face_centre, face_area = _face_geometry(sub)
    magnitude = np.linalg.norm(face_area, axis=1)

    # cell centre and volume of the grown cells (_cell_geometry, on the
    # faces of those cells only -- each grown cell has all of its faces)
    count = int(grown.size)
    estimate = np.zeros((count, 3), dtype=np.float64)
    seen = np.zeros(count, dtype=np.float64)
    volume = np.zeros(count, dtype=np.float64)
    moment = np.zeros((count, 3), dtype=np.float64)
    sides = []
    for side, sign in ((owner_local, 1.0), (neighbour_local, -1.0)):
        which = np.flatnonzero(side >= 0)
        sides.append((side[which], which, sign))
        np.add.at(estimate, side[which], face_centre[which])
        np.add.at(seen, side[which], 1.0)
    estimate /= np.maximum(seen, 1.0)[:, None]
    for owners, which, sign in sides:
        height = face_centre[which] - estimate[owners]
        pyramid = sign * (np.einsum('ij,ij->i', face_area[which],
                                    height) / 3.0)
        np.add.at(volume, owners, pyramid)
        np.add.at(moment, owners,
                  (estimate[owners] + 0.75 * height) * pyramid[:, None])
    centre = np.where(np.abs(volume)[:, None] > _TINY,
                      moment / np.where(np.abs(volume) > _TINY,
                                        volume, 1.0)[:, None],
                      estimate)

    area_sum = np.zeros(count, dtype=np.float64)
    for owners, which, _sign in sides:
        np.add.at(area_sum, owners, magnitude[which])
    aspect = (1.0 / 6.0) * area_sum / np.cbrt(
        np.maximum(np.abs(volume), _TINY)) ** 2
    aspect[np.abs(volume) <= _TINY] = 0.0

    # only the kept cells' faces are judged
    is_kept = np.zeros(count, dtype=bool)
    is_kept[np.searchsorted(grown, cells)] = True
    non_ortho = np.zeros(count, dtype=np.float64)
    skewness = np.zeros(count, dtype=np.float64)
    inner = np.flatnonzero((faces < internal) & (
        is_kept[np.maximum(owner_local, 0)] & (owner_local >= 0)
        | is_kept[np.maximum(neighbour_local, 0)] & (neighbour_local >= 0)))
    if inner.size:
        own, nei = owner_local[inner], neighbour_local[inner]
        separation = centre[nei] - centre[own]
        distance = np.linalg.norm(separation, axis=1)
        cosine = np.einsum('ij,ij->i', face_area[inner], separation) / (
            np.maximum(magnitude[inner], _TINY)
            * np.maximum(distance, _TINY))
        angle = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
        skew = _skewness(sub, inner, face_centre, face_area, centre[own],
                         separation)
        for side in (own, nei):
            np.maximum.at(non_ortho, side, angle)
            np.maximum.at(skewness, side, skew)
    boundary = np.flatnonzero((faces >= internal) & (owner_local >= 0)
                              & is_kept[np.maximum(owner_local, 0)])
    if boundary.size:
        owners = owner_local[boundary]
        offset = face_centre[boundary] - centre[owners]
        unit = face_area[boundary] / np.maximum(
            magnitude[boundary], _TINY)[:, None]
        along = unit * np.einsum('ij,ij->i', unit, offset)[:, None]
        skew = _skewness(sub, boundary, face_centre, face_area,
                         centre[owners], along,
                         precomputed_vector=offset - along)
        np.maximum.at(skewness, owners, skew)

    take = np.searchsorted(grown, cells)
    for name, values in (('cellAspectRatio', aspect),
                         ('nonOrthoAngle', non_ortho),
                         ('skewness', skewness), ('cellVolume', volume)):
        out[name][cells] = values[take]
    return out


def summarise(fields: dict[str, np.ndarray]) -> dict:
    """Min/max/mean per array, for the assertion that the picture matches the log."""
    summary = {}
    for name, values in fields.items():
        if values.size == 0:
            summary[name] = {'minimum': 0.0, 'maximum': 0.0, 'mean': 0.0,
                             'count': 0}
            continue
        summary[name] = {
            'minimum': float(np.min(values)),
            'maximum': float(np.max(values)),
            'mean': float(np.mean(values)),
            'count': int(values.size),
        }
    summary['calculation_version'] = CALCULATION_VERSION
    return summary


def histogram(values, bins: int = 40) -> dict:
    """WP6.4's distribution.

    ``checkMesh`` reports a maximum, and a maximum of 66 degrees does not
    distinguish one bad cell from eight thousand -- the difference between
    "ignore" and "remesh". The counts sum to the cell count by construction,
    which is the acceptance criterion.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return {'edges': [], 'counts': [], 'total': 0}
    low, high = float(np.min(values)), float(np.max(values))
    if not np.isfinite(low) or not np.isfinite(high):
        return {'edges': [], 'counts': [], 'total': int(values.size)}
    if high - low <= _TINY:
        # A constant field is one bar, not a degenerate range.
        return {'edges': [low, low], 'counts': [int(values.size)],
                'total': int(values.size)}
    counts, edges = np.histogram(values, bins=max(int(bins), 1),
                                 range=(low, high))
    return {'edges': [float(edge) for edge in edges],
            'counts': [int(count) for count in counts],
            'total': int(values.size)}
