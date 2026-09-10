#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Export-format capability registry + pre-export readiness checks."""
from __future__ import annotations

import importlib.util

from .base import ExportFormat, ExportCapability, ExportReport
from foammesh.core.format_registry import format_spec


_FORMAT_SPECS = {
    ExportFormat.OPENFOAM: format_spec('mesh.openfoam.export'),
    ExportFormat.VTK: format_spec('mesh.vtk.export'),
    ExportFormat.GMSH: format_spec('mesh.gmsh.export'),
    ExportFormat.CGNS: format_spec('mesh.cgns.export'),
    ExportFormat.SU2: format_spec('mesh.su2.export'),
}


#: Which pip extra actually ships the module a spec names. Plan 31, from
#: CAP-A's reading of the capability ledger. This used to answer 'export'
#: for every ``python:`` capability, so CGNS -- whose capability is
#: ``python:h5py`` -- claimed to be shipped by an extra that installs gmsh
#: and nothing else. No extra ships a CGNS writer: it comes from how VTK was
#: built, which is why the answer for it is None rather than a name.
_CAPABILITY_EXTRAS = {'gmsh': 'export'}


def _extra(spec) -> str | None:
    prefix = 'python:'
    if not spec.capability.startswith(prefix):
        return None
    return _CAPABILITY_EXTRAS.get(spec.capability[len(prefix):])


CAPABILITIES: dict[ExportFormat, ExportCapability] = {
    fmt: ExportCapability(
        fmt, spec.extensions[0] if spec.extensions else '',
        lossless=fmt is not ExportFormat.GMSH, requires_extra=_extra(spec),
        notes=spec.notes, display_name=spec.display_name,
        maturity=spec.maturity.value)
    for fmt, spec in _FORMAT_SPECS.items()
}


def list_formats() -> list[ExportFormat]:
    return list(CAPABILITIES.keys())


def capability(fmt: ExportFormat) -> ExportCapability:
    return CAPABILITIES[ExportFormat(fmt)]


#: What each pip extra actually installs, so a refusal can name the import
#: that failed rather than the extra it guesses at.
_EXTRA_MODULES = {'export': 'gmsh', 'cad': 'OCC', 'api': 'fastapi'}

#: Formats whose writer here is VTK's own. VTK is a hard requirement of the
#: application rather than an optional extra, so when it is what is missing,
#: "install the [export] extra" was never the answer.
_VTK_FORMATS = (ExportFormat.VTK, ExportFormat.SU2)


def _missing(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is None
    except (ImportError, ValueError):
        return True


def _extra_available(extra: str | None) -> bool:
    if extra is None:
        return True
    module = _EXTRA_MODULES.get(extra)
    return module is None or not _missing(module)


def unavailable_reason(cap: ExportCapability) -> str:
    """Why this format cannot be written here, or ``''`` when it can.

    Plan 31 DP-24. Every disabled row used to carry one sentence built from
    ``cap.requires_extra``: ``<format> export needs the [export] extra: pip
    install "foammesh[export]".`` MEASURED 2026-09-06, that sentence was wrong
    about the only row it was actually shown for. CGNS is unavailable on this
    host because ``import vtkmodules.vtkIOCGNSWriter`` raises
    ``ModuleNotFoundError`` -- VTK is installed and the VTU export in the same
    dialog writes files -- and no pip extra ships that module, so following
    the advice installed nothing new and changed nothing.

    A readiness reason now names the import that failed and says whether it is
    a package the user can install or a build variant they cannot, and every
    format in the table is asked the same way rather than CGNS being special
    cased. This function is also what :func:`_format_available` answers from,
    so a row can never be disabled without a reason or given one it does not
    have.
    """
    fmt = cap.format
    if fmt is ExportFormat.GMSH:
        from foammesh.core.export import gmsh_export
        return gmsh_export.unavailable_reason()
    if fmt is ExportFormat.CGNS:
        from foammesh.core.export import cgns_export
        return cgns_export.unavailable_reason()
    if fmt in _VTK_FORMATS and _missing('vtkmodules'):
        return (f'{cap.display_name or fmt.value} export needs VTK, and '
                '"import vtkmodules" fails here. VTK is a hard requirement of '
                'this application rather than an optional extra; reinstalling '
                'it (pip install vtk) is what restores this row.')
    extra = cap.requires_extra
    module = _EXTRA_MODULES.get(extra) if extra else None
    if module and _missing(module):
        return (f'{cap.display_name or fmt.value} export needs the {module} '
                f'module, and "import {module}" fails here. Install it with: '
                f'pip install "foammesh[{extra}]".')
    return ''


def _format_available(cap: ExportCapability) -> bool:
    """Whether this format can be written here, by whichever route it has.

    The pip extra is one way to have a writer, not the only one: Gmsh export
    also runs through the qualified WSL runtime, and CGNS depends on how VTK
    was built rather than on anything pip installs.
    """
    return not unavailable_reason(cap)


#: Formats whose reader knows only the four SU2 cell families. Plan 28: this
#: is a property of the destination, not of a lossy writer, which is why
#: `lossless` could never express it -- the SU2 writer is perfectly lossless
#: for a mesh SU2 can read, and cannot write one it cannot.
_SOLVER_FORMATS = (ExportFormat.SU2,)


def readiness(fmt: ExportFormat, *, has_polyhedra: bool = False,
              census=None) -> ExportReport:
    """Pre-export check.

    Missing extras are an error; a lossy format warns. A mesh the destination
    cannot read is an error, not a warning: offering "export anyway" produces a
    file the solver refuses to open, which is worse than refusing here.

    ``census`` is a :class:`~foammesh.core.mesh.census.MeshCensus`. It carries
    the counts and the prose, so the message names how much of the mesh is
    affected. ``has_polyhedra`` remains for callers that know only the fact:
    it is the same verdict with a vaguer reason.
    """
    cap = capability(fmt)
    report = ExportReport(format=ExportFormat(fmt))

    reason = unavailable_reason(cap)
    if reason:
        report.ok = False
        report.errors.append(reason)

    if cap.format in _SOLVER_FORMATS:
        if census is not None and not census.su2_readable:
            report.ok = False
            report.errors.append(census.reason)
        elif census is None and has_polyhedra:
            report.ok = False
            report.errors.append(
                'the mesh contains polyhedral cells; SU2 reads tetrahedra, '
                'hexahedra, prisms and pyramids only')
        for warning in getattr(census, 'warnings', ()):
            report.warnings.append(warning)

    if not cap.lossless:
        report.warnings.append(cap.notes)
        if has_polyhedra or (census is not None and census.polyhedral_cells):
            report.warnings.append(
                'Mesh contains polyhedral cells — this format may drop or split them.')

    return report
