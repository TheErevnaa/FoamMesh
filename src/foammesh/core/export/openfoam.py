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
