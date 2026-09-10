# FoamMesh branding assets

`src/resources/branding/foammesh_app_icon.png` is the approved application-icon
master. `white_logo.png` is the approved white Erevnaa viewport artwork.
Run `python scripts/generate_branding_assets.py` to regenerate PNG, Windows ICO,
macOS ICNS, and the checksum manifest. The viewport uses theme-addressable
Light/Dark resources derived from the Erevnaa artwork. Runtime opacity and
adaptive high-DPI sizing provide contrast; retired upstream artwork is never a
runtime fallback.

Packaging mappings:

- Windows executable/installer: `foammesh.ico`
- macOS application bundle: `foammesh.icns`
- Linux desktop/appstream: `foammesh_icon_256.png`
- Qt window and viewport: stable `/branding/*` aliases from `src/resource.qrc`

Bindings are defined in `packaging/foammesh.spec`,
`packaging/windows/FoamMesh.iss`, and `packaging/linux/`. The macOS bundle id is
`com.erevnaa.foammesh`; the Linux desktop/AppStream id is
`com.erevnaa.FoamMesh`.
