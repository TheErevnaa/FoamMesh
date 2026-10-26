"""The regions an OpenFOAM export splits the mesh into.

DP-6058. On snappy a region is a row of the ``region`` table: a name, a type
and the point that seeds it. On Gmsh a region is a solid (Plan 36 RP11):
applying one types the solid's volume control (``volumeType``) under the name
the user gave it, and writes no ``region`` row. The export, the
``regionProperties`` writer, the region-zone removal and the hand-off read
only the table, so a Gmsh case with two typed solids -- MEASURED in Plan 49
SR9, run sr9d: two touching cubes, one fluid and one solid -- exported as one
region, and the interface between them was refused as not coupled.

A Gmsh case's regions are its typed, included solids, named as their cell
zones publish (``core.gmsh.execution.typed_region_names``). Every other case
reads the table, as before.
"""
from __future__ import annotations


class SolidRegion:
    """A typed Gmsh solid, read as the export reads a ``region`` row."""

    #: Its cell zone is the mesher's, published under its name; there is no
    #: point to make one from.
    solid = True

    def __init__(self, name: str, type_: str):
        self.name, self.type = name, type_

    def value(self, field):
        if field == 'name':
            return self.name
        if field == 'type':
            return self.type
        raise KeyError(field)

    def __repr__(self):
        return f'SolidRegion({self.name!r}, {self.type!r})'


def are_solids(regions) -> bool:
    """Whether *regions* are typed solids, whose zones the mesher made."""
    return any(getattr(region, 'solid', False)
               for region in (regions or {}).values())


def solid_regions(db) -> dict | None:
    """``{control key: SolidRegion}`` for a Gmsh case, else ``None``.

    ``None`` when the configured engine meshes seeded regions, or when no
    solid is typed (a Gmsh case that never answered "how many regions?" is
    one region, and any stale table is read as before).
    """
    from foammesh.core.engine.registry import (
        ENGINE_REGISTRY, configured_engine_id)

    try:
        engine = ENGINE_REGISTRY.get(configured_engine_id(db))
    except Exception:  # noqa: BLE001 - no engine chosen, or an unknown one
        return None
    if not getattr(engine, 'regions_are_solids', False):
        return None
    try:
        rows = dict(db.getElements('gmsh/volumeControls') or {})
    except Exception:  # noqa: BLE001 - a case without the list
        return None
    from foammesh.core.gmsh.execution import typed_region_names
    from foammesh.core.mesh.cad_solids import typing_of

    names = typed_region_names(db)
    regions = {}
    for token, item in typing_of(rows).items():
        if not item.get('type') or not item.get('included') \
                or token not in names:
            continue
        regions[item['control']] = SolidRegion(names[token], item['type'])
    return regions or None


def export_regions(db) -> dict:
    """The regions the export splits by: Gmsh's typed solids, or the table."""
    found = solid_regions(db)
    if found is not None:
        return found
    return db.getElements('region')
