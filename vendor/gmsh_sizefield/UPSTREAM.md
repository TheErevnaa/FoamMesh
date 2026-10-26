# foammesh-sizefield: what is lifted from Gmsh, and what changed

`foammesh-sizefield` is Gmsh's automatic mesh size field (Arthur Bawin's
`AutomaticMeshSizeField`: curvature, thin-region feature size, gradation on a
p4est octree) built as a stand-alone Linux tool, so that FoamMesh can run it
on the tessellation the shared pipeline produces and feed the result to both
meshing routes (Plan 41, GN25, §4.7 and §4.8).

## Upstream

| Item | Value |
|---|---|
| Source | `gmsh-4.15.2-source.tgz` from https://gmsh.info/src/ |
| sha256 | `be3f66f2…` (recorded in full in Plan 41 §4.7) |
| Copyright | Gmsh: © 1997–2025 C. Geuzaine, J.-F. Remacle; HXT: © 2017–2018 Université catholique de Louvain |
| Licence | GPL-2.0-or-later (Gmsh and HXT); taken under GPL-3.0-or-later here, see `LICENSE.txt`, `hxt/LICENSE.txt`, `../../THIRD_PARTY.md`, `../../NOTICE` |

## Files

| Path here | Upstream path | State |
|---|---|---|
| `LICENSE.txt`, `CREDITS.txt` | `LICENSE.txt`, `CREDITS.txt` | verbatim |
| `hxt/LICENSE.txt`, `hxt/CREDITS.txt`, `hxt/README.md` | `contrib/hxt/` | verbatim |
| `hxt/core/`, `hxt/predicates/` | `contrib/hxt/core`, `contrib/hxt/predicates` | verbatim, built through their own `CMakeLists.txt` |
| `hxt/tetMesh/` | `contrib/hxt/tetMesh` | verbatim; only the Delaunay subset is compiled (`hxt_tetRefine.c`, `hxt_tetDelaunay.c`, `hxt_tetDelaunayReshape.c`, `hxt_tetFlag.c`, `hxt_tetRepair.c`, `hxt_tetUtils.c`, `hxt_tetSync.c`, `hxt_vertices.c`, `hxt_tetNodalSize.c`), never `hxt_tetMesh.c`, the optimisers or `tetBR` |
| `third_party/gmsh/rtree.h` | `src/common/rtree.h` | verbatim |
| `third_party/gmsh/robustPredicates.{h,cpp}` | `src/numeric/robustPredicates.{h,cpp}` | verbatim (Shewchuk's predicates in Gmsh's wrapper; compiled with `-fno-unsafe-math-optimizations -ffp-contract=off`) |
| `third_party/gmsh/curvature.{h,cpp}` | `src/numeric/curvature.{h,cpp}` | verbatim (Rusinkiewicz curvature on a triangle list) |
| `third_party/gmsh/SPoint3.h`, `SVector3.h`, `SBoundingBox3d.h` | `src/geo/` | verbatim |
| `src/automaticMeshSizeField.h` | `src/mesh/automaticMeshSizeField.h` | modified, see below |
| `src/automaticMeshSizeField.cpp` | `src/mesh/automaticMeshSizeField.cpp` | modified, see below |
| `src/gmsh_shims/*.h` | — | FoamMesh (stand-ins for `GmshMessage.h`, `Numeric.h`, `MVertex.h`, `MTetrahedron.h`, `STensor3.h`) |
| `src/surface_io.{h,cpp}`, `src/foammesh_sizefield.{h,cpp}`, `src/main.cpp` | — | FoamMesh |
| `CMakeLists.txt` | — | FoamMesh |

Not lifted, and why: `contrib/hxt/reparam` (its `hxtCurvatureRusinkiewicz` is
not what the size field calls; the field uses `src/numeric/curvature.cpp`),
`contrib/hxt/tetBR` (boundary recovery, only `hxt_tetMesh.c` needs it),
`src/geo/STensor3.*` (needs `fullMatrix`; the shim covers the three members the
compiled paths touch), the Gmsh `Field` machinery and `GModel`.

## Changes to the lifted size-field sources

Every change in `src/automaticMeshSizeField.{h,cpp}` is marked `// FOAMMESH:`.
`tools/foammesh.patch` is the unified diff from the upstream copy to these two
files, and `tools/relift.sh` rebuilds the whole lifted tree from the tarball
(sha256 checked), applies the patch, fails on any rejected hunk or missing
upstream file, and compares the result with this directory byte for byte
(run in the WSL runtime: `bash vendor/gmsh_sizefield/tools/relift.sh`).
`tools/hxt_files.txt` lists the HXT files taken from `contrib/hxt/`;
`tools/make_patch.sh` regenerates the patch after a deliberate edit of the two
files. A later Gmsh release is re-lifted by running `relift.sh` with the new
tarball (and its sha256 in the script) and resolving the rejected hunks in the
three replaced regions only.

Header:
- Gmsh `Field` subclass declaration removed; the forest structs and function
  declarations stay.
- `ForestOptions` gains `debugPos` and `debugDir`.
- `forest_leaf_t` and `forestCollectLeaves()` added.
- The functions upstream kept file-local (`featureSize`, `forestRefine`,
  `forestSizeSmoothing`, `hxtOctreeElementEstimation`, `forestSearchOneAniso`,
  `forestInterpolateDirections`, `hxtForestSearch`) are declared, because the
  driver lives in another translation unit.

Source:
- `GModel.h`, `GRegion.h`, `GFace.h`, `GEntity.h`, `hxt_edge.h`,
  `hxt_tetMesh.h` includes removed.
- Upstream lines 70–410 removed: `getAllFacesOfAllRegions`,
  `getAllEdgesOfAllFaces`, `Gmsh2Hxt`, `Gmsh2HxtLocal`, `Gmsh2HxtGlobal`
  built the `HXTMesh` from a Gmsh `GModel`. `surface_io.cpp` builds it from STL
  files (welded, one colour per named surface).
- Upstream lines 2462–2959 removed: the `Field` subclass (`operator()`,
  destructor, `updateHXT`, `update`). `foammesh_sizefield.cpp` runs the same
  sequence (per-surface curvature, Delaunay of the surface nodes, normals,
  R-tree, forest create / feature size / refine / smoothing / estimate) on the
  STL model.
- `forestCreate` no longer calls `sc_MPI_Init` when MPI is already up;
  `forestDelete` no longer finalises MPI (the driver does both once).
- The debug `.pos` files (`nonNorme.pos`, `norme.pos`, `medialAxis_toDraw.pos`,
  `keptEdges.pos`, `directions.pos`) were written unconditionally into the
  working directory; they are now written only with `--debug`, into the output
  directory.
- `featureSize` frees the `MVertex` / `MTetrahedron` objects it allocates
  (upstream leaked them).
- `forestCollectLeaves` added (leaves with shared corner points, for the
  `.vtu` export and the per-region estimate). Version 1.0.0: `forest_leaf_t`
  also carries the quadrant coordinates `xyz[3]` and the metric `m[6]`, and
  `forestRootCube()` returns the root cube of the octree (origin, edge
  length; quadrant coordinates run 0 .. 2^19 along it).

`featureSize()` itself (the Delaunay-pole thin-region rule) is verbatim over the
shims: `MVertex`, `MEdge`, `MEdgeLessThan`, `MFace`, `MTriangle`,
`MTetrahedron` in `src/gmsh_shims/MVertex.h` reproduce Gmsh's tetrahedron
edge and face numbering, circumcentre and outer radius, so
`commonFaceTetFast()` and the umbrella test behave as upstream.

## Outputs, version 1.0.0

`foammesh-sizefield --version` prints `1.0.0`; `field.json` carries
`version`. Written into `--out DIR`:

| File | Content |
|---|---|
| `leaves_ijk.npy` | uint32 N x 3, quadrant coordinates of each leaf's low corner |
| `leaves_level.npy` | uint8 N |
| `leaves_size.npy` | float32 N, the prescribed size in the leaf |
| `nodes_xyz.npy` | float64 M x 3, the shared leaf corner points |
| `nodes_size.npy` | float32 M, the minimum size over the leaves adjacent to the node |
| `hexes.npy` | int32 N x 8, corner indices in VTK order (also Gmsh's hexahedron order) |
| `leaves_metric.npy` | float32 N x 6 (xx, xy, xz, yy, yz, zz); only with `--aniso` |
| `surface.vtp`, `field.json` | always |
| `field.vtu`, `field.p4est` + `field.data`, `field.pos` | opt-in: `--vtu`, `--p4est`, `--pos` |

The `.npy` files are NumPy format 1.0, little endian, C order. `field.json`
adds `version`, `root` (`origin`, `length`, `max_level` 19), `peak_rss_mb`,
`leaves_by_level`, and per surface `feature_size_p10/p50` and
`curvature_p50/p90/max` (the larger principal curvature magnitude at the
surface's vertices).

`--aniso` (experimental, offered only under MMG3D) is FoamMesh's own rule,
because upstream's `assignSizeAfterRefinementAniso` is under `#if 0` and its
`updateHXT` has `forestInterpolateDirections` commented out: in a leaf that a
surface triangle crosses (exact triangle-box test), the size along the surface
normal is the leaf's smoothed isotropic size and along the principal curvature
directions k1 >= k2 it is `max(size, 2 pi / (n k1))` and
`max(size, 2 pi / ((n / 4) k2))`, with n the nodes per 2 pi, capped at `hmax`;
the normal, curvature and directions are those of the vertex of the nearest
crossing triangle closest to the leaf centre. Other leaves get the isotropic
metric. `field.json` reports `aniso_leaves_crossed_by_surface`.

## A defect found and fixed in the 0.1.0 driver

The 0.1.0 driver called `_nodeNormals.reserve()` and then, because the
4-argument `CurvatureRusinkiewicz` writes into the vector without sizing it
(upstream does the same and relies on the reserved capacity), `resize()`
zero-filled the normals. Every node normal was zero, which switched off the
normal test in `featureSize()` and made the thin-region rule fire where it
should not. Version 1.0.0 sizes the vector before the call. Field results from
0.1.0 are not comparable: elbow 308,057 leaves became 202,819, the jacketed
pipe 819,456 became 90,952, the quadcopter 2,912,568 became 2,874,292.

## Semantics worth knowing

- `nodesPerGap` (`--nodes-per-gap`, upstream `nPointsPerGap` / `features`)
  is the thin-region rule: the size across a gap is the gap divided by this
  count. It is not feature-edge detection; feature edges stay with
  FoamMesh's own feature-angle extraction.
- `nodesPerTwoPi` (`--nodes-per-two-pi`, upstream `nPointsPerCircle`) is the
  curvature rule, `h = 2π / (n · κ_max)`.
- Defaults as upstream: `hbulk = L/20`, `hmin = L/1000`, `hmax = hbulk`,
  gradation 1.1, where `L` is the longest bounding-box edge.
- The octree cube is 1.3 × the bounding box, centred on it; the estimate in
  `field.json` counts the whole cube, not the model's interior.

## Building

`scripts/build_sizefield.sh` (repository root) configures and builds inside
the WSL runtime, where `cmake`, `libp4est-dev 2.2` (an MPI build) and
`libsc-dev 2.3.1` are installed, and installs
`/opt/foammesh/bin/foammesh-sizefield`. The build directory is outside the
repository. Nothing here is built on Windows or packaged by PyInstaller.
