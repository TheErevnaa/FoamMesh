#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The *Cut cells* section mode as a VTK pipeline stage (Plan 37 UF7).

Whole cells the active plane meets, on both sides of it, masked by the other
enabled planes -- the rule is `foammesh.core.section.cut_cells`. It sits in
an actor's cut chain like any clip filter (``SetInputConnection`` /
``GetOutputPort``), so a mesh actor keeps its unstructured grid and a patch
or a geometry surface keeps its polydata.
"""
from __future__ import annotations

import numpy as np
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
from vtkmodules.util.vtkAlgorithm import VTKPythonAlgorithmBase
from vtkmodules.vtkCommonCore import vtkIdList
from vtkmodules.vtkCommonDataModel import (
    vtkDataObject, vtkDataSet, vtkPolyData, vtkUnstructuredGrid)
from vtkmodules.vtkFiltersCore import vtkThreshold
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

from foammesh.core.section.cut_cells import cut_cells_mask

__all__ = ['CutCellsFilter', 'cellLayout']

#: The cell array the cut-cells mask travels in (removed from the output).
_MASK = 'foammesh:cutCells'


def _arrays(cellArray):
    if cellArray is None or not cellArray.GetNumberOfCells():
        return np.zeros(1, dtype=np.int64), np.zeros(0, dtype=np.int64)
    return (vtk_to_numpy(cellArray.GetOffsetsArray()).astype(np.int64),
            vtk_to_numpy(cellArray.GetConnectivityArray()).astype(np.int64))


def cellLayout(dataSet):
    """``(offsets, connectivity)`` of every cell of *dataSet*, in cell order."""
    if isinstance(dataSet, vtkUnstructuredGrid):
        return _arrays(dataSet.GetCells())
    if isinstance(dataSet, vtkPolyData):
        # Poly data numbers its cells verts, lines, polys, strips.
        offsets, parts, base = [np.zeros(1, dtype=np.int64)], [], 0
        for cells in (dataSet.GetVerts(), dataSet.GetLines(),
                      dataSet.GetPolys(), dataSet.GetStrips()):
            o, c = _arrays(cells)
            offsets.append(o[1:] + base)
            parts.append(c)
            base += len(c)
        return (np.concatenate(offsets),
                np.concatenate(parts) if parts else np.zeros(0, np.int64))
    # Any other data set: ask it cell by cell.
    offsets, parts, ids = [0], [], vtkIdList()
    for cell in range(dataSet.GetNumberOfCells()):
        dataSet.GetCellPoints(cell, ids)
        parts.extend(ids.GetId(i) for i in range(ids.GetNumberOfIds()))
        offsets.append(len(parts))
    return np.array(offsets, np.int64), np.array(parts, np.int64)


class CutCellsFilter(VTKPythonAlgorithmBase):
    """Keep the cells the first plane meets, masked by the others.

    *planes* are ``vtkPlane`` objects; their normals point into the half each
    keeps. *polyData* makes the output poly data (for a surface actor).
    """

    def __init__(self, planes=(), polyData=False):
        VTKPythonAlgorithmBase.__init__(
            self, nInputPorts=1, nOutputPorts=1,
            outputType='vtkPolyData' if polyData else 'vtkUnstructuredGrid')
        self._polyData = polyData
        self._planes = [(tuple(plane.GetOrigin()), tuple(plane.GetNormal()))
                        for plane in planes]

    def FillInputPortInformation(self, port, info):
        info.Set(self.INPUT_REQUIRED_DATA_TYPE(), 'vtkDataSet')
        return 1

    def planes(self):
        return list(self._planes)

    def RequestData(self, request, inInfo, outInfo):
        source = vtkDataSet.GetData(inInfo[0])
        output = (vtkPolyData if self._polyData
                  else vtkUnstructuredGrid).GetData(outInfo)
        if source is None or not source.GetNumberOfCells() or not self._planes:
            output.Initialize()
            return 1
        points = vtk_to_numpy(source.GetPoints().GetData()).astype(float)
        offsets, connectivity = cellLayout(source)
        keep = cut_cells_mask(points, offsets, connectivity, self._planes)

        # The mask rides on a shallow copy as a cell array and a threshold
        # takes the cells it marks (this VTK's vtkExtractCells wants a
        # vtkIdList, filled one id at a time from Python).
        marked = source.NewInstance()
        marked.ShallowCopy(source)
        flags = numpy_to_vtk(keep.astype(np.uint8), deep=True)
        flags.SetName(_MASK)
        marked.GetCellData().AddArray(flags)

        threshold = vtkThreshold()
        threshold.SetInputData(marked)
        threshold.SetInputArrayToProcess(
            0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, _MASK)
        threshold.SetLowerThreshold(0.5)
        threshold.SetUpperThreshold(1.5)
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
        last = threshold
        if self._polyData:
            surface = vtkGeometryFilter()
            surface.SetInputConnection(threshold.GetOutputPort())
            last = surface
        last.Update()
        output.ShallowCopy(last.GetOutput())
        output.GetCellData().RemoveArray(_MASK)
        return 1
