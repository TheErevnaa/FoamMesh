"""Which cells of a zoneless mesh belong to which authored region.

DP-711 (viewport audit 0925 F1, case S5). snappyHexMesh in OpenFOAM 13 keeps
every region it is seeded into but names none of them, so a two-region case
(S5: a fluid and a solid, 10,608 cells in two disconnected pieces) reached the
viewport as one ``internalMesh`` with no zones -- and "will I be able to
select which region to visualize" had no answer: there was nothing named
fluid or solid to show or hide.

The export already knows how to recover the split (``region_zones``: the
connected piece around each region point). The viewport asks the same
question of the same files, so the picture and the exported case agree about
which cells are the fluid.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from foammesh.core.import_export.region_zones import (
    RegionZoneError, region_zones)
from foammesh.core.mesh.poly_mesh_boundary import (
    PolyMeshReadError, read_poly_mesh)

#: The scratch cell array the split thresholds on; never left on a part.
REGION_LABEL = '_foammeshRegion'


def seeded_region_cells(case_root, seeds,
                        expected_cells: int | None = None
                        ) -> dict[str, np.ndarray]:
    """``{region name: polyMesh cell ids}``, or ``{}`` when there is no split.

    Empty -- never an exception -- for fewer than two regions, a mesh the
    reader cannot parse, a mesh that already carries cell zones (those are
    drawn as themselves), or a mesh whose pieces do not match the region
    points one to one. The viewport then simply shows the mesh whole.

    ``expected_cells`` is how many cells the caller's own copy of the mesh
    holds. The ids returned are polyMesh cell labels, and they only name the
    same cells in another reader's grid when both hold the same count; a
    mismatch (a decomposed read, a mesh rewritten between the two reads)
    answers ``{}`` rather than splitting the wrong cells.
    """
    seeds = [(str(name), tuple(float(value) for value in point))
             for name, point in seeds if point is not None]
    if len(seeds) < 2:
        return {}
    poly_mesh = Path(case_root) / 'constant' / 'polyMesh'
    if not poly_mesh.is_dir():
        return {}
    try:
        mesh = read_poly_mesh(poly_mesh, layout_expectation='any')
    except (PolyMeshReadError, OSError, ValueError):
        return {}
    if not mesh.cell_count or mesh.cell_zones:
        return {}
    if expected_cells is not None and int(expected_cells) != mesh.cell_count:
        return {}
    try:
        return region_zones(mesh, seeds)
    except RegionZoneError:
        return {}


def region_grids(grid, regions: dict) -> dict:
    """``{region name: the cells of *grid* in that region}``, one grid each.

    *grid* is the reader's internal mesh, whose cell ``i`` is polyMesh cell
    ``i`` for a reconstructed case -- which :func:`seeded_region_cells`
    checked by count before handing the ids over. The split is a threshold on
    a label array carried by a shallow copy, so the reader's grid is left as
    it was and no per-cell Python loop runs.
    """
    from vtkmodules.vtkFiltersCore import vtkThreshold
    from vtkmodules.util.numpy_support import numpy_to_vtk

    count = grid.GetNumberOfCells()
    labels = np.full(count, -1, dtype=np.int32)
    for index, ids in enumerate(regions.values()):
        ids = np.asarray(ids, dtype=np.int64)
        labels[ids[(ids >= 0) & (ids < count)]] = index
    labelled = grid.NewInstance()
    labelled.ShallowCopy(grid)
    array = numpy_to_vtk(labels, deep=True)
    array.SetName(REGION_LABEL)
    labelled.GetCellData().AddArray(array)

    result = {}
    for index, name in enumerate(regions):
        threshold = vtkThreshold()
        threshold.SetInputData(labelled)
        threshold.SetInputArrayToProcess(
            0, 0, 0, 1, REGION_LABEL)          # 1 = cell data
        threshold.SetLowerThreshold(index)
        threshold.SetUpperThreshold(index)
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
        threshold.Update()
        part = threshold.GetOutput().NewInstance()
        part.ShallowCopy(threshold.GetOutput())
        part.GetCellData().RemoveArray(REGION_LABEL)
        if part.GetNumberOfCells():
            result[name] = part
    return result
