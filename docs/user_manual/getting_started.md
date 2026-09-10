# Getting Started

## Install (from source)

```bash
pip install -r requirements.txt          # core deps (PySide6, VTK, ...)
python convertUi.py                        # build Qt resources + .ui + translations
```

Optional features (extras):

```bash
pip install "foammesh[cad]"      # STEP/IGES/BREP import (OpenCASCADE)
pip install "foammesh[api]"      # REST/WebSocket API (FastAPI)
pip install "foammesh[agent]"    # agent strategy LLM bridge (Anthropic)
pip install "foammesh[export]"   # Gmsh export
```

## Launch

```bash
./foammesh.sh           # Linux/macOS
./foammesh.ps1          # Windows PowerShell
# or:
PYTHONPATH="src:vendor" python -m foammesh.main
```

## The meshing workflow

FoamMesh wraps the OpenFOAM meshing pipeline:

1. **Geometry** — import STL/OBJ (or STEP/IGES/BREP with `[cad]`); set units, scale,
   rotate, translate; run diagnostics (open edges, watertightness).
2. **Region** — identify fluid/solid regions, cell zones, interfaces.
3. **Base grid** — background `blockMesh` (domain bounds + cell counts).
4. **Castellation** — `snappyHexMesh` refinement (surface/feature/region levels).
5. **Snap** — snap the castellated mesh to the geometry.
6. **Boundary layers** — prism layers (per-patch counts, expansion, y+ target).
7. **Export** — OpenFOAM case (native), or VTK/Gmsh/CGNS/SU2.

Editing an earlier stage marks later stages **stale** rather than deleting
them — re-run only what changed.

## Headless / CLI

```bash
foammesh generate part.stl -o case/     # import -> generate an OpenFOAM case
foammesh mesh part.stl -o case/         # generate + run (if OpenFOAM on PATH)
foammesh checkmesh log/checkMesh.log    # parse a checkMesh log -> verdict
foammesh serve                          # run the REST/WebSocket API
```

## Requirements for meshing

Both meshing methods need the qualified **OpenFOAM Foundation 13** WSL runtime:
Snappy runs its stages there, and Gmsh runs there too and publishes through the
same `checkMesh`. FoamMesh generates the dictionaries and jobs and drives those
external runtimes; neither is bundled.

Gmsh additionally needs its own package inside that distribution:

```bash
wsl -d OpenFOAM13Runtime -u root -- pip3 install gmsh
```

See [Meshing with Gmsh](gmsh_workflow.md) for when to choose it over Snappy.
