#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The boundary's own feature edges, for a mesh nobody declared features on.

Plan 45 P11 (Q5 a). *Feature edges* drew only the features a geometry-fidelity
run recorded, or a native Gmsh mesh's curves. A snappy polyMesh has neither,
so on the meshes most people open the button did nothing. With neither
present, the edges are read off the boundary itself: every crease sharper
than the project's feature angle, every open rim and every non-manifold
edge, drawn as one actor over the surfaces.

Only the boundary patches are read -- never the volume -- so the work scales
with the boundary faces. The caller decides whether that is small enough to
do on the GUI thread (:func:`face_count`, :data:`SYNC_FACE_LIMIT`);
:func:`extract_edges` touches no renderer and may run on a worker.
"""
from __future__ import annotations

from vtkmodules.vtkCommonDataModel import vtkPolyData
from vtkmodules.vtkFiltersCore import (
    vtkAppendPolyData, vtkCleanPolyData, vtkFeatureEdges)
from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

from foammesh.view.theming.vtk_theme import rgb

#: Degrees of turn from flat, the project feature angle's own default.
DEFAULT_FEATURE_ANGLE = 30.0

#: Boundary faces above which the extraction goes to a worker thread.
SYNC_FACE_LIMIT = 200_000

#: The theme roles the edges are drawn in, first readable wins.
COLOUR_ROLES = ('viewport.silhouette', 'viewport.lines')
FALLBACK_COLOUR = '#202020'

LINE_WIDTH = 2.0

#: Object name of the actor, so a test or a pick can tell it apart.
ACTOR_NAME = 'features:boundary'


def _surface(data_set):
    """*data_set* as polydata, or ``None`` when it holds no faces."""
    if data_set is None:
        return None
    if isinstance(data_set, vtkPolyData):
        surface = data_set
    else:
        surface_filter = vtkDataSetSurfaceFilter()
        surface_filter.SetInputData(data_set)
        surface_filter.Update()
        surface = surface_filter.GetOutput()
    if surface.GetNumberOfCells() == 0:
        return None
    return surface


def face_count(data_sets) -> int:
    """How many faces the boundary datasets hold together."""
    total = 0
    for data_set in data_sets or ():
        count = getattr(data_set, 'GetNumberOfCells', None)
        if count is not None:
            total += int(count())
    return total


def _angle(feature_angle) -> float:
    try:
        value = float(feature_angle)
    except (TypeError, ValueError):
        return DEFAULT_FEATURE_ANGLE
    if value != value or value <= 0.0 or value >= 180.0:
        return DEFAULT_FEATURE_ANGLE
    return value


def extract_edges(data_sets, feature_angle=DEFAULT_FEATURE_ANGLE):
    """The feature, boundary and non-manifold edges of the patches together.

    The patches are merged first, so the line where two patches meet flat
    is not drawn -- only where the boundary actually turns, opens or
    branches. Returns line polydata, or ``None`` when there is no edge.
    """
    surfaces = [surface for surface in map(_surface, data_sets or ())
                if surface is not None]
    if not surfaces:
        return None
    append = vtkAppendPolyData()
    for surface in surfaces:
        append.AddInputData(surface)
    clean = vtkCleanPolyData()
    clean.SetInputConnection(append.GetOutputPort())
    clean.PointMergingOn()
    clean.ConvertLinesToPointsOff()
    clean.ConvertPolysToLinesOff()
    clean.ConvertStripsToPolysOff()
    edges = vtkFeatureEdges()
    edges.SetInputConnection(clean.GetOutputPort())
    edges.FeatureEdgesOn()
    edges.BoundaryEdgesOn()
    edges.NonManifoldEdgesOn()
    edges.ManifoldEdgesOff()
    edges.ColoringOff()
    edges.SetFeatureAngle(_angle(feature_angle))
    edges.Update()
    output = vtkPolyData()
    output.ShallowCopy(edges.GetOutput())
    if output.GetNumberOfLines() == 0:
        return None
    return output


def edge_colour(tokens=None) -> str:
    """The theme's edge colour."""
    if tokens is not None:
        for role in COLOUR_ROLES:
            try:
                value = tokens.value(role)
            except Exception:                                 # noqa: BLE001
                continue
            if value:
                return str(value)
    return FALLBACK_COLOUR


def build_actor(edges, tokens=None):
    """One unlit line actor over *edges*, drawn in front of the surfaces."""
    if edges is None:
        return None
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(edges)
    mapper.ScalarVisibilityOff()
    # The edges lie exactly on the faces they bound; without the offset they
    # z-fight them and strobe as the camera moves.
    mapper.SetResolveCoincidentTopologyToPolygonOffset()
    mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-1.0, -6.0)
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.SetObjectName(ACTOR_NAME)
    prop = actor.GetProperty()
    prop.SetLighting(False)
    prop.SetLineWidth(LINE_WIDTH)
    try:
        prop.SetColor(*rgb(edge_colour(tokens)))
    except (TypeError, ValueError):
        prop.SetColor(*rgb(FALLBACK_COLOUR))
    # A click must still resolve to the patch underneath.
    actor.PickableOff()
    return actor


def boundary_feature_edges(data_sets, feature_angle=DEFAULT_FEATURE_ANGLE,
                           tokens=None):
    """The actor for the boundary's own feature edges, or ``None``."""
    return build_actor(extract_edges(data_sets, feature_angle), tokens)
