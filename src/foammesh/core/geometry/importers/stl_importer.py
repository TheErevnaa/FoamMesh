#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Headless STL import via VTK.

Plan 28 WP7. An ASCII STL may hold several ``solid`` blocks, and those are
the only sub-surface structure the format can carry: a CAD exporter writes
one per face group, and FoamMesh's own named-solid writer writes one per
patch. Until WP7 the reader flattened every file to one surface, so a
multi-solid file imported as one boundary and a split geometry lost its
patches on the first re-read. Now a file with two or more solids comes back
tagged per solid in the ``cadFaceId`` cell array -- the same array a CAD
tessellation carries -- with the solid names kept on the surface.

Solids named ``face<N>`` (what the store writes) keep ``N`` as their id, so
a revision written from a split and read back has the ids its patch
records refer to. Any other names are numbered in file order.
"""
from __future__ import annotations

from pathlib import Path
import re

from vtkmodules.vtkIOGeometry import vtkSTLReader

from .base import ImportedSurface, ImportResult

#: The array vtkSTLReader fills with the 0-based solid index of each facet.
_SOLID_TAG_ARRAY = 'STLSolidLabeling'
_SOLID_LINE = re.compile(r'^\s*solid\b\s*(.*?)\s*$', re.IGNORECASE)
_OWN_NAME = re.compile(r'^face(\d+)$')


def _is_ascii_stl(path: Path) -> bool:
    """A binary STL may start with the word ``solid`` too; the facet keyword
    inside the first kilobyte is what tells them apart."""
    try:
        with path.open('rb') as stream:
            head = stream.read(1024)
    except OSError:
        return False
    if not head.lstrip().lower().startswith(b'solid'):
        return False
    try:
        text = head.decode('ascii')
    except UnicodeDecodeError:
        return False
    return 'facet' in text.lower() or 'endsolid' in text.lower()


def solid_names(path) -> list[str]:
    """The ``solid`` block names of an ASCII STL, in file order.

    Empty for a binary file. A nameless block is called ``solid_<index>``
    and a repeated name is suffixed, so the list can serve as region keys.
    """
    path = Path(path)
    if not _is_ascii_stl(path):
        return []
    names: list[str] = []
    seen: dict[str, int] = {}
    with path.open('r', encoding='ascii', errors='replace') as stream:
        for line in stream:
            match = _SOLID_LINE.match(line)
            if match is None:
                continue
            name = match.group(1).strip() or f'solid_{len(names) + 1}'
            if name in seen:
                seen[name] += 1
                name = f'{name}_{seen[name]}'
            seen.setdefault(name, 1)
            names.append(name)
    return names


def facet_count(path) -> int | None:
    """How many facets the file itself declares, before anything reads it.

    ``None`` when the number cannot be had from the file alone. A binary STL
    states its count in the four bytes after the 80-byte header; an ASCII one
    is counted by its ``facet`` keywords. This is deliberately not a VTK read:
    the whole point is to know what the reader was given, not what it kept.
    """
    path = Path(path)
    try:
        if not _is_ascii_stl(path):
            with path.open('rb') as stream:
                header = stream.read(84)
            if len(header) < 84:
                return None
            return int.from_bytes(header[80:84], 'little')
        facets = 0
        with path.open('r', encoding='ascii', errors='replace') as stream:
            for line in stream:
                if line.lstrip().lower().startswith('facet'):
                    facets += 1
        return facets
    except OSError:
        return None


def face_ids_for(names: list[str]) -> list[int]:
    """The ``cadFaceId`` each solid gets: its own number when every name is
    ``face<N>`` with distinct ``N``, else its position in the file."""
    numbers = [_OWN_NAME.match(name) for name in names]
    if names and all(numbers):
        ids = [int(match.group(1)) for match in numbers]
        if len(set(ids)) == len(ids):
            return ids
    return list(range(len(names)))


#: More shells than this and the file is a mesh of loose facets, not an
#: assembly: naming each one would bury the geometry list under noise.
_MAX_DERIVED_GROUPS = 64


def component_groups(polydata, stem: str):
    """Tag each connected shell of *polydata* as its own face, or None.

    The binary STL format carries no names at all, so the only sub-surface
    structure such a file has is where its triangles stop touching. Two or
    more shells are two or more boundaries -- the same answer the ASCII
    reader gets from ``solid`` blocks -- and returning them here is what
    makes them patches. Returns ``(polydata, names)``; ``None`` when there
    is one shell, or so many that they cannot be meant as boundaries.
    """
    from vtkmodules.vtkCommonCore import vtkIntArray
    from vtkmodules.vtkFiltersCore import vtkPolyDataConnectivityFilter

    from ..cad.surface_split import FACE_ID_ARRAY

    if polydata.GetNumberOfCells() <= 0:
        return None
    connectivity = vtkPolyDataConnectivityFilter()
    connectivity.SetInputData(polydata)
    connectivity.SetExtractionModeToAllRegions()
    connectivity.ColorRegionsOn()
    connectivity.Update()
    count = int(connectivity.GetNumberOfExtractedRegions())
    if count < 2 or count > _MAX_DERIVED_GROUPS:
        return None
    output = connectivity.GetOutput()
    if output.GetNumberOfCells() != polydata.GetNumberOfCells():
        return None
    # Which array `ColorRegionsOn` fills depends on the VTK build: some write
    # the cell array, some only the point one. A triangle belongs to the
    # region of its points either way.
    regions = output.GetCellData().GetArray('RegionId')
    point_regions = output.GetPointData().GetArray('RegionId')
    if regions is None and point_regions is None:
        return None
    face_ids = vtkIntArray()
    face_ids.SetName(FACE_ID_ARRAY)
    face_ids.SetNumberOfTuples(output.GetNumberOfCells())
    for cell_id in range(output.GetNumberOfCells()):
        if regions is not None:
            region = int(regions.GetTuple1(cell_id))
        else:
            cell = output.GetCell(cell_id)
            region = int(point_regions.GetTuple1(cell.GetPointId(0)))
        face_ids.SetValue(cell_id, region)
    output.GetCellData().RemoveArray('RegionId')
    output.GetPointData().RemoveArray('RegionId')
    output.GetCellData().RemoveArray(FACE_ID_ARRAY)
    output.GetCellData().AddArray(face_ids)
    return output, [f'{stem}_{index + 1}' for index in range(count)]


def read_stl(path) -> ImportResult:
    path = Path(path)
    reader = vtkSTLReader()
    reader.SetFileName(str(path))
    reader.ScalarTagsOn()
    reader.Update()
    polydata = reader.GetOutput()
    names = solid_names(path)
    derived = False
    cell_data = polydata.GetCellData()
    tags = cell_data.GetArray(_SOLID_TAG_ARRAY)
    if len(names) >= 2 and tags is not None:
        from vtkmodules.vtkCommonCore import vtkIntArray

        from ..cad.surface_split import FACE_ID_ARRAY

        ids = face_ids_for(names)
        face_ids = vtkIntArray()
        face_ids.SetName(FACE_ID_ARRAY)
        face_ids.SetNumberOfTuples(polydata.GetNumberOfCells())
        for cell_id in range(polydata.GetNumberOfCells()):
            index = int(tags.GetTuple1(cell_id))
            face_ids.SetValue(cell_id, ids[index] if 0 <= index < len(ids) else index)
        cell_data.RemoveArray(FACE_ID_ARRAY)
        cell_data.AddArray(face_ids)
    elif not names:
        # Binary: the file said nothing, so its shells are the only structure
        # it has. Until now every binary STL imported as a single boundary,
        # whatever it held.
        grouped = component_groups(polydata, path.stem)
        if grouped is not None:
            polydata, names = grouped
            cell_data = polydata.GetCellData()
            derived = True
    else:
        names = names[:1]
    # The reader's own tag array is a detail of this function; nothing
    # downstream reads it, and leaving it as the active scalars would make
    # every later writer and filter carry it along.
    cell_data.RemoveArray(_SOLID_TAG_ARRAY)
    surface = ImportedSurface(name=path.stem, polydata=polydata,
                              source_file=str(path), solid_names=names,
                              derived_names=derived)
    return ImportResult(surfaces=[surface], source_format='stl')
