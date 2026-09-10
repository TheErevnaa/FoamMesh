#!/usr/bin/env python
# -*- coding: utf-8 -*-

from foammesh.support.openfoam.dictionary.dictionary_file import DictionaryFile

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType


class CreatePatchDict(DictionaryFile):
    def __init__(self, prefix: str, fileSystem, db=None):
        super().__init__(fileSystem.caseRoot(), self.systemLocation(), 'createPatchDict')
        self._prefix = prefix
        self._db = db or app.db

    def build(self):
        if self._data is not None:
            return self

        self._data = {
            'pointSync': 'false',
            'patches': []
        }

        for interface in self._db.getElements(
                'geometry', lambda i, e: e['cfdType'] == CFDType.INTERFACE.value and not e['interRegion'] and not e['nonConformal']).values():
            name = interface.value('name')
            self._data['patches'].extend([
                {
                    'name': self._prefix + name,
                    'patchInfo': {
                        'type': 'cyclic',
                        'neighbourPatch': self._prefix + name + '_slave'
                    },
                    'constructFrom': 'patches',
                    'patches': [name]
                },
                {
                    'name': self._prefix + name + '_slave',
                    'patchInfo': {
                        'type': 'cyclic',
                        'neighbourPatch': self._prefix + name
                    },
                    'constructFrom': 'patches',
                    'patches': [name+'_slave']
                }
            ])

        return self
