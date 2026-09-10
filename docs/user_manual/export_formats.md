# Export Formats

FoamMesh exports to neutral formats without a product-specific path. What
leaves the case depends on which mesher made the mesh and what it was aimed
at, so this page starts with that distinction rather than with a list.

## Native, derived, exported

Every mesh artifact a case holds is one of three things, and the run records
which:

| Role | What it is | Where it comes from |
|---|---|---|
| **native** | the file the mesher itself wrote | snappyHexMesh writes `constant/polyMesh`; a Gmsh run writes `mesh.msh` in its own run directory, and `mesh.su2` as well when the target is SU2 |
| **visualization-derivative** | something made from the native artifact so it can be drawn or counted | a `polyMesh` staged under a run directory for the viewport; never published over the case's accepted mesh |
| **exported** | something made from the native artifact for another program | everything on this page |

Two consequences are worth knowing before you look for a file:

- **An accepted Gmsh run does not always publish a `constant/polyMesh`.** A
  polyMesh is published for the OpenFOAM target, and for a run with no target
  chosen at all — so the viewport and `checkMesh` have something to open. An
  SU2 run reads the file Gmsh wrote, so nothing is published and the `.msh`
  (with the `.su2` beside it) is permanently that run's result. A case in that
  state is finished, not broken.
- **A refused run keeps its mesh.** The candidate stays in its own run
  directory and is drawn from there. It is never published over an accepted
  mesh in order to be looked at.

## The formats

| Format | Suffix | What it carries | Element order | Needs |
|---|---|---|---|---|
| OpenFOAM case (native) | directory | `constant/polyMesh` plus the case files | first | — |
| Gmsh `.msh` | `.msh` | MSH **2.2** — the only version FoamMesh's own readers parse | first, second | `[export]` extra for a re-derived export; a Gmsh run's own file needs nothing |
| SU2 `.su2` | `.su2` | the `mesh.su2` the Gmsh run wrote, copied and re-counted; boundary patches become markers | first | — |
| VTK `.vtu` | `.vtu` | visualization and diagnostics | first | — |
| CGNS `.cgns` | `.cgns` | neutral CFD exchange | first | a VTK build with `vtkCGNSWriter` |
| Fluent `.msh` | `.msh` | `foamMeshToFluent` output, with that utility's documented zone limitations | first | `foamMeshToFluent` |
| Case archive | `.zip` | the case, with a manifest and checksums | — | — |

Arbitrary polyhedral cells may not survive a `.msh` or `.cgns` round trip;
hexes and tets are the safe shapes. The export warns before writing when that
applies.

### Why the SU2 export copies rather than rebuilds

When a Gmsh run wrote `mesh.su2`, that file is the export: it is copied out and
its elements are counted from the copy. Re-deriving an SU2 file from the
published polyMesh would export a mesh that had been through two conversions
and could differ from the one the run reported. The built-in writer remains as
the fallback for a case whose mesh did not come from Gmsh.

## Element order

Meshing at second order and exporting at second order are two different
capabilities, and FoamMesh keeps them apart:

| | Orders |
|---|---|
| What Gmsh meshes, and this application reads back and counts | 1, 2 |
| What the OpenFOAM exporter represents | 1 |
| What the SU2 exporter represents | 1 |

Selecting a target solver therefore constrains the order. The Element Order
control greys the order its target cannot carry and states the reason:
OpenFOAM 13 reads first-order MSH 2.2 and nothing else, through `gmshToFoam`
and through the direct polyMesh publisher alike; FoamMesh's own SU2 reader and
census hold the linear type codes only, so a quadratic `.su2` is one this
application would write and then be unable to open or count.

To mesh at second order, clear the target solver. The run keeps its native
`.msh`, which is read, counted and summarised normally; no polyMesh is
published and no solver file is written. Qualifying an exporter for second
order is separate work — see the follow-on packages in
[the capability matrix](capability_matrix.md).

## Readiness

Unavailable dependencies are reported as capability errors. Lossy formats
produce warnings, including an additional warning when polyhedral cells may be
dropped or split.

## GUI, API, and CLI

- GUI: use the Export step or Case Tools export actions.
- API: execute `case.export.native`, `case.export.vtk`, `case.export.gmsh`,
  `case.export.su2`, or
  `case.export.cgns` through
  `POST /api/v1/cases/{case_id}/operations/{operation_id}:execute`.
- CLI: use `foammesh export <case> <format> [destination]`.

The 2D plane and wedge flows require the corresponding OpenFOAM capabilities.
