#!/usr/bin/env python
# -*- coding: utf-8 -*-

from foammesh.support.openfoam.dictionary.dictionary_file import DictionaryFile
from foammesh.core.mesh.extrusion_options import ExtrudeModel


class ExtrudeMeshDict(DictionaryFile):
    def __init__(self, fileSystem):
        super().__init__(fileSystem.caseRoot(), self.systemLocation(), 'extrudeMeshDict')

    def build(self, p1, p2, options):
        self._data = {
            'constructFrom': 'patch',
            'sourceCase': '"."',
            # 'sourceCase': str(app.fileSystem.caseRoot()),
            'sourcePatches': [p1],
            'exposedPatchName': p2,
            'extrudeModel': options.model.value,
            'flipNormals': 'false',
            'mergeFaces': 'false',
        }

        if options.model == ExtrudeModel.PLANE:
            self._data['thickness'] = options.thickness
        elif options.model == ExtrudeModel.WEDGE:
            # Foundation v13 contract (verified live): the wedge model reads
            # axisPt/axis/angle; the legacy sectorCoeffs/point form fails with
            # "keyword axisPt is undefined".
            self._data['axisPt'] = options.point
            self._data['axis'] = options.axis
            self._data['angle'] = options.angle

        return self
