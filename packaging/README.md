# Packaging FoamMesh

FoamMesh is packaged with **PyInstaller**. A PyInstaller build runs on the target
OS (no cross-compilation), so build Windows installers on Windows, etc.

## Prerequisites (per build machine)

```bash
pip install -r requirements.txt
pip install pyinstaller
python convertUi.py          # generate resource_rc.py + *_ui.py (REQUIRED)
python scripts/generate_build_metadata.py
# optional extras to bundle their features:
pip install "foammesh[cad]" "foammesh[api]" "foammesh[agent]" "foammesh[export]"
```

## Build

```bash
pyinstaller packaging/foammesh.spec
# -> dist/FoamMesh/  (one-folder bundle; FoamMesh.app on macOS)
python scripts/package_branding_smoke.py
```

## Per-OS packaging of the bundle

| OS | Wrap `dist/FoamMesh/` as |
|----|--------------------------|
| Windows | Compile `packaging/windows/FoamMesh.iss` with Inno Setup; the branded ICO is bound to both executable and installer |
| Linux | Install the `.desktop`, AppStream XML and 256 px icon from `packaging/linux` / `src/resources/branding` |
| macOS | The spec creates `FoamMesh.app` with the branded ICNS and Erevnaa bundle identifier; sign/notarize it before a `.dmg` |

## Notes

- **OpenFOAM is not bundled.** FoamMesh generates dictionaries and drives OpenFOAM
  utilities found on `PATH`; users install OpenFOAM (Foundation v13 recommended)
  separately.
- The CAD extra (OpenCASCADE) is large; offer a separate "FoamMesh + CAD" bundle.
- A PyInstaller build must run on the OS it targets, so per-OS artifacts are
  produced on a machine (or CI runner) of that OS.

The actual installers are produced and smoke-tested on their target
operating system.
