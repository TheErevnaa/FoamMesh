# Troubleshooting

## "checkMesh / transformPoints / converter was not found"

FoamMesh probes utilities from the environment it was launched in. Launch it
from a shell with OpenFOAM v13 active, then reopen **Parallel → Environment**
(or restart) to re-probe. `foammesh formats` shows exactly what was found.

## A mesh action is disabled

Hover the menu item — the tooltip/status tip always states the reason:
no case, no complete `constant/polyMesh`, a running operation, or a missing
utility. FoamMesh never shows a dead action without an explanation.

## Mesh Check says "stale"

The mesh changed after the report was produced (transform, repair, import,
format conversion, restore). Run Mesh Check again; the report is keyed to the
mesh fingerprint, so staleness is detected even across sessions.

## An operation failed — where is my mesh?

Mesh-changing operations snapshot `constant/polyMesh` first and restore it
automatically on failure or cancel. Check **Edit → Transaction History** for
the event and its recovery status, and `foammesh/logs/` for the raw log.
A successful-but-unwanted result can be reverted with
**Mesh → Restore Previous Mesh...**.

## The case will not open in the authored workflow

Sidecar provenance and the on-disk mesh no longer agree (for example the mesh
was replaced outside FoamMesh). The case opens safely in external-mesh mode
and states why; **Start Meshing Workflow** re-enters authored navigation.

## Export destination errors

Exports refuse to overwrite existing files/directories and refuse a
destination inside the source case. Choose a fresh path; the completion
dialog's **Open Folder** shows exactly what was written.

## GUI does not start

- Regenerate UI/resources: `python convertUi.py`
- Verify environment: `python scripts/smoke_journey.py --phase gui` prints a
  precise import error when a dependency is missing (evidence under
  `build/smoke/`).
- Both `src/` and `vendor/` must be on `PYTHONPATH` (the launch scripts do
  this); `vendor/` carries PyFoam.

## "this project uses FoamMesh configuration version 13"

Version 13 projects cannot be opened. That version carried the SALOME hybrid
meshing pipeline, which has been removed and replaced by Gmsh, so a project
saved on it names an engine this build has no implementation for.

There is no migration, and deliberately so: the SALOME settings have no Gmsh
equivalent worth guessing at. NETGEN's fineness presets, its segments-per-radius
and element-size-weight knobs, and its surface-recipe choices have no Gmsh
counterpart at all.

Re-import the geometry into a new project. Everything outside the engine
section — the geometry, the preparation decision, regions and interface pairs
— is authored the same way, and the sizing intent transfers by eye: a SALOME
global target size is a Gmsh target size.
