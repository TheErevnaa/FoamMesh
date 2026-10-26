#!/usr/bin/env python
# -*- coding: utf-8 -*-

from foammesh.support.openfoam.constants import Directory
from foammesh.support.openfoam.dictionary.dictionary_file import DictionaryFile

from foammesh.app import app
from foammesh.db.configurations_schema import RegionType


class RegionProperties(DictionaryFile):
    def __init__(self, casePath, db=None):
        super().__init__(casePath, self.constantLocation(), Directory.REGION_PROPERTIES_FILE_NAME)
        self._db = db

    def build(self):
        # DP-6058. The regions the export splits by: on Gmsh the typed
        # solids, which hold no ``region`` row.
        from foammesh.core.import_export.export_regions import export_regions
        regions = export_regions(self._db if self._db is not None else app.db)

        if len(regions) > 1:
            fluids = []
            solids = []
            for region in regions.values():
                if region.value('type') == RegionType.SOLID.value:
                    solids.append(region.value('name'))
                else:
                    fluids.append(region.value('name'))

            self._data = {
                'regions': [
                    'fluid',
                    fluids,
                    'solid',
                    solids
                ]
            }

        return self
