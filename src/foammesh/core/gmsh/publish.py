"""Gmsh mesh to ``constant/polyMesh``, without a Gmsh install on the host.

The MSH file is parsed here rather than read back through the Gmsh API, so
publication is a pure host-side operation: the runtime is only needed to
*produce* a mesh, never to consume one.

The one rule that matters, and the one that took a while to find in WP0:

    **The boundary is derived from the cells, not from Gmsh.**

A face owned by exactly one cell is a boundary face. Gmsh's own surface
adjacency looks authoritative and is stale after a geo-kernel extrusion on
OCC-imported geometry -- it still reports the boundary-layer interface as
bounding one volume. Trusting it publishes interior faces as boundary patches,
and `checkMesh` reports the result as thousands of open cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path

import numpy as np

from foammesh.core.export.poly_mesh_writer import (
    FoamPolyMeshWriter, PolyMeshWriteError,
)
from foammesh.core.gmsh.layer_targets import (
    boundary_category, publishable_category,
)
from foammesh.core.mesh.census import MSH_HIGHER_ORDER_VOLUME_TYPES
from foammesh.core.quantities import agreeing, aligned, count_text
from foammesh.core.mesh.connectivity import (
    _FACES, signed_cell_volumes, straddling_faces,
)
from foammesh.core.mesh.model import (
    BoundaryBlock, CanonicalMesh, CellBlock, CellType,
)

#: MSH element type -> canonical cell type and node count.
VOLUME_TYPES = {
    4: (CellType.TETRA, 4),
    5: (CellType.HEXAHEDRON, 8),
    6: (CellType.PRISM, 6),
    7: (CellType.PYRAMID, 5),
}
SURFACE_TYPES = {
    2: (CellType.TRIANGLE, 3),
    3: (CellType.QUADRILATERAL, 4),
}

#: Volume elements this parser deliberately does not read, mapped to their
#: element order. They are named so the refusal can say what it found instead
#: of reporting an empty mesh (Plan 30 WP-07, F-36).
#:
#: Plan 31 FC-D widened this from the seven quadratic codes to every order
#: Gmsh reports, because the old set stopped one order short of the problem:
#: an order-3 tetrahedron is code 29, matched nothing, and brought back "the
#: mesh has no volume cells ... the CAD imported without solids" for a file
#: with 256 tetrahedra in it. See
#: :data:`foammesh.core.mesh.census.MSH_HIGHER_ORDER_VOLUME_TYPES`.
_HIGHER_ORDER_VOLUME_TYPES = MSH_HIGHER_ORDER_VOLUME_TYPES

#: Node permutations that flip a cell's orientation without changing its shape.
_FLIP = {
    CellType.TETRA: [0, 2, 1, 3],
    CellType.PRISM: [0, 2, 1, 3, 5, 4],
    CellType.HEXAHEDRON: [0, 3, 2, 1, 4, 7, 6, 5],
    CellType.PYRAMID: [0, 3, 2, 1, 4],
}


class PublishError(ValueError):
    pass


@dataclass(frozen=True)
class MshDocument:
    points: np.ndarray
    node_index: dict
    elements: list
    physical_names: dict


@dataclass(frozen=True)
class PublishReport:
    points: int
    cells: int
    cells_by_type: dict
    boundary_faces: int
    interior_2d_dropped: int
    flipped_cells: int
    patches: tuple = ()
    regions: tuple = ()
    warnings: tuple = ()
    export: dict = field(default_factory=dict)
    #: Full per-patch metadata as published, including ``patch_uuid`` and any
    #: recorded ``identity_origin``. Kept out of :meth:`to_dict` because the run
    #: manifest wants the patch *names*; the provenance sidecar is built from
    #: this in-process instead (Plan 23 WP2).
    patch_records: tuple = ()

    def to_dict(self) -> dict:
        return {
            'points': self.points, 'cells': self.cells,
            'cellsByType': dict(self.cells_by_type),
            'boundaryFaces': self.boundary_faces,
            'interior2dDropped': self.interior_2d_dropped,
            'flippedCells': self.flipped_cells,
            'patches': list(self.patches), 'regions': list(self.regions),
            'warnings': list(self.warnings), 'export': dict(self.export),
        }


# --------------------------------------------------------------------------- #
# MSH 2.2 reading
# --------------------------------------------------------------------------- #

def read_msh(path: str | Path) -> MshDocument:
    """Read an ASCII MSH 2.2 file.

    Version 2.2 is what the runner writes: OpenFOAM 13's ``gmshToFoam`` cannot
    read 4.1 at all, and one format serving both the direct route and the
    diagnostic fallback is one format to get right.
    """
    path = Path(path)
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError as error:
        raise PublishError(f'mesh file could not be read: {error}') from error

    node_ids: list[int] = []
    coordinates: list[tuple[float, float, float]] = []
    elements: list[tuple[int, int, list[int]]] = []
    names: dict[tuple[int, int], str] = {}
    index, version_seen = 0, False

    while index < len(lines):
        line = lines[index].strip()
        if line == '$MeshFormat':
            version = lines[index + 1].split()[0] if index + 1 < len(lines) else ''
            if not version.startswith('2.'):
                raise PublishError(
                    f'this reader implements MSH 2.2; the file declares '
                    f'{version or "no"} version')
            version_seen = True
            index += 2
        elif line == '$PhysicalNames':
            count = int(lines[index + 1])
            for row in lines[index + 2: index + 2 + count]:
                parts = row.split(None, 2)
                if len(parts) >= 3:
                    names[(int(parts[0]), int(parts[1]))] = (
                        parts[2].strip().strip('"'))
            index += 2 + count
        elif line == '$Nodes':
            count = int(lines[index + 1])
            for row in lines[index + 2: index + 2 + count]:
                parts = row.split()
                node_ids.append(int(parts[0]))
                coordinates.append(
                    (float(parts[1]), float(parts[2]), float(parts[3])))
            index += 2 + count
        elif line == '$Elements':
            count = int(lines[index + 1])
            for row in lines[index + 2: index + 2 + count]:
                parts = [int(value) for value in row.split()]
                if len(parts) < 3:
                    continue
                element_type, tag_count = parts[1], parts[2]
                physical = parts[3] if tag_count >= 1 else 0
                elements.append(
                    (element_type, physical, parts[3 + tag_count:]))
            index += 2 + count
        else:
            index += 1

    if not version_seen:
        raise PublishError('mesh file has no $MeshFormat section')
    if not node_ids:
        raise PublishError('mesh file contains no nodes')
    return MshDocument(
        points=np.asarray(coordinates, dtype=np.float64),
        node_index={node: position for position, node in enumerate(node_ids)},
        elements=elements, physical_names=names)


# --------------------------------------------------------------------------- #
# A planar section, one cell thick
# --------------------------------------------------------------------------- #
#
# FC-E. Gmsh's ``generate(2)`` produces a surface mesh, and OpenFOAM has no
# two-dimensional mesh format: a planar case is a three-dimensional mesh one
# cell thick between two ``empty`` patches, and an axisymmetric case is the
# same section revolved through a small angle between two ``wedge`` patches.
# Nothing in Gmsh writes either, so the extrusion happens here, on the host,
# from the section Gmsh did write. Doing it here rather than in the runner is
# what keeps the front and back faces exactly one cell apart: the count is a
# property of this code, not of an extrusion option that could be set to two.

#: MSH element type for a 2-node line. A planar section's boundary is curves,
#: so the side patches of the extruded mesh are read from these.
LINE_TYPE = 1
_AXIS_INDEX = {'x': 0, 'y': 1, 'z': 2}
#: A section element with more nodes on the revolve axis than this revolves
#: into nothing: every one of its faces is either on the axis or coincident
#: with another, and the cell has no volume.
_MAX_AXIS_NODES = {2: 2, 3: 0}


@dataclass(frozen=True)
class SectionExtrusion:
    """How a planar section becomes the one cell of thickness OpenFOAM needs.

    ``planar`` translates the section along the coordinate direction it is
    flat in; ``wedge`` revolves it about an in-plane coordinate axis by
    ``angle_degrees``, half each way, which is what makes the two faces
    symmetric about a coordinate plane -- the condition OpenFOAM's
    ``wedgePolyPatch`` checks before it will accept them.
    """

    mode: str = 'planar'
    thickness: float = 0.01
    angle_degrees: float = 5.0
    axis: str = 'x'
    front: str = 'front'
    back: str = 'back'

    @property
    def category(self) -> str:
        """The boundary category the two new patches publish under."""
        return 'wedge' if self.mode == 'wedge' else 'empty'

    @classmethod
    def from_dict(cls, values) -> 'SectionExtrusion | None':
        """Build one from the job's ``dimensionality`` block, or ``None``.

        ``None`` is the three-dimensional route: no extrusion, and the
        publisher's existing refusal for a mesh with no volume cells stands
        exactly as it did.
        """
        values = dict(values or {})
        mode = str(values.get('mode') or 'three_d').lower()
        if mode in ('', 'three_d', '3d', 'threed'):
            return None
        if mode in ('two_d', '2d', 'planar'):
            mode = 'planar'
        elif mode in ('axisymmetric', 'wedge'):
            mode = 'wedge'
        else:
            raise PublishError(
                f'{mode!r} is not a meshing dimensionality this publisher '
                'implements; it knows three_d, two_d and axisymmetric')
        # A typed zero is not an absent value: `or default` here would turn a
        # zero thickness into 0.01 m and a zero-degree wedge into 5 degrees.
        # The derivation refuses both before they reach this, and this does
        # not quietly undo that for a job built any other way.
        def number(key, default):
            value = values.get(key)
            return default if value is None or value == '' else float(value)

        return cls(
            mode=mode,
            thickness=number('thickness', 0.01),
            angle_degrees=number('wedgeAngle', 5.0),
            axis=str(values.get('wedgeAxis') or 'x').lower(),
            front=str(values.get('frontPatch') or 'front'),
            back=str(values.get('backPatch') or 'back'))


def _named_spreads(names: str, spreads) -> str:
    """``x=0.42, y=0.18, z=0`` -- three extents, to one precision.

    DP-165. The reader's whole job on this line is to see which of the three
    is the small one, and rendering them a number at a time gave each its own
    decimal count.
    """
    return ', '.join(f'{names[index]}={text}'
                     for index, text in enumerate(aligned(spreads)))


def _section_plane(points: np.ndarray) -> tuple[int, float]:
    """Which coordinate the section is flat in, and the value it holds.

    Measured rather than assumed: a section is only ever extruded along the
    direction it has no extent in, so that direction is read off the points.
    A section flat in two directions is a line and cannot be extruded into
    cells at all.
    """
    spreads = [float(points[:, axis].max() - points[:, axis].min())
               for axis in range(3)]
    scale = max(max(spreads), 1.0)
    flat = [axis for axis in range(3) if spreads[axis] <= 1e-8 * scale]
    names = 'xyz'
    if not flat:
        raise PublishError(
            'this mesh is not a planar section: it has extent in all three '
            f'directions ({_named_spreads(names, spreads)}). '
            'A two-dimensional case needs a section flat in one coordinate '
            'direction, which is the direction it is extruded along.')
    if len(flat) > 1:
        raise PublishError(
            'this mesh is flat in '
            f'{" and ".join(names[a] for a in flat)}, so it is a line rather '
            'than a section, and extruding it produces no cells')
    axis = flat[0]
    return axis, float(points[:, axis].mean())


def _wedge_frame(plan: SectionExtrusion, points: np.ndarray,
                 normal: int, offset: float) -> tuple[int, int]:
    """``(axis index, radial index)`` for a revolve, checked against the mesh.

    The two conditions are OpenFOAM's, not this code's. The centre plane of a
    wedge has to be a coordinate plane through the origin, or
    ``wedgePolyPatch`` refuses the patch; and the section has to lie on one
    side of the revolve axis, or revolving it folds the mesh through itself.
    """
    names = 'xyz'
    if plan.axis not in _AXIS_INDEX:
        raise PublishError(
            f'{plan.axis!r} is not a coordinate axis; a wedge is revolved '
            'about x, y or z')
    axis = _AXIS_INDEX[plan.axis]
    if axis == normal:
        raise PublishError(
            f'the section is flat in {names[normal]} and the revolve axis is '
            f'{names[axis]}, so the axis is normal to the section. The axis '
            'has to lie in the section plane.')
    radial = 3 - normal - axis
    scale = max(float(np.abs(points).max()), 1.0)
    if abs(offset) > 1e-8 * scale:
        raise PublishError(
            f'the section lies at {names[normal]}={offset:.6g}, so revolving '
            f'it about {names[axis]} would put the wedge centre plane off the '
            f'{names[axis]}{names[radial]} coordinate plane. OpenFOAM requires '
            'a wedge centre plane aligned with a coordinate plane; move the '
            f'section to {names[normal]}=0.')
    radii = points[:, radial]
    if float(radii.min()) < -1e-8 * scale and float(radii.max()) > 1e-8 * scale:
        raise PublishError(
            f'the section straddles the revolve axis: {names[radial]} runs '
            f'from {float(radii.min()):.6g} to {float(radii.max()):.6g}. An '
            'axisymmetric section has to lie on one side of its axis.')
    return axis, radial


def extrude_section(document: MshDocument,
                    plan: SectionExtrusion) -> tuple[MshDocument, list]:
    """One planar section in, one cell of three-dimensional mesh out.

    The result is an ordinary :class:`MshDocument`, so everything downstream
    -- the orientation fix, the boundary-from-ownership rule, patch typing --
    is the code that already publishes a Gmsh volume mesh. What this adds is
    the second layer of nodes, the cells between the layers, and the two new
    physical surfaces the layers become.

    Returned with it are the things the sweep decided that nobody asked for:
    a boundary lying on the revolve axis sweeps no area, so it publishes no
    patch, and a case that named that boundary would otherwise find it gone
    from the mesh with nothing said.
    """
    points = document.points
    lookup = document.node_index
    cells: list[tuple[int, int, list[int]]] = []
    edges: list[tuple[int, list[int]]] = []
    for element_type, physical, nodes in document.elements:
        if element_type in SURFACE_TYPES:
            _kind, size = SURFACE_TYPES[element_type]
            try:
                cells.append((element_type, physical,
                              [lookup[node] for node in nodes[:size]]))
            except KeyError as error:
                raise PublishError(
                    f'section element references undefined node '
                    f'{error}') from error
        elif element_type == LINE_TYPE:
            try:
                edges.append((physical, [lookup[node] for node in nodes[:2]]))
            except KeyError as error:
                raise PublishError(
                    f'section edge references undefined node {error}') from error
    if not cells:
        raise PublishError(
            'the job asked for a two-dimensional mesh and the file holds no '
            'surface elements, so there is no section to extrude')
    if not edges:
        raise PublishError(
            'the section has no boundary curves in the mesh, so the sides of '
            'the extruded cell could not be named. Every bounding curve needs '
            'a physical group or the published mesh would be open.')

    used = np.unique(np.asarray(
        [node for _t, _p, row in cells for node in row], dtype=np.int64))
    normal, offset = _section_plane(points[used])

    count = points.shape[0]
    top = np.arange(count, dtype=np.int64) + count
    bottom_points = points.copy()
    top_points = points.copy()
    if plan.mode == 'wedge':
        axis, radial = _wedge_frame(plan, points[used], normal, offset)
        half = math.radians(abs(plan.angle_degrees)) / 2.0
        radii = points[:, radial]
        bottom_points[:, radial] = radii * math.cos(half)
        bottom_points[:, normal] = -radii * math.sin(half)
        top_points[:, radial] = radii * math.cos(half)
        top_points[:, normal] = radii * math.sin(half)
        # A node on the axis has no circumference: its two copies are the same
        # point, and the cell standing on it loses that edge. Collapsing here
        # is what makes the cells against the axis the tetrahedra and pyramids
        # an axisymmetric mesh is made of, instead of prisms with a zero-length
        # edge that checkMesh reports as zero-area faces.
        scale = max(float(np.abs(points[used]).max()), 1.0)
        on_axis = np.abs(radii) <= 1e-8 * scale
        top[on_axis] = np.arange(count, dtype=np.int64)[on_axis]
    else:
        on_axis = np.zeros(count, dtype=bool)
        top_points[:, normal] = points[:, normal] + float(plan.thickness)

    new_points = np.concatenate([bottom_points, top_points])

    # -- physical groups ------------------------------------------------- #
    # The section's own numbering is not reused: a 2-D group in the section is
    # a *region* of the extruded mesh and a 1-D group is a *patch*, so both
    # move dimension and both are renumbered from one.
    region_ids: dict[int, int] = {}
    patch_ids: dict[int, int] = {}
    names: dict[tuple[int, int], str] = {}
    for physical in sorted({physical for _t, physical, _r in cells}):
        region_ids[physical] = len(region_ids) + 1
        names[(3, region_ids[physical])] = (
            document.physical_names.get((2, physical)) or f'region_{physical}')
    for physical in sorted({physical for physical, _r in edges}):
        patch_ids[physical] = len(patch_ids) + 1
        names[(2, patch_ids[physical])] = (
            document.physical_names.get((1, physical)) or f'edge_{physical}')
    front_id = len(patch_ids) + 1
    back_id = front_id + 1
    names[(2, front_id)] = plan.front
    names[(2, back_id)] = plan.back

    # -- cells, and the faces that follow from them ----------------------- #
    elements: list[tuple[int, int, list[int]]] = []
    collapsed_cells = 0
    for element_type, physical, row in cells:
        axis_nodes = [node for node in row if on_axis[node]]
        limit = _MAX_AXIS_NODES[element_type]
        if len(axis_nodes) > limit:
            raise PublishError(
                f'a section {"triangle" if element_type == 2 else "quadrangle"} '
                f'has {len(axis_nodes)} of its nodes on the revolve axis; '
                f'revolving it produces a cell with no volume. Refine the '
                'section away from the axis, or move the axis.')
        region = region_ids[physical]
        upper = [int(top[node]) for node in row]
        if not axis_nodes:
            kind = 6 if element_type == 2 else 5
            elements.append((kind, region, [int(n) for n in row] + upper))
        elif element_type == 2 and len(axis_nodes) == 1:
            # One node on the axis: the two faces that node's edges sweep
            # collapse to triangles and the cell is a pyramid standing on the
            # quadrilateral the opposite edge swept.
            collapsed_cells += 1
            apex = next(index for index, node in enumerate(row)
                        if on_axis[node])
            first, second = row[(apex + 1) % 3], row[(apex + 2) % 3]
            elements.append((7, region, [
                int(first), int(second), int(top[second]), int(top[first]),
                int(row[apex])]))
        else:
            # Two nodes on the axis: the edge between them sweeps nothing and
            # the cell is a tetrahedron.
            collapsed_cells += 1
            free = next(node for node in row if not on_axis[node])
            fixed = [node for node in row if on_axis[node]]
            elements.append((4, region, [
                int(fixed[0]), int(fixed[1]), int(free), int(top[free])]))
        # The two faces the section itself became. They are the ``empty`` or
        # ``wedge`` pair, and they are boundary faces because the mesh between
        # them is exactly one cell thick.
        elements.append((element_type, front_id, [int(n) for n in row]))
        elements.append((element_type, back_id, upper))

    swept: dict[int, int] = {}
    on_axis_only: dict[int, int] = {}
    for physical, row in edges:
        start, end = int(row[0]), int(row[1])
        lifted = [int(top[start]), int(top[end])]
        if on_axis[start] and on_axis[end]:
            # The edge lies on the axis and sweeps no area at all.
            on_axis_only[physical] = on_axis_only.get(physical, 0) + 1
            continue
        swept[physical] = swept.get(physical, 0) + 1
        patch = patch_ids[physical]
        if on_axis[start]:
            elements.append((2, patch, [start, end, lifted[1]]))
        elif on_axis[end]:
            elements.append((2, patch, [end, start, lifted[0]]))
        else:
            elements.append((3, patch, [start, end, lifted[1], lifted[0]]))

    warnings = []
    for physical, count in sorted(on_axis_only.items()):
        if swept.get(physical):
            continue
        name = (document.physical_names.get((1, physical))
                or f'curve group {physical}')
        warnings.append(
            f'the boundary {name!r} lies on the {plan.axis} axis this section '
            f'is revolved about, so its {count_text(count, "edge")} swept no '
            'area and it publishes no patch. An axisymmetric case has no '
            'boundary on its own centreline; the cells there are collapsed '
            'instead.')

    return MshDocument(
        points=new_points,
        node_index={index: index for index in range(new_points.shape[0])},
        elements=elements, physical_names=names), warnings


# --------------------------------------------------------------------------- #
# Canonical assembly
# --------------------------------------------------------------------------- #

def build_canonical(document: MshDocument, *, patch_metadata=None,
                    region_metadata=None, source_fingerprint: str = '',
                    extrusion: 'SectionExtrusion | None' = None,
                    ) -> tuple[CanonicalMesh, PublishReport]:
    """Turn a parsed MSH into a validated canonical mesh.

    ``extrusion`` is the two-dimensional route (FC-E). It is the *job's*
    answer, not the mesh's: a mesh with no volume cells is still the failed
    CAD import it always was unless the job asked for a section, and a job
    that did ask for one is refused if Gmsh handed it a volume mesh instead.
    """
    if extrusion is not None:
        volumes = sum(1 for element_type, _physical, _nodes in document.elements
                      if element_type in VOLUME_TYPES
                      or element_type in _HIGHER_ORDER_VOLUME_TYPES)
        if volumes:
            raise PublishError(
                f'the job asked for a {extrusion.mode} mesh and Gmsh produced '
                f'{count_text(volumes, "volume element")}. A section is '
                'extruded here into exactly one cell of thickness, so a mesh '
                'that already has cells cannot be the section this route '
                'publishes.')
        document, extrusion_warnings = extrude_section(document, extrusion)
    else:
        extrusion_warnings = []

    volume_rows: dict[CellType, list] = {}
    volume_regions: dict[CellType, list] = {}
    surface_rows: dict[CellType, list] = {}
    surface_patches: dict[CellType, list] = {}
    lookup = document.node_index

    for element_type, physical, nodes in document.elements:
        if element_type in VOLUME_TYPES:
            kind, size = VOLUME_TYPES[element_type]
            try:
                volume_rows.setdefault(kind, []).append(
                    [lookup[node] for node in nodes[:size]])
            except KeyError as error:
                raise PublishError(
                    f'element references undefined node {error}') from error
            volume_regions.setdefault(kind, []).append(physical)
        elif element_type in SURFACE_TYPES:
            kind, size = SURFACE_TYPES[element_type]
            try:
                surface_rows.setdefault(kind, []).append(
                    [lookup[node] for node in nodes[:size]])
            except KeyError as error:
                raise PublishError(
                    f'face references undefined node {error}') from error
            surface_patches.setdefault(kind, []).append(physical)

    if not volume_rows:
        # Plan 30 WP-07 (F-36). "No volume cells" was raised for any mesh this
        # parser could not use, and a quadratic mesh is exactly that: full of
        # volume elements, none of them first order. The message sent a user
        # to look at their CAD import, which was the one thing that had not
        # gone wrong. The census answers "of any order"; only when it finds
        # nothing at all is the original diagnosis the right one.
        higher_order = [
            _HIGHER_ORDER_VOLUME_TYPES[element_type]
            for element_type, _physical, _nodes in document.elements
            if element_type in _HIGHER_ORDER_VOLUME_TYPES]
        if higher_order:
            orders = sorted(set(higher_order))
            plural = '' if len(higher_order) == 1 else 's'
            if orders == [2]:
                described = f'second-order volume element{plural}'
                advice = ('mesh at element order 1, or keep the order and '
                          'export the SU2 file Gmsh wrote')
            else:
                named = ' and '.join(str(order) for order in orders)
                described = (f'volume element{plural} at element order {named}')
                advice = ('mesh at element order 1: no export route in '
                          'FoamMesh reads above order 2, and SU2 v8.4.0 does '
                          'not read this file either')
            raise PublishError(
                f'the mesh holds {len(higher_order)} {described} and no '
                'first-order ones. OpenFOAM reads first-order MSH 2.2 only, '
                f'so this mesh cannot become a polyMesh: {advice}')
        raise PublishError(
            'the mesh has no volume cells; Gmsh produced a surface mesh, which '
            'usually means the CAD imported without solids')

    # Gmsh emits every CAD vertex as a node, referenced by nothing.
    used = np.unique(np.concatenate([
        np.asarray(rows, dtype=np.int64).ravel()
        for rows in list(volume_rows.values()) + list(surface_rows.values())]))
    remap = np.full(document.points.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(used.size, dtype=np.int64)
    points = document.points[used]

    warnings: list[str] = list(extrusion_warnings)
    cell_blocks, flipped_total, next_cell = [], 0, 0
    for kind, rows in volume_rows.items():
        connectivity = remap[np.asarray(rows, dtype=np.int64)]
        regions = np.asarray(volume_regions[kind], dtype=np.int64)
        block = CellBlock(
            cell_type=kind, connectivity=connectivity,
            source_ids=np.arange(next_cell, next_cell + connectivity.shape[0]),
            region_ids=regions)
        volumes = signed_cell_volumes(points, block)
        negative = volumes < 0
        if negative.any():
            connectivity = connectivity.copy()
            connectivity[negative] = connectivity[negative][:, _FLIP[kind]]
            block = CellBlock(
                cell_type=kind, connectivity=connectivity,
                source_ids=block.source_ids, region_ids=regions)
            flipped_total += int(negative.sum())
        cell_blocks.append(block)
        next_cell += block.count

    # DP-400. Every cell is now positively oriented, which says nothing about
    # where it is. A boundary layer grown into volume the tetrahedra already
    # occupied leaves each cell well formed and each face shared by exactly
    # two of them, and publishes clean; `checkMesh` then calls those cells
    # open, three stages downstream. Ask it here instead.
    straddle = straddling_faces(points, cell_blocks)
    if straddle:
        raise PublishError(_straddle_message(straddle))

    # Boundary from cell ownership. See the module docstring: Gmsh's own
    # adjacency is stale after a geo-kernel extrusion and cannot be trusted.
    face_use: dict[tuple, int] = {}
    for block in cell_blocks:
        for local in _FACES[block.cell_type]:
            for row in block.connectivity[:, local]:
                key = tuple(sorted(int(value) for value in row))
                face_use[key] = face_use.get(key, 0) + 1

    boundary_blocks, dropped, next_face = [], 0, 0
    for kind, rows in surface_rows.items():
        connectivity = remap[np.asarray(rows, dtype=np.int64)]
        patch_ids = np.asarray(surface_patches[kind], dtype=np.int64)
        keep = np.fromiter(
            (face_use.get(tuple(sorted(int(value) for value in row)), 0) == 1
             for row in connectivity),
            dtype=bool, count=connectivity.shape[0])
        dropped += int((~keep).sum())
        connectivity, patch_ids = connectivity[keep], patch_ids[keep]
        if not connectivity.shape[0]:
            continue
        boundary_blocks.append(BoundaryBlock(
            cell_type=kind, connectivity=connectivity,
            source_ids=np.arange(next_face, next_face + connectivity.shape[0]),
            patch_ids=patch_ids))
        next_face += connectivity.shape[0]

    expected = sum(1 for count in face_use.values() if count == 1)
    if next_face > expected:
        # DP-462. More named faces than the mesh has boundary faces is not an
        # open mesh, it is the opposite: a face claimed by two physical
        # surfaces at once. MEASURED on `tee_with_plug` gmsh, 21 September
        # 2026 -- 27,394 free faces, every one of them named, and 29,432 kept
        # copies, because 2,038 faces carry both `tee_with_plug_fluid` and
        # `tee_with_plug_plug`, the plug's skin lying flush along the fluid's
        # outer wall. The old message read `only 29432 are named` of a mesh
        # with 27394 to name and sent the reader to look for a hole. A
        # polyMesh face belongs to exactly one patch, so this is refused
        # rather than resolved: picking a winner would put a boundary
        # condition on a wall the user named twice without saying which name
        # the solver would see.
        raise PublishError(_overlap_message(boundary_blocks, face_use,
                                            document.physical_names,
                                            next_face - expected))
    if next_face < expected:
        raise PublishError(
            f'the mesh has {expected} boundary faces but only {next_face} are '
            'named by a physical group; every boundary face needs a patch or '
            'the published mesh would be open')
    if dropped:
        warnings.append(
            f'{count_text(dropped, "two-dimensional element")} lay on '
            f'interior interfaces and {agreeing(dropped, "was", "were")} not '
            'published as boundary faces')
    if flipped_total:
        warnings.append(
            f'{count_text(flipped_total, "cell")} had negative orientation '
            f'and {agreeing(flipped_total, "was", "were")} flipped to '
            'positive')

    patches = _patch_metadata(
        boundary_blocks, document.physical_names, patch_metadata)
    regions = _region_metadata(
        cell_blocks, document.physical_names, region_metadata)

    mesh = CanonicalMesh(
        points=points, cell_blocks=tuple(cell_blocks),
        boundary_blocks=tuple(boundary_blocks), patches=patches,
        regions=regions, source_engine='gmsh',
        source_fingerprint=source_fingerprint or 'gmsh-mesh')
    report = PublishReport(
        points=mesh.point_count, cells=mesh.cell_count,
        cells_by_type={block.cell_type.value: block.count
                       for block in mesh.cell_blocks},
        boundary_faces=mesh.boundary_face_count,
        interior_2d_dropped=dropped, flipped_cells=flipped_total,
        patches=tuple(item.get('name', '') for item in patches.values()),
        regions=tuple(item.get('name', '') for item in regions.values()),
        warnings=tuple(warnings),
        patch_records=tuple(dict(item) for item in patches.values()))
    return mesh, report


def _overlap_message(boundary_blocks, face_use, physical_names,
                     surplus: int) -> str:
    """Name the surfaces that claim the same boundary face (DP-462).

    The count alone does not tell a user what to change. The pair of patch
    names does: it says which two surfaces of their own model overlap, and
    the face count says over how much of it.
    """
    claims: dict[tuple, set] = {}
    for block in boundary_blocks:
        for row, patch in zip(block.connectivity, block.patch_ids):
            key = tuple(sorted(int(value) for value in row))
            claims.setdefault(key, set()).add(int(patch))
    shared = [tags for tags in claims.values() if len(tags) > 1]
    pairs: dict[tuple, int] = {}
    for tags in shared:
        pairs[tuple(sorted(tags))] = pairs.get(tuple(sorted(tags)), 0) + 1

    def named(tag: int) -> str:
        return physical_names.get((2, tag)) or f'physical surface {tag}'

    described = '; '.join(
        f'{" and ".join(named(tag) for tag in tags)} share '
        f'{count_text(shared_faces, "boundary face")}'
        for tags, shared_faces in sorted(pairs.items(), key=lambda item: -item[1]))
    if not described:
        described = (f'{count_text(surplus, "boundary face")} carry more than '
                     'one physical surface')
    return (
        f'{count_text(surplus, "boundary face")} in this mesh '
        f'{agreeing(surplus, "is", "are")} named by more than one physical '
        f'surface, and a polyMesh face belongs to exactly one patch: '
        f'{described}. The mesh is closed — every boundary face has a name — '
        'so what has to change is the geometry: two of the named surfaces '
        'occupy the same ground, and only one of them can carry the boundary '
        'condition there.')


def _straddle_message(straddle) -> str:
    """Why a mesh with cells inside other cells is refused rather than written.

    DP-400. `heated_duct` published, was accepted by the quality verdict --
    gamma mean 0.877, nothing below threshold -- and was then found unusable
    by `checkMesh`: "Open cells found, max cell openness: 1, number of open
    cells 342". The 342 is this count. The mesh is not repairable here: the
    layer has to be grown differently, or not at all.

    The wording names the shapes because they are the tell. A layer extruded
    into a tetrahedral fill shows up as the layer's own quadrilateral sides
    and the triangular caps of the cells it landed on, and saying so points
    at the extrusion rather than at the CAD.
    """
    shapes = []
    if straddle.triangles:
        shapes.append(count_text(straddle.triangles, 'triangle'))
    if straddle.quadrilaterals:
        shapes.append(
            count_text(straddle.quadrilaterals, 'quadrilateral'))
    where = ''
    if straddle.samples:
        point, owner, neighbour = straddle.samples[0]
        where = (', first between cells {0} and {1} at ({2:g}, {3:g}, {4:g})'
                 .format(owner, neighbour, *point))
    return (f'{count_text(straddle.count, "interior face")} '
            f'{agreeing(straddle.count, "does", "do")} not lie between the '
            f'two cells sharing {agreeing(straddle.count, "it", "them")}: '
            f'both cells are on the same side, so they occupy the same space '
            f'({" and ".join(shapes)}, '
            f'{count_text(straddle.cells, "cell")} in all){where}. This is '
            'usually a boundary layer grown into volume the mesh already '
            'filled; reduce the layer thickness or the number of layers on '
            'the surfaces it was grown from. Publishing would give a mesh '
            'OpenFOAM reports as open.')


def _patch_metadata(boundary_blocks, physical_names, supplied):
    """Name and type each patch, preferring the prepared categories.

    Patch typing is what turns a published mesh into one the solver can use:
    without a category every patch writes as a plain ``patch`` and the mesh
    reaches the solver with no walls.
    """
    supplied = dict(supplied or {})
    ids = set()
    for block in boundary_blocks:
        ids.update(int(value) for value in np.unique(block.patch_ids))
    patches = {}
    for patch_id in sorted(ids):
        declared = physical_names.get((2, patch_id))
        # A 2-D physical group Gmsh did not name, or one the prepared manifest
        # does not know, gets an invented name and a guessed category. That is
        # the silent identity loss Plan 23 §4 makes a hard failure, so it is
        # recorded here rather than smoothed over: the patch still publishes so
        # the mesh stays usable, but it can never carry a rated verdict.
        name = declared or f'patch_{patch_id}'
        provided = supplied.get(name) or supplied.get(patch_id)
        item = dict(provided or {})
        item.setdefault('name', name)
        item.setdefault('solver_name', name)
        item.setdefault('stable_id', name)
        if provided is None or not declared:
            item['identity_origin'] = 'fabricated'
            item['identity_reason'] = (
                f'physical surface {patch_id} has no name in the Gmsh mesh'
                if not declared else
                f'{name} is not declared in the prepared group manifest')
        if 'category' not in item and boundary_category(name) == 'far_field':
            # The runner builds the farfield and names its outer faces
            # ``far_field``, ``far_field_side``, ``far_field_xMin`` ...; the
            # prepared manifest never saw them, so the name is the category.
            # Defaulted to ``wall``, a solver would read the outer boundary
            # of an external flow as a no-slip surface (Plan 37 UF20).
            item['category'] = 'far_field'
        if 'category' not in item:
            item['category'] = 'wall'
            item.setdefault('identity_origin', 'fabricated')
            item.setdefault(
                'identity_reason',
                f'{name} has no prepared boundary category; defaulted to wall')
        patches[patch_id] = item
    return patches


def _region_metadata(cell_blocks, physical_names, supplied):
    supplied = dict(supplied or {})
    ids = set()
    for block in cell_blocks:
        ids.update(int(value) for value in np.unique(block.region_ids))
    regions = {}
    for region_id in sorted(ids):
        name = physical_names.get((3, region_id)) or f'region_{region_id}'
        item = dict(supplied.get(name) or supplied.get(region_id) or {})
        item.setdefault('name', name)
        item.setdefault('type', 'fluid')
        regions[region_id] = item
    return regions


# --------------------------------------------------------------------------- #
# Patch metadata from prepared geometry and periodic pairs
# --------------------------------------------------------------------------- #

def patch_metadata_from(categories=None, periodic_pairs=(),
                        identities=None) -> dict:
    """Build the per-patch metadata the writer needs.

    ``categories`` maps a solver patch name to a prepared boundary category,
    exactly as ``_prepared_boundary_categories`` produces it. ``identities``
    maps the same solver name to its prepared ``patch_uuid``.

    The UUID is carried as its own key and **not** substituted for
    ``stable_id``. ``stable_id`` is what ``_patch_sort_key`` orders published
    patches by, so overwriting it would reorder the boundary file and shift
    every ``startFace`` -- changing published meshes for a bookkeeping reason.
    The join Plan 23 §4 needs lives in the ``patch-identity`` sidecar, which
    costs the mesh nothing.

    Periodic pairs become matched ``cyclic`` patches carrying the transform,
    which is what lets OpenFOAM couple them.
    """
    identities = dict(identities or {})
    metadata: dict[str, dict] = {}
    for name, category in dict(categories or {}).items():
        metadata[str(name)] = {
            'name': str(name), 'solver_name': str(name),
            'stable_id': str(name), 'category': str(category or 'wall'),
        }
        patch_uuid = str(identities.get(str(name)) or '').strip()
        if patch_uuid:
            metadata[str(name)]['patch_uuid'] = patch_uuid
    for pair in periodic_pairs or ():
        master = str(pair.get('masterSolverName') or pair.get('masterScope') or '')
        slave = str(pair.get('slaveSolverName') or pair.get('slaveScope') or '')
        if not master or not slave:
            continue
        shared = {
            'coupling': 'cyclic',
            'pair_id': str(pair.get('name') or pair.get('controlId') or ''),
            'match_tolerance': float(pair.get('matchTolerance', 1e-6) or 1e-6),
        }
        if str(pair.get('transform')) == 'rotation':
            shared.update({
                'transform': 'rotational',
                'rotation_axis': list(pair.get('rotationAxis') or (0, 0, 1)),
                'rotation_centre': list(pair.get('rotationCentre') or (0, 0, 0)),
                'rotation_angle_degrees': float(
                    pair.get('rotationAngleDegrees', 0.0) or 0.0),
            })
        else:
            shared.update({
                'transform': 'translational',
                'translation': list(pair.get('translation') or (0, 0, 0)),
            })
        for name, role, neighbour in ((master, 'master', slave),
                                      (slave, 'slave', master)):
            item = metadata.setdefault(name, {
                'name': name, 'solver_name': name, 'stable_id': name})
            item['category'] = 'cyclic'
            item['interface'] = dict(
                shared, role=role, neighbour_stable_id=neighbour,
                neighbour_solver_name=neighbour)
    return metadata


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #

def substitute_mesh_constrained(categories) -> tuple[dict, list]:
    """Publish `wall` in place of a type the mesh would have to earn.

    DP-445. `wedge` and `empty` are not declarations, they are geometric
    contracts: a `wedgePolyPatch` needs two planar faces spanning a small
    angle about a common axis on a mesh one cell thick, and an `empty` patch
    needs that same mesh's flat front or back. This pipeline publishes a
    three-dimensional tetrahedral or hex-dominant mesh everywhere except the
    section extrusion, so neither contract can hold.

    The category arrives here off the leading word of a patch name, so a
    model called `wedge` whose surfaces the user named `wedge_wall1 ..
    wedge_wall7` publishes seven wedge patches -- and seven of them cannot
    be a pair in any case. MEASURED on that fixture: it is a 0.6x0.3x0.3 box
    cut by a box rotated 35 degrees, a solid named for its shape, and the
    collision with the boundary-role vocabulary is a coincidence of English.
    The snappy pipeline has guarded exactly this since snappyHexMesh died
    with SIGFPE inside `Foam::wedgePolyPatch::calcGeometry` on the same file;
    this path had no counterpart.

    Returns the mapping to publish and the substitutions made, so the report
    can say what was taken away rather than take it away silently.
    """
    published = dict(categories or {})
    unmeshable = []
    for name, category in list(published.items()):
        replacement = publishable_category(category)
        if replacement != category:
            published[name] = replacement
            unmeshable.append((str(name), str(category).strip().lower()))
    return published, unmeshable


def publish(msh_path: str | Path, destination: str | Path, *,
            categories=None, periodic_pairs=(), region_metadata=None,
            identities=None, source_fingerprint: str = '',
            extrusion: 'SectionExtrusion | None' = None) -> PublishReport:
    """Read a Gmsh mesh and write ``constant/polyMesh`` at ``destination``."""
    document = read_msh(msh_path)
    # DP-445, and note where this sits: *above* the extrusion block. The
    # extrusion is the one legitimate producer of these types and writes its
    # two entries over this mapping a few lines below, so ordering alone
    # keeps a deliberate `wedge` apart from a guessed one and no provenance
    # flag has to be invented to carry the difference.
    categories, unmeshable = substitute_mesh_constrained(categories)
    if extrusion is not None:
        # The front and back categories are the extrusion's, not the prepared
        # geometry's: they are the two faces this code just made, and typing
        # them anything but `empty`/`wedge` publishes a mesh OpenFOAM reads as
        # three-dimensional. Written over rather than defaulted for that
        # reason.
        categories[extrusion.front] = extrusion.category
        categories[extrusion.back] = extrusion.category
    mesh, report = build_canonical(
        document,
        patch_metadata=patch_metadata_from(
            categories, periodic_pairs, identities),
        region_metadata=region_metadata,
        source_fingerprint=source_fingerprint or Path(msh_path).name,
        extrusion=extrusion)
    try:
        written = FoamPolyMeshWriter().write(destination, mesh)
    except PolyMeshWriteError as error:
        raise PublishError(str(error)) from error
    return PublishReport(
        points=report.points, cells=report.cells,
        cells_by_type=report.cells_by_type,
        boundary_faces=report.boundary_faces,
        interior_2d_dropped=report.interior_2d_dropped,
        flipped_cells=report.flipped_cells, patches=report.patches,
        regions=report.regions,
        warnings=tuple(report.warnings) + tuple(
            f'{name} is named for the {requested} category, but a '
            f'{requested} patch describes a mesh one cell thick and this '
            f'pipeline published a three-dimensional one, which OpenFOAM '
            f'aborts on. It is published as a wall. Revolve or extrude a '
            f'section if the case is axisymmetric or two-dimensional — that '
            f'path sets these types on the two faces it makes — or rename '
            f'the surface so it does not lead with "{requested}".'
            for name, requested in unmeshable),
        export=written.to_dict(), patch_records=report.patch_records)


def canonical_from_msh(msh_path: str | Path, **kwargs) -> CanonicalMesh:
    """The canonical mesh alone, for callers that want to store or inspect it."""
    mesh, _report = build_canonical(read_msh(msh_path), **kwargs)
    return mesh
