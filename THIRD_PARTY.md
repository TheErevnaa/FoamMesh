# Third-Party Components

FoamMesh is derived from **BARAM / BaramMesh** (NEXTfoam Co., Ltd.), GPL-3.0.
It depends on and/or bundles the following third-party software. Each component
remains under its own license; this list is informational and will be kept in
sync as dependencies change (finalized in the phase-11 packaging work).

## Vendored in-tree

| Component | Location | License | Notes |
|-----------|----------|---------|-------|
| PyFoam 2021.6 | `vendor/PyFoam/` | GPL-2.0-or-later | OpenFOAM case/dictionary/polyMesh handling. The upstream grant explicitly permits GPL v2 **or any later version**, making its inclusion compatible with FoamMesh's GPL-3.0-or-later distribution. |
| Gmsh 4.15.2 `AutomaticMeshSizeField` (Arthur Bawin; C. Geuzaine, J.-F. Remacle) | `vendor/gmsh_sizefield/src/` | GPL-2.0-or-later (Gmsh licence, with the Gmsh special exception, which this tool does not need and does not claim) | The automatic curvature / proximity / gradation size field from Gmsh's `src/mesh/automaticMeshSizeField.{cpp,h}`, lifted from `gmsh-4.15.2-source.tgz` (sha256 `be3f66f2…`) and built as the stand-alone Linux tool `foammesh-sizefield` for both meshing routes (Plan 41 GN25). The GModel-coupled regions are replaced; every local change is marked `FOAMMESH` and listed in `vendor/gmsh_sizefield/UPSTREAM.md`. Taken under GPL-3.0-or-later by the "or later" grant, as PyFoam is. *In tree since 2026-10-09.* |
| HXT (Université catholique de Louvain, 2017–2018) | `vendor/gmsh_sizefield/hxt/` (`core`, `predicates`, `tetMesh`; `reparam` and `tetBR` not lifted) | GPL-2.0-or-later (HXT licence, with its Gmsh-combination exception) | Mesh container, bounding boxes, robust predicates and the Delaunay kernel the size field's thin-region rule uses. Verbatim from `contrib/hxt/` of the same tarball; `hxt/LICENSE.txt`, `hxt/CREDITS.txt` and `hxt/README.md` kept. |
| Gmsh helpers: `curvature.{h,cpp}` (Rusinkiewicz curvature), `SPoint3.h`, `SVector3.h`, `SBoundingBox3d.h`, `robustPredicates.{h,cpp}` (Shewchuk's predicates in Gmsh's wrapper), `rtree.h` | `vendor/gmsh_sizefield/third_party/gmsh/` | GPL-2.0-or-later (Gmsh); the predicates and the R-tree carry their own permissive notices | Verbatim from `src/numeric`, `src/geo` and `src/common` of the same tarball. |

## Bundled Gmsh build

`src/resources/gmsh_runtime/linux-x86_64/gmsh-4.15.2-revoropt-6.tar.gz` is
FoamMesh's own Linux build of Gmsh 4.15.2 (`bin/gmsh`, `lib/libgmsh.so`,
`lib/gmsh.py`). The app installs it into the user's WSL distribution with
"Set up FoamMesh's Gmsh". Its corresponding source is the Gmsh 4.15.2 source
tarball and the libraries below at the versions and SHA-256 sums pinned in
`scripts/gmsh_build/pins.sh`, plus FoamMesh's patches in
`scripts/gmsh_build/patches/`. `scripts/gmsh_build/build_gmsh.sh` rebuilds the
bundle from them.

| Component | Version | License | How it is in the build |
|-----------|---------|---------|------------------------|
| Gmsh, with the contrib libraries its build compiles in (Revoropt among them) | 4.15.2 + 5 FoamMesh patches (`gmsh-4.15.2-*.patch`) | GPL-2.0-or-later, taken under GPL-3; each contrib library under the licence in its folder of the Gmsh source | the program and `libgmsh` |
| Open CASCADE Technology | 7.8.1 + 1 FoamMesh patch (`occt-7.8.1-*.patch`) | LGPL-2.1 with the Open CASCADE exception | linked statically |
| Mmg | 5.7.3 | LGPL-3.0 | linked statically |
| PETSc | 3.22.0 | BSD-2-Clause | linked statically |
| HDF5 | 1.10.11 | BSD-3-Clause style (HDF5 licence) | linked statically |
| MED-fichier | 4.1.0 | LGPL-3.0 | linked statically |
| CGNS | 4.4.0 | zlib style | linked statically |
| FLTK | 1.3.8 | LGPL-2.0 with the FLTK static-linking exceptions | linked statically |
| libjpeg-turbo (`libjpeg.so.8`, Ubuntu 22.04) | as packaged | IJG, BSD-3-Clause and zlib | shared library beside `libgmsh` |

## Python runtime dependencies (see `requirements.txt`)

| Component | License (typical) | Use |
|-----------|-------------------|-----|
| PySide6 (Qt for Python) | LGPL-3.0 / commercial | GUI toolkit |
| VTK | BSD-3-Clause | 3D rendering, STL/geometry processing |
| NumPy, pandas, matplotlib, pyqtgraph | BSD-style | numerics / plotting |
| h5py, tables | BSD-style | HDF5 project storage |
| lxml | BSD-style | XML utilities used by `foammesh.support` |
| qasync | BSD-2-Clause | asyncio + Qt event loop |
| superqt, PySide6-QtAds | MIT / LGPL | extra Qt widgets / docking |
| thermo | MIT | thermophysical data |
| PyYAML, openpyxl, python-dateutil, bidict, filelock, psutil | misc permissive | utilities |
| posthog | MIT | optional analytics (OFF by default — no `_config.py` shipped) |

## Asset packs

| Component | Location | License |
|-----------|----------|---------|
| Ionicons | `src/resources/ionicons*` | MIT |
| Project graphics icons | `src/resources/graphicsIcons` | per upstream BARAM |

## Optional/runtime integrations

OpenCASCADE / pythonocc-core (LGPL-2.1) provides STEP/IGES/BREP CAD import.
FastAPI / Starlette / uvicorn (MIT/BSD) provide the optional API facade.
Gmsh (GPL-2.0-or-later, taken under GPL-3) is the second meshing engine; it is
driven in the qualified WSL runtime. FoamMesh ships its own Linux build of it
(see "Bundled Gmsh build" above), which the app copies into the user's WSL
distribution on request. p4est and libsc
(BSD-style / GPL-2.0-or-later respectively, Ubuntu 22.04 packages `libp4est-dev`
2.2 and `libsc-dev` 2.3.1) are linked statically into the Linux binary
`foammesh-sizefield` (`src/resources/sizefield/linux-x86_64/`), which FoamMesh
ships and copies into the user's WSL distribution on request. OpenFOAM is likewise a
separately installed external runtime and is not bundled in the FoamMesh
distribution.
