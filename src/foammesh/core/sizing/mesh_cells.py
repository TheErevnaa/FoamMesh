"""Cell centres and volumes of a polyMesh, for the Size conformity indicator.

Plan 41 GN25c (section 12.2 S6). The same decomposition OpenFOAM uses for its
own cell centres: faces split into triangles about an estimated face centre,
cells into pyramids about an estimated cell centre. No Qt, no VTK.
"""
from __future__ import annotations

import numpy as np

from . import conformity as conformity_mod


def cell_geometry(poly) -> tuple[np.ndarray, np.ndarray]:
    """``(centres (n, 3), volumes (n,))`` of a ``PolyMesh``."""
    points = np.asarray(poly.points, dtype=float)
    vertices = np.asarray(poly.face_vertices, dtype=np.int64)
    offsets = np.asarray(poly.face_offsets, dtype=np.int64)
    n_faces = len(offsets) - 1
    counts = np.diff(offsets)
    face_of = np.repeat(np.arange(n_faces), counts)
    # The next vertex round each face, so every corner is one triangle edge.
    following = np.arange(len(vertices)) + 1
    last = offsets[1:] - 1
    following[last] = offsets[:-1]
    a = points[vertices]
    b = points[vertices[following]]
    estimate = np.zeros((n_faces, 3))
    for axis in range(3):
        estimate[:, axis] = np.bincount(
            face_of, weights=a[:, axis], minlength=n_faces) / np.maximum(
                counts, 1)
    c = estimate[face_of]
    area = 0.5 * np.cross(a - c, b - c)
    magnitude = np.linalg.norm(area, axis=1)
    centroid = (c + a + b) / 3.0
    sf = np.zeros((n_faces, 3))
    weighted = np.zeros((n_faces, 3))
    for axis in range(3):
        sf[:, axis] = np.bincount(face_of, weights=area[:, axis],
                                  minlength=n_faces)
        weighted[:, axis] = np.bincount(
            face_of, weights=magnitude * centroid[:, axis],
            minlength=n_faces)
    total = np.bincount(face_of, weights=magnitude, minlength=n_faces)
    face_centre = np.where(total[:, None] > 0,
                           weighted / np.maximum(total, 1e-300)[:, None],
                           estimate)
    owner = np.asarray(poly.owner, dtype=np.int64)
    neighbour = np.asarray(poly.neighbour, dtype=np.int64)
    n_cells = int(max(owner.max(), neighbour.max() if len(neighbour) else 0)) + 1
    cell_of = np.concatenate([owner, neighbour])
    face_index = np.concatenate([np.arange(n_faces), np.arange(len(neighbour))])
    sign = np.concatenate([np.ones(n_faces), -np.ones(len(neighbour))])
    guess = np.zeros((n_cells, 3))
    seen = np.bincount(cell_of, minlength=n_cells).astype(float)
    for axis in range(3):
        guess[:, axis] = np.bincount(
            cell_of, weights=face_centre[face_index, axis],
            minlength=n_cells) / np.maximum(seen, 1.0)
    fc = face_centre[face_index]
    pyramid = sign * np.einsum('ij,ij->i', sf[face_index],
                               fc - guess[cell_of]) / 3.0
    pyramid_centre = 0.75 * fc + 0.25 * guess[cell_of]
    volume = np.bincount(cell_of, weights=pyramid, minlength=n_cells)
    centre = np.zeros((n_cells, 3))
    for axis in range(3):
        centre[:, axis] = np.bincount(
            cell_of, weights=pyramid * pyramid_centre[:, axis],
            minlength=n_cells)
    centre = np.where(np.abs(volume)[:, None] > 0,
                      centre / np.where(volume == 0, 1.0, volume)[:, None],
                      guess)
    return centre, volume


def hex_cells(poly) -> np.ndarray:
    """Boolean mask of the cells with exactly six faces, all quadrilaterals.

    ``refineMesh`` (hexRef8) splits hexahedra only; a prism or a cell with a
    hanging node stops it.
    """
    offsets = np.asarray(poly.face_offsets, dtype=np.int64)
    quad = np.diff(offsets) == 4
    owner = np.asarray(poly.owner, dtype=np.int64)
    neighbour = np.asarray(poly.neighbour, dtype=np.int64)
    n_cells = int(max(owner.max(), neighbour.max() if len(neighbour) else 0)) + 1
    cell_of = np.concatenate([owner, neighbour])
    quad_of = np.concatenate([quad, quad[:len(neighbour)]])
    faces = np.bincount(cell_of, minlength=n_cells)
    quads = np.bincount(cell_of, weights=quad_of, minlength=n_cells)
    return (faces == 6) & (quads == 6)


def tet_cells(poly) -> np.ndarray:
    """Boolean mask of the cells with exactly four faces, all triangles."""
    offsets = np.asarray(poly.face_offsets, dtype=np.int64)
    triangle = np.diff(offsets) == 3
    owner = np.asarray(poly.owner, dtype=np.int64)
    neighbour = np.asarray(poly.neighbour, dtype=np.int64)
    n_cells = int(max(owner.max(), neighbour.max() if len(neighbour) else 0)) + 1
    cell_of = np.concatenate([owner, neighbour])
    tri_of = np.concatenate([triangle, triangle[:len(neighbour)]])
    faces = np.bincount(cell_of, minlength=n_cells)
    triangles = np.bincount(cell_of, weights=tri_of, minlength=n_cells)
    return (faces == 4) & (triangles == 4)


def cell_sizes(poly, volumes) -> np.ndarray:
    """The size each cell is judged by: the edge of the cube with its volume,
    and for a tetrahedron the edge of the regular tetrahedron with its volume
    (a tetrahedron of edge ``a`` has ``cbrt(volume) = 0.49 a``, so the cube
    reading would call every well-sized tetrahedron half the size asked for).
    """
    sizes = conformity_mod.cell_size(volumes)
    tets = tet_cells(poly)
    volumes = np.asarray(volumes, dtype=float)
    sizes[tets] = conformity_mod.tet_size(volumes[tets])
    return sizes


def mesh_conformity(field, case_path) -> conformity_mod.Conformity:
    """Size conformity of the polyMesh in a case against the field."""
    from foammesh.core.mesh.poly_mesh_boundary import read_poly_mesh
    poly = read_poly_mesh(case_path)
    centres, volumes = cell_geometry(poly)
    return conformity_mod.conformity(
        field, centres, cell_sizes(poly, volumes))


def worst_cells(found: conformity_mod.Conformity, limit: int = 200):
    """Indices of the most under-resolved cells, for picking."""
    ratio = np.where(np.isfinite(found.ratio), found.ratio, -np.inf)
    order = np.argsort(ratio)[::-1][:limit]
    return [int(i) for i in order if found.under[i]]
