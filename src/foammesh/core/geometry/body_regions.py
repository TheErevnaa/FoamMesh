"""Which region of the mesh each body of the project ends up in.

A region interface is authored between two *bodies*: Split interfaces names
the two volumes it cut apart (``geometry:<volume key>``), because at the cut
nobody has said yet what regions the mesh will be split into. The export and
the pre-run check need the *mesh* region on each side: the seeded region on
snappy (the ``region`` table), the typed solid on Gmsh. The two names need
not agree -- a body imported as ``01_hot_fluid_r10mm_L500mm`` becomes the
region ``Hot_fluid`` -- so a side is matched by what the body is, not only by
what it is called:

- by name, when the body and the region carry the same one;
- on Gmsh, by the solid the body is (its region uuid, which the typed volume
  control is keyed by);
- otherwise, by the one region seed that lies inside the body's closed
  surface: its own skin plus the interfaces that bound it.

A body no region can be matched to is simply absent from the answer.
"""
from __future__ import annotations

import re
from pathlib import Path

from foammesh.db import surface_intent as model


def _text(value) -> str:
    return model._text(value)


def _foam_word(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]', '_', name)


def _by_name(name: str, regions) -> str | None:
    for candidate in (name, _foam_word(name), name.replace(' ', '_')):
        if candidate and candidate in regions:
            return candidate
    return None


def referenced_bodies(document) -> list[str]:
    """The volume keys a relation names as one of its sides."""
    keys = []
    for relation in ((document or {}).get('surfaceRelations') or {}).values():
        if not isinstance(relation, dict):
            continue
        for side in ('ownerRegion', 'neighbourRegion'):
            key = model.region_key(relation.get(side))
            if key is not None and key not in keys:
                keys.append(key)
    return keys


def _surface_rows(geometry, volume) -> list[dict]:
    return [row for row in geometry.values()
            if model._is_surface(row) and str(row.get('volume')) == str(volume)]


def _bounding_geometry_ids(document, volume) -> list[str]:
    """The artifacts whose faces bound *volume*: its own surfaces, and the
    surfaces of every relation that names it as a side."""
    geometry = (document or {}).get('geometry') or {}
    found = [_text(row.get('geometryId'))
             for row in _surface_rows(geometry, volume)]
    reference = model.region_ref(volume)
    for relation in ((document or {}).get('surfaceRelations') or {}).values():
        if not isinstance(relation, dict):
            continue
        if reference not in (_text(relation.get('ownerRegion')),
                             _text(relation.get('neighbourRegion'))):
            continue
        for side in ('ownerSurface', 'neighbourSurface'):
            row = geometry.get(str(relation.get(side)))
            if isinstance(row, dict):
                found.append(_text(row.get('geometryId')))
    return [item for item in dict.fromkeys(found) if item]


def _triangles(paths) -> tuple[list, list]:
    """``(triangles, coordinates)`` of the STL files *paths*, unwelded."""
    import vtk

    triangles, coordinates = [], []
    for path in paths:
        reader = vtk.vtkSTLReader()
        reader.SetFileName(str(path))
        reader.Update()
        surface = reader.GetOutput()
        points = surface.GetPoints()
        if points is None:
            continue
        base = len(coordinates)
        coordinates.extend(points.GetPoint(index)
                           for index in range(points.GetNumberOfPoints()))
        ids = vtk.vtkIdList()
        for cell in range(surface.GetNumberOfCells()):
            surface.GetCellPoints(cell, ids)
            if ids.GetNumberOfIds() == 3:
                triangles.append(tuple(base + ids.GetId(n) for n in range(3)))
    return triangles, coordinates


def _gmsh_names(document) -> dict:
    """Solid region uuid -> the typed region name, on Gmsh."""
    from foammesh.core.mesh.cad_solids import typing_of

    rows = ((document or {}).get('gmsh') or {}).get('volumeControls') or {}
    return {token: str(item.get('name') or '').strip()
            for token, item in typing_of(rows if isinstance(rows, dict)
                                         else {}).items()
            if item.get('type') and item.get('included')
            and str(item.get('name') or '').strip()}


def body_regions(document, case_path=None) -> dict[str, str]:
    """``{volume key: mesh region name}`` for every body a relation names.

    *document* is the plain project document; *case_path* finds the geometry
    store, without which only the name and the Gmsh solid are tried.
    """
    document = document if isinstance(document, dict) else {}
    regions = model.mesh_region_names(document)
    bodies = referenced_bodies(document)
    if not regions or not bodies:
        return {}
    geometry = document.get('geometry') or {}
    engine = _text((document.get('mesh') or {}).get('engine')).lower()
    entries = {}
    if case_path is not None:
        from .store import GeometryArtifactStore

        try:
            entries = {str(entry.get('geometry_id')): entry
                       for entry in GeometryArtifactStore(case_path).entries()}
        except Exception:  # noqa: BLE001 - no store, no geometric matching
            entries = {}
    seeds = []
    if engine != 'gmsh':
        for _key, row in model._sorted_items(document.get('region') or {}):
            if not isinstance(row, dict) or not _text(row.get('name')):
                continue
            point = row.get('point') or {}
            try:
                seeds.append((_text(row.get('name')),
                              tuple(float(point[axis]) for axis in 'xyz')))
            except (KeyError, TypeError, ValueError):
                continue
    typed = _gmsh_names(document) if engine == 'gmsh' else {}

    answer: dict[str, str] = {}
    for volume in bodies:
        row = geometry.get(str(volume))
        if not isinstance(row, dict):
            continue
        found = _by_name(_text(row.get('name')), regions)
        if found is None and engine == 'gmsh':
            tokens = [_text(row.get('regionUuid'))]
            from foammesh.core.mesh.cad_solids import entry_regions

            for surface in _surface_rows(geometry, volume):
                geometry_id = _text(surface.get('geometryId'))
                if not geometry_id:
                    continue
                # A plain STL has no region records: entry_regions gives it
                # the uuid the volume controls use, made from its geometry
                # id alone, so it is found even without the store (the Gmsh
                # job's own gate passes no case path).
                entry = entries.get(geometry_id) or {'geometry_id': geometry_id}
                tokens.extend(_text(item.get('region_uuid'))
                              for item in entry_regions(entry))
            names = {typed[token] for token in tokens if token in typed}
            if len(names) == 1:
                found = _by_name(next(iter(names)), regions)
        if found is None and seeds and entries:
            paths = [entries[item]['artifact']
                     for item in _bounding_geometry_ids(document, volume)
                     if item in entries and entries[item].get('artifact')
                     and Path(entries[item]['artifact']).suffix.lower()
                     == '.stl']
            if paths:
                from .interface_split import inside

                triangles, coordinates = _triangles(paths)
                held = [name for name, point in seeds
                        if inside(point, triangles, coordinates)]
                if len(held) == 1:
                    found = _by_name(held[0], regions)
        if found is not None:
            answer[str(volume)] = found
    return answer


def side_region(document, reference, bodies: dict) -> str | None:
    """The mesh region one side of a relation names, or ``None``."""
    regions = model.mesh_region_names(document)
    name = model.region_name(reference)
    if name is not None:
        return _by_name(name, regions)
    key = model.region_key(reference)
    if key is not None:
        return bodies.get(str(key))
    return None
