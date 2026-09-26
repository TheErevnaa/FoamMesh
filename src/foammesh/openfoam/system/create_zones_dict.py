#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``system/createZonesDict`` for the cell zones an export adds to the mesh.

DP-553. The export used to write a ``topoSetDict`` of ``cellSet`` actions
followed by ``cellZoneSet``/``setToCellZone`` and run ``topoSet``. OpenFOAM 13
still builds that utility, but prints on every run that it "has been superseded
by createZones and is now deprecated" (``topoSet.C``), and zones are made by
``createZones`` from ``zoneGenerators`` (``src/meshTools/zoneGenerators``).
Each entry below is one generator, in the keywords its ``Usage`` block names:

* ``box``           -- ``box (min) (max)`` (``volume/box/box.H``)
* ``cylinder``      -- ``point1``, ``point2``, ``radius`` (``cylinder.H``)
* ``sphere``        -- ``centre``, ``radius`` (``sphere.H``)
* ``insideSurface`` -- ``surface closedTriSurface; file "<name>.stl"``, read
  from ``constant/geometry`` or ``constant/triSurface`` (``insideSurface.C``,
  ``searchableSurface::geometryDir``)
* ``remove``        -- ``cellZones (...)`` (``remove/remove.H``)

MEASURED on OpenFOAM 13 (a 10x10x10 blockMesh unit cube, then this writer's
dictionary): ``createZones`` wrote ``constant/polyMesh/cellZones`` holding
``boxZone`` 125, ``ballZone`` 31, ``pipeZone`` 90 and ``stlzone`` 27 cells.
After ``splitMeshRegions -cellZonesOnly`` each region mesh held both split
zones (its own whole, the other empty); ``createZones -region fluid`` read
``system/fluid/createZonesDict`` and left exactly ``boxZone`` there, so the
``remove`` below is needed and works without a ``zoneType``.
"""

from foammesh.support.openfoam.dictionary.dictionary_file import DictionaryFile

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType, Shape


#: The generator that drops the whole-region zones a region split leaves.
REMOVE_REGION_ZONES = 'removeRegionZones'


def pointToEntry(point):
    x, y, z = point
    return f'({x} {y} {z})'


def surfaceFileName(name):
    """The file a tri-surface cell zone is read from, beside the mesh."""
    return f'{name}.stl'


class CreateZonesDict(DictionaryFile):
    def __init__(self, file_system=None, db=None):
        self._db = db or app.db
        file_system = file_system or app.fileSystem
        super().__init__(file_system.caseRoot(), self.systemLocation(), 'createZonesDict')

    def setRegion(self, rname):
        """Write under ``system/<region>``, where ``createZones -region`` reads.

        ``createZones`` reads the dictionary through ``systemDict`` with the
        mesh as its registry, and a region mesh's ``dbDir`` is its name, so
        the file for ``-region solid`` is ``system/solid/createZonesDict``.
        """
        self._header['location'] = self.systemLocation(rname).as_posix()

        return self

    def triSurfaceVolumes(self):
        """The names of the tri-surface volumes typed cell zone."""
        return [geometry.value('name') for geometry in self._cellZoneVolumes()
                if geometry.value('shape') == Shape.TRI_SURFACE_MESH.value]

    def _cellZoneVolumes(self):
        return self._db.getElements(
            'geometry',
            lambda i, e: e['cfdType'] == CFDType.CELL_ZONE.value).values()

    def build(self, surfaces=()):
        """One generator per cell-zone volume.

        ``surfaces`` names the tri-surface volumes whose ``<name>.stl`` the
        caller has put where ``insideSurface`` reads it. A tri-surface volume
        without one is not regenerated here: that zone is the mesher's -- the
        snappy dictionary makes it from the same surface (``case_builder``,
        DP-387) and Gmsh from the body -- and a generator pointing at a file
        that is not there only stops ``createZones``.
        """
        if self._data is not None:
            return self

        generators = {}
        regions = self._db.getElements('region')
        if len(regions) > 1:
            # ``splitMeshRegions -cellZonesOnly`` hands every region mesh the
            # zones it was split by (DP-538 writes one per region), so each
            # region would carry a zone that is the whole of itself. Removed
            # first, so a volume the user named like a region is kept.
            generators[REMOVE_REGION_ZONES] = {
                'type': 'remove',
                'cellZones': [region.value('name') for region in regions.values()],
            }

        for geometry in self._cellZoneVolumes():
            generator = self._generator(geometry, surfaces)
            if generator is not None:
                generators[geometry.value('name')] = generator

        # A multi-region export with no cell-zone volume still removes the
        # region zones; a single-region one with none has nothing to run.
        if generators:
            self._data = generators

        return self

    def generatesZones(self):
        """Whether any generator makes a zone, rather than only removing.

        DP-563 (0924 rerun). ``createZones`` writes only the zone lists that
        are not empty (``createZones.C``: "Write the zone lists that are not
        empty"), so a dictionary that removes every zone a region holds
        changes nothing on disk. MEASURED on S6: ``createZones -region
        cube_b_fluid`` read this dictionary, printed ``End``, and both region
        zones were still in the exported ``cellZones``.
        """
        return any(name != REMOVE_REGION_ZONES for name in (self._data or {}))

    def _generator(self, geometry, surfaces):
        shape = geometry.value('shape')
        name = geometry.value('name')

        if shape == Shape.TRI_SURFACE_MESH.value:
            if name not in surfaces:
                return None
            return self._cellGenerator('insideSurface', {
                'surface': 'closedTriSurface',
                'file': f'"{surfaceFileName(name)}"',
            })

        point1 = geometry.vector('point1')
        point2 = geometry.vector('point2')
        if shape == Shape.HEX.value or shape == Shape.HEX6.value:
            return self._cellGenerator('box', {
                'box': (pointToEntry(point1), pointToEntry(point2)),
            })
        if shape == Shape.CYLINDER.value:
            return self._cellGenerator('cylinder', {
                'point1': pointToEntry(point1),
                'point2': pointToEntry(point2),
                'radius': geometry.value('radius'),
            })
        if shape == Shape.SPHERE.value:
            return self._cellGenerator('sphere', {
                'centre': pointToEntry(point1),
                'radius': geometry.value('radius'),
            })

        # The open surfaces (plane, disk, plate) enclose no cells.
        return None

    def _cellGenerator(self, type_, entries):
        return {'type': type_, 'zoneType': 'cell', **entries}
