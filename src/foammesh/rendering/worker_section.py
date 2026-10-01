#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Draw the exact section the mesh worker computed (Plan 37 UF10).

The worker publishes ``section.vtp``: the polygons the planes meet, with cell
data ``sourceCellId`` (the original cell each polygon came from) and any
source arrays. The section tool reads it off the GUI thread and hands the
result to one :class:`WorkerSectionActor`.

MEASURED (``vtkXMLPolyDataReader``, appended raw, this machine): 100 000
quads 6 MB in 12 ms, 500 000 in 30 ms, 2 000 000 (the worker's default
polygon cap) 128 MB in 98-119 ms on the calling thread. Read through
``asyncio.to_thread`` the event loop's longest stall during the 2 M read was
16 ms, so the read is always taken off the GUI thread.
"""
from __future__ import annotations

from pathlib import Path


def readSectionSurface(path):
    """The ``vtkPolyData`` in *path* (``section.vtp``); thread-safe."""
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader

    reader = vtkXMLPolyDataReader()
    reader.SetFileName(str(Path(path)))
    reader.Update()
    if reader.GetErrorCode():
        raise OSError(f'could not read {path}')
    return reader.GetOutput()


def fieldString(polyData, name):
    """A string the worker stamped in the field data (``meshRevision``)."""
    array = polyData.GetFieldData().GetAbstractArray(name) \
        if polyData is not None else None
    if array is None or array.GetNumberOfValues() < 1:
        return None
    return array.GetValue(0)


class WorkerSectionActor:
    """The worker's section on screen.

    Like a cap: no pick, no part in a camera fit, drawn forward of the cut
    face it lies on. Unlike a cap it *is* cells -- every polygon names its
    source cell -- so it can take cell colours (UF9).
    """

    def __init__(self, colour='#b8c2cf'):
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
        from foammesh.view.theming.vtk_theme import rgb

        self._data = vtkPolyData()
        self._completion = None
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(self._data)
        mapper.ScalarVisibilityOff()
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('workerSection')
        actor.GetProperty().SetColor(*rgb(colour))
        actor.GetProperty().EdgeVisibilityOff()
        actor.PickableOff()
        actor.UseBoundsOff()
        actor.VisibilityOff()
        self._actor = actor
        self._mapper = mapper

    def actor(self):
        return self._actor

    def mapper(self):
        return self._mapper

    def polyData(self):
        return self._data

    def completion(self):
        """The `SectionCompletion` shown, or ``None``."""
        return self._completion

    def isShown(self) -> bool:
        return self._completion is not None and bool(
            self._actor.GetVisibility())

    def show(self, polyData, completion) -> None:
        """Show *polyData* (read off-thread) for *completion*."""
        self._completion = completion
        # A shallow copy: the arrays were built on the reader's thread and
        # nothing else holds them.
        self._data.ShallowCopy(polyData)
        self._data.Modified()
        self._actor.SetVisibility(self._data.GetNumberOfCells() > 0)

    def setVisible(self, visible: bool) -> None:
        self._actor.SetVisibility(bool(visible) and self._completion is not None
                                  and self._data.GetNumberOfCells() > 0)

    def clear(self):
        """Take the section down; returns the completion it showed."""
        completion, self._completion = self._completion, None
        self._data.Initialize()
        self._data.Modified()
        self._actor.VisibilityOff()
        return completion
