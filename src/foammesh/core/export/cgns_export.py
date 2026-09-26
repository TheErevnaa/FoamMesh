#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CGNS (.cgns) export via VTK's CGNS writer (lazy).

Requires a VTK build that ships ``vtkCGNSWriter`` (vtkIOCGNSWriter). Not all VTK
wheels include it; this module checks and fails clearly if absent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


#: The one module this writer needs, named wherever its absence is reported.
WRITER_MODULE = 'vtkmodules.vtkIOCGNSWriter'


def is_available() -> bool:
    try:
        return importlib.util.find_spec(WRITER_MODULE) is not None
    except (ImportError, ValueError):
        return False


def unavailable_reason() -> str:
    """Why this route cannot write here -- empty when it can.

    Plan 31 DP-24. The export dialog used to disable this row with ``cgns
    export needs the [export] extra: pip install "foammesh[export]"``, which
    was the same generic sentence every format in the readiness table produced
    and was wrong about this one. MEASURED 2026-09-06: VTK itself is installed
    and working -- the VTU export in the same dialog writes files -- and what
    raises ``ModuleNotFoundError`` is ``vtkmodules.vtkIOCGNSWriter``, a module
    that ships inside a VTK build rather than in any pip extra. A user who
    followed the advice installed what they already had and saw no change.
    """
    if is_available():
        return ''
    if importlib.util.find_spec('vtkmodules') is None:
        return ('CGNS export needs VTK, and "import vtkmodules" fails here. '
                'VTK is a hard requirement of this application; reinstalling '
                'it (pip install vtk) is what restores this row.')
    return (
        f'CGNS export needs a VTK build that includes the CGNS writer: '
        f'"import {WRITER_MODULE}" raises ModuleNotFoundError here, while VTK '
        'itself is installed and working. No pip extra ships that module, so '
        'installing "foammesh[export]" will not change this — a VTK build '
        'carrying vtkCGNSWriter is what this route needs. A case with an '
        'accepted Gmsh run exports CGNS through Gmsh instead and needs no VTK '
        'module at all.')


def require() -> None:
    if not is_available():
        raise RuntimeError(unavailable_reason())


def write_cgns(dataset, dest) -> Path:
    """Write a vtk dataset to a .cgns file. Requires vtkCGNSWriter."""
    require()
    from vtkmodules.vtkIOCGNSWriter import vtkCGNSWriter
    out = Path(dest)
    writer = vtkCGNSWriter()
    writer.SetFileName(str(out))
    writer.SetInputData(dataset)
    writer.Write()
    return out
