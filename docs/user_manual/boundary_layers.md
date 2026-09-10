# Boundary Layers & y+

## Shrink algorithm

snappyHexMesh supports two layer-shrink algorithms; FoamMesh exposes **both**:

- `displacementMedialAxis` — robust default.
- `displacementMotionSolver` — better for complex thin gaps.

## y+ / first-cell-height calculator

Given freestream velocity `U`, reference length `L`, kinematic viscosity `ν`, and
a target `y+`, FoamMesh estimates the wall-adjacent cell height:

```
Re   = U·L/ν
Cf   = 0.026 / Re^(1/7)         (turbulent flat-plate)
τ_w  = Cf·½·ρ·U²
u_τ  = sqrt(τ_w/ρ)
y    = y+ · ν / u_τ
```

Use the result as the first-layer thickness (≈ 2·y for the cell-centre distance).
The calculator round-trips: it also reports the y+ implied by a given height.

## Layer thickness math

- total stack: `first · (r^n − 1)/(r − 1)` (geometric, ratio `r`, `n` layers)
- final layer: `first · r^(n−1)`
- the expansion ratio for a desired (first, overall, n) is solved numerically.

## Per-patch control

Set number of layers, thickness model, and expansion ratio per wall patch. The
generated `snappyHexMeshDict` `addLayersControls` reflects the choices.

## Limits

These are the boundaries of what the two pipelines will do with layers. Each
is a refusal with a reason, not a silent downgrade.

### snappyHexMesh

- **`relativeSizes` is global.** OpenFOAM Foundation 13 applies it to the whole
  `addLayersControls` block, so every *active* layer group in a case must agree
  on it. A case whose groups disagree is refused when the dictionary is built —
  "Foundation 13 applies relativeSizes globally; all active layer groups must
  use the same relativeSizes value" — rather than written out with one group's
  choice quietly imposed on the others. Per-patch `relativeSizes` is not
  written; it is listed as a follow-on in the
  [capability matrix](capability_matrix.md).
- **Patches are named literally.** FoamMesh writes one `layers` entry per patch
  under its own name (a slave side as `<name>_slave`). Regular-expression patch
  entries, which snappy itself accepts, are not authored here.
- **A group mentioned with zero layers is frozen, not dropped**: it is written
  as `nSurfaceLayers 0`, so the dictionary says what was decided about it.

### Gmsh

- Layers are a real extrusion: the imported solid is removed and the core
  volume is rebuilt from the layer's inner surfaces. Stacking prisms on an
  unchanged tet mesh was measured to add 8.75% volume while every quality
  metric still looked healthy, which is why it is not done that way.
- **Scope is the whole boundary, or the patches you name.** Naming patches
  requires each skipped surface to be deleted and rebuilt from the extrusion's
  inner rim; measured live on Gmsh 4.15.2 with a 0.1 x 0.1 x 0.6 m duct, the
  whole boundary gives 392 prisms rooted on the inlet and outlet planes, and
  walls-only gives zero cap prisms at the same 0.006 m³.
- **Multi-volume assemblies get no layers at all.** A layer is carved out of a
  single core volume, so a shared interface would leave neither side closed;
  the run is refused rather than producing a shell.
- Thicknesses are cumulative and grow inward.

## Diagnosing failures

After meshing, the **layer coverage report** (parsed from the snappy log) shows
achieved vs. requested layers and coverage % per patch, and lists patches where
layers failed (common causes: sharp angles, thin gaps, excessive thickness ratio).
