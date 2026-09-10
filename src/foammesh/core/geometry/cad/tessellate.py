#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Controlled CAD tessellation to a tri-surface for snappyHexMesh.

``TessellationParams`` (pure, tested) exposes the OCCT ``BRepMesh`` controls so
the user drives facet density. ``tessellate()`` runs OCCT lazily and emits a
vtkPolyData with a per-triangle face-id array (so patches map to CAD faces). The
CAD shape stays the source; the tri-surface is a regenerable artifact.
"""
from __future__ import annotations

from dataclasses import dataclass

from .availability import require


@dataclass
class TessellationParams:
    linear_deflection: float = 0.1      # chord tolerance, in model units
    angular_deflection_deg: float = 20.0
    relative: bool = False              # linear deflection relative to edge length
    parallel: bool = True

    def validate(self) -> None:
        if self.linear_deflection <= 0:
            raise ValueError('linear_deflection must be > 0')
        if not (0 < self.angular_deflection_deg < 180):
            raise ValueError('angular_deflection_deg must be in (0, 180)')


def tessellate(shape, params: TessellationParams | None = None):
    """Tessellate an OCCT shape into a vtkPolyData. Requires OCCT ([cad] extra)."""
    require()
    params = params or TessellationParams()
    params.validate()

    import math
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCC.Core.TopoDS import topods
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.TopLoc import TopLoc_Location
    from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
    from vtkmodules.vtkCommonCore import vtkPoints, vtkIntArray

    BRepMesh_IncrementalMesh(
        shape, params.linear_deflection, params.relative,
        math.radians(params.angular_deflection_deg), params.parallel)

    points = vtkPoints()
    triangles = vtkCellArray()
    face_ids = vtkIntArray()
    face_ids.SetName('cadFaceId')

    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    face_index = 0
    base = 0
    while explorer.More():
        face = topods.Face(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation(face, location)
        if triangulation is not None:
            trsf = location.Transformation()
            n = triangulation.NbNodes()
            for i in range(1, n + 1):
                p = triangulation.Node(i).Transformed(trsf)
                points.InsertNextPoint(p.X(), p.Y(), p.Z())
            for i in range(1, triangulation.NbTriangles() + 1):
                t = triangulation.Triangle(i)
                a, b, c = t.Get()
                if face.Orientation() == TopAbs_REVERSED:
                    b, c = c, b
                triangles.InsertNextCell(3)
                triangles.InsertCellPoint(base + a - 1)
                triangles.InsertCellPoint(base + b - 1)
                triangles.InsertCellPoint(base + c - 1)
                face_ids.InsertNextValue(face_index)
            base += n
        face_index += 1
        explorer.Next()

    polydata = vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetPolys(triangles)
    polydata.GetCellData().AddArray(face_ids)
    # OCCT triangulates per face, so shared CAD vertices otherwise appear as
    # duplicate VTK points and a closed solid is falsely diagnosed as open.
    # An explicit scale tied to the requested chord tolerance keeps this
    # deterministic while preserving the per-cell cadFaceId array.
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData
    clean = vtkCleanPolyData()
    clean.SetInputData(polydata)
    clean.PointMergingOn()
    clean.ToleranceIsAbsoluteOn()
    clean.SetAbsoluteTolerance(max(params.linear_deflection * 1e-6, 1e-12))
    clean.Update()
    output = vtkPolyData()
    output.DeepCopy(clean.GetOutput())
    return output
