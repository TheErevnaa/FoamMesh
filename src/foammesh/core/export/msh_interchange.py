#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""The interchange file the Gmsh export converts from -- patch names included.

Plan 31 DP-23. ``ImportExportService.export_gmsh`` used to hand Gmsh a legacy
ASCII ``.vtk`` written from the published ``constant/polyMesh``. A legacy VTK
file has no way to say what a boundary is called, so nothing downstream of that
intermediate had a patch name to write.

MEASURED 2026-09-06 on ``tests/fixtures/cases/single_hex``, whose
``constant/polyMesh/boundary`` names exactly one patch ``walls`` with six
faces: the old route produced a 240-byte MSH 4.1 file reading back as
``{"nodes": 8, "cells": 1, "groups": []}``, with an ``$Entities`` block of
``0 0 0 1`` -- one unnamed volume and not a single surface for a name to
attach to. The geometry was right and the boundary identity was gone, so the
receiving solver had nothing to apply a boundary condition to.

The intermediate written here is MSH 2.2 ASCII instead. It is the simplest
format in the chain that carries ``$PhysicalNames``, so each polyMesh patch
becomes one named surface group and each boundary face is tagged with it.
Everything else stays as it was: Gmsh still opens the intermediate and writes
whatever the destination suffix asks for, and the file that lands is still
read back independently before the export is called a success.

Two routes were measured and rejected first:

* **A richer VTK intermediate.** The name loss is a property of the legacy
  format, so a multiblock VTK carrying block names is the obvious repair --
  except that Gmsh's VTK reader reads the legacy grammar and takes no block
  names from it, so the names would be dropped at the same seam one step
  later.
* **Copying the Gmsh run's own ``mesh.msh``**, the way the MED and UNV
  exports do. That file does keep its names, and it is the right source when
  it exists; a snappyHexMesh case has no Gmsh run at all, and this route is
  the only one such a case has. Rerouting would have deleted the export for
  every snappy mesh.

MSH has no polyhedron element, so a cell VTK reports as ``VTK_POLYHEDRON`` is
refused by name and by count rather than written as something else. That is
not a new limit: MEASURED the same day, the old route on
``test_cases/snappyhexmesh/box_with_cavity`` (70,744 cells, 12,246 of them
polyhedral) died inside the WSL Gmsh runtime with ``Unknown type of cell 42``
and surfaced as a ``RuntimeError`` rather than the ``ValueError`` the export
contract promises. The refusal below says what is wrong before anything is
written.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from foammesh.core.mesh.msh_scene import SURFACE_TYPES, VOLUME_TYPES

#: VTK cell type -> MSH element type, inverted from the one table this
#: application already keeps for reading MSH files back. The node orders agree
#: between the two for every entry, which is why no permutation table appears
#: here either -- see ``core.mesh.msh_scene``.
MSH_VOLUME_TYPE = {vtk: msh for msh, (vtk, _nodes) in VOLUME_TYPES.items()}
MSH_SURFACE_TYPE = {vtk: msh for msh, (vtk, _nodes) in SURFACE_TYPES.items()}

#: What snappyHexMesh leaves at every refinement transition, named rather than
#: numbered so a refusal reads as a sentence.
_VTK_POLYHEDRON = 42


class MshInterchangeError(ValueError):
    """The case could not be written as an MSH interchange, and why."""


@dataclass(frozen=True)
class InterchangeReport:
    """What was written, and therefore what reading it back has to give.

    The counts and names an export holds the converted file against. They come
    from the mesh that was written, never from the writer having returned.
    """

    path: Path
    nodes: int
    cells: int
    groups: tuple
    boundary_faces: int

    def to_dict(self) -> dict:
        return {'path': str(self.path), 'nodes': self.nodes,
                'cells': self.cells, 'groups': list(self.groups),
                'boundary_faces': self.boundary_faces}


def _refuse_unwritable(interior, patches) -> None:
    """Refuse a mesh MSH cannot hold, naming how much of it is the problem."""
    volume_offenders: dict = {}
    for index in range(interior.GetNumberOfCells()):
        code = interior.GetCellType(index)
        if code not in MSH_VOLUME_TYPE:
            volume_offenders[code] = volume_offenders.get(code, 0) + 1
    surface_offenders = 0
    for _name, block in patches:
        for index in range(block.GetNumberOfCells()):
            if block.GetCellType(index) not in MSH_SURFACE_TYPE:
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
            codes = ', '.join(str(code) for code in sorted(volume_offenders)
                              if code != _VTK_POLYHEDRON)
            parts.append(f'{other} cells have unsupported VTK types ({codes})')
    if surface_offenders:
        parts.append(
            f'{surface_offenders} boundary '
            f'{"face has" if surface_offenders == 1 else "faces have"} '
            'five or more vertices')
    raise MshInterchangeError(
        'a Gmsh mesh holds tetrahedra, hexahedra, prisms and pyramids only; '
        + ', and '.join(parts)
        + '. Export this case as OpenFOAM or VTU, which keep polyhedra.')


def _group_label(name: str, used: set) -> str:
    """A ``$PhysicalNames`` label: quoted, so only the quote needs removing."""
    label = str(name).replace('"', '').strip() or 'patch'
    if label in used:
        suffix = 2
        while f'{label}_{suffix}' in used:
            suffix += 1
        label = f'{label}_{suffix}'
    used.add(label)
    return label


def write_msh_interchange(interior, patches, path) -> InterchangeReport:
    """Write *interior* plus named *patches* as MSH 2.2, and say what went in.

    ``patches`` is the ``(name, block)`` sequence
    :func:`~foammesh.core.export.vtk_export.load_case_blocks` returns. Patch
    blocks carry their own point arrays while MSH indexes one global list, so
    patch points are matched back to interior points by coordinate -- the same
    join the SU2 export makes, and a face that cannot be matched aborts the
    write, because half a boundary is worse than no file.
    """
    path = Path(path)
    named = [(name, block) for name, block in patches if block.GetNumberOfCells()]
    _refuse_unwritable(interior, named)

    points = np.asarray(
        [interior.GetPoint(index) for index in range(interior.GetNumberOfPoints())],
        dtype=np.float64)
    if not len(points) or not interior.GetNumberOfCells():
        raise MshInterchangeError('this case has no internal mesh to export')
    lookup = {tuple(np.round(row, 12)): index for index, row in enumerate(points)}

    used: set = set()
    labels = [_group_label(name, used) for name, _block in named]

    rows: list = []
    for index in range(interior.GetNumberOfCells()):
        ids = interior.GetCell(index).GetPointIds()
        nodes = [ids.GetId(position) + 1 for position in range(ids.GetNumberOfIds())]
        # Volume elements get an elementary entity of their own and no
        # physical group: the polyMesh names boundaries, not the interior, and
        # inventing a name for the volume would put a group in the exported
        # file that the case never had.
        rows.append((MSH_VOLUME_TYPE[interior.GetCellType(index)], 0, 1, nodes))

    unmatched = 0
    boundary_faces = 0
    for tag, (_name, block) in enumerate(named, start=1):
        for index in range(block.GetNumberOfCells()):
            ids = block.GetCell(index).GetPointIds()
            mapped = []
            for position in range(ids.GetNumberOfIds()):
                key = tuple(np.round(block.GetPoint(ids.GetId(position)), 12))
                target = lookup.get(key)
                if target is None:
                    mapped = []
                    break
                mapped.append(target + 1)
            if not mapped:
                unmatched += 1
                continue
            rows.append((MSH_SURFACE_TYPE[block.GetCellType(index)],
                         tag, tag + 1, mapped))
            boundary_faces += 1
    if unmatched:
        raise MshInterchangeError(
            f'{unmatched} boundary face(s) could not be matched to an interior '
            'point; the exported patches would be incomplete')

    lines = ['$MeshFormat', '2.2 0 8', '$EndMeshFormat',
             '$PhysicalNames', str(len(labels))]
    lines.extend(f'2 {tag} "{label}"' for tag, label in enumerate(labels, start=1))
    lines.append('$EndPhysicalNames')
    lines.append('$Nodes')
    lines.append(str(len(points)))
    lines.extend(f'{index} {x:.17g} {y:.17g} {z:.17g}'
                 for index, (x, y, z) in enumerate(points, start=1))
    lines.append('$EndNodes')
    lines.append('$Elements')
    lines.append(str(len(rows)))
    lines.extend(
        f'{number} {element} 2 {physical} {entity} '
        + ' '.join(str(value) for value in nodes)
        for number, (element, physical, entity, nodes) in enumerate(rows, start=1))
    lines.append('$EndElements')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='ascii', newline='\n')

    return InterchangeReport(
        path=path, nodes=len(points), cells=interior.GetNumberOfCells(),
        groups=tuple(labels), boundary_faces=boundary_faces)
