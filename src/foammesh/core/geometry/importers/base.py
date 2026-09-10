#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Surface import dispatch + result types.

Headless: returns VTK polydata + bounding box; no Qt. The GUI import dialog wraps
this and adds the unit wizard (see core.geometry.units).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from foammesh.core.format_registry import core_geometry_suffixes

from ..bbox import BBox


@dataclass
class ImportedSurface:
    name: str
    polydata: object            # vtkPolyData
    source_file: str = ''
    #: Plan 28 WP7. The ``solid`` names of an STL with more than one; in
    #: ``cadFaceId`` order, so index *i* is the name of face id *i* unless
    #: the names themselves carry the id (``face<N>``).
    solid_names: list[str] = field(default_factory=list)
    #: True when ``solid_names`` was worked out from the geometry (a binary
    #: STL's connected shells, an OBJ's ``g`` groups) rather than read from
    #: named blocks the file format carries. The store rewrites the artifact
    #: in that case, so the names survive the next read: a byte-for-byte copy
    #: of a binary STL cannot say what its pieces are called.
    derived_names: bool = False

    @property
    def bbox(self) -> BBox:
        return BBox.from_polydata(self.polydata)

    @property
    def n_cells(self) -> int:
        return int(self.polydata.GetNumberOfCells())

    @property
    def n_points(self) -> int:
        return int(self.polydata.GetNumberOfPoints())


@dataclass
class ImportResult:
    surfaces: list[ImportedSurface] = field(default_factory=list)
    source_format: str = ''

    @property
    def bbox(self) -> BBox | None:
        return BBox.union(s.bbox for s in self.surfaces)


SUPPORTED_SUFFIXES = core_geometry_suffixes()


def import_surface(path) -> ImportResult:
    """Import a surface-geometry file (.stl or .obj) by extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == '.stl':
        from .stl_importer import read_stl
        return read_stl(path)
    if suffix == '.obj':
        from .obj_importer import read_obj
        return read_obj(path)
    raise ValueError(f'unsupported geometry format: {suffix!r} '
                     f'(supported: {", ".join(SUPPORTED_SUFFIXES)})')
