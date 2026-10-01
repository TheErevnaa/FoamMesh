#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""What a section is coloured by, shared by the local cut and the worker's.

Plan 37 UF9. A cut face is a piece of a cell, and every piece carries the
original cell's id, ``sourceCellId`` (int64, the polyMesh cell index). The
local cut stamps it on the volume before cutting (VTK passes cell data through
every clip, slice and cut-cells filter, and through triangulation); the
section worker writes it with every polygon. A per-cell value is read by that
id, so it survives every mode and every triangulation.

The contract (for the section tool and for anything drawing a section later):

* a colour is a `ColourChoice` -- its ``key``, the cell-data ``array`` it
  reads (the same name locally and in the worker's ``section.vtp``), whether
  it is ``categorical``, and the ``worker`` array name to ask the section
  worker for;
* `joinCellValues` reads a whole-mesh per-cell field onto the pieces by
  ``sourceCellId`` and refuses a field of another mesh revision or length
  (`StaleField`) -- a padded or shifted field would paint the wrong cells;
* a missing array is `MissingField`, never zeros;
* caps are filled from surfaces and have no cells: they are never coloured
  (`CAP_COLOURABLE`);
* `ColourRange` freezes a scalar range during a gesture;
* `mapping` says whether the legend is categorical (codes and names) or a
  scalar range.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: The id of the original cell on every piece of a section.
SOURCE_CELL_ID = 'sourceCellId'
#: The mesh revision a section was cut from (field data), when known.
MESH_REVISION = 'meshRevision'
#: Caps are filled from surfaces, not cut from cells: nothing per-cell to show.
CAP_COLOURABLE = False

#: Cell-type codes, the same locally (from VTK cell types) and in the worker
#: (from the cell's faces).
HEXAHEDRON, PRISM, TETRAHEDRON, PYRAMID, POLYHEDRON = range(5)
CELL_TYPE_NAMES = ('Hexahedron', 'Prism', 'Tetrahedron', 'Pyramid',
                   'Polyhedron')
#: VTK cell type -> code (anything else is a polyhedron).
VTK_CELL_TYPES = {12: HEXAHEDRON, 13: PRISM, 10: TETRAHEDRON, 14: PYRAMID}


@dataclass(frozen=True)
class ColourChoice:
    key: str
    label: str
    #: The cell-data array read, locally and in the worker's section.vtp.
    array: str | None
    categorical: bool = False
    #: The array name the section worker is asked for (``None``: none).
    worker: str | None = None
    #: Only the section worker has it (not the local volume).
    workerOnly: bool = False


NONE = 'none'
CELL_TYPE = 'cell_type'
CELL_LEVEL = 'cell_level'
CELL_ZONE = 'cell_zone'
QUALITY_PREFIX = 'quality.'

#: The quality fields of `core.quality.cell_fields`, with their labels.
QUALITY_FIELDS = (('cellAspectRatio', 'Aspect ratio'),
                  ('nonOrthoAngle', 'Non-orthogonality'),
                  ('skewness', 'Skewness'),
                  ('cellVolume', 'Cell volume'))

CHOICES = (
    ColourChoice(NONE, 'Solid', None),
    *(ColourChoice(QUALITY_PREFIX + name, f'Quality: {label}', name,
                   worker=QUALITY_PREFIX + name)
      for name, label in QUALITY_FIELDS),
    ColourChoice(CELL_TYPE, 'Cell type', 'cellType', categorical=True,
                 worker=CELL_TYPE),
    ColourChoice(CELL_LEVEL, 'Refinement level', 'cellLevel',
                 categorical=True, worker=CELL_LEVEL, workerOnly=True),
    ColourChoice(CELL_ZONE, 'Region / zone', 'cellZone', categorical=True,
                 worker=CELL_ZONE, workerOnly=True),
)
BY_KEY = {choice.key: choice for choice in CHOICES}


def choice(key) -> ColourChoice:
    return BY_KEY.get(key, BY_KEY[NONE])


class MissingField(LookupError):
    """The array is not there; the section is left uncoloured, with this."""


class StaleField(ValueError):
    """The field is of another mesh (revision or cell count)."""


# -- sources ------------------------------------------------------------------ #

#: Where the section on screen comes from (`availability`).
SOURCE_NONE = 'none'
SOURCE_BOUNDARY = 'boundary'
SOURCE_VOLUME = 'volume'
SOURCE_WORKER = 'worker'

REASON_NO_SECTION = 'There is no section to colour.'
REASON_BOUNDARY = ('The section is a boundary-only preview: there are no '
                   'cells in it to colour. Load cells for the cut first.')
REASON_WORKER_ONLY = ('Only the section worker reads this from the case; '
                      'use Load cells for the cut.')


def availability(key, source, *, unavailable=None):
    """``(allowed, reason)`` for colouring a section from *source* by *key*.

    *unavailable* is the worker's ``unavailable_arrays`` {worker name:
    reason} (a mesh without cellLevel, without cellZones).
    """
    item = choice(key)
    if item.key == NONE:
        return True, ''
    if source == SOURCE_NONE:
        return False, REASON_NO_SECTION
    if source == SOURCE_BOUNDARY:
        return False, REASON_BOUNDARY
    if source == SOURCE_VOLUME and item.workerOnly:
        return False, REASON_WORKER_ONLY
    reason = (unavailable or {}).get(item.worker)
    if reason:
        return False, reason[:1].upper() + reason[1:] + '.'
    return True, ''


# -- values ------------------------------------------------------------------- #

def joinCellValues(sourceCellIds, perCell, *, fieldRevision=None,
                   meshRevision=None):
    """``perCell[sourceCellIds]``, refused when it is not this mesh's field.

    *perCell* is one value per cell of the whole mesh; *fieldRevision* the
    revision it was computed from and *meshRevision* the section's. An id
    past its end, or two known revisions that differ, is `StaleField`.
    """
    if perCell is None:
        raise MissingField('the field was not computed')
    if fieldRevision and meshRevision and fieldRevision != meshRevision:
        raise StaleField(f'the field is of mesh revision {fieldRevision}, '
                         f'the section of {meshRevision}')
    ids = np.asarray(sourceCellIds, dtype=np.int64)
    values = np.asarray(perCell)
    if ids.size and (ids.min() < 0 or ids.max() >= values.shape[0]):
        raise StaleField(f'the field has {values.shape[0]} cells; the '
                         f'section names cell {int(ids.max())}')
    return values[ids]


def cellTypeCodes(vtkTypes) -> np.ndarray:
    """VTK cell types -> `CELL_TYPE_NAMES` codes."""
    types = np.asarray(vtkTypes, dtype=np.int64)
    codes = np.full(types.shape, POLYHEDRON, dtype=np.int32)
    for vtkType, code in VTK_CELL_TYPES.items():
        codes[types == vtkType] = code
    return codes


def cellTypesFromFaces(owner, neighbour, faceSizes, nCells) -> np.ndarray:
    """Cell-type codes from a polyMesh's faces (the worker's; no shapes).

    Hexahedron: six quads. Prism: two triangles and three quads.
    Tetrahedron: four triangles. Pyramid: four triangles and a quad. Any
    other cell -- a hex with a split face included -- is a polyhedron, as
    the OpenFOAM reader draws it.
    """
    owner = np.asarray(owner, dtype=np.int64)
    neighbour = np.asarray(neighbour, dtype=np.int64)
    sizes = np.asarray(faceSizes, dtype=np.int64)
    internal = len(neighbour)
    cells = np.concatenate([owner, neighbour])
    faceSize = np.concatenate([sizes, sizes[:internal]])
    count = np.bincount(cells, minlength=nCells)
    tris = np.bincount(cells, weights=faceSize == 3, minlength=nCells)
    quads = np.bincount(cells, weights=faceSize == 4, minlength=nCells)
    codes = np.full(nCells, POLYHEDRON, dtype=np.int32)
    codes[(count == 6) & (quads == 6)] = HEXAHEDRON
    codes[(count == 5) & (tris == 2) & (quads == 3)] = PRISM
    codes[(count == 4) & (tris == 4)] = TETRAHEDRON
    codes[(count == 5) & (tris == 4) & (quads == 1)] = PYRAMID
    return codes


# -- the legend ---------------------------------------------------------------- #

class ColourRange:
    """A scalar range that holds still while a plane is dragged."""

    def __init__(self):
        self._range = None
        self._frozen = False

    def freeze(self):
        self._frozen = True

    def thaw(self):
        self._frozen = False

    def isFrozen(self) -> bool:
        return self._frozen

    def reset(self):
        self._range = None
        self._frozen = False

    def rangeFor(self, values):
        """The range to map *values* with: theirs, unless frozen."""
        if self._frozen and self._range is not None:
            return self._range
        values = np.asarray(values, dtype=np.float64)
        values = values[np.isfinite(values)] if values.size else values
        if not values.size:
            return self._range or (0.0, 1.0)
        low, high = float(values.min()), float(values.max())
        if high <= low:
            high = low + (abs(low) * 1e-6 or 1.0)
        self._range = (low, high)
        return self._range


def mapping(key, values=None, *, colourRange=None, names=None):
    """What the legend shows for *key* over *values*.

    ``{'kind': 'none'}``, ``{'kind': 'categorical', 'array', 'label',
    'categories': [(code, name), ...]}`` or ``{'kind': 'scalar', 'array',
    'label', 'range': (low, high)}``. *names* name a categorical array's
    codes (the worker's ``cellZoneNames``).
    """
    item = choice(key)
    if item.key == NONE:
        return {'kind': 'none'}
    if item.categorical:
        codes = (sorted(int(code) for code in np.unique(values))
                 if values is not None and len(values) else [])
        if item.key == CELL_TYPE:
            label = CELL_TYPE_NAMES
        else:
            label = names
        categories = []
        for code in codes:
            if item.key == CELL_ZONE and code < 0:
                name = 'No zone'
            elif label is not None and 0 <= code < len(label):
                name = str(label[code])
            elif item.key == CELL_LEVEL:
                name = f'Level {code}'
            else:
                name = str(code)
            categories.append((code, name))
        return {'kind': 'categorical', 'array': item.array,
                'label': item.label, 'categories': categories}
    colourRange = colourRange or ColourRange()
    return {'kind': 'scalar', 'array': item.array, 'label': item.label,
            'range': colourRange.rangeFor(values if values is not None
                                          else [])}


def legendText(mapped) -> str:
    kind = mapped.get('kind')
    if kind == 'categorical':
        return '{}: {}'.format(mapped['label'], ', '.join(
            name for _code, name in mapped['categories']))
    if kind == 'scalar':
        low, high = mapped['range']
        return f'{mapped["label"]}: {low:.4g} – {high:.4g}'
    return ''
