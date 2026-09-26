"""What shapes a published mesh actually holds, and who can read them.

Plan 28 WP3. Every downstream decision about SU2 -- whether the export format
is offered, whether the writer runs, what the quality verdict says -- rested on
guesses before this module existed. The export page called ``readiness()`` with
``has_polyhedra`` left at its default of ``False``, so it reported SU2 ready for
every snappyHexMesh case ever produced; the SU2 writer sampled the first 64
cells of a VTK block to decide what the block was, and wrote whatever cell type
it found straight into the file.

The census is arithmetic over ``constant/polyMesh``, not a sample and not an
inference from which engine ran. It classifies cells the way OpenFOAM's own
``primitiveMeshCheck`` does, by counting the faces of a cell and the vertices of
each face:

===============  =====================================
Cell             Faces
===============  =====================================
tetrahedron      4 triangles
pyramid          4 triangles + 1 quadrilateral
prism            2 triangles + 3 quadrilaterals
hexahedron       6 quadrilaterals
polyhedron       anything else
===============  =====================================

A polygon face -- five or more vertices -- is counted separately, because it
disqualifies a mesh from SU2 export even when every cell it bounds happens to
be countable, and because it is the more directly recognisable symptom: it is
what a 2:1 refinement transition leaves behind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .poly_mesh_boundary import PolyMesh, PolyMeshReadError, read_poly_mesh


#: The four cell families SU2's mesh reader knows. Kept as a literal here
#: rather than imported from the engine registry so the census stays a
#: statement about the mesh, and the registry stays the statement about
#: engines; the two are asserted equal in the WP0 suite.
SU2_CELL_FAMILIES = ('tetrahedron', 'hexahedron', 'prism', 'pyramid')

#: The two destinations in this product that hold those four families and
#: nothing else, as the sentence each refuses with and the way out each offers.
#: DP-26. They are kept together because what follows the opening clause is
#: identical: the counts are a property of the mesh, not of who is reading it.
#: Every writer and the export page take their wording from here, so a user
#: reads the same sentence before choosing a format that they would have read
#: after being refused by it.
SU2_READER = 'SU2 reads tetrahedra, hexahedra, prisms and pyramids only'
SU2_ADVICE = 'Mesh with Gmsh to export this case for SU2.'
MSH_READER = ('a Gmsh mesh holds tetrahedra, hexahedra, prisms and pyramids '
              'only')
MSH_ADVICE = 'Export this case as OpenFOAM or VTU, which keep polyhedra.'


@dataclass(frozen=True)
class MeshCensus:
    """The shapes in one published mesh."""

    case_path: Path
    cell_count: int
    #: Per family, how many cells. Families with no cells are still present, so
    #: a caller can render the table without special-casing zero.
    cells_by_family: dict
    polygon_faces: int
    marker_count: int
    #: Empty when the mesh was read. A read failure is reported rather than
    #: raised, because every caller here is answering "can we export?" and the
    #: honest answer to an unreadable mesh is no, with the reason.
    read_error: str = ''
    warnings: tuple = ()
    _extra_reasons: tuple = field(default_factory=tuple, repr=False)

    @property
    def polyhedral_cells(self) -> int:
        return int(self.cells_by_family.get('polyhedron', 0))

    @property
    def su2_readable(self) -> bool:
        return not self.read_error and not self.reason

    @property
    def reason(self) -> str:
        """Why SU2 cannot read this mesh, or '' when it can.

        Written to be shown to a user unchanged, and to carry the counts: "some
        cells are polyhedral" is not actionable, "1 of 1 cells is polyhedral"
        tells them how much of the mesh is affected and therefore whether to
        re-mesh or to change engine.
        """
        return self.reason_for(SU2_READER)

    def reason_for(self, reader: str) -> str:
        """The same measurement, opened with *reader*'s own sentence.

        DP-26. MSH holds the same four families SU2 does, and the counts that
        disqualify a mesh from one disqualify it from the other; only the
        clause naming who cannot read it differs. Callers pass the sentence
        their own refusal already uses, so nothing has to be reconciled by the
        person reading two of them.
        """
        if self.read_error:
            return self.read_error
        parts = []
        if self.polyhedral_cells:
            parts.append(
                f'{self.polyhedral_cells} of {self.cell_count} cells '
                f'{"is" if self.polyhedral_cells == 1 else "are"} polyhedral')
        if self.polygon_faces:
            parts.append(
                f'{self.polygon_faces} '
                f'{"face has" if self.polygon_faces == 1 else "faces have"} '
                'five or more vertices')
        parts.extend(self._extra_reasons)
        if not parts:
            return ''
        return reader + '; ' + ', and '.join(parts)

    def to_dict(self) -> dict:
        return {
            'cell_count': self.cell_count,
            'cells_by_family': dict(self.cells_by_family),
            'polyhedral_cells': self.polyhedral_cells,
            'polygon_faces': self.polygon_faces,
            'marker_count': self.marker_count,
            'su2_readable': self.su2_readable,
            'reason': self.reason,
            'read_error': self.read_error,
            'warnings': list(self.warnings),
        }


def classify_cells(mesh: PolyMesh) -> dict:
    """Count cells by family, using OpenFOAM's face-count classification."""
    counts = {name: 0 for name in SU2_CELL_FAMILIES}
    counts['polyhedron'] = 0
    if mesh.cell_count == 0:
        return counts

    sizes = np.diff(mesh.face_offsets)
    # Per cell, how many faces of each vertex count. Three columns is enough:
    # triangles, quadrilaterals, and everything else lumped together, because
    # every named family is made of the first two only.
    triangles = np.zeros(mesh.cell_count, dtype=np.int64)
    quads = np.zeros(mesh.cell_count, dtype=np.int64)
    other = np.zeros(mesh.cell_count, dtype=np.int64)
    for owners in (mesh.owner, mesh.neighbour):
        if owners.size == 0:
            continue
        face_sizes = sizes[:owners.size]
        np.add.at(triangles, owners, (face_sizes == 3).astype(np.int64))
        np.add.at(quads, owners, (face_sizes == 4).astype(np.int64))
        np.add.at(other, owners, (face_sizes > 4).astype(np.int64))

    faces = triangles + quads + other
    clean = other == 0
    is_tet = clean & (faces == 4) & (triangles == 4)
    is_pyramid = clean & (faces == 5) & (triangles == 4) & (quads == 1)
    is_prism = clean & (faces == 5) & (triangles == 2) & (quads == 3)
    is_hex = clean & (faces == 6) & (quads == 6)

    counts['tetrahedron'] = int(is_tet.sum())
    counts['pyramid'] = int(is_pyramid.sum())
    counts['prism'] = int(is_prism.sum())
    counts['hexahedron'] = int(is_hex.sum())
    counts['polyhedron'] = int(
        mesh.cell_count - is_tet.sum() - is_pyramid.sum()
        - is_prism.sum() - is_hex.sum())
    return counts


def cell_census(case_path, *, layout_expectation: str = 'reconstructed'
                ) -> MeshCensus:
    """Census the mesh at ``case_path``, reporting rather than raising.

    ``case_path`` may be the case directory or the ``polyMesh`` directory.
    """
    case_path = Path(case_path)
    try:
        mesh = read_poly_mesh(case_path, layout_expectation=layout_expectation)
    except PolyMeshReadError as error:
        return MeshCensus(
            case_path=case_path, cell_count=0, cells_by_family={},
            polygon_faces=0, marker_count=0, read_error=str(error))

    sizes = np.diff(mesh.face_offsets)
    warnings = []
    extra = []

    empty_patches = [patch.name for patch in mesh.patches if patch.n_faces == 0]
    if len(mesh.patches) < 2:
        # SU2 applies boundary conditions per marker. One marker means every
        # boundary of the domain takes the same condition, which is a mesh that
        # imports and cannot be set up -- worth saying before the export, not
        # after it.
        warnings.append(
            'the mesh has only one boundary marker, so every boundary would '
            'take the same SU2 condition; split it before setting up the case')
    if empty_patches:
        warnings.append(
            'these boundary patches carry no faces and would export as empty '
            'markers: ' + ', '.join(empty_patches))
    if mesh.cell_count == 0:
        extra.append('the mesh holds no cells')

    return MeshCensus(
        case_path=case_path,
        cell_count=mesh.cell_count,
        cells_by_family=classify_cells(mesh),
        polygon_faces=int((sizes > 4).sum()),
        marker_count=len(mesh.patches),
        warnings=tuple(warnings),
        _extra_reasons=tuple(extra),
    )


# --------------------------------------------------------------------------- #
# Element censuses: the mesh file itself, before anything publishes it
# --------------------------------------------------------------------------- #
#
# Plan 30 WP-07 (F-36). ``cell_census`` above reads ``constant/polyMesh``, so
# it can only speak about a mesh that was published -- and a second-order mesh
# never is, because the polyMesh publisher reads first-order elements only. The
# consequence was that a quadratic SU2 run died in the publisher with "the mesh
# has no volume cells", which is the message for CAD that imported without
# solids: it was the one thing that had not gone wrong. These two functions
# count the elements in the file the target actually consumes.

#: MSH 2.2 element type -> (family, node count, element order). Quadratic types
#: are here in both their complete and serendipity forms, because Gmsh writes
#: whichever the ``secondOrderIncomplete`` control asked for and the census must
#: recognise the mesh either way.
MSH_VOLUME_TYPES = {
    4: ('tetrahedron', 4, 1),
    5: ('hexahedron', 8, 1),
    6: ('prism', 6, 1),
    7: ('pyramid', 5, 1),
    11: ('tetrahedron', 10, 2),
    12: ('hexahedron', 27, 2),
    13: ('prism', 18, 2),
    14: ('pyramid', 14, 2),
    17: ('hexahedron', 20, 2),
    18: ('prism', 15, 2),
    19: ('pyramid', 13, 2),
}
MSH_SURFACE_TYPES = {
    2: ('triangle', 3, 1),
    3: ('quadrilateral', 4, 1),
    9: ('triangle', 6, 2),
    10: ('quadrilateral', 9, 2),
    16: ('quadrilateral', 8, 2),
}

#: MSH volume element type -> element order, for **every** order Gmsh 4.15.2
#: reports, not just the quadratic ones. Read out of Gmsh itself rather than
#: transcribed from memory: ``gmsh.model.mesh.getElementProperties`` was walked
#: over codes 1..199 and every three-dimensional tetrahedron, hexahedron, prism
#: or pyramid of order two or above is here. The probe is kept at
#: ``plans/evidence/plan31/fcd-order-optimisers/probes/gmsh-element-type-codes.json``.
#:
#: Plan 31 FC-D. The tables that preceded this one stopped at order 2, so an
#: order-3 mesh -- Gmsh writes tetrahedron code 29 for one -- fell through
#: every branch and the polyMesh publisher reported "the mesh has no volume
#: cells; Gmsh produced a surface mesh, which usually means the CAD imported
#: without solids". MEASURED on
#: ``plans/evidence/plan31/fcd-order-optimisers/order-fixtures/order3.msh``,
#: a sphere with 256 tetrahedra in it. That is the same misdiagnosis Plan 30
#: WP-07 removed for order 2, still live one order up. Naming the codes lets
#: the refusal say which order it found.
#:
#: Prisms above order 2 are absent because this Gmsh build does not report
#: them: codes 90 and 91 raise from ``getElementProperties``. An absent code
#: stays "an element type this census does not know", which is the truth.
MSH_HIGHER_ORDER_VOLUME_TYPES = {
    11: 2, 12: 2, 13: 2, 14: 2, 17: 2, 18: 2, 19: 2,
    29: 3, 30: 4, 31: 5, 32: 4, 33: 5,
    71: 6, 72: 7, 73: 8, 74: 9, 75: 10,
    79: 6, 80: 7, 81: 8, 82: 9, 83: 10,
    92: 3, 93: 4, 94: 5, 95: 6, 96: 7, 97: 8, 98: 9,
    99: 3, 100: 4, 101: 5, 102: 6, 103: 7, 104: 8, 105: 9,
    118: 3, 119: 4, 120: 5, 121: 6, 122: 7, 123: 8, 124: 9,
    125: 3, 126: 4, 127: 5, 128: 6, 129: 7, 130: 8, 131: 9,
    137: 3,
}

#: MSH element types that are neither surfaces nor volumes: the point and the
#: line families, of every order. Gmsh writes them for every geometric curve
#: and physical point in the model, so they are in almost every file. Named
#: here rather than left to fall through, so that "an element type this census
#: does not know" can be a warning about something genuinely unrecognised
#: instead of a note on every file the mesher produces (Plan 31 DP-17).
MSH_POINT_AND_LINE_TYPES = frozenset({1, 8, 15, 26, 27, 28})

#: SU2 identifies elements by VTK type code. These are the linear codes, which
#: are the ones FoamMesh's own SU2 reader (``core/export/su2_reader.py``) and
#: this census can turn back into a mesh. ``(family, node count, is volume)``.
SU2_ELEMENT_TYPES = {
    5: ('triangle', 3, False),
    9: ('quadrilateral', 4, False),
    10: ('tetrahedron', 4, True),
    12: ('hexahedron', 8, True),
    13: ('prism', 6, True),
    14: ('pyramid', 5, True),
}

#: The second-order VTK codes Gmsh's SU2 writer emits when the mesh is
#: quadratic. Plan 31 CP-01 (C31-02). These are *written by Gmsh* and read by
#: neither this application's SU2 reader nor, before this change, this census:
#: a quadratic ``.su2`` counted as an empty mesh and was reported as CAD that
#: imported without solids. They are counted and flagged here so a file that
#: exists says what it holds, while :data:`ELEMENT_ORDER_REFUSALS` in
#: ``core/gmsh/plan_derivation.py`` stops FoamMesh writing a new one. Emitted
#: and qualified are two different statements and this table is the first.
SU2_SECOND_ORDER_ELEMENT_TYPES = {
    22: ('triangle', 6, False),
    23: ('quadrilateral', 8, False),
    24: ('tetrahedron', 10, True),
    25: ('hexahedron', 20, True),
    26: ('prism', 15, True),
    27: ('pyramid', 13, True),
}

#: DP-674. The VTK line codes, linear and quadratic. Gmsh writes the boundary
#: of a two-dimensional SU2 mesh (``NDIME= 2``) as lines under each marker, so
#: they are boundary elements the file is meant to hold, not codes SU2 cannot
#: read. Counting them as unknown made a planar mesh read as an unreadable one.
SU2_LINE_TYPES = frozenset({3, 21})

#: The VTK arbitrary-order Lagrange codes. Gmsh's SU2 writer emits these for
#: any mesh above element order 2 -- one code per family, with the order left
#: implicit in the node count -- so they are what an order-3, order-4 and
#: order-5 export all look like on disk. MEASURED (Plan 31 FC-D): the order 3,
#: 4 and 5 sphere fixtures each wrote code 69 for their triangles and code 71
#: for their tetrahedra, and SU2 v8.4.0 refuses all three.
VTK_LAGRANGE_ELEMENT_TYPES = frozenset({68, 69, 70, 71, 72, 73, 74})


@dataclass(frozen=True)
class ElementCensus:
    """What one mesh *file* holds, counted from the file.

    This is the census the SU2 route runs in place of the polyMesh publication
    it does not need: SU2 reads the file Gmsh wrote, so the file is the thing
    worth counting.
    """

    path: Path
    #: ``msh`` or ``su2``.
    file_format: str
    volume_by_family: dict
    surface_by_family: dict
    #: The highest element order present. ``0`` when there are no elements.
    element_order: int = 0
    markers: tuple = ()
    point_count: int = 0
    read_error: str = ''
    warnings: tuple = ()
    #: Element type codes found in the file that neither table recognises, and
    #: the count of rows carrying them. Plan 31 FC-D: without this, a mesh made
    #: entirely of element types the census has never met is indistinguishable
    #: from an empty one, and :attr:`summary` said "holds no volume elements"
    #: about a file with 256 tetrahedra in it.
    unreadable_codes: tuple = ()
    unreadable_count: int = 0
    #: DP-674. ``2`` for a planar or axisymmetric section, whose cells are
    #: triangles and quadrilaterals; ``3`` for a volume mesh.
    dimension: int = 3

    @property
    def volume_count(self) -> int:
        return int(sum(self.volume_by_family.values()))

    @property
    def surface_count(self) -> int:
        return int(sum(self.surface_by_family.values()))

    @property
    def has_volume_elements(self) -> bool:
        """True when the file holds a volume element *of any order*.

        The predicate the publisher's "no volume cells" error should have been
        asking: a quadratic tetrahedron is a volume element, and refusing it
        for being unreadable by one route is a different sentence from saying
        the CAD had no solids.
        """
        return self.volume_count > 0

    @property
    def has_cells(self) -> bool:
        """True when the file holds the cells a mesh of its dimension needs.

        DP-674. A two-dimensional section is made of faces: its cells are the
        triangles and quadrilaterals, which an MSH census files as surface
        elements and the SU2 census (reading ``NDIME= 2``) as cells.
        """
        if self.dimension == 2:
            return self.volume_count > 0 or self.surface_count > 0
        return self.has_volume_elements

    @property
    def summary(self) -> str:
        """One line, written to be shown to a user unchanged."""
        if self.read_error:
            return self.read_error
        if self.dimension == 2 and self.has_cells:
            # DP-674. A section's cells are faces; say so rather than call a
            # planar mesh one with no volume elements.
            cells = self.volume_by_family if self.volume_count else                 self.surface_by_family
            count = int(sum(cells.values()))
            shapes = ', '.join(
                f'{number} {name}{"" if number == 1 else "s"}'
                for name, number in sorted(cells.items()) if number)
            return f'{count} two-dimensional cells ({shapes})'
        if not self.has_volume_elements:
            if self.unreadable_codes:
                # Plan 31 FC-D. "No volume elements" is a statement about the
                # mesh; "none this census can read" is a statement about the
                # census. Saying the first when the second is true sent a user
                # to look at their CAD.
                codes = ', '.join(str(code) for code in self.unreadable_codes)
                return (f'{self.path.name} holds {self.unreadable_count} '
                        f'elements of type code {codes}, which this '
                        'application does not read')
            return f'{self.path.name} holds no volume elements'
        shapes = ', '.join(
            f'{count} {name}{"" if count == 1 else "s"}'
            for name, count in sorted(self.volume_by_family.items())
            if count)
        order = 'second-order' if self.element_order >= 2 else 'first-order'
        markers = (f', {len(self.markers)} marker'
                   f'{"" if len(self.markers) == 1 else "s"}'
                   if self.markers else '')
        return f'{self.volume_count} {order} elements ({shapes}){markers}'

    def to_dict(self) -> dict:
        return {
            'path': str(self.path),
            'format': self.file_format,
            'volume_by_family': dict(self.volume_by_family),
            'surface_by_family': dict(self.surface_by_family),
            'volume_count': self.volume_count,
            'surface_count': self.surface_count,
            'element_order': self.element_order,
            'markers': list(self.markers),
            'point_count': self.point_count,
            'summary': self.summary,
            'read_error': self.read_error,
            'warnings': list(self.warnings),
            'unreadable_codes': list(self.unreadable_codes),
            'unreadable_count': self.unreadable_count,
            'dimension': self.dimension,
        }


def _empty_families(names) -> dict:
    return {name: 0 for name in names}


class MshCensusRefusal(Exception):
    """Why the census will not count an MSH file.

    Raised inside the parsers below and turned into
    :attr:`ElementCensus.read_error` at the top of
    :func:`msh_element_census`, so callers see the shape they already handle
    for a path that could not be opened. The rule the whole module follows is
    that a number the file does not support is worse than no number: the
    census exists to be an independent check on what is on disk, and a check
    that guesses is not one.
    """


def _msh_refused(path: Path, message: str) -> ElementCensus:
    """A census that counted nothing and says why."""
    return ElementCensus(
        path=path, file_format='msh',
        volume_by_family=_empty_families(SU2_CELL_FAMILIES),
        surface_by_family=_empty_families(('triangle', 'quadrilateral')),
        read_error=message)


def _msh_ints(lines, index: int, count: int, what: str) -> list:
    """The first ``count`` integers on ``lines[index]``, or a refusal."""
    if index >= len(lines):
        raise MshCensusRefusal(f'{what} is missing: the file ends first')
    parts = lines[index].split()
    if len(parts) < count:
        raise MshCensusRefusal(
            f'{what} should hold {count} numbers and holds {len(parts)}: '
            f'{lines[index].strip()!r}')
    try:
        return [int(value) for value in parts[:count]]
    except ValueError as error:
        raise MshCensusRefusal(
            f'{what} is not a row of numbers: {lines[index].strip()!r}'
        ) from error


def _msh_expect_end(lines, cursor: int, tag: str, section: str) -> int:
    """Assert the section ends exactly where its declared counts predicted.

    This is the whole consistency check, and it is a strong one: the parsers
    below advance the cursor purely by arithmetic over the counts the file
    declares, so a file that ends its section anywhere other than here has
    declared counts its rows do not support -- truncated, padded, or a
    structure the census misread. Either way the honest answer is a refusal.
    """
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines):
        raise MshCensusRefusal(
            f'{section} is truncated: the file ends where {tag} should be')
    if lines[cursor].strip() != tag:
        raise MshCensusRefusal(
            f'{section} does not end where its declared counts say it should; '
            f'{tag} was expected on line {cursor + 1} and '
            f'{lines[cursor].strip()!r} is there instead')
    return cursor + 1


class _MshTally:
    """Running per-family counts, shared by the 2.2 and 4.1 parsers."""

    def __init__(self) -> None:
        self.volume = _empty_families(SU2_CELL_FAMILIES)
        self.surface = _empty_families(('triangle', 'quadrilateral'))
        self.order = 0
        self.unknown: set = set()
        self.unknown_rows = 0

    def add(self, element_type: int, count: int = 1) -> None:
        if element_type in MSH_VOLUME_TYPES:
            family, _size, order = MSH_VOLUME_TYPES[element_type]
            self.volume[family] += count
        elif element_type in MSH_SURFACE_TYPES:
            family, _size, order = MSH_SURFACE_TYPES[element_type]
            self.surface[family] += count
        elif element_type in MSH_POINT_AND_LINE_TYPES:
            return
        else:
            self.unknown.add(element_type)
            self.unknown_rows += count
            return
        self.order = max(self.order, order)


def _msh_declared_format(lines) -> tuple:
    """``(version string, file type)`` from ``$MeshFormat``."""
    for index, row in enumerate(lines):
        if row.strip() != '$MeshFormat':
            continue
        if index + 1 >= len(lines) or not lines[index + 1].split():
            raise MshCensusRefusal('the $MeshFormat section is empty')
        parts = lines[index + 1].split()
        try:
            file_type = int(parts[1]) if len(parts) > 1 else 0
        except ValueError as error:
            raise MshCensusRefusal(
                f'$MeshFormat declares an unreadable file type {parts[1]!r}'
            ) from error
        return parts[0], file_type
    raise MshCensusRefusal('the file has no $MeshFormat section')


def _msh_physical_names(lines, index: int, names: list) -> int:
    """``$PhysicalNames`` -- identical in 2.2 and 4.1."""
    count = _msh_ints(lines, index + 1, 1, '$PhysicalNames count')[0]
    if index + 2 + count > len(lines):
        raise MshCensusRefusal(
            f'$PhysicalNames declares {count} names and the file holds '
            f'{max(0, len(lines) - index - 2)} rows after the count')
    for row in lines[index + 2:index + 2 + count]:
        parts = row.split('"')
        if len(parts) >= 2:
            names.append(parts[1])
    return _msh_expect_end(lines, index + 2 + count,
                           '$EndPhysicalNames', '$PhysicalNames')


def _census_msh_22(lines, tally: _MshTally) -> tuple:
    """MSH 2.2: one flat count per section, then that many rows."""
    points = 0
    nodes_seen = False
    names: list = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line == '$Nodes':
            count = _msh_ints(lines, index + 1, 1, '$Nodes count')[0]
            if index + 2 + count > len(lines):
                raise MshCensusRefusal(
                    f'$Nodes declares {count} nodes and the file holds '
                    f'{max(0, len(lines) - index - 2)} rows after the count')
            points, nodes_seen = count, True
            index = _msh_expect_end(lines, index + 2 + count,
                                    '$EndNodes', '$Nodes')
            continue
        if line == '$PhysicalNames':
            index = _msh_physical_names(lines, index, names)
            continue
        if line == '$Elements':
            count = _msh_ints(lines, index + 1, 1, '$Elements count')[0]
            if index + 2 + count > len(lines):
                raise MshCensusRefusal(
                    f'$Elements declares {count} elements and the file holds '
                    f'{max(0, len(lines) - index - 2)} rows after the count')
            for row in lines[index + 2:index + 2 + count]:
                parts = row.split()
                if len(parts) < 3:
                    raise MshCensusRefusal(
                        'an $Elements row is not an MSH 2.2 element '
                        f'(tag, type, tag count, ...): {row.strip()!r}')
                try:
                    tally.add(int(parts[1]))
                except ValueError as error:
                    raise MshCensusRefusal(
                        'an $Elements row does not name an element type: '
                        f'{row.strip()!r}') from error
            index = _msh_expect_end(lines, index + 2 + count,
                                    '$EndElements', '$Elements')
            continue
        index += 1
    if not nodes_seen:
        raise MshCensusRefusal('the file has no $Nodes section')
    return points, names


def _census_nodes_41(lines, index: int) -> tuple:
    """``$Nodes`` in MSH 4.1. Returns ``(node count, next line)``.

    The section header is ``numEntityBlocks numNodes minTag maxTag`` and the
    node count is field *1*. Each entity block then carries its own header --
    ``entityDim entityTag parametric numNodesInBlock`` -- followed by that
    many tag rows and that many coordinate rows.
    """
    blocks, declared, _low, _high = _msh_ints(
        lines, index + 1, 4, '$Nodes header')
    if blocks < 0 or declared < 0:
        raise MshCensusRefusal(
            f'$Nodes declares {blocks} entity blocks and {declared} nodes')
    cursor = index + 2
    counted = 0
    for block in range(blocks):
        what = f'$Nodes entity block {block + 1} header'
        in_block = _msh_ints(lines, cursor, 4, what)[3]
        if in_block < 0:
            raise MshCensusRefusal(f'{what} declares {in_block} nodes')
        cursor += 1 + 2 * in_block
        if cursor > len(lines):
            raise MshCensusRefusal(
                f'$Nodes entity block {block + 1} declares {in_block} nodes '
                'and the file ends inside it')
        counted += in_block
    if counted != declared:
        raise MshCensusRefusal(
            f'$Nodes declares {declared} nodes and its {blocks} entity blocks '
            f'hold {counted}')
    return declared, _msh_expect_end(lines, cursor, '$EndNodes', '$Nodes')


def _census_elements_41(lines, index: int, tally: _MshTally) -> int:
    """``$Elements`` in MSH 4.1. Returns the next line to read.

    Same shape as ``$Nodes``: the header's field 1 is the element count, and
    each entity block's header is ``entityDim entityTag elementType
    numElementsInBlock``. Every element in one block has that block's type, so
    the tally is arithmetic over the block headers -- the payload rows are
    ``elementTag nodeTag ...`` and carry no type of their own. Reading field 1
    of a payload row, as this function's predecessor did, reads a node tag.
    """
    blocks, declared, _low, _high = _msh_ints(
        lines, index + 1, 4, '$Elements header')
    if blocks < 0 or declared < 0:
        raise MshCensusRefusal(
            f'$Elements declares {blocks} entity blocks and {declared} '
            'elements')
    cursor = index + 2
    counted = 0
    for block in range(blocks):
        what = f'$Elements entity block {block + 1} header'
        _dim, _tag, element_type, in_block = _msh_ints(lines, cursor, 4, what)
        cursor += 1
        if in_block < 0 or cursor + in_block > len(lines):
            raise MshCensusRefusal(
                f'$Elements entity block {block + 1} declares {in_block} '
                'elements and the file ends inside it')
        tally.add(element_type, in_block)
        counted += in_block
        cursor += in_block
    if counted != declared:
        raise MshCensusRefusal(
            f'$Elements declares {declared} elements and its {blocks} entity '
            f'blocks hold {counted}')
    return _msh_expect_end(lines, cursor, '$EndElements', '$Elements')


def _census_msh_41(lines, tally: _MshTally) -> tuple:
    """MSH 4.1: entity blocks, each with its own header and payload."""
    points = 0
    nodes_seen = False
    names: list = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line == '$Nodes':
            points, index = _census_nodes_41(lines, index)
            nodes_seen = True
            continue
        if line == '$Elements':
            index = _census_elements_41(lines, index, tally)
            continue
        if line == '$PhysicalNames':
            index = _msh_physical_names(lines, index, names)
            continue
        index += 1
    if not nodes_seen:
        raise MshCensusRefusal('the file has no $Nodes section')
    return points, names


def msh_element_census(path) -> ElementCensus:
    """Count the elements in a Gmsh MSH file, by family and order.

    Plan 31 DP-17. This read MSH 2.2 only, and said so in one line, but it was
    handed 4.1 files and answered them anyway. MSH 4.1 begins ``$Nodes`` and
    ``$Elements`` with ``numEntityBlocks numNodes minTag maxTag``; this
    function took field *0* -- the block count -- as the node count, skipped
    that many rows, and then read field 1 of whatever rows it landed on as an
    element type. MEASURED on
    ``plans/evidence/plan31/audit-reproductions-20260906/order-1/mesh.msh``: it
    reported 27 points and one second-order hexahedron, with no error and no
    warning, for a mesh whose SU2 twin censuses at 294 points and 956 linear
    tetrahedra. ``read_msh`` refuses the same bytes loudly, so the two readers
    disagreed about one file and only the loud one was right.

    That mattered because this census is the native-Gmsh acceptance leg's
    independent check that the ``.msh`` on disk is the mesh the user was shown.
    A check that invents plausible numbers is not a check.

    Both block grammars are parsed now, and everything else is refused rather
    than approximated: an unknown version, a binary file, a section whose
    declared counts its rows do not support. A refusal comes back as
    :attr:`ElementCensus.read_error` with every count at zero.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as error:
        return _msh_refused(path, f'could not read {path.name}: {error}')

    lines = text.splitlines()
    tally = _MshTally()
    try:
        version, file_type = _msh_declared_format(lines)
        if file_type != 0:
            raise MshCensusRefusal(
                f'the file is binary MSH (file-type {file_type}) and the '
                'census reads the ASCII form')
        if version.startswith('2.'):
            points, names = _census_msh_22(lines, tally)
        elif version == '4.1':
            points, names = _census_msh_41(lines, tally)
        else:
            raise MshCensusRefusal(
                'the element census reads MSH 2.2 and MSH 4.1, and this file '
                f'declares version {version}')
    except MshCensusRefusal as refusal:
        return _msh_refused(path, f'{path.name} could not be counted: '
                                  f'{refusal}')

    warnings = []
    if tally.unknown:
        warnings.append(
            'the MSH file holds element type codes this census does not know: '
            + ', '.join(str(code) for code in sorted(tally.unknown))
            + '; they are not counted in either family')
        # Plan 31 FC-D. If any of those codes is a volume element of an order
        # above the two this application reads, say the order out loud: it is
        # the actionable half of the sentence, and without it the caller is
        # left to conclude that the CAD imported without solids.
        orders = sorted({MSH_HIGHER_ORDER_VOLUME_TYPES[code]
                         for code in tally.unknown
                         if code in MSH_HIGHER_ORDER_VOLUME_TYPES})
        if orders:
            warnings.append(
                'the MSH file is meshed at element order '
                + ', '.join(str(order) for order in orders)
                + '; FoamMesh reads first- and second-order elements, so this '
                  'mesh is intact but nothing in this application opens it')
    return ElementCensus(
        path=path, file_format='msh', volume_by_family=tally.volume,
        surface_by_family=tally.surface, element_order=tally.order,
        markers=tuple(names), point_count=points, warnings=tuple(warnings),
        unreadable_codes=tuple(sorted(tally.unknown)),
        unreadable_count=tally.unknown_rows)


def su2_element_census(path) -> ElementCensus:
    """Count the elements and markers in a native SU2 mesh file.

    Plan 31 CP-01 (C31-02) corrects what this docstring used to claim. It said
    a ``.su2`` census always reports order 1 because "the writer emits the
    corner nodes", and that was measured to be false: Gmsh's SU2 writer, given
    a quadratic mesh, writes the second-order VTK codes -- a tetrahedron
    becomes code 24 with ten nodes. The old table held the linear codes only,
    so every one of those elements fell into the "unknown" branch and the file
    came back with a volume count of zero: an existing mesh reported as CAD
    that imported without solids.

    Both tables are read now. The order comes from the codes actually found,
    and a file holding second-order elements is counted *and* carries a warning
    saying they are outside what this application's SU2 reader opens -- what
    Gmsh emits and what FoamMesh has qualified are two statements, and the
    census makes both.
    """
    path = Path(path)
    volume = _empty_families(SU2_CELL_FAMILIES)
    surface = _empty_families(('triangle', 'quadrilateral'))
    markers: list[str] = []
    points = 0
    order = 0
    warnings: list[str] = []
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as error:
        return ElementCensus(path=path, file_format='su2',
                             volume_by_family=volume, surface_by_family=surface,
                             read_error=f'could not read {path.name}: {error}')

    in_marker = False
    remaining = 0
    dimension = 3
    unknown: set = set()
    unknown_rows = 0
    second_order: set = set()
    for row in text.splitlines():
        line = row.strip()
        if not line or line.startswith('%'):
            continue
        head = line.split('=')[0].strip().upper()
        if head == 'NDIME':
            # DP-674. A planar SU2 mesh says so here, and its cells are faces.
            try:
                dimension = int(line.split('=')[1].split()[0])
            except (IndexError, ValueError):
                dimension = 3
            continue
        if head == 'NPOIN':
            try:
                points = int(line.split('=')[1].split()[0])
            except (IndexError, ValueError):
                points = 0
            remaining = 0
            in_marker = False
            continue
        if head == 'NELEM':
            in_marker = False
            try:
                remaining = int(line.split('=')[1].split()[0])
            except (IndexError, ValueError):
                remaining = 0
            continue
        if head == 'NMARK':
            remaining = 0
            continue
        if head == 'MARKER_TAG':
            markers.append(line.split('=', 1)[1].strip())
            in_marker = True
            remaining = 0
            continue
        if head == 'MARKER_ELEMS':
            try:
                remaining = int(line.split('=')[1].split()[0])
            except (IndexError, ValueError):
                remaining = 0
            continue
        if remaining <= 0:
            continue
        remaining -= 1
        try:
            code = int(line.split()[0])
        except (IndexError, ValueError):
            continue
        if code in SU2_LINE_TYPES:
            # DP-674. A line is the boundary of a planar section.
            surface['line'] = surface.get('line', 0) + 1
            continue
        entry = SU2_ELEMENT_TYPES.get(code)
        element_order = 1
        if entry is None:
            entry = SU2_SECOND_ORDER_ELEMENT_TYPES.get(code)
            element_order = 2
            if entry is not None:
                second_order.add(code)
        if entry is None:
            unknown.add(code)
            unknown_rows += 1
            continue
        family, _size, is_volume = entry
        order = max(order, element_order)
        if (is_volume or dimension == 2) and not in_marker:
            # DP-674: in a planar file the faces outside a marker are cells.
            volume[family] = volume.get(family, 0) + 1
        else:
            surface[family] = surface.get(family, 0) + 1

    if second_order:
        # Counted, not hidden: the elements are in the file. Flagged, not
        # accepted: FoamMesh's SU2 reader stops at the linear codes, so this
        # file will not open in the viewport (C31-02).
        warnings.append(
            'the SU2 file holds second-order element type codes '
            + ', '.join(str(code) for code in sorted(second_order))
            + ", which Gmsh writes for a quadratic mesh and FoamMesh's SU2 "
              'reader does not open; the elements are counted here but the '
              'file is not one this application can display')
    if unknown:
        warnings.append(
            'the SU2 file holds element type codes SU2 does not read: '
            + ', '.join(str(code) for code in sorted(unknown)))
        if unknown & VTK_LAGRANGE_ELEMENT_TYPES:
            # Plan 31 FC-D, MEASURED. Gmsh writes the VTK Lagrange codes for
            # any mesh above order 2, and SU2 v8.4.0 itself will not read them:
            # given the order-3 fixture it printed "256 volume elements" and
            # then exited on "Mismatch between NPOIN and number of points
            # listed in mesh file". The log is at
            # plans/evidence/plan31/fcd-order-optimisers/probes/
            # su2-8.4.0-reads-order-1-2-3.txt.
            warnings.append(
                'those are the VTK arbitrary-order Lagrange codes, which Gmsh '
                'writes above element order 2; SU2 v8.4.0 exits on this file '
                'with a point-count mismatch, so the order, not the export, '
                'is what has to change')
    return ElementCensus(
        path=path, file_format='su2', volume_by_family=volume,
        surface_by_family=surface, element_order=order,
        markers=tuple(markers), point_count=points, warnings=tuple(warnings),
        unreadable_codes=tuple(sorted(unknown)), unreadable_count=unknown_rows,
        dimension=2 if dimension == 2 else 3)


def element_census(path) -> ElementCensus:
    """Census whichever of the two mesh files it was handed."""
    path = Path(path)
    if path.suffix.lower() == '.su2':
        return su2_element_census(path)
    return msh_element_census(path)
