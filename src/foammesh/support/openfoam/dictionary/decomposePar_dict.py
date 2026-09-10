#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The ``decomposeParDict`` dictionary file.

Plan 26 WP9.2 settled this class's fate: **revived, not deleted**. It had zero
production callers, declared nine methods, and hardcoded ``scotch`` -- so it
was the second of two writers disagreeing about the same dictionary, which is
exactly the shape the plan set out to remove. Rather than leave two, it
delegates to :mod:`foammesh.openfoam.decomposition`, which is also what gives
it the coefficients ``hierarchical`` and ``simple`` require: a dictionary
naming either of them without an ``n`` vector is one ``decomposePar`` rejects.
"""

from enum import Enum

from foammesh.support.openfoam.dictionary.dictionary_file import DictionaryFile


class MethodType(Enum):
    """Every method ``decomposeParDict`` accepts.

    Wider than what this product writes on purpose -- it is the file format's
    vocabulary, not the set the GUI offers. See
    :class:`~foammesh.db.configurations_schema.DecompositionMethod` for the
    three that are exposed and why the other six are not.
    """

    NONE         = 'none'
    MANUAL       = 'manual'
    SIMPLE       = 'simple'
    HIERARCHICAL = 'hierarchical'
    KAHIP        = 'kahip'
    METIS        = 'metis'
    SCOTCH       = 'scotch'
    STRUCTURED   = 'structured'
    MULTILEVEL   = 'multiLevel'


class DecomposeParDict(DictionaryFile):
    def __init__(self, casePath, rname: str = '', numCores: int = 1,
                 singleProcessorFaceSets: list[str] = [],
                 method: str = 'scotch', order: str = 'xyz', cells=None):
        super().__init__(casePath, self.systemLocation(rname), 'decomposeParDict')

        self._numCores = numCores
        self._rname = ''
        self._method = method
        self._order = order
        self._cells = cells

        self._singleProcessorFaceSets = singleProcessorFaceSets

    def build(self):
        if self._data is not None:
            return self

        from foammesh.openfoam.decomposition import build as build_document

        self._data = build_document(
            self._numCores, method=self._method, order=self._order,
            cells=self._cells,
            single_processor_face_sets=self._singleProcessorFaceSets)
        return self
