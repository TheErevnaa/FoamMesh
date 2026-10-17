#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The local clip, when the planes do not simply intersect (Plan 39 VI10b).

``ActorInfo.clip`` chained one clip per plane: the intersection of the kept
half-spaces, whole cells kept if any part was inside. The section tool now
also offers (`core.section.pieces`):

* *Remove the common part* -- one clip by an implicit function that is
  positive anywhere a plane's removed side is (``vtkImplicitBoolean``, the
  maximum of the negated planes), not a chain;
* *Keep each plane's piece* -- one clip per plane from the same source, the
  results appended;
* the whole-cell rule -- *Entirely inside* is ``ExtractBoundaryCells`` off;
  *Centroid inside* keeps the cells whose centre is on the kept side
  (:class:`CentroidExtract`).

:func:`set_options` is called by the section tool before it clips, and
:func:`stages` is what ``ActorInfo.clip`` asks. Nothing here changes while
the options are the defaults: ``stages`` answers ``None`` and the old chain
is built.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from vtkmodules.vtkCommonDataModel import vtkImplicitBoolean, vtkPlane
from vtkmodules.vtkFiltersCore import vtkAppendFilter, vtkAppendPolyData
from vtkmodules.vtkFiltersProgrammable import vtkProgrammableFilter

from foammesh.core.section.pieces import CellRule, Combine, region_value

__all__ = ['Options', 'set_options', 'options', 'stages', 'Stages',
           'CentroidExtract']

_POLY_FILTERS = ('vtkClipPolyData', 'vtkExtractPolyDataGeometry')


@dataclass(frozen=True)
class Options:
    combine: Combine = Combine.COMMON
    rule: CellRule = CellRule.ANY

    @property
    def isDefault(self) -> bool:
        return self.combine is Combine.COMMON and self.rule is CellRule.ANY


_current = Options()


def set_options(combine=Combine.COMMON, rule=CellRule.ANY) -> None:
    """What the next clip combines by (the section tool sets it per apply)."""
    global _current
    _current = Options(Combine(combine), CellRule(rule))


def options() -> Options:
    return _current


@dataclass
class Stages:
    """The filters one clip adds, in order, and where the result comes out."""
    filters: list
    output: object


def _negated(plane: vtkPlane) -> vtkPlane:
    other = vtkPlane()
    other.SetOrigin(*plane.GetOrigin())
    other.SetNormal(*(-value for value in plane.GetNormal()))
    return other


def removedSide(planes):
    """An implicit function that is positive where any plane's removed side
    is. ``vtkImplicitBoolean`` intersection takes the maximum of its
    functions' values; the planes are negated so a point on a removed side
    has a positive value, which is what the clip filters keep."""
    boolean = vtkImplicitBoolean()
    boolean.SetOperationTypeToIntersection()
    for plane in planes:
        boolean.AddFunction(_negated(plane))
    return boolean


def _entire(cut):
    """*Entirely inside*: a cell is kept only if all of it is."""
    if hasattr(cut, 'SetExtractBoundaryCells'):
        cut.SetExtractBoundaryCells(False)
    return cut


class CentroidExtract:
    """Keep the cells whose centre is in the region (a VTK algorithm).

    The region is read from the plane origins and normals: positive where
    the planes keep it for *combine* (`pieces.region_value`). Wraps a
    ``vtkProgrammableFilter`` so it chains like any other filter.
    """

    def __init__(self, planes, combine=Combine.COMMON):
        self._planes = [(np.array(p.GetOrigin(), float),
                         np.array(p.GetNormal(), float)) for p in planes]
        self._combine = Combine(combine)
        self._filter = vtkProgrammableFilter()
        self._filter.SetExecuteMethod(self._execute)

    def SetInputConnection(self, port):
        self._filter.SetInputConnection(port)

    def RemoveAllInputConnections(self, index):
        self._filter.RemoveAllInputConnections(index)

    def GetOutputPort(self):
        return self._filter.GetOutputPort()

    def GetOutput(self):
        return self._filter.GetOutput()

    def Update(self):
        self._filter.Update()

    def __getattr__(self, name):
        return getattr(self._filter, name)

    def keptCells(self, data) -> np.ndarray:
        """Boolean per cell: is its centre in the region."""
        from vtkmodules.util.numpy_support import vtk_to_numpy
        from vtkmodules.vtkFiltersCore import vtkCellCenters

        centres = vtkCellCenters()
        centres.SetInputData(data)
        centres.Update()
        points = centres.GetOutput().GetPoints()
        if points is None or not points.GetNumberOfPoints():
            return np.zeros(0, dtype=bool)
        xyz = vtk_to_numpy(points.GetData())
        kept = np.array([(xyz - origin) @ normal
                         for origin, normal in self._planes])
        return region_value(kept, self._combine) > 0.0

    def _execute(self):
        from vtkmodules.util.numpy_support import numpy_to_vtk
        from vtkmodules.vtkFiltersCore import vtkThreshold

        data = self._filter.GetInput()
        out = self._filter.GetOutput()
        mask = self.keptCells(data)
        marked = data.NewInstance()
        marked.ShallowCopy(data)
        flags = numpy_to_vtk(mask.astype(np.uint8), deep=True)
        flags.SetName('foammeshCentroidKept')
        marked.GetCellData().AddArray(flags)
        marked.GetCellData().SetActiveScalars('foammeshCentroidKept')
        threshold = vtkThreshold()
        threshold.SetInputData(marked)
        threshold.SetInputArrayToProcess(
            0, 0, 0, 1, 'foammeshCentroidKept')
        threshold.SetLowerThreshold(0.5)
        threshold.SetUpperThreshold(1.5)
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
        threshold.Update()
        out.ShallowCopy(threshold.GetOutput())
        out.GetCellData().RemoveArray('foammeshCentroidKept')


def stages(actor, planes, inputFilter):
    """The filters for a clip by *planes* under the current options, or
    ``None`` when the chain of single clips is exactly right.

    *actor* builds the single clip for one implicit function
    (``actor._clipFilter``) and says whether it is a volume
    (``_surfaceFilter``) and whether the whole-cell clip is on
    (``_cutMode``).
    """
    chosen = options()
    if chosen.isDefault or not planes:
        return None
    from foammesh.rendering.actor_info import CutMode

    whole = actor._cutMode is CutMode.CRINKLE
    rule = chosen.rule if whole else CellRule.ANY

    def single(function):
        cut = actor._clipFilter(function)
        return _entire(cut) if rule is CellRule.ENTIRE else cut

    centroid = rule is CellRule.CENTROID and hasattr(actor, '_surfaceFilter')
    if chosen.combine is Combine.EACH and len(planes) > 1:
        branches = []
        for plane in planes:
            cut = (CentroidExtract([plane]) if centroid else single(plane))
            cut.SetInputConnection(inputFilter.GetOutputPort())
            branches.append(cut)
        poly = type(branches[0]).__name__ in _POLY_FILTERS
        merge = vtkAppendPolyData() if poly else vtkAppendFilter()
        for cut in branches:
            merge.AddInputConnection(cut.GetOutputPort())
        return Stages(branches + [merge], merge)
    if chosen.combine is Combine.REMOVE_COMMON and not whole:
        return _outside_pieces(actor, planes, inputFilter)
    if chosen.combine is Combine.REMOVE_COMMON:
        if centroid:
            cut = CentroidExtract(planes, Combine.REMOVE_COMMON)
        else:
            cut = single(removedSide(planes))
        cut.SetInputConnection(inputFilter.GetOutputPort())
        return Stages([cut], cut)
    # the common part, by a rule other than "any part"
    if centroid:
        cut = CentroidExtract(planes, Combine.COMMON)
        cut.SetInputConnection(inputFilter.GetOutputPort())
        return Stages([cut], cut)
    chain = []
    last = inputFilter
    for plane in planes:
        cut = single(plane)
        cut.SetInputConnection(last.GetOutputPort())
        chain.append(cut)
        last = cut
    return Stages(chain, last)


def _outside_pieces(actor, planes, inputFilter):
    """Everything outside the common part, as disjoint convex pieces.

    A clip interpolates its function along an edge, which is exact for a
    plane and not for the maximum of several (a crease inside a cell), so the
    smooth clip cuts ``H1^c``, ``H1 & H2^c``, ``H1 & H2 & H3^c`` ... each by
    a chain of plane clips and appends them.
    """
    branches, filters = [], []
    for index, plane in enumerate(planes):
        last = inputFilter
        for function in [*planes[:index], _negated(plane)]:
            cut = actor._clipFilter(function)
            cut.SetInputConnection(last.GetOutputPort())
            filters.append(cut)
            last = cut
        branches.append(last)
    if len(branches) == 1:
        return Stages(filters, branches[0])
    poly = type(filters[0]).__name__ in _POLY_FILTERS
    merge = vtkAppendPolyData() if poly else vtkAppendFilter()
    for last in branches:
        merge.AddInputConnection(last.GetOutputPort())
    return Stages(filters + [merge], merge)
