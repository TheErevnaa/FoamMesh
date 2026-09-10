#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""One surface per boundary, read back out of a geometry artifact.

The patch manifest says which sub-surfaces make up each boundary; the
artifact holds the triangles. Anything that has to show a boundary --
the geometry tree, the case builder, a renderer -- needs it as a
surface rather than as a record, and this is where the record becomes
one.

The manifest and the artifact agree by solid name: the store writes one
``solid`` block per sub-surface, named as the patch record's
``source_ref.original_name`` (``face<N>`` when a CAD face carries no
name of its own), and a merged boundary keeps one member record per
block it covers. So a row's triangles are the union of its members'
blocks, which is exactly what a merge means.
"""
from __future__ import annotations

from pathlib import Path


def member_solid_names(row: dict) -> list[str]:
    """The artifact solid blocks one manifest row covers, in row order."""
    names: list[str] = []
    for ref in row.get('source_refs') or ():
        original = ref.get('original_name')
        face_index = ref.get('face_index')
        if original:
            names.append(str(original))
        elif isinstance(face_index, int):
            names.append('face{0}'.format(face_index))
    return names


def patch_polydata(artifact, rows: list) -> dict:
    """Boundary name -> the triangles it covers, for the rows given.

    A row whose blocks are not in the artifact is left out rather than
    returned empty: a caller writing database rows has to be able to
    tell "this boundary is not in this file" from "this boundary is
    empty", and only the first is a reason to leave the old row alone.

    An artifact with a single unnamed solid is the whole surface, so a
    lone row gets all of it -- that is the un-split import, where the
    manifest has one record and the file has one block.
    """
    from vtkmodules.vtkFiltersCore import vtkAppendPolyData

    from ..cad.surface_split import split_by_face_id_indexed
    from ..importers.stl_importer import face_ids_for, read_stl

    surfaces = read_stl(Path(artifact)).surfaces
    if not surfaces:
        return {}
    surface = surfaces[0]
    solids = list(surface.solid_names or ())
    pieces = {face_id: piece
              for face_id, _name, piece in split_by_face_id_indexed(surface.polydata)}
    by_solid = dict(zip(solids, face_ids_for(solids)))

    out: dict = {}
    for row in rows or ():
        name = row.get('name')
        if not name:
            continue
        members = member_solid_names(row)
        if not members and len(rows) == 1:
            # One record, one untagged file: the row is the whole surface.
            out[str(name)] = surface.polydata
            continue
        found = [pieces[by_solid[solid]]
                 for solid in members
                 if solid in by_solid and by_solid[solid] in pieces]
        if not found:
            continue
        if len(found) == 1:
            out[str(name)] = found[0]
            continue
        append = vtkAppendPolyData()
        for piece in found:
            append.AddInputData(piece)
        append.Update()
        out[str(name)] = append.GetOutput()
    return out
