"""Authoritative import/export format capability metadata.

The desktop chooser, facade, converter services, and export readiness checks
all consume this registry.  Keeping the complete U4 contract here prevents
format labels, extensions, maturity, and capability requirements from drifting
between GUI-specific tables and service-specific enums.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FormatKind(str, Enum):
    GEOMETRY = 'geometry'
    MESH = 'mesh'


class FormatMaturity(str, Enum):
    STABLE = 'stable'
    EXPERIMENTAL = 'experimental'


class FormatDirection(str, Enum):
    IMPORT = 'import'
    EXPORT = 'export'


@dataclass(frozen=True)
class FormatSpec:
    id: str
    display_name: str
    extensions: tuple[str, ...]
    kind: FormatKind
    provider: str
    capability: str
    maturity: FormatMaturity
    mutates_case: bool
    importer: str
    direction: FormatDirection
    notes: str = ''

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'display_name': self.display_name,
            'extensions': list(self.extensions),
            'kind': self.kind.value,
            'provider': self.provider,
            'capability': self.capability,
            'maturity': self.maturity.value,
            'mutates_case': self.mutates_case,
            'importer': self.importer,
            'direction': self.direction.value,
            'notes': self.notes,
        }


_SPECS = (
    # Geometry import
    FormatSpec('geometry.stl.import', 'STL surface', ('.stl',), FormatKind.GEOMETRY,
               'FoamMesh core', 'core', FormatMaturity.STABLE, False,
               'GeometryArtifactStore.import_file', FormatDirection.IMPORT),
    FormatSpec('geometry.obj.import', 'Wavefront OBJ surface', ('.obj',), FormatKind.GEOMETRY,
               'FoamMesh core', 'core', FormatMaturity.STABLE, False,
               'GeometryArtifactStore.import_file', FormatDirection.IMPORT),
    FormatSpec('geometry.step.import', 'STEP CAD', ('.step', '.stp'), FormatKind.GEOMETRY,
               'Open CASCADE', 'python:OCC', FormatMaturity.EXPERIMENTAL, False,
               'foammesh.core.geometry.cad.read_cad', FormatDirection.IMPORT),
    FormatSpec('geometry.iges.import', 'IGES CAD', ('.iges', '.igs'), FormatKind.GEOMETRY,
               'Open CASCADE', 'python:OCC', FormatMaturity.EXPERIMENTAL, False,
               'foammesh.core.geometry.cad.read_cad', FormatDirection.IMPORT),
    FormatSpec('geometry.brep.import', 'Open CASCADE BREP', ('.brep',), FormatKind.GEOMETRY,
               'Open CASCADE', 'python:OCC', FormatMaturity.EXPERIMENTAL, False,
               'foammesh.core.geometry.cad.read_cad', FormatDirection.IMPORT),

    # Mesh import.  Converter-backed formats remain experimental until their
    # live fixture/round-trip evidence is recorded on the target host.
    FormatSpec('mesh.native.import', 'Native OpenFOAM case/polyMesh directory', (),
               FormatKind.MESH, 'FoamMesh core', 'core', FormatMaturity.STABLE, True,
               'NativeMeshImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.fluent.import', 'Fluent mesh (.msh)', ('.msh',), FormatKind.MESH,
               'OpenFOAM', 'utility:fluentMeshToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.gmsh.import', 'Gmsh mesh (.msh)', ('.msh',), FormatKind.MESH,
               'OpenFOAM', 'utility:gmshToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.gambit.import', 'GAMBIT neutral (.neu)', ('.neu',), FormatKind.MESH,
               'OpenFOAM', 'utility:gambitToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.ideas_unv.import', 'I-DEAS Universal (.unv)', ('.unv',), FormatKind.MESH,
               'OpenFOAM', 'utility:ideasUnvToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.ansys.import', 'ANSYS mesh (.ans)', ('.ans',), FormatKind.MESH,
               'OpenFOAM', 'utility:ansysToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.cfx4.import', 'CFX4 mesh (.geo)', ('.geo',), FormatKind.MESH,
               'OpenFOAM', 'utility:cfx4ToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT),
    FormatSpec('mesh.star_cd.import', 'STAR-CD / PROSTAR', ('.vrt', '.cel', '.inp'),
               FormatKind.MESH, 'OpenFOAM', 'utility:star3ToFoam',
               FormatMaturity.EXPERIMENTAL, True, 'ConverterImportService',
               FormatDirection.IMPORT),
    FormatSpec('mesh.plot3d.import', 'Plot3D mesh', ('.xyz', '.p3d', '.q'), FormatKind.MESH,
               'OpenFOAM', 'utility:plot3dToFoam', FormatMaturity.EXPERIMENTAL, True,
               'ConverterImportService', FormatDirection.IMPORT,
               'Exact Plot3D dialect support is determined by the configured utility.'),

    # Mesh/case export
    FormatSpec('mesh.openfoam.export', 'OpenFOAM case (native)', (), FormatKind.MESH,
               'FoamMesh core', 'core', FormatMaturity.STABLE, False,
               'ImportExportService.export_native_case', FormatDirection.EXPORT,
               'Native constant/polyMesh plus case directory.'),
    FormatSpec('mesh.vtk.export', 'VTK unstructured grid (.vtu)', ('.vtu',), FormatKind.MESH,
               'VTK', 'core', FormatMaturity.STABLE, False,
               'ImportExportService.export_vtu', FormatDirection.EXPORT,
               'VTK unstructured grid for visualization and diagnostics.'),
    FormatSpec('mesh.gmsh.export', 'Gmsh mesh (.msh)', ('.msh',), FormatKind.MESH,
               'Gmsh', 'python:gmsh', FormatMaturity.EXPERIMENTAL, False,
               'ImportExportService.export_gmsh', FormatDirection.EXPORT,
               'Arbitrary polyhedral cells may not round-trip; hex/tet recommended.'),
    FormatSpec('mesh.cgns.export', 'CGNS (.cgns)', ('.cgns',), FormatKind.MESH,
               'CGNS', 'python:h5py', FormatMaturity.EXPERIMENTAL, False,
               'ImportExportService.export_cgns', FormatDirection.EXPORT,
               'Neutral CFD exchange format.'),
    FormatSpec('mesh.med.export', 'MED mesh (.med), for SALOME and Code_Aster',
               ('.med',), FormatKind.MESH,
               'Gmsh', 'python:gmsh', FormatMaturity.EXPERIMENTAL, False,
               'ImportExportService.export_med', FormatDirection.EXPORT,
               'Written by the Gmsh writer from the mesh the accepted run '
               'produced. Measured: node '
               'and element counts and every physical group name survive a '
               'read-back, but the names come back padded to 80 characters, so '
               'a reader must strip them before comparing.'),
    FormatSpec('mesh.cgns.gmsh.export', 'CGNS (.cgns) via Gmsh', ('.cgns',),
               FormatKind.MESH, 'Gmsh', 'python:gmsh',
               FormatMaturity.EXPERIMENTAL, False,
               'ImportExportService.export_cgns', FormatDirection.EXPORT,
               'The same file as mesh.cgns.export, written by Gmsh instead of '
               'VTK, so it is available wherever meshing is rather than only '
               'where a VTK CGNS module is installed. Measured: counts and '
               'group names round-trip exactly.'),
    # Plan 31 FC-F. UNV rounds out the trio the Gmsh writer was measured on.
    # The import side of this registry also declares `mesh.ideas_unv.import`
    # through `ideasUnvToFoam`; that round trip was not measured here, so the
    # claim below stays limited to what the export was measured to do.
    FormatSpec('mesh.unv.export', 'I-DEAS Universal mesh (.unv)', ('.unv',),
               FormatKind.MESH, 'Gmsh', 'python:gmsh',
               FormatMaturity.EXPERIMENTAL, False,
               'ImportExportService.export_unv', FormatDirection.EXPORT,
               'Written by the Gmsh writer from the mesh the accepted run '
               'produced. Measured: node and element counts and every '
               'physical group name round-trip exactly.'),
    FormatSpec('mesh.su2.export', 'SU2 native mesh (.su2)', ('.su2',), FormatKind.MESH,
               'SU2', 'core', FormatMaturity.STABLE, True,
               'ImportExportService.export_su2', FormatDirection.EXPORT,
               'Written from the canonical mesh; tets, hexes, prisms and '
               'pyramids all round-trip, and every boundary patch becomes a '
               'marker.'),
    FormatSpec('mesh.openfoam_format.export', 'OpenFOAM ASCII/binary conversion (in place)', (),
               FormatKind.MESH, 'OpenFOAM', 'utility:foamFormatConvert',
               FormatMaturity.STABLE, True, 'FoamFormatConvertService',
               FormatDirection.EXPORT,
               'Rewrites case IO objects using controlDict write settings.'),
    FormatSpec('mesh.fluent.export', 'Fluent mesh (.msh) via foamMeshToFluent', ('.msh',),
               FormatKind.MESH, 'OpenFOAM', 'utility:foamMeshToFluent',
               FormatMaturity.STABLE, False, 'FluentMeshExportService',
               FormatDirection.EXPORT,
               'Writes fluentInterface/<case>.msh; documented Fluent limitations apply.'),
)

FORMAT_REGISTRY: dict[str, FormatSpec] = {spec.id: spec for spec in _SPECS}


def format_spec(format_id: str) -> FormatSpec:
    return FORMAT_REGISTRY[format_id]


def list_format_specs(*, kind: FormatKind | str | None = None,
                      direction: FormatDirection | str | None = None) -> tuple[FormatSpec, ...]:
    kind_value = FormatKind(kind) if kind is not None else None
    direction_value = FormatDirection(direction) if direction is not None else None
    return tuple(spec for spec in _SPECS
                 if (kind_value is None or spec.kind is kind_value)
                 and (direction_value is None or spec.direction is direction_value))


def converter_spec(converter_id: str) -> FormatSpec:
    return format_spec(f'mesh.{converter_id}.import')


def geometry_suffix_formats() -> dict[str, str]:
    """Map each CAD import suffix to its canonical format name (``.step`` → ``step``).

    Derived from the registry so CAD suffix detection cannot drift from the
    authoritative format list.
    """
    mapping: dict[str, str] = {}
    for spec in list_format_specs(kind=FormatKind.GEOMETRY, direction=FormatDirection.IMPORT):
        if spec.capability != 'python:OCC':
            continue
        name = spec.id.split('.')[1]  # geometry.<name>.import
        for extension in spec.extensions:
            mapping[extension] = name
    return mapping


def core_geometry_suffixes() -> tuple[str, ...]:
    """The surface-import suffixes handled by the built-in (non-CAD) importer."""
    suffixes: list[str] = []
    for spec in list_format_specs(kind=FormatKind.GEOMETRY, direction=FormatDirection.IMPORT):
        if spec.capability == 'core':
            suffixes.extend(spec.extensions)
    return tuple(dict.fromkeys(suffixes))
