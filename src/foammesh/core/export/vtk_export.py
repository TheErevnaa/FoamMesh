#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""VTK/VTU export for visualization and diagnostics (headless)."""
from __future__ import annotations

from pathlib import Path


def load_case_dataset(case_path):
    """Read a case's internal mesh into a ``vtkUnstructuredGrid`` headlessly.

    Uses VTK's own OpenFOAM reader, so no solver installation or PyFoam is
    required.  Raises ``ValueError`` when no internal mesh can be read.
    """
    from vtkmodules.vtkCommonDataModel import vtkDataObjectTreeIterator, vtkUnstructuredGrid
    from vtkmodules.vtkIOGeometry import vtkOpenFOAMReader

    case = Path(case_path)
    marker = next(iter(sorted(case.glob('*.foam'))), None)
    transient = None
    if marker is None:
        transient = case / '.foammesh-export.foam'
        transient.write_text('', encoding='utf-8')
        marker = transient
    try:
        reader = vtkOpenFOAMReader()
        reader.SetFileName(str(marker))
        reader.SetCreateCellToPoint(False)
        reader.EnableAllPatchArrays()
        reader.Update()
        output = reader.GetOutput()
        iterator = vtkDataObjectTreeIterator()
        iterator.SetDataSet(output)
        iterator.VisitOnlyLeavesOn()
        iterator.InitTraversal()
        while not iterator.IsDoneWithTraversal():
            leaf = iterator.GetCurrentDataObject()
            if isinstance(leaf, vtkUnstructuredGrid) and leaf.GetNumberOfCells() > 0:
                return leaf
            iterator.GoToNextItem()
        raise ValueError('VTK could not read an internal mesh from this case')
    finally:
        if transient is not None:
            transient.unlink(missing_ok=True)


def load_case_blocks(case_path):
    """``(internal mesh, [(patch name, patch block)])`` for one case.

    Plan 31 DP-23. The reader has to be told twice to hand over the patches:
    ``EnableAllPatchArrays()`` leaves the individual arrays at status 0, so a
    caller that stops there gets the interior alone and every export built on
    it loses the boundaries. This is the one place that knows that, rather
    than each exporter learning it separately -- the SU2 export learned it
    first (Plan 28) and the Gmsh export needed exactly the same blocks.

    Raises ``ValueError`` when no internal mesh can be read.
    """
    from vtkmodules.vtkCommonDataModel import (
        vtkCompositeDataSet, vtkDataObjectTreeIterator, vtkPolyData,
        vtkUnstructuredGrid,
    )
    from vtkmodules.vtkIOGeometry import vtkOpenFOAMReader

    case = Path(case_path)
    marker = next(iter(sorted(case.glob('*.foam'))), None)
    transient = None
    if marker is None:
        transient = case / '.foammesh-export.foam'
        transient.write_text('', encoding='utf-8')
        marker = transient
    try:
        reader = vtkOpenFOAMReader()
        reader.SetFileName(str(marker))
        reader.SetCreateCellToPoint(False)
        reader.EnableAllPatchArrays()
        reader.UpdateInformation()
        for index in range(reader.GetNumberOfPatchArrays()):
            reader.SetPatchArrayStatus(reader.GetPatchArrayName(index), 1)
        reader.Update()
        iterator = vtkDataObjectTreeIterator()
        iterator.SetDataSet(reader.GetOutput())
        iterator.VisitOnlyLeavesOn()
        iterator.InitTraversal()

        interior = None
        patches = []
        while not iterator.IsDoneWithTraversal():
            leaf = iterator.GetCurrentDataObject()
            name = ''
            if iterator.HasCurrentMetaData():
                name = iterator.GetCurrentMetaData().Get(
                    vtkCompositeDataSet.NAME()) or ''
            # Plan 28: classify by block name, not by sampling cell types.
            # The reader calls the interior `internalMesh` and every other leaf
            # is a patch, which is a fact about the reader rather than a guess.
            # Sampling the first 64 cells got both halves of the decision wrong
            # on a snappy mesh: a block of polyhedra matched neither set, so it
            # was silently dropped.
            if isinstance(leaf, (vtkUnstructuredGrid, vtkPolyData)) and \
                    leaf.GetNumberOfCells():
                if str(name) == 'internalMesh' or (
                        not name and isinstance(leaf, vtkUnstructuredGrid)):
                    if interior is None:
                        interior = leaf
                else:
                    patches.append((str(name) or f'patch_{len(patches)}', leaf))
            iterator.GoToNextItem()
        if interior is None:
            raise ValueError('no internal mesh could be read from this case')
        return interior, patches
    finally:
        if transient is not None:
            transient.unlink(missing_ok=True)


def read_vtu_counts(path) -> tuple[int, int]:
    """Independent read-back used to validate a written .vtu file."""
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    return grid.GetNumberOfPoints(), grid.GetNumberOfCells()


def write_legacy_vtk(dataset, path) -> Path:
    """Write legacy ASCII .vtk — the interchange format Gmsh can open."""
    from vtkmodules.vtkIOLegacy import vtkUnstructuredGridWriter

    path = Path(path)
    writer = vtkUnstructuredGridWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(dataset)
    if hasattr(writer, 'SetFileVersion'):
        # VTK 9.1+ writes legacy 5.1 (OFFSETS/CONNECTIVITY cell blocks),
        # which Gmsh does not read: every conversion failed with "Error
        # loading". The 4.2 layout is what Gmsh, and everything else that
        # reads legacy VTK, expects.
        writer.SetFileVersion(42)
    if writer.Write() != 1:
        raise OSError(f'could not write legacy VTK file: {path}')
    return path


def write_vtu(dataset, path) -> Path:
    """Write a vtkUnstructuredGrid (or polydata via append) to a .vtu file."""
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridWriter
    from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid

    grid = dataset
    if not isinstance(dataset, vtkUnstructuredGrid):
        from vtkmodules.vtkFiltersCore import vtkAppendFilter
        app = vtkAppendFilter()
        app.SetInputData(dataset)
        app.Update()
        grid = app.GetOutput()

    path = Path(path)
    writer = vtkXMLUnstructuredGridWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(grid)
    writer.Write()
    return path
