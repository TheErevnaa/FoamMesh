#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import sys
from pathlib import Path

from vtkmodules.vtkCommonCore import vtkIdTypeArray, vtkIdList, vtkIntArray
from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray, vtkCell, vtkLine, vtkDataObject
from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkIdFilter, vtkFeatureEdges, \
    vtkPolyDataEdgeConnectivityFilter, vtkThreshold, vtkCleanPolyData
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints
from vtkmodules.vtkFiltersVerdict import vtkCellSizeFilter
from vtkmodules.vtkIOGeometry import vtkSTLReader, vtkOBJReader


class StringIndex:
    def __init__(self):
        self._index2string = []

    def putString(self, string: str) -> int:
        self._index2string.append(string)
        return len(self._index2string) - 1

    def getString(self, index: int) -> str:
        if index < len(self._index2string):
            return self._index2string[index]
        else:
            raise KeyError

    def clear(self):
        self._index2string = []


class StlSurface:
    """
    This class is for am STL vtkPolyData with file name and solid name
    """
    def __init__(self, polyData: vtkPolyData, fName: str, sName: str, sIndex: int):
        self.polyData = polyData
        self.fName = fName
        self.sName = sName
        self.sIndex = sIndex
        # DP-383. Which artifact and which patch record this piece came from,
        # for the pieces that were read back out of the geometry artifact
        # store. A surface loaded any other way has neither and says so.
        self.geometryId = None
        self.patchUuid = None
        # DP-419. Which CAD body this face bounds, where the import knew.
        self.regionUuid = None


def isClosed(surfaces):
    if isinstance(surfaces, StlSurface):
        return vtkSelectEnclosedPoints.IsSurfaceClosed(surfaces.polyData)
    elif isinstance(surfaces, list):
        if not len(surfaces) > 0:
            return False
        if len(surfaces) == 1:
            return vtkSelectEnclosedPoints.IsSurfaceClosed(surfaces[0].polyData)

        appendFilter = vtkAppendPolyData()

        for s in surfaces:
            appendFilter.AddInputData(s.polyData)

        appendFilter.Update()

        cleanFilter = vtkCleanPolyData()
        cleanFilter.SetInputData(appendFilter.GetOutput())
        cleanFilter.Update()

        if vtkSelectEnclosedPoints.IsSurfaceClosed(cleanFilter.GetOutput()):
            return True
        else:
            return False
    else:
        raise ValueError


def composeVolume(surfaces):
    volumes = []
    remains = []

    for s in surfaces:
        if isClosed(s):
            volumes.append([s])
        else:
            remains.append(s)

    if isClosed(remains):
        volumes.append(remains)
        remains = []

    return volumes, remains


class StlImporter:
    def __init__(self):
        self._stringIndices = StringIndex()
        self._solids: list[StlSurface] = []
        self._surfaceList: list[StlSurface] = []

    def load(self, files: list[Path]):
        self._stringIndices.clear()
        self._solids.clear()
        self._surfaceList.clear()
        for f in files:
            solids = self._loadSTLFile(f)
            # solids and surfaceList are same without split
            self._solids.extend(solids)
            self._surfaceList.extend(solids)

    def loadNamedSolids(self, path: Path, names: dict, fileName: str = None,
                        volumeName: str = None, geometryId: str = None,
                        uuids: dict = None) -> list:
        """Load a file the geometry artifact store wrote, naming its solids
        as the store's patch records do.

        The store writes one ``face<N>`` solid per boundary and keeps the
        boundary's own name on its patch record. ``names`` maps the solid
        name to that record name, so the surfaces shown here carry the
        names both meshers will stage; a solid without an entry keeps the
        name it was written with. ``fileName`` is what the pieces are
        grouped under (the source's stem rather than the artifact's
        ``rev2``) and ``volumeName`` names the volume they close, if they do.

        ``geometryId`` and ``uuids`` (patch name -> patch uuid) stamp each
        piece with the record it stands for, so the database row written from
        it can be traced back to the manifest by identity rather than by the
        name, which the two stores are allowed to spell differently (DP-383).
        """
        self.load([Path(path)])
        pieces = []
        for surface in self._surfaceList:
            name = names.get(surface.sName) or surface.sName
            count = surface.polyData.GetNumberOfCells()
            fName = fileName or surface.fName
            # AddArray replaces an array of the same name, so the tags the
            # feature-angle preview reads follow the rename.
            self._addArray(surface.polyData, 'fIndex', fName, count)
            sIndex = self._addArray(surface.polyData, 'sIndex', name, count)
            piece = StlSurface(surface.polyData, fName, name, sIndex)
            piece.geometryId = geometryId
            piece.patchUuid = (uuids or {}).get(name)
            if volumeName:
                piece.volumeName = volumeName
            pieces.append(piece)
        self._solids[:] = pieces
        self._surfaceList[:] = pieces
        return pieces

    def split(self, angle: float, minArea: float):
        appendFilter = vtkAppendPolyData()

        for solid in self._solids:
            appendFilter.AddInputData(solid.polyData)

        appendFilter.Update()

        polyData: vtkPolyData = appendFilter.GetOutput()

        idFilter = vtkIdFilter()
        idFilter.SetInputData(polyData)
        idFilter.PointIdsOn()
        idFilter.CellIdsOff()
        idFilter.FieldDataOff()
        idFilter.SetPointIdsArrayName('pointId')
        idFilter.Update()

        orgSurface: vtkPolyData = idFilter.GetOutput()

        edgeFilter = vtkFeatureEdges()
        edgeFilter.SetInputData(orgSurface)
        edgeFilter.SetFeatureAngle(angle)
        edgeFilter.ExtractAllEdgeTypesOff()
        edgeFilter.FeatureEdgesOn()
        edgeFilter.BoundaryEdgesOn()
        edgeFilter.Update()

        edges: vtkPolyData = edgeFilter.GetOutput()

        orgPtIdScalars: vtkIdTypeArray = edges.GetPointData().GetScalars("pointId")

        lines = vtkCellArray()

        for i in range(0, edges.GetNumberOfCells()):
            cell: vtkCell = edges.GetCell(i)
            pointIds: vtkIdList = cell.GetPointIds()

            p1 = int(orgPtIdScalars.GetTuple(pointIds.GetId(0))[0])
            p2 = int(orgPtIdScalars.GetTuple(pointIds.GetId(1))[0])

            line = vtkLine()
            line.GetPointIds().SetId(0, p1)
            line.GetPointIds().SetId(1, p2)
            lines.InsertNextCell(line)

        # barrier should have the same points with original surface
        barrier = vtkPolyData()
        barrier.SetPoints(orgSurface.GetPoints())
        barrier.SetLines(lines)

        conn = vtkPolyDataEdgeConnectivityFilter()
        conn.SetInputData(orgSurface)
        conn.SetSourceData(barrier)
        conn.SetExtractionModeToAllRegions()
        conn.BarrierEdgesOn()
        conn.ScalarConnectivityOff()
        conn.GrowLargeRegionsOn()
        conn.SetLargeRegionThreshold(minArea)
        conn.ColorRegionsOn()
        conn.CellRegionAreasOn()
        conn.Update()

        regionedData = conn.GetOutput()
        regionedData.GetPointData().RemoveArray("pointId")

        self._surfaceList.clear()
        segments = []
        totalArea = conn.GetTotalArea()
        numRegions = conn.GetNumberOfExtractedRegions()
        for rid in range(0, numRegions):
            t = vtkThreshold()
            t.SetInputData(regionedData)
            t.SetLowerThreshold(rid - 0.5)
            t.SetUpperThreshold(rid + 0.5)
            t.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
            t.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, 'RegionId')
            t.Update()

            # Output of vtkThreshold filter is Unstructured Grid
            # Convert it to vtkPolyData by vtkGeometryFilter
            geometry = vtkGeometryFilter()
            geometry.SetInputData(t.GetOutput())
            geometry.Update()

            polyData: vtkPolyData = geometry.GetOutput()
            if polyData.GetNumberOfCells() == 0:
                continue

            fIndex = polyData.GetCellData().GetAbstractArray("fIndex").GetValue(0)
            fName = self._stringIndices.getString(fIndex)

            sIndex = polyData.GetCellData().GetAbstractArray("sIndex").GetValue(0)
            sName = self._stringIndices.getString(sIndex)

            area = polyData.GetCellData().GetAbstractArray("CellRegionArea").GetValue(0)

            surface = StlSurface(polyData, fName, sName, sIndex)
            self._surfaceList.append(surface)

            segments.append((rid, (area / totalArea) * 100))

        return segments, regionedData, edges

    def _loadOBJFile(self, path: Path, sanitizeName):
        reader = vtkOBJReader()
        reader.SetFileName(str(path))
        reader.Update()

        cleanFilter = vtkCleanPolyData()
        cleanFilter.SetInputData(reader.GetOutput())
        cleanFilter.Update()

        cellSizeFilter = vtkCellSizeFilter()
        cellSizeFilter.SetInputData(cleanFilter.GetOutput())
        cellSizeFilter.Update()

        threshold = vtkThreshold()
        threshold.AllScalarsOff()
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_UPPER)
        threshold.SetUpperThreshold(sys.float_info.min)
        threshold.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, 'Area')
        threshold.SetInputData(cellSizeFilter.GetOutput())
        threshold.Update()

        geometry = vtkGeometryFilter()
        geometry.SetInputData(threshold.GetOutput())
        geometry.Update()

        surf: vtkPolyData = geometry.GetOutput()
        numCells = surf.GetNumberOfCells()

        fName = sanitizeName(path.stem)
        self._addArray(surf, 'fIndex', fName, numCells)
        sName = ''
        sIndex = self._addArray(surf, 'sIndex', sName, numCells)

        return [StlSurface(surf, fName, sName, sIndex)]

    def _loadSTLFile(self, path: Path):
        def sanitizeName(name):
            if name =='':
                return name
            
            sanitized = re.sub(r'\W+', '_', name, flags=re.ASCII)
            first = '_' if sanitized[0].isdigit() else ''
            
            return first + sanitized
            
        # OBJ has no STL solid-labeling / header; read it as a single solid.
        if Path(path).suffix.lower() == '.obj':
            return self._loadOBJFile(path, sanitizeName)

        reader: vtkSTLReader = vtkSTLReader()
        reader.SetFileName(str(path))
        reader.ScalarTagsOn()
        reader.Update()

        cleanFilter = vtkCleanPolyData()
        cleanFilter.SetInputData(reader.GetOutput())
        cleanFilter.Update()

        cellSizeFilter = vtkCellSizeFilter()
        cellSizeFilter.SetInputData(cleanFilter.GetOutput())
        cellSizeFilter.Update()

        threshold = vtkThreshold()
        threshold.AllScalarsOff()
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_UPPER)

        threshold.SetUpperThreshold(sys.float_info.min)  # To get only the cells bigger than zero
        threshold.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, 'Area')

        threshold.SetInputData(cellSizeFilter.GetOutput())
        threshold.Update()

        # Output of vtkThreshold filter is Unstructured Grid
        # Convert it to vtkPolyData by vtkGeometryFilter
        geometry = vtkGeometryFilter()
        geometry.SetInputData(threshold.GetOutput())
        geometry.Update()

        stl: vtkPolyData = geometry.GetOutput()

        numCells = stl.GetNumberOfCells()

        fName = sanitizeName(path.stem)
        self._addArray(stl, 'fIndex', fName, numCells)

        if reader.GetBinaryHeader() is not None:  # BINARY STL
            sName = ''
            sIndex = self._addArray(stl, 'sIndex', sName, numCells)

            return [StlSurface(stl, fName, sName, sIndex)]

        # ASCII STL
        names = list(map(str.strip, reader.GetHeader().splitlines()))
        names = [sanitizeName(n) for n in names]  # collapse multiple whitespaces into underscore

        minSolid, maxSolid = stl.GetCellData().GetScalars('STLSolidLabeling').GetRange()
        if minSolid == maxSolid:
            sName = names[0] if names else ''
            sIndex = self._addArray(stl, 'sIndex', sName, numCells)

            return [StlSurface(stl, fName, sName, sIndex)]

        solids = []
        for sId in range(int(minSolid), int(maxSolid) + 1):
            threshold = vtkThreshold()
            threshold.SetInputData(stl)
            threshold.SetLowerThreshold(sId - 0.5)
            threshold.SetUpperThreshold(sId + 0.5)
            threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
            threshold.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, 'STLSolidLabeling')
            threshold.Update()

            # Output of vtkThreshold filter is Unstructured Grid
            # Convert it to vtkPolyData by vtkGeometryFilter

            geometryFilter = vtkGeometryFilter()
            geometryFilter.SetInputData(threshold.GetOutput())
            geometryFilter.Update()

            solid = geometryFilter.GetOutput()

            sName = names[sId] if sId < len(names) and names[sId] else ''
            sIndex = self._addArray(solid, 'sIndex', sName, solid.GetNumberOfCells())

            solids.append(StlSurface(solid, fName, sName, sIndex))

        return solids

    def _addArray(self, polyData: vtkPolyData, arrayName: str, value: str, count: int):
        index = self._stringIndices.putString(value)
        array = vtkIntArray()
        array.SetName(arrayName)
        for i in range(0, count):
            array.InsertNextValue(index)
        polyData.GetCellData().AddArray(array)

        return index

    def identifyVolumes(self):
        volumes = []
        surfaces = []

        # R173. In the order the file holds them, not a set's order. This is
        # the order the pieces are written to the project, and so the order
        # the Geometry tree lists them in: a feature split of a tee makes
        # tee_1 .. tee_4 and the user has to map each row onto a face, which
        # is needlessly hard when the rows come out as tee_2, tee_3, tee_4,
        # tee_1. Grouping is unchanged; only the order the groups are walked.
        fileNames = list(dict.fromkeys(s.fName for s in self._surfaceList))
        for fName in fileNames:
            surfacesInFile = [s for s in self._surfaceList if s.fName == fName]
            sIndices = list(dict.fromkeys(s.sIndex for s in surfacesInFile))

            remains = []
            for sIndex in sIndices:
                # identify segment volumes and solid volumes
                surfacesInSolid = [s for s in surfacesInFile if s.sIndex == sIndex]
                vList, sList = composeVolume(surfacesInSolid)
                volumes.extend(vList)
                remains.extend(sList)

            # identify file volume
            if isClosed(remains):
                volumes.append(remains)
            else:
                surfaces.extend(remains)

        # identify all selected files volume
        if isClosed(surfaces):
            volumes.append(surfaces)
            surfaces = []

        return volumes, surfaces

