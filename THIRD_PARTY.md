# Third-Party Components

FoamMesh is derived from **BARAM / BaramMesh** (NEXTfoam Co., Ltd.), GPL-3.0.
It depends on and/or bundles the following third-party software. Each component
remains under its own license; this list is informational and will be kept in
sync as dependencies change (finalized in the phase-11 packaging work).

## Vendored in-tree

| Component | Location | License | Notes |
|-----------|----------|---------|-------|
| PyFoam 2021.6 | `vendor/PyFoam/` | GPL-2.0-or-later | OpenFOAM case/dictionary/polyMesh handling. The upstream grant explicitly permits GPL v2 **or any later version**, making its inclusion compatible with FoamMesh's GPL-3.0-or-later distribution. |

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
driven in the qualified WSL runtime and is not bundled. OpenFOAM is likewise a
separately installed external runtime and is not bundled in the FoamMesh
distribution.
