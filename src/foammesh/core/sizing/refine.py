"""Refine to field: ``refineMesh`` on a cell set cut from the size field.

Plan 41 GN25c (section 12.2 S6, 4.8.3 "new action after castellation").
For gaps ``gapLevelIncrement`` cannot reach: the cells whose size exceeds
1.5 x the field there go into a ``cellSet`` and ``refineMesh`` splits them.
Off by default; the card offers it only after a mesh exists.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import snappy_mapping
from .mesh_cells import cell_geometry, hex_cells
from .conformity import cell_size

SET_NAME = 'foammeshRefine'
DICT_NAME = 'refineMeshDict'

_DICT = """FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      {name};
}}

// OpenFOAM 13: the cells come from a zone generated from the set, and
// hexRef8 splits each one into eight the way snappyHexMesh does, so
// the result stays refinable by it.
hexRef8         yes;
zone
{{
    type        set;
    zoneType    cell;
    setName     {set_name};
}}
"""

_SET = """FoamFile
{{
    version     2.0;
    format      ascii;
    class       cellSet;
    location    "constant/polyMesh/sets";
    object      {name};
}}

{count}
(
{labels}
)
"""


class RefineError(RuntimeError):
    pass


def cells_to_refine(field, case_path, factor=snappy_mapping.REFINE_FACTOR):
    """``(labels, cell count)`` of the cells bigger than ``factor`` x field."""
    from foammesh.core.mesh.poly_mesh_boundary import (
        PolyMeshReadError, read_poly_mesh)
    try:
        poly = read_poly_mesh(case_path)
    except (PolyMeshReadError, OSError) as error:
        raise RefineError(f'there is no mesh to refine: {error}') from error
    centres, volumes = cell_geometry(poly)
    sizes = cell_size(volumes)
    wanted = field.lookup(centres)
    mask = snappy_mapping.refine_to_field_cells(sizes, wanted)
    if factor != snappy_mapping.REFINE_FACTOR:
        with np.errstate(invalid='ignore'):
            mask = sizes > factor * wanted
    # hexRef8 splits hexahedra; the rest stay as they are.
    mask = mask & hex_cells(poly)
    return np.nonzero(mask)[0], int(len(volumes))


def write_files(case_path, labels) -> dict:
    """Write the cell set and the dictionary; returns their paths."""
    case = Path(case_path)
    sets = case / 'constant' / 'polyMesh' / 'sets'
    sets.mkdir(parents=True, exist_ok=True)
    cell_set = sets / SET_NAME
    cell_set.write_text(_SET.format(
        name=SET_NAME, count=len(labels),
        labels='\n'.join(str(int(i)) for i in labels)), 'utf-8')
    system = case / 'system'
    system.mkdir(parents=True, exist_ok=True)
    dictionary = system / DICT_NAME
    dictionary.write_text(_DICT.format(
        name=DICT_NAME, set_name=SET_NAME        ), 'utf-8')
    return {'cell_set': cell_set, 'dict': dictionary}


def argv(case_path) -> tuple:
    return ('-case', str(case_path), '-overwrite', '-dict',
            f'system/{DICT_NAME}')
