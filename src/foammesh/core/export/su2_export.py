"""SU2 native mesh export: the fallback route, for meshes Gmsh did not write.

**Plan 30 WP-07 (F-08) demoted this module.** Gmsh's own SU2 writer is now the
one SU2 writer for anything Gmsh meshed: the run writes ``mesh.su2``, the run
manifest lists it as an artifact, and ``ImportExportService.export_su2`` copies
that file. Two writers producing two different files under one name is how a
user could be handed a mesh that was not the one their run produced -- and on
a second-order mesh the difference is not cosmetic, because the ``.su2`` Gmsh
wrote is the only form that mesh has.

What is left here is the route a **snappyHexMesh** case has and Gmsh does not
provide, since Gmsh cannot read a polyMesh. It runs when no native artifact
exists. The rest of this docstring is the Plan 26 reasoning that kept the
module alive, and still explains why it cannot simply be deleted.

Plan 26 WP10.1 set out to delete this module and delegate to Gmsh's own SU2
writer, on the grounds that ``write_su2`` takes a :class:`CanonicalMesh` whose
only production constructor is ``core/gmsh/publish.py``, that no
polyMesh -> CanonicalMesh reader exists, and therefore that no snappy mesh can
reach SU2 at all. **The last step does not follow, and measurement disproves
it.** :func:`write_su2_from_case` reads an OpenFOAM case through
``vtkOpenFOAMReader`` rather than through the canonical store, and it is what
``ImportExportService.export_su2`` actually calls. Measured on committed
snappy meshes: ``annulus`` exports 150,166 elements with 23,958 marker faces,
``box_with_cavity`` 70,744 elements.

So delegating to Gmsh would not consolidate two implementations of one job --
it would **delete the only route by which a snappy mesh reaches SU2**, because
Gmsh cannot read a polyMesh. The module stays, and this docstring is corrected
instead. What it used to claim -- "written from the canonical mesh on the
host... serves every engine" -- conflated the two entry points into one:

:func:`write_su2`
    from a :class:`CanonicalMesh`. In production that means a Gmsh mesh, since
    that is the only thing which constructs one.
:func:`write_su2_from_case`
    from any OpenFOAM case directory, via VTK. This is the snappy route, and
    the one the export service uses.

Neither needs a Gmsh install, which is the one part of the original claim that
was true of both.

Gmsh's own writer handles the hybrid layer mesh correctly -- ``NELEM= 21233``
(12,083 tets + 9,150 prisms), prisms typed VTK 13, ``NMARK= 3`` with patch
names intact (``plans/evidence/plan26-mmg-prisms/``) -- so a Gmsh boundary-layer
mesh is fully exportable either way. Markers come from physical groups there,
which the production runner does set.

The SU2 ``.su2`` format is plain ASCII. Elements are identified by their VTK
type code, which is the only part worth stating carefully, since a wrong code
produces a file SU2 reads without complaint and interprets as the wrong shape.

A marker-less ``.su2`` is readable and unsimulatable -- there are no boundaries
to apply conditions to -- so both entry points refuse to write one rather than
producing a file whose defect only shows up in the solver.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from foammesh.core.mesh.census import SU2_ADVICE, SU2_READER
from foammesh.core.mesh.model import CanonicalMesh, CellType
from foammesh.core.quantities import count_text

#: VTK type codes, which SU2 adopts verbatim.
VTK_TYPE = {
    CellType.TRIANGLE: 5,
    CellType.QUADRILATERAL: 9,
    CellType.TETRA: 10,
    CellType.HEXAHEDRON: 12,
    CellType.PRISM: 13,
    CellType.PYRAMID: 14,
}


class Su2ExportError(ValueError):
    pass


@dataclass(frozen=True)
class Su2ExportReport:
    destination: str
    dimensions: int
    points: int
    elements: int
    markers: tuple
    marker_elements: int
    #: Written, but worth saying out loud. Plan 28: a single marker means every
    #: boundary of the domain takes the same SU2 condition, which is a mesh
    #: that imports and cannot be set up.
    warnings: tuple = ()

    def to_dict(self) -> dict:
        return {
            'destination': self.destination,
            'dimensions': self.dimensions,
            'points': self.points,
            'elements': self.elements,
            'markers': list(self.markers),
            'markerElements': self.marker_elements,
            'warnings': list(self.warnings),
        }


def write_su2(mesh: CanonicalMesh, destination: str | Path) -> Su2ExportReport:
    """Write ``mesh`` as an SU2 native mesh file."""
    destination = Path(destination)
    if destination.exists():
        raise Su2ExportError(f'SU2 destination already exists: {destination}')
    if not mesh.cell_blocks:
        raise Su2ExportError('an SU2 mesh needs volume cells')

    for block in mesh.cell_blocks:
        if block.cell_type not in VTK_TYPE:
            raise Su2ExportError(
                f'SU2 export does not support {block.cell_type.value} cells')

    # Group boundary faces by patch so each becomes one SU2 marker.
    markers: dict[int, list[tuple[int, np.ndarray]]] = {}
    for block in mesh.boundary_blocks:
        code = VTK_TYPE[block.cell_type]
        for patch_id in np.unique(block.patch_ids):
            selected = block.connectivity[block.patch_ids == patch_id]
            markers.setdefault(int(patch_id), []).append((code, selected))

    # Plan 26 WP10.1. `write_su2_from_case` already refused this; the canonical
    # path did not, so the two entry points disagreed about whether a
    # marker-less file was acceptable. An SU2 mesh with NMARK=0 is readable and
    # unsimulatable -- there are no boundaries to apply conditions to -- and
    # the defect only shows up in the solver, which is the worst place to find
    # it. Gmsh emits markers from physical groups, so any path reaching a
    # writer without them produces exactly this.
    if not markers:
        raise Su2ExportError(
            'this mesh carries no boundary patches, so the SU2 file would '
            'have no markers and no boundary conditions could be applied')

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    element_count = mesh.cell_count
    marker_element_count = 0
    marker_names: list[str] = []

    with temporary.open('w', encoding='ascii', newline='\n') as stream:
        stream.write('%\n% FoamMesh SU2 export\n%\n')
        stream.write('NDIME= 3\n')
        stream.write(f'NELEM= {element_count}\n')
        index = 0
        for block in mesh.cell_blocks:
            code = VTK_TYPE[block.cell_type]
            for row in block.connectivity:
                nodes = ' '.join(str(int(value)) for value in row)
                stream.write(f'{code} {nodes} {index}\n')
                index += 1

        stream.write(f'NPOIN= {mesh.point_count}\n')
        for position, (x, y, z) in enumerate(mesh.points):
            stream.write(f'{x:.17g} {y:.17g} {z:.17g} {position}\n')

        stream.write(f'NMARK= {len(markers)}\n')
        for patch_id in sorted(markers, key=lambda key: _marker_name(mesh, key)):
            name = _marker_name(mesh, patch_id)
            marker_names.append(name)
            groups = markers[patch_id]
            count = sum(len(rows) for _code, rows in groups)
            marker_element_count += count
            stream.write(f'MARKER_TAG= {name}\n')
            stream.write(f'MARKER_ELEMS= {count}\n')
            for code, rows in groups:
                for row in rows:
                    nodes = ' '.join(str(int(value)) for value in row)
                    stream.write(f'{code} {nodes}\n')

    temporary.replace(destination)
    return Su2ExportReport(
        destination=str(destination), dimensions=3, points=mesh.point_count,
        elements=element_count, markers=tuple(marker_names),
        marker_elements=marker_element_count)


def _marker_name(mesh: CanonicalMesh, patch_id: int) -> str:
    item = mesh.patches.get(int(patch_id), {})
    name = str(item.get('solver_name') or item.get('name')
               or f'patch_{int(patch_id)}').strip()
    # SU2 marker tags are whitespace-delimited tokens.
    return name.replace(' ', '_') or f'patch_{int(patch_id)}'


def read_su2_summary(path: str | Path) -> dict:
    """Parse a written SU2 file back, for validation rather than for use.

    Reading the file independently is the point: comparing an exporter's own
    report against itself proves nothing.
    """
    path = Path(path)
    summary = {'dimensions': 0, 'elements': 0, 'points': 0,
               'markers': [], 'markerElements': 0}
    element_rows = point_rows = 0
    marker_rows = 0
    expect = None
    for raw in path.read_text(encoding='ascii').splitlines():
        line = raw.strip()
        if not line or line.startswith('%'):
            continue
        if line.startswith('NDIME='):
            summary['dimensions'] = int(line.split('=', 1)[1])
        elif line.startswith('NELEM='):
            summary['elements'] = int(line.split('=', 1)[1])
            expect = 'element'
        elif line.startswith('NPOIN='):
            summary['points'] = int(line.split('=', 1)[1])
            expect = 'point'
        elif line.startswith('NMARK='):
            expect = None
        elif line.startswith('MARKER_TAG='):
            summary['markers'].append(line.split('=', 1)[1].strip())
            expect = None
        elif line.startswith('MARKER_ELEMS='):
            summary['markerElements'] += int(line.split('=', 1)[1])
            expect = 'marker'
        elif expect == 'element':
            element_rows += 1
        elif expect == 'point':
            point_rows += 1
        elif expect == 'marker':
            marker_rows += 1
    summary['elementRows'] = element_rows
    summary['pointRows'] = point_rows
    summary['markerRows'] = marker_rows
    return summary


def validate_against(path: str | Path, *, points: int, cells: int,
                     boundary_faces: int) -> tuple[bool, tuple[str, ...]]:
    """Check a written SU2 file against the mesh it claims to represent."""
    summary = read_su2_summary(path)
    problems = []
    if summary['dimensions'] != 3:
        problems.append(f'NDIME is {summary["dimensions"]}, expected 3')
    for label, declared, rows, expected in (
            ('element', summary['elements'], summary['elementRows'], cells),
            ('point', summary['points'], summary['pointRows'], points)):
        if declared != expected:
            problems.append(
                f'declares {declared} {label}s, the mesh has {expected}')
        if rows != declared:
            problems.append(
                f'declares {declared} {label}s but wrote {rows} rows')
    if summary['markerElements'] != boundary_faces:
        problems.append(
            f'declares {summary["markerElements"]} marker elements, the mesh '
            f'has {boundary_faces} boundary faces')
    if summary['markerRows'] != summary['markerElements']:
        problems.append(
            f'declares {summary["markerElements"]} marker elements but wrote '
            f'{summary["markerRows"]} rows')
    return not problems, tuple(problems)


# --------------------------------------------------------------------------- #
# Case-level export
# --------------------------------------------------------------------------- #

_VOLUME_CODES = frozenset({10, 12, 13, 14})
_SURFACE_CODES = frozenset({5, 9})
#: What snappyHexMesh leaves at every refinement transition, and the one
#: unsupported type worth naming rather than numbering.
_VTK_POLYHEDRON = 42


def write_su2_from_case(case_path: str | Path, destination: str | Path
                        ) -> Su2ExportReport:
    """Export an OpenFOAM case as SU2, boundary patches included.

    Patches become SU2 markers. An SU2 mesh with no markers has no boundaries
    to apply conditions to, so the patch blocks are read as well as the
    interior rather than exporting a mesh nobody can run.
    """
    from foammesh.core.export.vtk_export import load_case_blocks

    case = Path(case_path)
    destination = Path(destination)
    if destination.exists():
        raise Su2ExportError(f'SU2 destination already exists: {destination}')

    # Plan 31 DP-23 moved the reader into `vtk_export.load_case_blocks`: the
    # Gmsh export needs the interior and the named patch blocks in exactly the
    # same shape, and the one thing worth getting right here -- that
    # `EnableAllPatchArrays()` leaves each array switched off, so a caller who
    # stops there gets no boundaries at all -- should be learned once.
    try:
        interior, patches = load_case_blocks(case)
    except Su2ExportError:
        raise
    except ValueError as error:
        raise Su2ExportError(str(error)) from error
    _refuse_unreadable(interior, patches)
    if not patches:
        raise Su2ExportError(
            'this case exposes no boundary patches, so the SU2 file would '
            'have no markers and no boundary conditions could be applied')
    warnings = []
    if len(patches) < 2:
        warnings.append(
            'the mesh has only one boundary marker, so every boundary '
            'would take the same SU2 condition')
    return _write_from_vtk(interior, patches, destination,
                           warnings=tuple(warnings))


def _refuse_unreadable(interior, patches) -> None:
    """Refuse a mesh SU2 cannot read, naming how much of it is the problem.

    The writer used to put ``interior.GetCellType(index)`` straight into the
    file, so a polyhedron went out as VTK type 42 and SU2 rejected the file at
    load. Counting first costs one pass and turns a solver-side failure into a
    sentence the user can act on before anything is written.
    """
    volume_offenders = {}
    for index in range(interior.GetNumberOfCells()):
        code = interior.GetCellType(index)
        if code not in _VOLUME_CODES:
            volume_offenders[code] = volume_offenders.get(code, 0) + 1
    surface_offenders = 0
    for _name, block in patches:
        for index in range(block.GetNumberOfCells()):
            if block.GetCellType(index) not in _SURFACE_CODES:
                surface_offenders += 1

    if not volume_offenders and not surface_offenders:
        return

    parts = []
    total = sum(volume_offenders.values())
    if total:
        polyhedra = volume_offenders.get(_VTK_POLYHEDRON, 0)
        cells = interior.GetNumberOfCells()
        if polyhedra:
            parts.append(f'{polyhedra} of {cells} cells '
                         f'{"is" if polyhedra == 1 else "are"} polyhedral')
        other = total - polyhedra
        if other:
            codes = ', '.join(
                str(code) for code in sorted(volume_offenders)
                if code != _VTK_POLYHEDRON)
            parts.append(f'{other} cells have unsupported VTK types ({codes})')
    if surface_offenders:
        parts.append(
            f'{surface_offenders} boundary '
            f'{"face has" if surface_offenders == 1 else "faces have"} '
            'five or more vertices')
    raise Su2ExportError(
        SU2_READER + '; ' + ', and '.join(parts) + '. ' + SU2_ADVICE)



def _write_from_vtk(interior, patches, destination: Path, *,
                    warnings: tuple = ()) -> Su2ExportReport:
    """Write SU2 from VTK blocks, mapping patch points onto interior ones.

    Patch blocks carry their own point arrays while SU2 indexes one global
    list, so patch points are matched back to interior points by coordinate.
    A face that cannot be matched aborts the export: half a marker is worse
    than no file.
    """
    points = np.asarray(
        [interior.GetPoint(index) for index in range(interior.GetNumberOfPoints())],
        dtype=np.float64)
    lookup = {tuple(np.round(row, 12)): index for index, row in enumerate(points)}

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    marker_names, marker_total, unmatched = [], 0, 0

    with temporary.open('w', encoding='ascii', newline='\n') as stream:
        stream.write('%\n% FoamMesh SU2 export\n%\n')
        stream.write('NDIME= 3\n')
        stream.write(f'NELEM= {interior.GetNumberOfCells()}\n')
        for index in range(interior.GetNumberOfCells()):
            ids = interior.GetCell(index).GetPointIds()
            nodes = ' '.join(
                str(ids.GetId(position)) for position in range(ids.GetNumberOfIds()))
            stream.write(f'{interior.GetCellType(index)} {nodes} {index}\n')

        stream.write(f'NPOIN= {len(points)}\n')
        for position, (x, y, z) in enumerate(points):
            stream.write(f'{x:.17g} {y:.17g} {z:.17g} {position}\n')

        rendered = []
        for name, block in patches:
            rows = []
            for index in range(block.GetNumberOfCells()):
                ids = block.GetCell(index).GetPointIds()
                mapped = []
                for position in range(ids.GetNumberOfIds()):
                    key = tuple(np.round(block.GetPoint(ids.GetId(position)), 12))
                    target = lookup.get(key)
                    if target is None:
                        mapped = []
                        break
                    mapped.append(target)
                if mapped:
                    rows.append((block.GetCellType(index), mapped))
                else:
                    unmatched += 1
            if rows:
                rendered.append((str(name).replace(' ', '_'), rows))

        stream.write(f'NMARK= {len(rendered)}\n')
        for name, rows in rendered:
            marker_names.append(name)
            marker_total += len(rows)
            stream.write(f'MARKER_TAG= {name}\n')
            stream.write(f'MARKER_ELEMS= {len(rows)}\n')
            for code, mapped in rows:
                stream.write(
                    f'{code} ' + ' '.join(str(value) for value in mapped) + '\n')

    if unmatched:
        temporary.unlink(missing_ok=True)
        raise Su2ExportError(
            f'{count_text(unmatched, "boundary face")} could not be matched '
            'to an interior point; the exported markers would be incomplete')
    temporary.replace(destination)
    return Su2ExportReport(
        destination=str(destination), dimensions=3, points=len(points),
        elements=interior.GetNumberOfCells(), markers=tuple(marker_names),
        marker_elements=marker_total, warnings=tuple(warnings))
