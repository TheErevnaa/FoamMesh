#!/usr/bin/env python
# -*- coding: utf-8 -*-

import math

from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkActor, vtkFollower
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkHexahedron, vtkCellArray, vtkUnstructuredGrid, vtkPolygon, vtkPolyData
from vtkmodules.vtkFiltersSources import vtkLineSource, vtkSphereSource
from vtkmodules.vtkFiltersCore import vtkTubeFilter, vtkFeatureEdges
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
from vtkmodules.vtkRenderingFreeType import vtkVectorText


def polyDataToActor(polyData):
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(polyData)
    mapper.ScalarVisibilityOff()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetAmbient(0.2)
    actor.GetProperty().SetDiffuse(0.3)

    return actor


def polyDataToFeatureActor(polyData):
    edges = vtkFeatureEdges()
    edges.SetInputData(polyData)
    edges.Update()

    return polyDataToActor(edges.GetOutput())


def hexPolyData(point1, point2):
    xMin, yMin, zMin = point1
    xMax, yMax, zMax = point2

    pointCoordinates = list()
    pointCoordinates.append([xMin, yMin, zMin])
    pointCoordinates.append([xMax, yMin, zMin])
    pointCoordinates.append([xMax, yMax, zMin])
    pointCoordinates.append([xMin, yMax, zMin])
    pointCoordinates.append([xMin, yMin, zMax])
    pointCoordinates.append([xMax, yMin, zMax])
    pointCoordinates.append([xMax, yMax, zMax])
    pointCoordinates.append([xMin, yMax, zMax])

    points = vtkPoints()

    hexahedron = vtkHexahedron()

    for i in range(0, len(pointCoordinates)):
        points.InsertNextPoint(pointCoordinates[i])
        hexahedron.GetPointIds().SetId(i, i)
    #
    # hexs = vtkCellArray()
    # hexs.InsertNextCell(hexahedron)

    uGrid = vtkUnstructuredGrid()
    uGrid.SetPoints(points)
    uGrid.InsertNextCell(hexahedron.GetCellType(), hexahedron.GetPointIds())

    geometryFilter = vtkGeometryFilter()
    geometryFilter.SetInputData(uGrid)
    geometryFilter.Update()
    #
    # mapper = vtkDataSetMapper()
    # mapper.SetInputData(geometryFilter.GetOutput())
    #
    # actor = vtkActor()
    # actor.SetMapper(mapper)
    # actor.GetProperty().SetColor(0.8, 0.8, 0.8)

    return geometryFilter.GetOutput()


def cylinderPolyData(point1, point2, radius):
    line = vtkLineSource()
    line.SetPoint1(*point1)
    line.SetPoint2(*point2)

    cyl = vtkTubeFilter()
    cyl.SetInputConnection(line.GetOutputPort())
    cyl.SetRadius(float(radius))
    cyl.SetNumberOfSides(64)
    cyl.CappingOn()

    geometryFilter = vtkGeometryFilter()
    geometryFilter.SetInputConnection(cyl.GetOutputPort())
    geometryFilter.Update()

    return geometryFilter.GetOutput()


def spherePolyData(point, radius):
    sphere = vtkSphereSource()
    sphere.SetCenter(*point)
    sphere.SetRadius(radius)
    sphere.SetPhiResolution(100)
    sphere.SetThetaResolution(100)
    sphere.Update()
    #
    # mapper = vtkDataSetMapper()
    # mapper.SetInputConnection(sphere.GetOutputPort())
    #
    # geometryFilter = vtkGeometryFilter()
    # geometryFilter.SetInputConnection(mapper.GetOutputPort())
    # geometryFilter.Update()

    return sphere.GetOutput()


def _inPlaneAxes(normal):
    """Two unit vectors spanning the plane a normal defines.

    Plan 31. The plane and disk previews both need to draw something flat
    facing a direction the user typed, and a normal on its own does not say
    which way "up" is in that plane. Any consistent pair will do for a
    preview; this crosses the normal with whichever axis it is least aligned
    with, so the pair never degenerates.
    """
    length = math.sqrt(sum(component * component for component in normal))
    if length == 0:
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    unit = [component / length for component in normal]
    reference = [0.0, 0.0, 0.0]
    reference[min(range(3), key=lambda i: abs(unit[i]))] = 1.0
    first = [unit[1] * reference[2] - unit[2] * reference[1],
             unit[2] * reference[0] - unit[0] * reference[2],
             unit[0] * reference[1] - unit[1] * reference[0]]
    firstLength = math.sqrt(sum(component * component for component in first))
    first = [component / firstLength for component in first]
    second = [unit[1] * first[2] - unit[2] * first[1],
              unit[2] * first[0] - unit[0] * first[2],
              unit[0] * first[1] - unit[1] * first[0]]
    return tuple(first), tuple(second)


def planePolyData(point, normal, extent=1.0):
    """A square standing in for an infinite plane.

    The surface OpenFOAM builds has no edges at all; this square only shows
    where the plane sits and which way it faces, which is what the user is
    checking when they press Preview.
    """
    first, second = _inPlaneAxes(normal)
    corners = []
    for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        corners.append(tuple(
            point[i] + su * extent * first[i] + sv * extent * second[i]
            for i in range(3)))
    return polygonPolyData(corners)


def diskPolyData(origin, normal, radius, resolution=64):
    """A filled circle of *radius* at *origin*, facing *normal*."""
    if radius <= 0:
        return None
    first, second = _inPlaneAxes(normal)
    corners = []
    for step in range(resolution):
        angle = 2.0 * math.pi * step / resolution
        cosine, sine = math.cos(angle), math.sin(angle)
        corners.append(tuple(
            origin[i] + radius * (cosine * first[i] + sine * second[i])
            for i in range(3)))
    return polygonPolyData(corners)


def openPlatePolyData(origin, span):
    """The axis-aligned rectangle OpenFOAM's ``plate`` describes.

    Named ``open`` to keep it apart from ``geometry_manager.platePolyData``,
    which draws one of the six faces of a hex6 -- this program has called those
    "plates" since before OpenFOAM's ``plate`` surface could be reached from
    it, and the two are not the same thing.

    The zero component of the span is the direction the plate faces
    (``plate_searchableSurface.C:59-85``), so the other two are its edges and
    the four corners are origin, origin+a, origin+a+b, origin+b. A span that
    does not have exactly two non-zero entries is not a plate and gets no
    preview -- which is the same answer OpenFOAM gives it, earlier.
    """
    edges = []
    for axis in range(3):
        if span[axis] == 0:
            continue
        edge = [0.0, 0.0, 0.0]
        edge[axis] = span[axis]
        edges.append(edge)
    if len(edges) != 2:
        return None
    first, second = edges
    corners = [
        tuple(origin),
        tuple(origin[i] + first[i] for i in range(3)),
        tuple(origin[i] + first[i] + second[i] for i in range(3)),
        tuple(origin[i] + second[i] for i in range(3)),
    ]
    return polygonPolyData(corners)


def polygonPolyData(points):
    vPoints = vtkPoints()
    for p in points:
        vPoints.InsertNextPoint(*p)

    polygon = vtkPolygon()
    polygon.GetPointIds().SetNumberOfIds(len(points))
    for i in range(len(points)):
        polygon.GetPointIds().SetId(i, i)

    polygons = vtkCellArray()
    polygons.InsertNextCell(polygon)

    polygonPolyData = vtkPolyData()
    polygonPolyData.SetPoints(vPoints)
    polygonPolyData.SetPolys(polygons)
    #
    # mapper = vtkPolyDataMapper()
    # mapper.SetInputData(polygonPolyData)
    #
    # actor = vtkActor()
    # actor.SetMapper(mapper)

    return polygonPolyData


def lineActor(point1, point2):
    lineSource = vtkLineSource()
    lineSource.SetPoint1(point1)
    lineSource.SetPoint2(point2)

    mapper = vtkPolyDataMapper()
    mapper.SetInputConnection(lineSource.GetOutputPort())

    actor = vtkActor()
    actor.SetMapper(mapper)

    return actor


def labelActor(text):
    label = vtkVectorText()
    label.SetText(text)

    mapper = vtkPolyDataMapper()
    mapper.SetInputConnection(label.GetOutputPort())

    actor = vtkFollower()
    actor.SetMapper(mapper)

    return actor
