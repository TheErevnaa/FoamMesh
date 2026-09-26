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

### Polyhedral cells, and the two formats that cannot hold them

MSH and SU2 carry exactly four cell families — tetrahedra, hexahedra, prisms
and pyramids. Neither has a polyhedron element, so a mesh containing one
cannot be written in either format. This is a property of the formats, not a
limitation of this application.

snappyHexMesh produces polyhedra as a matter of course: they appear at every
2:1 refinement transition, which is what a castellated mesh is made of. Of the
sixty snappyHexMesh cases published with FoamMesh, **fifty-nine contain
polyhedral cells**. The sixtieth, `pipe_step`, is 4,374 pure hexahedra with no
refinement transition, and it exports to MSH perfectly well.

So the two pipelines reach different destinations, by design:

| Made by | Can be exported as |
|---|---|
| **Gmsh** | OpenFOAM, MSH, SU2, VTU, CGNS, Fluent |
| **snappyHexMesh** | OpenFOAM, VTU, Fluent — and MSH or SU2 only for a mesh that happens to hold no polyhedra |

Fluent's format is on the snappy row because it *does* have a polyhedron:
`foamMeshToFluent` writes element type 7 for any cell that is not a tet, hex,
pyramid or prism, warning that the result needs a polyhedral-capable Fluent
reader. OpenFOAM and VTU carry polyhedra natively.

The export dialog decides this **per case, by counting the mesh in front of
it**, not by asking which mesher ran. A snappy mesh with no polyhedra is
offered MSH and writes it. A mesh that holds them is refused before anything
is written, with the count and the formats that do keep polyhedra:

> a Gmsh mesh holds tetrahedra, hexahedra, prisms and pyramids only; 12246 of
> 70744 cells are polyhedral, and 17176 faces have five or more vertices.
> Export this case as OpenFOAM or VTU, which keep polyhedra.

FoamMesh does **not** decompose polyhedra on the way out. Decomposing them
would hand another solver a different mesh from the one `checkMesh` graded — a
different cell count, and in the dual-mesh case different points — and an
export is not the place to silently change what was meshed. If you need a
Gmsh-pipeline mesh for SU2, mesh it with Gmsh.

Arbitrary polyhedral cells may also not survive a `.cgns` round trip; hexes
and tets are the safe shapes. That export warns before writing when it
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

Unavailable dependencies are reported as capability errors, and so is a mesh
a format cannot hold: MSH and SU2 come back unavailable, with the polyhedron
count, for a mesh that carries any. Lossy formats produce warnings — but a
format that will refuse to write at all reports an error, never a warning,
because "may drop or split" describes a lossy export and no export happens.

## GUI, API, and CLI

- GUI: use the Export step or Case Tools export actions.
- API: execute `case.export.native`, `case.export.vtk`, `case.export.gmsh`,
  `case.export.su2`, or
  `case.export.cgns` through
  `POST /api/v1/cases/{case_id}/operations/{operation_id}:execute`.
- CLI: use `foammesh export <case> <format> [destination]`.

The 2D plane and wedge flows require the corresponding OpenFOAM capabilities.
