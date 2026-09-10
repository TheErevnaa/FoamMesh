# Supported Formats

FoamMesh keeps its format lists **truthful for your configured environment**:
every format stays visible in the UI, and anything unavailable is disabled
with the exact reason (a missing utility or Python extra). Nothing
unavailable is ever presented as runnable. `foammesh formats` prints the
same matrix as JSON.

## Geometry import (File → Load Model → Geometry)

| Format | Provider | Availability |
|---|---|---|
| STL (ASCII and binary) | core importer | always |
| OBJ | core importer | always |
| STEP/STP | OpenCASCADE | `[cad]` extra |
| IGES/IGS | OpenCASCADE | `[cad]` extra |
| BREP | OpenCASCADE | `[cad]` extra |

### What is read, and what is not

- **ASCII STL** carries its `solid` names, and each named block becomes a
  surface. **Binary STL carries no names at all**: its pieces are worked out
  from triangle connectivity and named `solid_<index>`, so two disconnected
  cubes in one binary file import as two shells rather than one boundary.
- **OBJ** groups are read the same way; like a binary STL's shells they exist
  in memory and are re-derived on load rather than stored.
- **CAD** is imported as the OCCT shape and tessellated under your control;
  see [CAD import](cad_import.md). Only a surface that can bound a volume can
  become a domain — one that does not close is refused with the reason instead
  of being meshed into a solid that does not exist.
- A tessellated import carries no CAD corners and no OCC solid, so the
  structured (transfinite) Gmsh surfaces and the boolean farfield box are
  refused on it with the reason, rather than attempted and half-applied.

## Mesh import (File → Load Model → Mesh)

| Format | Utility | Notes |
|---|---|---|
| Native `polyMesh` case | — (validated staged copy) | Asks **Replace Current Mesh** (recovery-backed) or **Import into New Case Copy** (current case untouched) |
| Fluent `.msh` | `fluentMeshToFoam` | ASCII input only; conversion warnings are surfaced after import |
| Gmsh `.msh` | `gmshToFoam` | **MSH 2.2 only.** OpenFOAM 13's `gmshToFoam` cannot read 4.1, and this application's own MSH readers — the polyMesh publisher, the element census and the geometry-fidelity check — parse 2.2 as well. A 4.1 file is not partially read: the census takes its block-count header for an entity count and reports no volume elements at all |
| GAMBIT `.neu` | `gambitToFoam` | |
| I-DEAS Universal `.unv` | `ideasUnvToFoam` | distinct from ANSYS |
| ANSYS `.ans` | `ansysToFoam` | distinct from I-DEAS |
| CFX4 `.geo` | `cfx4ToFoam` | legacy |
| STAR-CD / PROSTAR | `star3ToFoam` | legacy |

Converter imports are staged and recoverable: the previous mesh is
snapshotted, the produced mesh is validated, and provenance (source file,
SHA-256 checksum, converter, log) is committed atomically. A failed or
cancelled import restores the prior mesh.

A case written by an older FoamMesh is not brought in through these
converters. It is adopted, which is a different operation with its own
refusals — see [legacy case migration](legacy_case_migration.md).

## Export (Case Tools → Export)

| Format | Maturity | Provider | Validation |
|---|---|---|---|
| OpenFOAM case (native) | stable | staged case copy | case validation + atomic copy |
| VTK `.vtu` | stable | built-in VTK writer | written file re-read with an independent reader; point/cell counts must match |
| SU2 `.su2` | stable | the `mesh.su2` a Gmsh run wrote, or the built-in writer for a mesh from elsewhere | elements re-counted from the written file; first order only |
| OpenFOAM ASCII/binary (in place) | stable | `foamFormatConvert` | recovery-backed; `controlDict` write settings are restored afterwards unless you choose **Keep these write settings** |
| Fluent `.msh` | stable | `foamMeshToFluent` | expected-output validation; documented Fluent zone/boundary limitations are shown |
| Gmsh `.msh` | **experimental** | `[export]` extra (gmsh) | arbitrary polyhedral cells may not round-trip; hex/tet recommended |
| CGNS `.cgns` | **experimental** | VTK CGNS writer | requires a VTK build with `vtkCGNSWriter` |
| Case archive `.zip` | stable | built-in | manifest + checksums |

Which of these is the *result* of a run and which is derived from it is set
out in [export formats](export_formats.md), together with the element orders
each exporter can carry.

Experimental formats are labelled in the export dialog and warn before
writing. Every completed export shows the exact output path, size, and
warnings, offers **Open Folder**, and is recorded in the case's transaction
history.

Headless equivalents: `foammesh export <case> <format> [dest]` and the
matching semantic `case.export.*` operation through `/api/v1`.
