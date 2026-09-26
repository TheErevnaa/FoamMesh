# FoamMesh Release Notes

## 1.1.0 — 2026-09-25

- **Viewport.** A region picker and a selectable parts list show one region or
  several; Fit to selection frames only what is selected, and Back/Forward
  history follows every fit and is cleared when another model loads. Mesh
  lines have an opacity, colour and width control in the toolbar and the
  display panel. A section plane moves when its origin handle is dragged
  along its normal, and a locked plane keeps moving for the whole Ctrl+drag. The
  background colour can be reset to the theme.
- **Comparing the mesh with its geometry.** Deviation from the loaded geometry
  is painted on one symmetric scale with a titled legend, a histogram and the
  share of faces within a tolerance. Poor cells are coloured on a scale over
  their band, drawn in front of the surfaces behind them.
- **Render quality.** View > Render quality offers Performance, Balanced (the
  default) and Quality. See-through geometry is anti-aliased again, and the
  lighting shows the shape of curved parts. Performance is 40-50 % faster than
  before on meshes of 100k-200k cells.
- **Menus and settings.** Settings opens one Preferences dialog. The Parallel
  menu is gone; Meshing resources in the workflow is the only core count. Dead
  and duplicate menu entries were removed, Redo and Close have standard
  shortcuts, and Undo is locked while a batch writes the case.
- **Workflow.** Quality verdicts name the check that produced them, the
  outline ticks finished steps, and a snappy stage's mesh appears in the
  viewport as soon as that stage finishes.
- **Advice when a mesh fails.** A skewed or non-orthogonal snappy mesh is told
  that finer cells help; layers that were not grown are told what to change; a
  Gmsh refusal with Optimize off names Optimize, not the repair pass.
- **Mesh > Scale, Translate and Rotate** open on a cold WSL start (the first
  one can take about 20 s while WSL starts; later ones open in under half a
  second), and Scale takes a single factor such as 200.
- A mesh changed outside FoamMesh is no longer credited to the run that made
  the previous one, and a single-region mesh no longer reads "in 1 region ()".
- A timed-out runtime probe no longer leaves a `.pid` file in the folder the
  app was started from.

## Plan 22 — Gmsh replaces the SALOME pipeline — 2026-07-31

- **The SALOME hybrid pipeline is gone.** Its code, tests, scripts, schema
  section have been removed, along with the 23.4 GB `FoamMeshSalome915` WSL
  distribution.
  Measured against the same fifteen geometries, SALOME meshed twelve and only
  two of those passed `checkMesh`, at roughly sixty seconds a case.
- **Gmsh 4.15.2 is the second pipeline**, running in the existing
  `OpenFOAM13Runtime` WSL distribution alongside OpenFOAM Foundation 13. Its
  runtime is an 89 MB shared library. It meshes all fifteen geometries, all
  fifteen pass `checkMesh`, median maximum non-orthogonality is 50.1 against
  SALOME's 50.4, and the mean wall clock is 3.75 s a case.
- **Configuration version 13 → 14.** Projects saved on version 13 cannot be
  opened; the error names the version and the reason rather than saying only
  that the version is unsupported. FoamMesh has had no release, so no saved
  project is affected.
- **Boundary layers apply to every boundary surface.** Gmsh grows real 3D prism
  layers off imported CAD, but cannot restrict them to a chosen subset of
  patches. Per-patch layer selection remains a Snappy capability. The control
  says so rather than offering a scope Gmsh would ignore.
- MED import and the MED-based canonical entry point are removed with the
  SALOME pipeline.
- This is a new-build schema. FoamMesh does not include old-project migration,
  compatibility readers, legacy writers, or retired product runtimes.

## Plan 20 dual-pipeline build — 2026-07-30

- New cases begin with no meshing method selected. Choosing a meshing method
  mounts only that engine's workflow and preserves the inactive engine's
  settings without exposing its task pages.
- All OpenFOAM execution targets OpenFOAM Foundation 13 in the qualified
  `OpenFOAM13Runtime` WSL distribution. Native-PATH, OpenCFD, bundled-solver,
  and mixed-profile execution are disabled.
- Surface, volume, layer, zone, and interface editors use stable prepared
  geometry IDs and drive viewport highlighting. Interface selection
  highlights both master and slave faces.
- Live retained references cover Snappy serial/2-rank/4-rank meshes. Published
  meshes pass Foundation-v13 `checkMesh`.

## Unreleased — UIX overhaul (plan phases U1–U7)

### Highlights

- **Deterministic automation journeys**: geometry diagnostics, full authored
  meshing, separately confirmed QA adjustment, recovery-backed transform,
  staged converter import, and export/save/archive use exact parameter-bound
  facade plans across in-process and REST transports.
- **Fail-closed release evidence**: live OpenFOAM, GUI/visual/accessibility,
  packaged desktop/headless, clean-checkout, and reproducibility
  matrices produce one checksummed candidate record. Missing host evidence can
  no longer be represented by an unconditional test skip.

- **Main-window-first shell**: nine-menu structure (File · Edit · Mesh ·
  Case Tools · View · Parallel · Settings · External Tools · Help) with a
  central action policy — no dead actions; disabled actions state why.
- **Case lifecycle**: sidecar metadata with mesh fingerprints, workflow/
  mesh-origin provenance, locking, atomic saves, and an auditable unified
  transaction history (state edits + artifact events).
- **Mesh operations**: structured Mesh Info (topology, bounds, zones,
  file metadata, report export); Foundation-v13 transforms with bounding-box
  preview and automatic rollback; Mesh Check dashboard with streamed output,
  fingerprint-keyed staleness, and failed-set highlighting; staged surface
  and post-mesh repair; **Restore Previous Mesh**.
- **Import/export matrix**: seven capability-gated mesh converters with
  staged, checksummed, recoverable imports; copy-vs-replace native import;
  unified export dialog (native case, VTU with read-back validation,
  Gmsh/CGNS experimental, Fluent via `foamMeshToFluent`, in-place
  ASCII/binary conversion via `foamFormatConvert` with controlDict
  preserve/restore); completion dialogs with path/size/warnings/Open Folder.
- **Jobs**: bounded-output streaming job manager with process-tree cancel and
  a compact status-bar progress surface (name, elapsed, Cancel, Show Log).
- **Headless parity**: the CLI (`info`, `check`, `transform`, `restore`,
  `import-mesh`, `export`, `formats`, `history`) and REST API case routes
  call the exact services the GUI uses.
- **Accessibility**: text verdicts (never colour alone), safe default buttons
  on destructive confirms, screen-reader names for icon-only controls.

### New-build schema policy

- FoamMesh accepts only the current project and configuration schema.
- Older or future schema versions are rejected explicitly; there is no
  compatibility reader, conversion command, or silent value translation.
- The authored Export step records into the same export history as
  Case Tools → Export.

### Known gaps in this build

- The live Foundation-v13 converter/export matrix (real utilities on the
  validation machine) is pending.
- Gmsh and CGNS export remain **experimental** until live round-trip
  fixtures pass.
- Tutorials are shipped with the local FoamMesh documentation set.
