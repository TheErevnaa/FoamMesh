#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Native OpenFOAM export: validate and package a generated case directory.

The OpenFOAM case *is* the native output (system/ dicts + constant/polyMesh after
meshing). Export validates the case and copies it to a standalone destination.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import ExportFormat, ExportReport


#: The files an OpenFOAM mesh is made of.  Every reader -- ``checkMesh``, the
#: solvers, ParaView's ``.foam`` reader -- opens all five, so a directory
#: holding four of them is not a mesh that is mostly there, it is a mesh
#: nothing will read.
#:
#: DP-266 wrote this list in ``core/import_export/service.py``, which is one
#: of the two callers of :func:`validate_case`; the verdict itself did not
#: know it, so the other caller and every reader of the report were still
#: told a four-file directory was a mesh. It lives here, beside the verdict,
#: because ``service.py`` already imports this module and the reverse import
#: would be a cycle.
POLY_MESH_FILES = ('points', 'faces', 'owner', 'neighbour', 'boundary')


def missing_poly_mesh_files(case_path) -> list[str]:
    """Which of :data:`POLY_MESH_FILES` a case's mesh directory is without.

    Empty when the case has no ``constant/polyMesh`` at all: a case that is
    dictionaries and no mesh yet is a different matter, and
    :func:`validate_case` already warns about it. ``.gz`` counts, because
    OpenFOAM writes compressed meshes under the same names and reads them
    back the same way.
    """
    mesh = Path(case_path) / 'constant' / 'polyMesh'
    if not mesh.is_dir():
        return []
    return [name for name in POLY_MESH_FILES
            if not (mesh / name).is_file()
            and not (mesh / f'{name}.gz').is_file()]


def incomplete_mesh_message(absent) -> str:
    """The one sentence every route says about a mesh written halfway.

    One wording, because the service and the native writer both refuse on
    this and a reader who met two spellings of the same refusal would have to
    work out whether they were the same complaint.
    """
    return ('constant/polyMesh is incomplete, so this case is not a mesh any '
            'OpenFOAM reader will open: missing ' + ', '.join(absent)
            + '. Re-run the mesh before exporting it.')


def validate_case(case_dir) -> ExportReport:
    case = Path(case_dir)
    report = ExportReport(format=ExportFormat.OPENFOAM)
    if not case.is_dir():
        report.ok = False
        report.errors.append(f'case directory not found: {case}')
        return report
    has_system = (case / 'system').is_dir()
    has_mesh = (case / 'constant' / 'polyMesh').is_dir()
    if not has_system and not has_mesh:
        report.ok = False
        report.errors.append('no system/ dicts or constant/polyMesh in case')
    if has_system and not has_mesh:
        report.warnings.append('case has dictionaries but no polyMesh yet '
                               '(run blockMesh/snappyHexMesh first).')
    # DP-266 left this standing: the presence of the mesh directory stood in
    # for the presence of a mesh, so a run killed between `owner` and
    # `neighbour` passed the verdict and every caller of it.  A case with no
    # mesh directory at all is not taken here -- that is the warning above,
    # and a mesh not built yet is a different matter from one written
    # halfway.
    absent = missing_poly_mesh_files(case)
    if absent:
        report.ok = False
        report.errors.append(incomplete_mesh_message(absent))
    return report


def export_openfoam(case_dir, dest_dir) -> Path:
    """Copy the OpenFOAM case to *dest_dir* (the native export)."""
    case = Path(case_dir)
    report = validate_case(case)
    if not report.ok:
        raise ValueError('; '.join(report.errors))
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    # `0/` is part of the case, not an optional extra: an OpenFOAM case without
    # its initial-conditions directory is one the solver refuses to start, and
    # the two-dimensional export path and every hand-authored case carry one.
    # Copying only system/ and constant/ silently produced an export the user
    # had to repair by hand. It stays optional because a mesh-only case has no
    # `0/` to copy.
    for sub in ('system', 'constant', '0'):
        src = case / sub
        if src.is_dir():
            shutil.copytree(src, dest / sub, dirs_exist_ok=True)
    return dest
