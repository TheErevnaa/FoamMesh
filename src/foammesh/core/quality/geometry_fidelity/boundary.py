"""Named boundary sections, keyed by the identity §4 measures per section.

Plan 23 WP3. The checker never works on "the boundary": §4 requires every
prepared section to be judged independently, because a nearest-distance
statistic over the union would let a face assigned to one patch sit on a nearby
but incorrect patch and still score well.

So this turns a published mesh into sections carrying the prepared
``patch_uuid``, and states plainly which sections could not be identified. A
section whose identity was invented cannot be rated -- it would attach a
measurement to geometry it may not describe.

Reconciliation is against the prepared group manifest, so all four outcomes are
explicit rather than implied by absence:

* **matched** -- published, prepared, and the join verifies;
* **missing** -- prepared but never published; the mesher dropped it;
* **unexpected** -- published but not prepared; ``defaultFaces``, a fabricated
  ``patch_<id>``, or an STL stem standing in for a UUID;
* **empty** -- published with no faces at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from foammesh.core.mesh.poly_mesh_boundary import (
    PolyMesh, face_areas, face_magsf, read_poly_mesh, triangulate_faces,
)
from foammesh.core.quality.patch_identity import IdentityMap


class BoundaryAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class BoundarySection:
    """One named boundary section of a subject mesh."""

    solver_name: str
    patch_uuid: str | None
    status: str                       # matched | missing | unexpected | empty
    face_ids: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    #: Area of the triangle soup a sampler walks.
    area: float = 0.0
    #: OpenFOAM's ``magSf``. Differs from ``area`` on warped faces, and the two
    #: answer different questions (§4), so both are carried.
    magsf: float = 0.0
    patch_type: str = ''
    category: str = ''
    reason: str = ''

    @property
    def rated(self) -> bool:
        """Only a verified identity may carry a rated verdict."""
        return self.status == 'matched' and bool(self.patch_uuid)

    @property
    def face_count(self) -> int:
        return int(self.face_ids.size)

    def to_dict(self) -> dict:
        return {
            'solver_name': self.solver_name, 'patch_uuid': self.patch_uuid,
            'status': self.status, 'face_count': self.face_count,
            'area': self.area, 'magsf': self.magsf,
            'patch_type': self.patch_type, 'category': self.category,
            'rated': self.rated, 'reason': self.reason,
        }


@dataclass(frozen=True)
class BoundaryModel:
    """Every section of one subject mesh, reconciled against the prepared set."""

    mesh: PolyMesh
    sections: tuple[BoundarySection, ...]
    checkpoint: str = 'final'
    engine_id: str = ''

    @property
    def rated(self) -> bool:
        """A model is rated only when every section is."""
        return bool(self.sections) and all(item.rated for item in self.sections)

    def unrated_reason(self) -> str:
        if self.rated:
            return ''
        if not self.sections:
            return 'the published mesh declares no boundary sections'
        problems = [f'{item.solver_name} ({item.status})'
                    for item in self.sections if not item.rated]
        return 'sections that cannot be rated: ' + ', '.join(problems)

    def section(self, name: str) -> BoundarySection | None:
        for item in self.sections:
            if item.solver_name == name:
                return item
        return None

    def by_uuid(self, patch_uuid: str) -> BoundarySection | None:
        for item in self.sections:
            if item.patch_uuid == patch_uuid:
                return item
        return None

    def triangulate(self, section: BoundarySection | str):
        """Triangles for one section, with the originating face id retained."""
        item = self.section(section) if isinstance(section, str) else section
        if item is None:
            raise BoundaryAdapterError(f'no such boundary section: {section}')
        return triangulate_faces(self.mesh, item.face_ids)

    def to_dict(self) -> dict:
        return {
            'checkpoint': self.checkpoint, 'engine_id': self.engine_id,
            'rated': self.rated, 'unrated_reason': self.unrated_reason(),
            'sections': [item.to_dict() for item in self.sections],
        }


def sections_from(mesh: PolyMesh, identity: IdentityMap, *,
                  expected_uuids=None) -> tuple[BoundarySection, ...]:
    """Reconcile a mesh's patches against the identities that should be there.

    ``expected_uuids`` is what the prepared manifest declared. Anything in it
    that the mesh does not carry is ``missing`` -- which is the whole point:
    a dropped patch is invisible to a check that only walks what was published.
    """
    expected = list(expected_uuids or [
        item.patch_uuid for item in identity.entries if item.patch_uuid])
    seen: set[str] = set()
    sections: list[BoundarySection] = []

    for patch in mesh.patches:
        entry = identity.get(patch.name)
        face_ids = mesh.patch_face_ids(patch)
        if patch.n_faces == 0:
            sections.append(BoundarySection(
                patch.name, entry.patch_uuid if entry else None, 'empty',
                face_ids, patch_type=patch.patch_type,
                reason='the published patch has no faces'))
            continue

        areas = face_areas(mesh, face_ids)
        magsf = face_magsf(mesh, face_ids)
        if entry is None:
            sections.append(BoundarySection(
                patch.name, None, 'unexpected', face_ids,
                float(areas.sum()), float(magsf.sum()), patch.patch_type,
                reason=('published patch is absent from the identity sidecar, '
                        'so it cannot be traced to prepared geometry')))
            continue
        if not entry.rated:
            sections.append(BoundarySection(
                patch.name, entry.patch_uuid, 'unexpected', face_ids,
                float(areas.sum()), float(magsf.sum()), patch.patch_type,
                entry.category,
                reason=entry.reason or 'identity was fabricated'))
            continue

        seen.add(entry.patch_uuid)
        sections.append(BoundarySection(
            patch.name, entry.patch_uuid, 'matched', face_ids,
            float(areas.sum()), float(magsf.sum()), patch.patch_type,
            entry.category))

    for patch_uuid in expected:
        if patch_uuid in seen:
            continue
        entry = next((item for item in identity.entries
                      if item.patch_uuid == patch_uuid), None)
        sections.append(BoundarySection(
            entry.solver_name if entry else patch_uuid, patch_uuid, 'missing',
            reason=('the prepared section was never published; the mesher '
                    'dropped it')))
    return tuple(sections)


def load(case_or_mesh: str | Path, identity: IdentityMap, *,
         checkpoint: str = 'final', expected_uuids=None,
         layout_expectation: str = 'reconstructed') -> BoundaryModel:
    """Read a published mesh and reconcile its sections in one step."""
    mesh = read_poly_mesh(case_or_mesh, layout_expectation=layout_expectation)
    return BoundaryModel(
        mesh=mesh,
        sections=sections_from(mesh, identity, expected_uuids=expected_uuids),
        checkpoint=checkpoint, engine_id=identity.engine_id)


# --------------------------------------------------------------------------- #
# The native Gmsh boundary (GF1)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class NativeSection:
    """One named 2-D physical group of a Gmsh ``.msh``."""

    name: str
    patch_uuid: str | None
    status: str
    vertices: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    triangles: np.ndarray = field(default_factory=lambda: np.empty((0, 3), np.int64))
    area: float = 0.0
    reason: str = ''

    @property
    def rated(self) -> bool:
        return self.status == 'matched' and bool(self.patch_uuid)

    def to_dict(self) -> dict:
        return {'name': self.name, 'patch_uuid': self.patch_uuid,
                'status': self.status, 'triangles': int(len(self.triangles)),
                'area': self.area, 'rated': self.rated, 'reason': self.reason}


#: MSH element types that carry a surface. Higher-order faces are reported
#: rather than silently decimated to their corners, because dropping the
#: mid-side nodes would understate exactly the facet-interior error GF1 exists
#: to measure.
_MSH_TRIANGLE, _MSH_QUAD = 2, 3
_MSH_HIGHER_ORDER = {9, 10, 16, 20, 21, 22, 23, 24, 25}


def native_sections(document, identity: IdentityMap, *,
                    expected_uuids=None) -> tuple[NativeSection, ...]:
    """Named boundary triangles from a parsed Gmsh mesh.

    GF1 isolates mesher behaviour before publication, so it reads the classified
    2-D elements already present in the native file rather than re-meshing.
    """
    points = np.asarray(document.points, dtype=np.float64)
    index = document.node_index
    physical_names = dict(document.physical_names or {})

    by_group: dict[int, list[list[int]]] = {}
    unsupported: dict[int, set[int]] = {}
    for element_type, physical, nodes in document.elements:
        if element_type == _MSH_TRIANGLE:
            local = [index[value] for value in nodes[:3]]
            by_group.setdefault(int(physical), []).append(local)
        elif element_type == _MSH_QUAD:
            local = [index[value] for value in nodes[:4]]
            # Fixed split, so the same mesh yields the same triangles.
            by_group.setdefault(int(physical), []).extend(
                [[local[0], local[1], local[2]],
                 [local[0], local[2], local[3]]])
        elif element_type in _MSH_HIGHER_ORDER:
            unsupported.setdefault(int(physical), set()).add(element_type)

    expected = list(expected_uuids or [
        item.patch_uuid for item in identity.entries if item.patch_uuid])
    seen: set[str] = set()
    sections: list[NativeSection] = []

    for physical in sorted(set(by_group) | set(unsupported)):
        name = physical_names.get((2, physical)) or f'patch_{physical}'
        entry = identity.get(name)
        rows = np.asarray(by_group.get(physical, []), dtype=np.int64)
        area = 0.0
        if rows.size:
            a, b, c = points[rows[:, 0]], points[rows[:, 1]], points[rows[:, 2]]
            area = float(
                0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())

        if physical in unsupported:
            sections.append(NativeSection(
                name, entry.patch_uuid if entry else None, 'unsupported',
                points, rows, area,
                reason=('the group holds higher-order surface elements '
                        f'{sorted(unsupported[physical])}, whose mid-side nodes '
                        'this reader would discard')))
            continue
        if entry is None or not entry.rated:
            sections.append(NativeSection(
                name, entry.patch_uuid if entry else None, 'unexpected',
                points, rows, area,
                reason=(entry.reason if entry else
                        'the native group is absent from the identity sidecar')))
            continue
        seen.add(entry.patch_uuid)
        sections.append(NativeSection(
            name, entry.patch_uuid, 'matched', points, rows, area))

    for patch_uuid in expected:
        if patch_uuid in seen:
            continue
        entry = next((item for item in identity.entries
                      if item.patch_uuid == patch_uuid), None)
        sections.append(NativeSection(
            entry.solver_name if entry else patch_uuid, patch_uuid, 'missing',
            reason='the prepared section has no classified group in the mesh'))
    return tuple(sections)


def load_native(msh_path: str | Path, identity: IdentityMap, *,
                expected_uuids=None) -> tuple[NativeSection, ...]:
    from foammesh.core.gmsh.publish import read_msh

    return native_sections(read_msh(msh_path), identity,
                           expected_uuids=expected_uuids)


# --------------------------------------------------------------------------- #
# Extraction integrity (the WP3 gate)
# --------------------------------------------------------------------------- #

def outward_fraction(mesh: PolyMesh, section: BoundarySection) -> float:
    """Fraction of a section's faces whose normal points out of its owner cell.

    Orientation is one of the five properties the WP3 gate says must survive
    extraction, and it is not implied by area: a reversed patch has exactly the
    right area and the wrong sign everywhere. OpenFOAM boundary faces point out
    of the domain, so a correctly extracted section scores 1.0.
    """
    if section.face_count == 0:
        return 0.0
    centres = np.empty((section.face_count, 3))
    normals = np.empty((section.face_count, 3))
    for position, face_id in enumerate(section.face_ids):
        loop = mesh.points[mesh.face(int(face_id))]
        centres[position] = loop.mean(axis=0)
        normals[position] = 0.5 * np.cross(
            loop, np.roll(loop, -1, axis=0)).sum(axis=0)

    owners = mesh.owner[section.face_ids]
    centroids = _cell_centroids(mesh, np.unique(owners))
    outward = np.zeros(section.face_count, dtype=bool)
    for position, cell in enumerate(owners):
        centre = centroids.get(int(cell))
        if centre is None:
            continue
        outward[position] = float(
            np.dot(normals[position], centres[position] - centre)) > 0
    return float(outward.mean())


def _cell_centroids(mesh: PolyMesh, cells: np.ndarray) -> dict:
    """Approximate centre of each cell, from **all** of its faces.

    A cell owns only some of its faces; for the rest it is the *neighbour*.
    Averaging the owned faces alone puts the centre off toward one side, and on
    an interior cell that is roughly half the faces — enough to flip the sign
    this function exists to test. The single-cell fixture that first covered
    this hid the error completely, because one cell owns every face it has.
    """
    wanted = {int(value) for value in cells}
    members: dict[int, list[int]] = {value: [] for value in wanted}
    for face_id, cell in enumerate(mesh.owner):
        if int(cell) in wanted:
            members[int(cell)].append(face_id)
    for face_id, cell in enumerate(mesh.neighbour):
        if int(cell) in wanted:
            members[int(cell)].append(face_id)

    centroids = {}
    for cell, faces in members.items():
        if not faces:
            continue
        centroids[cell] = np.mean(
            [mesh.points[mesh.face(int(face))].mean(axis=0) for face in faces],
            axis=0)
    return centroids


def extraction_fingerprint(sections) -> str:
    """A digest of what was extracted, for the report's staleness check.

    Covers identity, counts and both areas — so a section that silently changes
    shape between the extraction and the report cannot go unnoticed.
    """
    import hashlib
    import json

    payload = json.dumps(
        [[item.solver_name, item.patch_uuid, item.status, item.face_count,
          f'{item.area:.17g}', f'{item.magsf:.17g}']
         for item in sections],
        sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
