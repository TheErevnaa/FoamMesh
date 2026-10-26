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

from foammesh.rendering import quality_scale
from foammesh.support import colour_standard
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
    elif quality_scale.is_quality_array(mapped.get('array')):
        # Plan 39 VI9: the table the Quality inspector paints the volume with.
        low, high = mapped['range']
        return quality_scale.lookup_table(mapped['array'], low, high)
    else:
        low, high = mapped['range']
        return colour_standard.lookup_table('section', low, high)
    table.Build()
    return table


class MapperColouring:
    """Colours mappers by a mapping and gives back what they had."""

    def __init__(self):
        self._saved = {}

    def colour(self, mapper, mapped) -> None:
        # Plan 39 VI9: a metric the Quality inspector is colouring takes the
        # inspector's band, so the cut and the volume agree. Updated in place:
        # the caller words its legend from the same mapping.
        mapped.update(quality_scale.share(mapped))
        key = id(mapper)
        if key not in self._saved:
            self._saved[key] = (mapper, mapper.GetScalarVisibility(),
                                mapper.GetLookupTable())
        mapper.SetLookupTable(lookupTable(mapped))
        mapper.SetScalarModeToUseCellFieldData()
        mapper.SelectColorArray(mapped['array'])
        # The table carries the range (it may be narrower than the data:
        # Around the limit), so a change of the standard moves both.
        mapper.UseLookupTableScalarRangeOn()
        if mapped['kind'] != 'categorical':
            mapper.SetScalarRange(*mapper.GetLookupTable().GetTableRange())
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


# -- Plan 48 GM12: colouring a section of a Gmsh mesh (plan 4.10.1) ------------- #

#: Gmsh's own categorical colourings: (key, label, array on the dataset).
#: The quality measures are `section_colour`'s ``gmsh.<name>`` choices already.
GMSH_CATEGORIES = (
    ('gmsh.physical', 'Gmsh: physical group', 'gmshPhysicalGroup'),
    ('gmsh.entity', 'Gmsh: entity', 'gmshEntity'),
    ('gmsh.partition', 'Gmsh: partition', 'gmshPartitionId'),
    ('gmsh.element_type', 'Gmsh: element type', 'gmshElementTypeId'),
)
GMSH_CATEGORY_KEYS = tuple(item[0] for item in GMSH_CATEGORIES)


def gmshChoices():
    """`section_colour.ColourChoice`s for the four categorical Gmsh colourings."""
    from foammesh.core.section.section_colour import ColourChoice

    return tuple(ColourChoice(key, label, array, categorical=True, gmsh=True,
                              worker=None)
                 for key, label, array in GMSH_CATEGORIES)


def isGmshCategory(key) -> bool:
    return key in GMSH_CATEGORY_KEYS


def gmshMapping(key, values, names=()) -> dict:
    """The legend mapping of a Gmsh categorical colouring over *values*.

    The same dict `section_colour.mapping` gives a categorical choice
    (``kind``, ``array``, ``label``, ``categories``), so `lookupTable` and
    `MapperColouring.colour` take it unchanged.  *values* are the codes on the
    cells on screen; *names* the label of each code (-1 is "none").
    """
    label = {k: lab for k, lab, _a in GMSH_CATEGORIES}.get(key)
    array = {k: arr for k, _l, arr in GMSH_CATEGORIES}.get(key)
    if label is None:
        return {'kind': 'none'}
    codes = (sorted(int(code) for code in np.unique(values))
             if values is not None and len(values) else [])
    categories = []
    for code in codes:
        if code < 0:
            name = 'None'
        elif 0 <= code < len(names):
            name = str(names[code])
        else:
            name = str(code)
        categories.append((code, name))
    return {'kind': 'categorical', 'array': array, 'label': label,
            'categories': categories}
