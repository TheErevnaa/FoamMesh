# Installing FoamMesh

## From a release bundle

Download the bundle for your platform and run the installer (Windows), copy the
`.app` to `/Applications` (macOS), or install the `.desktop` entry and icon from
the extracted folder (Linux).

## From source

Python 3.11 or newer is required.

```bash
pip install -r requirements.txt
python convertUi.py     # builds Qt resources — required before first launch
./foammesh.sh           # or ./foammesh.ps1 on Windows
```

Optional extras:

```bash
pip install "pythonocc-core>=7.7"   # STEP/IGES/BREP CAD import
pip install "gmsh>=4.11"            # Gmsh meshing engine
pip install "fastapi>=0.110" "uvicorn>=0.27" "websockets>=12"   # API facade
```

## OpenFOAM

OpenFOAM is **not** bundled with FoamMesh. Install it separately and make its
utilities reachable from the environment FoamMesh runs in; the meshing runtime
is configured in the application's settings.

See [`docs/user_manual/openfoam_environment.md`](docs/user_manual/openfoam_environment.md).
