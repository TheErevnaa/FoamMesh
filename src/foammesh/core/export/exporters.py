#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Unified export dispatch across formats.

`available(fmt)` reports whether a format can run in this environment; `export()`
runs the right writer after a readiness check. OpenFOAM/VTK work everywhere;
Gmsh/CGNS need their extras and fail with a clear message otherwise.
"""
from __future__ import annotations

from pathlib import Path

from .base import ExportFormat
from .registry import readiness
from . import openfoam as _openfoam
from . import gmsh_export as _gmsh
from . import cgns_export as _cgns
from .su2_export import write_su2
from .vtk_export import write_vtu


def available(fmt) -> bool:
    fmt = ExportFormat(fmt)
    if fmt in (ExportFormat.OPENFOAM, ExportFormat.VTK, ExportFormat.SU2):
        # SU2 is written from the canonical mesh in pure Python: no extra.
        return True
    if fmt is ExportFormat.GMSH:
        return _gmsh.is_available()
    return _cgns.is_available()


def export(fmt, source, dest) -> Path:
    """Export *source* to *dest* in *fmt*.

    source semantics by format:
      openfoam -> a case directory; vtk/cgns -> a vtk dataset;
      gmsh -> a mesh file; su2 -> a CanonicalMesh.
    """
    fmt = ExportFormat(fmt)
    report = readiness(fmt)
    if not report.ok:
        raise RuntimeError('; '.join(report.errors))

    if fmt is ExportFormat.OPENFOAM:
        return _openfoam.export_openfoam(source, dest)
    if fmt is ExportFormat.VTK:
        return write_vtu(source, dest)
    if fmt is ExportFormat.GMSH:
        return _gmsh.convert_with_gmsh(source, dest)
    if fmt is ExportFormat.SU2:
        write_su2(source, dest)
        return Path(dest)
    return _cgns.write_cgns(source, dest)
