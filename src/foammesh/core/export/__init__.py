#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Neutral mesh export.

Provides a pluggable set of standalone mesh exporters:
OpenFOAM (native), VTK/VTU (diagnostics), Gmsh and CGNS (neutral exchange). A
capability registry records which formats are lossless vs. lossy and what extra
they need, and a readiness check warns before a lossy export.
"""

from .base import ExportCapability, ExportReport, ExportFormat
from .registry import list_formats, capability, readiness, CAPABILITIES
from .exporters import export, available

__all__ = [
    'ExportCapability', 'ExportReport', 'ExportFormat',
    'list_formats', 'capability', 'readiness', 'CAPABILITIES',
    'export', 'available',
]
from .poly_mesh_writer import FoamPolyMeshWriter, PolyMeshWriteError, PolyMeshWriteReport

__all__ = ['FoamPolyMeshWriter', 'PolyMeshWriteError', 'PolyMeshWriteReport']
