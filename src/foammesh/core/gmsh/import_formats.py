"""Open / Merge and Reclassify on the Gmsh session: the catalogue (Plan 41 GN5).

Gmsh's ``File > Open`` / ``File > Merge`` formats and the ``Mesh > Reclassify
2D`` / ``Create topology`` / ``Create geometry`` dialogs as data, with every
refusal worded here so the page never has to guess.  No Qt, no Gmsh: the
checks run on the host before anything reaches the session.

The formats are the ones Gmsh 4.15.2 reads, measured on the installed build
(2026-10-10).  Abaqus ``.inp`` is the one the plan listed that this build can
not read: it has a writer only, and a file with that extension falls into the
``.geo`` parser.  It is refused with that reason, which is also what the
native GUI would answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: target units Open CASCADE accepts for STEP and IGES (measured: ``IN`` and
#: arbitrary names are refused by this build).  ``''`` is Gmsh's own default,
#: millimetre.
UNITS = (
    ('', 'Gmsh default (millimetre)'),
    ('UM', 'Micrometre'),
    ('MM', 'Millimetre'),
    ('CM', 'Centimetre'),
    ('M', 'Metre'),
    ('KM', 'Kilometre'),
    ('FT', 'Foot'),
)
UNIT_FACTOR_TO_M = {'': 1e-3, 'UM': 1e-6, 'MM': 1e-3, 'CM': 1e-2, 'M': 1.0,
                    'KM': 1e3, 'FT': 0.3048}


class ImportRefusal(ValueError):
    """A request the Gmsh session would refuse or mangle, with the reason."""


@dataclass(frozen=True)
class Format:
    extension: str
    label: str
    kind: str                # cad | geo | tessellation | mesh
    note: str = ''


FORMATS = (
    Format('.geo', 'Gmsh script', 'geo',
           'run by the .geo parser; entities appear once it is synchronised'),
    Format('.geo_unrolled', 'Gmsh unrolled script', 'geo'),
    Format('.step', 'STEP', 'cad', 'carries its unit'),
    Format('.stp', 'STEP', 'cad', 'carries its unit'),
    Format('.iges', 'IGES', 'cad', 'carries its unit'),
    Format('.igs', 'IGES', 'cad', 'carries its unit'),
    Format('.brep', 'Open CASCADE BREP', 'cad',
           'carries no unit: the scale factor is the only handle'),
    Format('.xao', 'XAO', 'cad', 'keeps its groups as physical groups'),
    Format('.stl', 'STL', 'tessellation'),
    Format('.obj', 'Wavefront OBJ', 'tessellation'),
    Format('.ply', 'PLY', 'tessellation'),
    Format('.ply2', 'PLY2', 'tessellation'),
    Format('.off', 'OFF', 'tessellation'),
    Format('.msh', 'Gmsh mesh (any version)', 'mesh',
           'versions 2.2, 4.0 and 4.1; keeps physical groups'),
    Format('.med', 'MED', 'mesh', 'keeps its groups'),
    Format('.cgns', 'CGNS', 'mesh', 'keeps its zones as groups'),
    Format('.unv', 'I-DEAS UNV', 'mesh'),
    Format('.bdf', 'Nastran BDF', 'mesh'),
    Format('.mesh', 'Medit', 'mesh'),
    Format('.vtk', 'VTK (legacy)', 'mesh'),
    Format('.nii', 'NIfTI (Nii2mesh)', 'mesh',
           'an isosurface of the voxels, as a surface mesh'),
)
BY_EXTENSION = {item.extension: item for item in FORMATS}

REFUSED = {
    '.inp': 'this Gmsh build (4.15.2) writes Abaqus INP but has no reader for '
            'it, so the file would fall into the .geo parser and fail',
    '.vtu': 'VTK XML is not read by Gmsh; save the mesh as legacy .vtk',
    '.vtp': 'VTK XML is not read by Gmsh; save the surface as STL or legacy '
            '.vtk',
    '.dat': 'a .dat file is not one format; rename it to the format it holds',
}


#: Plan 42 SG4 (D11). The unit of the prepared model every project carries.
SHARED_UNIT = 'M'

#: Formats the shared import (Geometry step) reads. Gmsh's Open does not
#: take them; they come in through the shared import, and Merge still adds
#: any of them to the open model.
SHARED_EXTENSIONS = frozenset(
    {'.stl', '.obj', '.step', '.stp', '.iges', '.igs', '.brep'})


def open_extensions() -> tuple:
    """Extensions the Open mode takes: the formats only Gmsh reads."""
    return tuple(item.extension for item in FORMATS
                 if item.extension not in SHARED_EXTENSIONS)


def refuse_open(path, mode: str) -> None:
    """Open (replace the model) is for formats only Gmsh reads (D11)."""
    ext = extension(path)
    if mode == 'open' and ext in SHARED_EXTENSIONS:
        raise ImportRefusal(
            f'{ext} comes in through the Geometry step, not through Open: '
            'import it there, or choose Merge to add it to this model')


def extension(path) -> str:
    name = Path(str(path)).name.lower()
    if name.endswith('.nii.gz'):
        return '.nii'
    return Path(name).suffix


def file_filter(mode: str = 'merge') -> str:
    """The open-file dialog's filter string for *mode*."""
    names = open_extensions() if mode == 'open' else [
        item.extension for item in FORMATS]
    exts = ' '.join('*' + name for name in names)
    return 'Gmsh-readable files (%s);;All files (*)' % exts


def classify(path) -> Format:
    ext = extension(path)
    if ext in REFUSED:
        raise ImportRefusal(f'{ext} cannot be opened: {REFUSED[ext]}')
    item = BY_EXTENSION.get(ext)
    if item is None:
        raise ImportRefusal(
            f'{ext or "this file"} is not a format Gmsh reads; the formats '
            'are ' + ', '.join(sorted(BY_EXTENSION)))
    return item


@dataclass(frozen=True)
class OpenRequest:
    path: str
    mode: str                       # open | merge
    kind: str
    unit: str = ''
    scaling: float = 1.0
    classify: dict | None = None

    def kwargs(self) -> dict:
        out = {'mode': self.mode, 'unit': self.unit, 'scaling': self.scaling}
        if self.classify is not None:
            out['classify'] = dict(self.classify)
        return out


def reclassify_arguments(angle=40.0, boundary=True, reparametrize=True,
                         curve_angle=180.0, export_discrete=True,
                         create_geometry=True) -> dict:
    """Every argument of ``classifySurfaces`` (and the following
    ``createGeometry``), checked.  A ``curve_angle`` of 180 degrees means
    "do not split curves" (Gmsh's Pi).
    """
    try:
        angle = float(angle)
        curve_angle = float(curve_angle)
    except (TypeError, ValueError):
        raise ImportRefusal('the angles must be numbers of degrees') from None
    if not 0.0 < angle <= 180.0:
        raise ImportRefusal('the classification angle must be between 0 and '
                            '180 degrees')
    if not 0.0 < curve_angle <= 180.0:
        raise ImportRefusal('the curve angle must be between 0 and 180 '
                            'degrees (180 does not split curves)')
    return {'angle': angle, 'boundary': bool(boundary),
            'reparametrize': bool(reparametrize), 'curve_angle': curve_angle,
            'export_discrete': bool(export_discrete),
            'create_geometry': bool(create_geometry)}


def plan_open(path, mode: str = 'open', *, unit: str = '', scaling=1.0,
              classify_angle=None, **classify_options) -> OpenRequest:
    """Check an Open / Merge request and return what the session is sent.

    *classify_angle*, for a tessellation only, classifies the surface straight
    after the read with the other ``classify_options``
    (see :func:`reclassify_arguments`).
    """
    if mode not in ('open', 'merge'):
        raise ImportRefusal('choose Open (replace the model) or Merge (add '
                            'to it)')
    if not str(path).strip():
        raise ImportRefusal('choose a file')
    item = classify(path)
    if unit not in dict(UNITS):
        raise ImportRefusal(
            f'unit {unit!r} is not accepted by Open CASCADE here; the '
            'units are ' + ', '.join(repr(u) for u, _ in UNITS))
    try:
        scaling = float(scaling)
    except (TypeError, ValueError):
        raise ImportRefusal('the scale factor must be a number') from None
    if scaling <= 0.0:
        raise ImportRefusal('the scale factor must be positive')
    if item.kind != 'cad' and (unit or scaling != 1.0):
        raise ImportRefusal(
            f'{item.label} files have no unit to convert: the unit and the '
            'scale factor apply to STEP, IGES, BREP only. Scale the model '
            'after the import instead')
    if item.extension == '.xao' and (unit or scaling != 1.0):
        raise ImportRefusal('XAO is read through the script parser, which '
                            'has no unit or scale handle')
    classification = None
    if classify_angle is not None:
        if item.kind != 'tessellation':
            raise ImportRefusal(
                f'{item.label} is not a tessellation: classify it after the '
                'read with Reclassify')
        classification = reclassify_arguments(classify_angle,
                                              **classify_options)
    return OpenRequest(str(path), mode, item.kind, unit, scaling,
                       classification)


def scale_to_metres(unit: str, scaling: float = 1.0) -> float:
    """How many metres one model unit is after importing with *unit*."""
    return UNIT_FACTOR_TO_M[unit] / float(scaling)
