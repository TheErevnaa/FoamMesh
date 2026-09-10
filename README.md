# FoamMesh

**FoamMesh** is a free, open-source, standalone meshing workbench for
**OpenFOAM**. It provides a graphical workflow around `blockMesh`,
`surfaceFeatures`, `snappyHexMesh`, `checkMesh` and `Gmsh` — import geometry,
build a mesh, check its quality, and export it for your OpenFOAM case.

> **Version 1.0.0**

## Features

- Geometry import: STL/OBJ surfaces and STEP/IGES/BREP CAD (with the `cad` extra)
- Region, boundary-layer and refinement setup through a guided workflow
- Two meshing engines: `snappyHexMesh` and `Gmsh`
- Mesh quality checking via `checkMesh`, with diagnostics surfaced in the GUI
- Export to OpenFOAM `polyMesh`, plus neutral formats for downstream tools
- A CLI and an optional REST/WebSocket API over the same core

## Requirements

- Python 3.11 or newer
- An OpenFOAM installation — **not bundled**. FoamMesh generates dictionaries
  and drives the OpenFOAM utilities it finds; install OpenFOAM separately.
- Gmsh, if you want the Gmsh engine — also not bundled.

## Running from source

FoamMesh uses a `src/` layout. Put the source roots on the import path (the
launchers do this for you) and build the Qt resources first.

```bash
# 1. create/activate a virtual environment, then:
pip install -r requirements.txt

# 2. build Qt resources, .ui files, and translations (REQUIRED):
python convertUi.py

# 3. launch (the launcher sets PYTHONPATH=src;vendor):
./foammesh.sh         # Linux/macOS
./foammesh.ps1        # Windows PowerShell
# or directly:
PYTHONPATH="src:vendor" python -m foammesh.main
```

The CLI is available as `python -m foammesh.cli.main` with the same path setup.

## Building a desktop bundle

See [`packaging/README.md`](packaging/README.md) for the PyInstaller recipe and
the per-OS installer steps (Inno Setup on Windows, `.desktop`/AppStream on
Linux, `.app` on macOS).

## Documentation

- [Getting started](docs/user_manual/getting_started.md)
- [User manual](docs/user_manual/) — CAD import, boundary layers, mesh check &
  repair, export formats, OpenFOAM environment, troubleshooting
- [Tutorials](docs/tutorials/)
- [API reference](docs/api_reference.md)

## Project layout

```
src/        first-party code: foammesh, widgets, analytics, resources
vendor/     vendored third-party dependency (PyFoam)
docs/       user manual and tutorials (shipped with the app)
packaging/  PyInstaller spec and per-OS installer definitions
scripts/    build-time asset and metadata generation
```

## Supported platforms

- Windows 10 or later
- Ubuntu 20.04 or later (and compatible Linux distributions)
- macOS 10.14 or later

## License & attribution

FoamMesh is distributed under the **GNU General Public License v3.0**
(see [LICENSE](LICENSE)). It is derived from the **BARAM / BaramMesh**
open-source project by **NEXTfoam Co., Ltd.** (GPL-3.0). See [NOTICE](NOTICE)
and [THIRD_PARTY.md](THIRD_PARTY.md).

FoamMesh is **not** approved or endorsed by OpenCFD Limited, producer and
distributor of the OpenFOAM software and owner of the OPENFOAM® and OpenCFD®
trademarks, nor by the OpenFOAM Foundation, CFD Direct, or NEXTfoam Co., Ltd.
