#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Export interfaces + capability/report types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExportFormat(str, Enum):
    OPENFOAM = 'openfoam'
    VTK = 'vtk'
    GMSH = 'gmsh'
    CGNS = 'cgns'
    SU2 = 'su2'


@dataclass(frozen=True)
class ExportCapability:
    format: ExportFormat
    suffix: str               # '' for directory-based (OpenFOAM)
    lossless: bool
    requires_extra: str | None  # pip extra needed, or None
    notes: str = ''
    display_name: str = ''
    maturity: str = 'stable'  # 'stable' | 'experimental' (§13.1 registry contract)


@dataclass
class ExportReport:
    format: ExportFormat
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {'format': self.format.value, 'ok': self.ok,
                'warnings': list(self.warnings), 'errors': list(self.errors)}
