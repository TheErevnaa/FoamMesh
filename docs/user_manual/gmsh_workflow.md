# Meshing with Gmsh

Gmsh is FoamMesh's second meshing method, beside snappyHexMesh. It meshes
your geometry directly rather than carving a background grid, which suits
internal flow — pipes, manifolds, ducts, anything where the fluid volume is
already a closed solid or a closed surface.

It takes CAD (STEP, IGES, BREP) and tessellated surfaces (STL, OBJ) alike; the
two arrive by different routes and behave differently, which the
[Import](#import-stl-and-step) section sets out.

Choose it on the **Mesh setup** step. The workflow that appears is Gmsh's
own; Snappy's settings are kept untouched in case you switch back.

## Before you start

Both methods need the qualified **OpenFOAM Foundation 13** WSL runtime. Gmsh
runs inside the same distribution, so there is nothing separate to install
beyond Gmsh itself:

```bash
wsl -d OpenFOAM13Runtime -u root -- pip3 install gmsh
```

The Mesh setup step probes the runtime and reports what it found — the
Gmsh version and the distribution — or why it could not use it. The reasons are
specific: a missing distribution, a missing package, or a version below the
qualified minimum are each reported differently, because the fix differs.

## Preparation is a shared row

Gmsh imports the prepared geometry, and preparing it is **3. Preparation**,
the third shared row — for CAD and for STL alike. Every case walks that row
before any Gmsh row opens, and the press on it prepares the geometry and
grades it, so planning can no longer happen without it. Snappy takes the
healing defaults; on the Gmsh route you say which healing you want, in the
Advanced section of that row.

**Your CAD must declare its unit.** Gmsh honours the unit in the STEP file. A
part authored in millimetres but declared in metres imports a thousand times
too large, and every size you then set is meaningless. FoamMesh converts the
declared unit to metres on import and records the bounding box it actually
read; check it in the Advanced section of **3. Preparation** if a mesh comes
out absurdly coarse or fine.

An STL carries no unit at all, so the unit you declare in FoamMesh is the only
one there is.

## Import: STL and STEP

### What Gmsh does with a tessellated file

STL and OBJ are supported inputs, not a fallback. Gmsh's CAD importer reads CAD
only, so a tessellated surface takes a different path: the triangulation is
merged, duplicate nodes are removed, and the triangles are **classified into
surface patches** by dihedral angle — the *classification angle*, 40° unless you
change it. The classified patches are turned into a discrete geometry that
meshes like any other.

Three consequences worth knowing before you rely on it:

- **The surface must be closed.** Gmsh builds a volume from the classified
  shells. An STL with holes in it produces no volume and the run says so.
- **The outermost shell is the boundary; every other shell is a void.** Several
  surfaces in one job describe a domain with things cut out of it — a farfield
  box with a body inside it meshes the fluid, not the body. Shells are ordered
  by bounding-box volume.
- **Patch names come from the file's `solid` blocks.** Each `solid` in an STL
  arrives as its own surface, and the classified pieces are traced back to the
  solid they were cut from, so the pieces keep that name. A piece that cannot be
  traced keeps a generated `face_N` name and the run warns about it.

CAD and tessellated inputs **cannot be mixed in one job**. All CAD or all
surfaces; the run refuses the mixture rather than importing half of it.

Multi-volume assemblies come in as one zone per body. Boundary layers are not
available on them — see below.

### STL and STEP, on both engines

The same file does not mean the same thing to both meshers. This is what each
engine does today:

| Concern | STL to snappy | STL to Gmsh | STEP to snappy | STEP to Gmsh |
|---|---|---|---|---|
| File meshed | `.surface.stl` | primary STL | tessellated `rev1.stl` at fixed deflection | the STEP |
| Prepare needed | no (auto `as_is`) | yes (explicit) | no | yes |
| Domain | fluid seed | outer shell | fluid seed | CAD solids |
| Regions / zones | user volumes | one zone per body | user volumes | one zone per body |
| Feature-angle split | offered | offered | refused | n/a (faces kept) |
| Unit | declared, applied | declared, applied | declared, applied to STL only | raw file units |
| Deflection control | none | n/a | none (`CadPanel` unmounted) | n/a |
| Patch names | STL solids / `face<N>` | `face_N` | `face<N>` | CAD face tags |
| Interfaces | `interface_pairs` inert | inert | inert | inert |

Read the last row plainly: **interface pairs are recorded and not written by
either engine**, on either input. Do not rely on them for a conjugate case yet.

IGES and BREP follow the STEP column, and OBJ follows STL without the `solid`
grouping. None of those three has been tested end to end here.

## The workflow

The outline lists twelve rows. Rows 1 to 3 — **Geometry**, **Mesh setup** and
**Preparation** — are shared with Snappy, and the questions Gmsh asks about
how your CAD is read are in the Advanced section of **3. Preparation**. Rows 4
to 12 are the engine rows, and are the sections below. The forward button at
the bottom of the window carries the label of the row you are standing on:

| Row | Forward button says |
| --- | --- |
| 4. Global sizing | Proceed |
| 5. Size fields | Proceed |
| 6. Curve controls | Proceed |
| 7. Volume controls | Proceed |
| 8. Boundary layers | Proceed |
| 9. Periodic pairs | Proceed |
| 10. Generate mesh | Generate & Proceed |
| 11. Quality | Check & Proceed |
| 12. Export | Export mesh |

**Generate & Proceed** runs Gmsh, measures native fidelity and publishes the
mesh — three tasks, one press. **Check & Proceed** runs `checkMesh`, geometry
fidelity, resolution and the summary. When one of those sub-steps fails, the
row stops there and the status line names the sub-step that failed.

**Size Fields**, **Curve Controls**, **Volume Controls**, **Boundary Layers**
and **Periodic Pairs** are optional. Proceed on one you left empty records a
skip and says so, rather than running nothing and reporting success.

Running every remaining stage in one press is **Run to end**, on the Gmsh
heading row, and only there.

### Preparation: how your CAD is read

These settings are in the Advanced section of **3. Preparation**, the third
shared row, rather than on a row of their own.

Import healing. Both healing options are **off by default**, and that is
deliberate:

- **Sew faces** converts a closed solid into a loose set of faces. Meshing then
  produces a surface with no cells and reports no error. Turn it on only for an
  import that genuinely arrives as disconnected faces.
- **Fix degenerate edges** removes the seam at a sphere's poles or a cone's
  apex, after which the surface cannot be meshed at all.
- **Rebuild solids** reconstructs a solid from a closed shell. This is the one
  to reach for when your CAD is a surface model rather than a solid: sewing
  alone leaves zero volumes and the mesh comes back empty, while sewing **plus**
  rebuild solids recovers the volume and meshes normally.

Enabling sewing without rebuild solids shows a warning saying so.

### Global Sizing

Target and minimum element size, a size factor, and curvature adaptation.
Leaving the target at zero derives it from the bounding-box diagonal, and the
run says so rather than silently substituting a number.

**Cell shape** is tetrahedral by default. Choosing hexahedral subdivides the
mesh into hexahedra: measured on four geometries every one passes `checkMesh`,
at roughly four times the cell count and higher non-orthogonality (74–82
against 43–67 for tetrahedra). Worth it when your solver or discretisation
prefers hexes; not worth it otherwise.

Gmsh's 3D *recombination* is deliberately not offered. It produces
tetrahedron/pyramid meshes that Gmsh reports as sound — zero inverted elements
— and OpenFOAM rejects: negative cell volumes, open cells, and
non-orthogonality above 145° on every geometry tried.

The **volume algorithm** defaults to Delaunay. HXT is roughly twice as fast and
is the threaded one; Delaunay gives slightly better quality. Note that thread
count changes the mesh with HXT, not just the wall clock, because HXT
partitions the domain by thread.

### Size Fields

Spatial refinement, and the main reason to reach for Gmsh. A field is either

- a **distance threshold** — refine near a chosen surface, ramping from
  `sizeInside` at `distanceMin` to `sizeOutside` at `distanceMax`; or
- an **analytic region** — a box, ball, cylinder or frustum positioned by its
  own coordinates, needing no surface scope; or
- a **math expression** — `math_eval`, an expression in `x`, `y` and `z` that
  sets the size everywhere. `0.002 + 0.05*sqrt(x*x + y*y)` refines towards the
  axis of a pipe; `0.004 + 0.1*abs(z - 0.2)` refines around a plane.

Fields combine as a minimum: at any point the finest requested size wins. Add
as many as you like.

An expression is checked before a run starts, in two ways. Every name in it
must be one Gmsh knows — `x`, `y`, `z` and the usual functions — so a typo is
caught rather than discovered mid-mesh. And the size it produces is sampled
across your geometry and turned into an element estimate: an expression that
asks for tens of millions of cells, or one that goes to zero or negative
anywhere, is refused with the number rather than attempted.

### Curve Controls

Fix the node count along the boundary curves of a face group (transfinite), or
set a local element size there. Transfinite curves are how you get a structured
node distribution along an edge.

### Volume Controls

Per-volume sizing and region typing for multi-volume CAD. Fluid and solid
regions publish as OpenFOAM cell zones; dead volumes are left out of the mesh.

### Boundary Layers

Prism layers grown into the volume from the wall.

Specify the stack either as a first height with a growth ratio, or as a total
thickness with a layer count. After a run the **achieved** first-layer height is
measured from the mesh and shown beside the requested one.

**Choose the patches that get layers.** Naming none means every boundary
surface, which is the original behaviour and rarely what you want: prisms on an
inlet and an outlet are wrong for every flow case, and on a measured venturi the
worst elements in the finished mesh were exactly the stacks sitting on the inlet
and outlet planes. Name the walls and leave the caps out.

How it works, because the shape of the geometry decides whether it can:

Gmsh grows layers by extruding the chosen surfaces inwards, deleting the
original volume, and rebuilding the core from the layer's inner faces. With only
some surfaces extruded, that core is left with an opening where each un-layered
patch used to be, and FoamMesh closes each one with a flat face built on the rim
the extrusion left behind. Measured on a 0.1 × 0.1 × 0.6 m duct with layers on
the four walls only: 14,199 tets plus 16,712 prisms, a meshed volume of
0.005999988 m³ against an analytic 0.006, and not one prism rooted on either cap
— against 388 of them when every surface is extruded.

That rebuild sets two conditions, and a run that breaks either is **refused with
the reason** rather than published:

- **The layer has to surround the patches without one.** Layers on the walls
  with bare caps works; layers on the inlet with bare walls does not — the
  opening left behind loops around the tube instead of standing in for a patch,
  and the face built across it would cut through the domain.
- **Each un-layered patch has to be flat.** The opening can only be closed with
  a planar face, so a curved patch left without a layer is refused, with the
  out-of-plane distance quoted.

Two further limits:

- **Single volume only.** On a multi-volume assembly the core rebuild cannot
  close either side of a shared interface, so layers are refused outright with
  that as the reason. Mesh it without layers, or one volume per job.
- **A name that matches nothing is refused.** Patch names that match no
  imported surface produce a warning naming them, and the layer still grows on
  the ones that did match. If *none* of them match there is nothing left to
  grow on, and the run is refused rather than published as a mesh with no
  near-wall resolution anywhere. The message names both the patches that were
  asked for and the surfaces this run imported, which is where the mismatch
  usually shows: a tessellated file carrying no patch names of its own arrives
  as `face_1`, `face_2`, … , and no name taken from the model will match those.

If a rebuild is refused and you cannot restructure the geometry, snappyHexMesh
grows layers on any selection of patches without this constraint.

### Periodic Pairs

Translational or rotational periodicity, published as matched OpenFOAM `cyclic`
patches with the transform attached.

The transform maps the **master** surface onto the **slave**. Pointing it the
wrong way is the usual cause of *"no corresponding point"* from Gmsh; the error
message says so.

### Generate mesh, Publish and QA

**Generate mesh** runs Gmsh in the qualified runtime as a separate
process — Gmsh aborts on some malformed geometry, and an abort must not take
the application with it. Progress streams while it runs.

Every control is recorded as requested-against-achieved. Options are read back
from Gmsh after being set, so a value the mesher declined shows as a mismatch
rather than as a success. The quality figures are measured from the finished
mesh, never read back from what was asked for.

If the achieved element quality falls below the **minimum quality** you set,
the mesh does not publish. You can accept it anyway, and that acceptance is
recorded on the run.

**Publish** writes `constant/polyMesh` directly. Patch types come from the
boundary categories on your prepared geometry, so a Gmsh mesh and a Snappy mesh
of the same part type their patches identically.

**Quality** runs OpenFOAM 13 `checkMesh` — the same OpenFOAM that produced the
mesh judging it.

## Exporting

`.su2` is available for SU2, alongside OpenFOAM, VTK, Gmsh and CGNS. SU2 export
needs no extra package and works for Snappy meshes too. Boundary patches become
SU2 markers. When the run itself wrote `mesh.su2`, that file is the export: it
is copied out rather than re-derived, so what you hand to the solver is the
mesh the run reported.

Both exporters represent **first-order** elements. The mesher writes order 1
and 2, and second order is reachable with no target selected; an order-2 export
is refused with the reason rather than quietly downgraded. See
[export formats](export_formats.md).

## When to use which method

| | Gmsh | snappyHexMesh |
|---|---|---|
| geometry | closed CAD solids or closed STL/OBJ | CAD or dirty surface data |
| input preparation | the healing is yours to choose on **3. Preparation** | the defaults are taken for you |
| cells | tetrahedra, prisms | hex-dominant |
| refinement | composable size fields | surface and volume levels |
| boundary layers | per patch, if the layer surrounds the rest and those are flat | per patch, unconditionally |
| multi-volume + layers | refused | supported |
| structured edges | transfinite curves | — |
| periodicity | periodic pairs | — |
| speed | seconds | minutes |

Snappy remains the right choice for external aerodynamics, for surface data that
is not watertight, for multi-region assemblies that need layers, and whenever
the patches you want layers on do not enclose the ones you do not. Gmsh is the
faster route for internal flow in a clean closed domain — CAD or STL — and the
only one with spatial size fields.

## Troubleshooting

**"the import produced N surfaces and no volumes"** — the CAD arrived without
solids. If Sew faces is on, either turn it off (it converts solids into face
sets) or turn on **Rebuild solids** as well, which recovers a solid from the
sewn shell. If the CAD is genuinely a surface model, Rebuild solids is the
setting you want.

**"Impossible to mesh periodic surface"** — turn Fix degenerate edges off. It
strips the seam that spheres and cones need.

**"the boundary layer leaves an opening that does not lie on any of the patches
without one"** — the layer does not surround the patches you left bare. Add the
patches around them to the layer selection, or turn layers off.

**"the patches left without a boundary layer … span N m out of any one plane"** —
a patch without a layer is curved, and the opening the layer leaves can only be
closed flat. Grow the layer on it too.

**"boundary layers are not supported on an N-volume assembly"** — the core
rebuild cannot close a shared interface. Mesh without layers, or split the
geometry into one job per volume.

**"… matched no imported surface"** — none of the patches picked for boundary
layers exists in this geometry, so there was nothing to grow a layer on. The
message lists the surfaces the run did import; pick from those on the Boundary
Layers page, or turn layers off. A tessellated import with no patch names of
its own arrives as `face_1`, `face_2`, … , which is the usual cause.

**"CAD and tessellated geometry cannot be mixed in one job"** — a job is all
STEP/IGES/BREP or all STL/OBJ. Convert the odd one out.

**"the tessellated import produced no surfaces to mesh"** — the STL did not
classify into patches. Check the classification angle, and check the file is a
surface rather than an empty or malformed one.

**A mesh far coarser or finer than expected** — check the bounding box
reported in the Advanced section of **3. Preparation**. It is almost always a
CAD file whose declared unit does not match its numbers.

**"no corresponding point" on a periodic pair** — the transform is mapping
slave onto master. Reverse it.

**"asks for roughly N elements"** — the target size is too small for the
domain. Raise it and refine locally with a size field instead.

**"unknown name(s) in the expression"** — a `math_eval` field uses something
Gmsh's parser does not have. The message lists what is available.

**"the expression is zero, negative or undefined at N points"** — an element
size has to be positive everywhere in the domain, not just where you were
thinking. `x` alone goes negative wherever x does; `0.01 + 0.05*abs(x)` does
not.
