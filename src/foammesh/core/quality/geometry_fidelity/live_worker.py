"""DP-2741. The live deviation measurement, in a worker process.

The VTK wheels hold the GIL inside their C++ calls (measured: one 40.7 s
``vtkImplicitPolyDataDistance.FunctionValue`` stopped the main thread for
40.7 s), so a Python thread does not take the measurement off the GUI; a
process does. The window hands over plain arrays:

* the reference: its points and its polygons (offsets and connectivity);
* each surface: the centres of its faces, in its own face order.

The worker prepares the reference (merge, triangulate, orient), measures each
centre and hands back the distances in millimetres, whether the reference is
closed, the notice, and the prepared reference -- the window keeps it to
re-measure one surface when a Surfaces tick changes.

Everything here is numpy and VTK; no Qt.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import live_distance

#: The mesh worker's name for :func:`run`.
OPERATION = 'quality.live_deviation'


# -- arrays <-> vtkPolyData -------------------------------------------------- #

def polys_arrays(polydata) -> dict | None:
    """``points``, ``offsets`` and ``connectivity`` of ``polydata``'s polygons.

    ``None`` when it has strips, lines or vertices to lose: the window then
    measures in process, as before.
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy

    if polydata is None or polydata.GetNumberOfPoints() == 0:
        return None
    if (polydata.GetNumberOfStrips() or polydata.GetNumberOfLines()
            or polydata.GetNumberOfVerts()):
        return None
    polys = polydata.GetPolys()
    return {
        'points': np.ascontiguousarray(
            vtk_to_numpy(polydata.GetPoints().GetData()), dtype=np.float64),
        'offsets': np.asarray(vtk_to_numpy(polys.GetOffsetsArray()),
                              dtype=np.int64),
        'connectivity': np.asarray(
            vtk_to_numpy(polys.GetConnectivityArray()), dtype=np.int64),
    }


def polydata_from(points, offsets, connectivity, normals=None):
    """A vtkPolyData of polygons from :func:`polys_arrays`' arrays."""
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(np.ascontiguousarray(
        np.asarray(points, dtype=np.float64).reshape(-1, 3)), deep=True))
    cells = vtkCellArray()
    cells.SetData(
        numpy_to_vtk(np.ascontiguousarray(offsets, dtype=np.int64),
                     deep=True),
        numpy_to_vtk(np.ascontiguousarray(connectivity, dtype=np.int64),
                     deep=True))
    polydata = vtkPolyData()
    polydata.SetPoints(pts)
    polydata.SetPolys(cells)
    if normals is not None and len(normals):
        array = numpy_to_vtk(np.ascontiguousarray(
            np.asarray(normals, dtype=np.float64).reshape(-1, 3)), deep=True)
        array.SetName('Normals')
        polydata.GetCellData().SetNormals(array)
    return polydata


def face_centres(surface) -> np.ndarray:
    """The centre of each face of ``surface``, as ``signed_face_distances``
    measures it; ``(0, 3)`` when it has none, ``None`` when VTK gives a
    different count (those faces are then not measured)."""
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkFiltersCore import vtkCellCenters

    count = surface.GetNumberOfCells() if surface is not None else 0
    if count == 0:
        return np.zeros((0, 3), dtype=np.float64)
    centres = vtkCellCenters()
    centres.SetInputData(surface)
    centres.VertexCellsOff()
    centres.Update()
    points = centres.GetOutput().GetPoints()
    if points is None or points.GetNumberOfPoints() != count:
        return None
    return np.ascontiguousarray(vtk_to_numpy(points.GetData()),
                                dtype=np.float64)


# -- the window's side --------------------------------------------------------- #

def write_inputs(path, reference: dict, centres: dict) -> list:
    """Write the hand-over to ``path`` (an ``.npz``); returns the names in
    the order the worker answers them."""
    names = list(centres)
    arrays = {'ref_' + key: value for key, value in reference.items()}
    for index, name in enumerate(names):
        arrays[f'c{index}'] = centres[name]
    np.savez(path, **arrays)
    return names


async def measure(reference: dict, centres: dict, unit, *, group: str = ''):
    """Measure in a worker process: ``(names, WorkerOutcome)``.

    The hand-over goes through a folder of its own, removed afterwards; the
    file writes run on a thread so the caller's event loop keeps turning.
    """
    import asyncio
    import shutil
    import tempfile

    from foammesh.core.jobs import local_worker

    work = Path(await asyncio.to_thread(
        tempfile.mkdtemp, prefix='foammesh-deviation-'))
    try:
        inputs = work / 'inputs.npz'
        names = await asyncio.to_thread(write_inputs, inputs, reference,
                                        centres)
        outcome = await local_worker.run_worker(
            OPERATION, {'inputs_path': str(inputs), 'unit': unit},
            group=group)
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    return names, outcome


def read_answer(payload: dict, names: list, unit):
    """``(name -> mm, PreparedReference)`` from the worker's payload."""
    values = payload.get('values') or {}
    distances = {name: np.asarray(values[f'd{index}'], dtype=np.float64)
                 for index, name in enumerate(names) if f'd{index}' in values}
    closed = bool(payload.get('closed'))
    notice = str(payload.get('notice') or '')
    prepared = live_distance.PreparedReference(
        polydata_from(values.get('p_points', ()), values.get('p_offsets', (0,)),
                      values.get('p_connectivity', ()),
                      values.get('p_normals')),
        closed, notice)
    return distances, prepared


# -- the worker's side ------------------------------------------------------------ #

def distances_at(reference, centres: np.ndarray) -> np.ndarray:
    """The signed distance (model units) of each of ``centres`` to
    ``reference``, as ``signed_face_distances`` measures a face centre."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkDoubleArray
    from vtkmodules.vtkFiltersCore import (
        vtkImplicitPolyDataDistance, vtkTriangleFilter)

    if centres is None:
        return None
    count = len(centres)
    if count == 0:
        return np.zeros(0, dtype=np.float64)
    if reference is None or reference.GetNumberOfCells() == 0:
        return np.full(count, np.nan, dtype=np.float64)
    triangles = vtkTriangleFilter()
    triangles.SetInputData(reference)
    triangles.Update()
    implicit = vtkImplicitPolyDataDistance()
    implicit.SetInput(triangles.GetOutput())
    inputs = numpy_to_vtk(np.ascontiguousarray(centres, dtype=np.float64),
                          deep=True)
    output = vtkDoubleArray()
    implicit.FunctionValue(inputs, output)
    return vtk_to_numpy(output).astype(np.float64, copy=True)


def run(args: dict) -> dict:
    """The worker's body: ``args['inputs_path']`` in, distances out (mm)."""
    unit = args.get('unit', live_distance.DEFAULT_UNIT)
    with np.load(Path(args['inputs_path'])) as archive:
        reference = polydata_from(archive['ref_points'],
                                  archive['ref_offsets'],
                                  archive['ref_connectivity'])
        centres = {int(name[1:]): np.array(archive[name])
                   for name in archive.files if name.startswith('c')}
    prepared = live_distance.prepare_reference(reference)
    factor = live_distance.unit_to_mm(unit)
    values = {}
    for index, points in sorted(centres.items()):
        measured = distances_at(prepared.polydata, points)
        if measured is None:
            continue
        measured = measured * factor
        values[f'd{index}'] = (measured if prepared.closed
                               else np.abs(measured))
    arrays = polys_arrays(prepared.polydata) or {}
    for key, value in arrays.items():
        values['p_' + key] = value
    normals = (prepared.polydata.GetCellData().GetNormals()
               if prepared.polydata is not None else None)
    if normals is not None:
        from vtkmodules.util.numpy_support import vtk_to_numpy

        values['p_normals'] = vtk_to_numpy(normals)
    return {'closed': prepared.closed, 'notice': prepared.notice,
            'values': values}
