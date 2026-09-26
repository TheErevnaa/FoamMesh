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

FoamMesh wraps the OpenFOAM meshing pipeline. Three rows are shared by both
meshers:

1. **Geometry** — import STL/OBJ (or STEP/IGES/BREP with `[cad]`); set units, scale,
   rotate, translate; run diagnostics (open edges, watertightness).
2. **Mesh setup** — choose the mesher, and the settings the run is executed with.
3. **Preparation** — repair, wrap, or accept the geometry as-is. The meshers read
   the prepared revision, not the import.

The rows after **Preparation** belong to the mesher you chose, and are numbered
from 4. Snappy runs to row 11 and Gmsh to row 12; the last row of each is
**Export** — OpenFOAM case (native), or VTK/Gmsh/CGNS/SU2.

Editing an earlier stage marks later stages **stale** rather than deleting
them — re-run only what changed.

### Moving forward

There is one forward control, at the bottom of the window, and its label says
what the press will do on the row you are standing on. On a row that only
records what you filled in it reads **Proceed**; on a row that runs something
it names the act — **Generate grid & Proceed**, **Extract features & Proceed**,
**Castellate & Proceed**, **Snap & Proceed**, **Apply layers & Proceed**,
**Generate & Proceed**, **Check & Proceed** — and on the last row,
**Export mesh**. Its tooltip says the same thing in a sentence.

One press settles one row, including any sub-steps folded into it, and then
opens the next row. While a press that runs something is in flight, both
forward controls are disabled, so a second click cannot queue a second run;
they come back when it settles, whether it succeeded or failed.

Running everything that is left in one go is a separate control: **Run to end**,
on the mesher's heading row. The bottom bar never offers it.

An optional row you leave empty is **skipped** rather than run, and the status
line says which row was skipped and why. Filling it in afterwards undoes that:
the next press on the row really runs it.

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
