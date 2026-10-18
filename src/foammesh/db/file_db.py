#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import Enum

CONFIGURATIONS_KEY = 'configurations'

POLYDATA_PREFIX = 'polyData'

#: Plan 35 CR9. The ``seq`` of the last change set this file's content
#: includes. Recovery replays only the autosave records after it.
SAVED_SEQ_ATTRIBUTE = 'foammesh_saved_seq'


def _h5py():
    """Plan 45 start-up: h5py is imported on the first read or write of a
    case file, not by importing the application, where it held every launch
    before the window could be built (as did vtkIOXML, imported below where
    it is used)."""
    import h5py
    return h5py


class FileGroup(Enum):
    GEOMETRY_POLY_DATA = 'geometry'


def newFiles():
    return {
        FileGroup.GEOMETRY_POLY_DATA.value: {}
    }


def writeConfigurations(path, configurations, files, savedSeq=None):
    with _h5py().File(path, 'w') as f:
        f[CONFIGURATIONS_KEY] = configurations
        if savedSeq:
            f.attrs[SAVED_SEQ_ATTRIBUTE] = int(savedSeq)

        geometryPolyData = f.create_group(FileGroup.GEOMETRY_POLY_DATA.value)
        polyData = files[FileGroup.GEOMETRY_POLY_DATA.value]
        if polyData:
            from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter
        for key in polyData:
            if polyData[key]:
                writer = vtkXMLPolyDataWriter()
                writer.SetInputData(polyData[key])
                writer.WriteToOutputStringOn()
                writer.Update()

                geometryPolyData[key] = writer.GetOutputString()


def readSavedSeq(path):
    """The ``saved_seq`` a configuration file carries; 0 when it has none."""
    try:
        with _h5py().File(path, 'r') as f:
            return int(f.attrs.get(SAVED_SEQ_ATTRIBUTE, 0))
    except (OSError, ValueError, TypeError):
        return 0


def readConfigurations(path):
    with _h5py().File(path, 'r') as f:
        configurations = f[CONFIGURATIONS_KEY][()]

        files = {}
        maxIds = {}

        geometryPolyData = f[FileGroup.GEOMETRY_POLY_DATA.value]
        polyData = {}
        maxIndex = 0
        prefixLen = len(POLYDATA_PREFIX)
        from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
        for key in geometryPolyData.keys():
            reader = vtkXMLPolyDataReader()
            reader.ReadFromInputStringOn()
            reader.SetInputString(geometryPolyData[key][()])
            reader.Update()
            polyData[key] = reader.GetOutput()

            index = int(key[prefixLen:])
            if index > maxIndex:
                maxIndex = index

        files[FileGroup.GEOMETRY_POLY_DATA.value] = polyData
        maxIds[FileGroup.GEOMETRY_POLY_DATA.value] = maxIndex

        return configurations, files, maxIds
