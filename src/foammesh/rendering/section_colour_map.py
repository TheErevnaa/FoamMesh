#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Draw a section coloured by a per-cell value (Plan 37 UF9).

The mapping itself -- which array, categorical or scalar, the range -- is
`core.section.section_colour`'s; this module hands it to VTK mappers and
stamps the ids the mapping reads onto a local volume.
"""
from __future__ import annotations

import numpy as np
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
from vtkmodules.vtkCommonCore import vtkLookupTable

from foammesh.core.section.section_colour import (
    SOURCE_CELL_ID, cellTypeCodes)

#: Distinct colours for categories (repeat past the end).
CATEGORY_COLOURS = (
    (0.122, 0.467, 0.706), (1.000, 0.498, 0.055), (0.173, 0.627, 0.173),
    (0.839, 0.153, 0.157), (0.580, 0.404, 0.741), (0.549, 0.337, 0.294),
    (0.890, 0.467, 0.761), (0.498, 0.498, 0.498), (0.737, 0.741, 0.133),
    (0.090, 0.745, 0.812))


def cellArray(dataSet, name):
    """The cell-data array *name* of *dataSet* as numpy, or ``None``."""
    if dataSet is None or not name:
        return None
    array = dataSet.GetCellData().GetArray(name)
    if array is None:
        return None
    return vtk_to_numpy(array)


def stampCellIdentity(dataSet) -> bool:
    """Give a local volume ``sourceCellId`` and ``cellType``, once.

    Cell data rides through every clip, slice and cut-cells filter and
    through triangulation, so each piece of a cut keeps its cell's id and
    type. Returns whether anything was added (the dataset changed).
    """
    if dataSet is None or dataSet.GetNumberOfCells() <= 0:
        return False
    cellData = dataSet.GetCellData()
    changed = False
    count = dataSet.GetNumberOfCells()
    if cellData.GetArray(SOURCE_CELL_ID) is None:
        ids = numpy_to_vtk(np.arange(count, dtype=np.int64), deep=True)
        ids.SetName(SOURCE_CELL_ID)
        cellData.AddArray(ids)
        changed = True
    if cellData.GetArray('cellType') is None:
        getTypes = getattr(dataSet, 'GetCellTypesArray', None)
        types = getTypes() if getTypes is not None else None
        if types is not None:
            codes = cellTypeCodes(vtk_to_numpy(types))
        else:
            codes = cellTypeCodes([dataSet.GetCellType(index)
                                   for index in range(count)])
        array = numpy_to_vtk(codes, deep=True)
        array.SetName('cellType')
        cellData.AddArray(array)
        changed = True
    return changed


def lookupTable(mapped) -> vtkLookupTable:
    table = vtkLookupTable()
    if mapped['kind'] == 'categorical':
        categories = mapped['categories']
        table.IndexedLookupOn()
        table.SetNumberOfTableValues(max(1, len(categories)))
        for index, (code, name) in enumerate(categories):
            red, green, blue = CATEGORY_COLOURS[index % len(CATEGORY_COLOURS)]
            table.SetTableValue(index, red, green, blue, 1.0)
            table.SetAnnotation(int(code), name)
    else:
        table.SetHueRange(0.667, 0.0)
        low, high = mapped['range']
        table.SetRange(low, high)
    table.Build()
    return table


class MapperColouring:
    """Colours mappers by a mapping and gives back what they had."""

    def __init__(self):
        self._saved = {}

    def colour(self, mapper, mapped) -> None:
        key = id(mapper)
        if key not in self._saved:
            self._saved[key] = (mapper, mapper.GetScalarVisibility(),
                                mapper.GetLookupTable())
        mapper.SetLookupTable(lookupTable(mapped))
        mapper.SetScalarModeToUseCellFieldData()
        mapper.SelectColorArray(mapped['array'])
        if mapped['kind'] == 'categorical':
            mapper.UseLookupTableScalarRangeOn()
        else:
            mapper.UseLookupTableScalarRangeOff()
            mapper.SetScalarRange(*mapped['range'])
        mapper.ScalarVisibilityOn()

    def isColoured(self, mapper) -> bool:
        return id(mapper) in self._saved

    def restore(self, mapper=None) -> None:
        keys = [id(mapper)] if mapper is not None else list(self._saved)
        for key in keys:
            saved = self._saved.pop(key, None)
            if saved is None:
                continue
            target, visible, table = saved
            target.SetScalarVisibility(visible)
            if table is not None:
                target.SetLookupTable(table)
            target.SetScalarModeToDefault()
