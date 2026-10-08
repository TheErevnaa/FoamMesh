# FoamMesh Release Notes

## 1.1.0 — 2026-09-26

- **Detect the fluid regions.** Domain & regions asks how many fluid regions
  there are and whether the flow is external, then finds every space the
  surfaces close off. Each space is listed with its volume, its distance to the
  nearest wall and a colour; tick the ones to keep, rename or retype them, and
  Accept all applies them in one undo step. When the count asked for is not
  what the geometry holds, the panel says so and offers the count it found, or
  the outside as external flow. A thin closed space is listed and flagged, not
  dropped.
- **The domain box is drawn.** The snappy domain box is resolved once from the
  base grid and drawn on Domain & regions, so detection and the mesh work on
  the same box. A domain made of several blocks (an L shape) is judged block by
  block.
- **A region is a volume you can see.** While a region is edited, the space its
  seed will mesh is drawn as a translucent volume in the region's colour, and
  each region is labelled in the view with its name and volume.
- **Drag the seed.** The regions editor no longer blocks the viewport. A
  region's seed is a handle: drag an axis arrow, a plane or the centre;
  Alt+Arrow nudges it one step (Shift for ten), Ctrl+Z undoes a move, and the
  readout says whether the seed is inside the space it names. The seed casts
  shadows on the walls behind it and can be placed on a section plane. A
  snapped or detected seed never lands on a mesh face.
- **Two seeds in one space are caught.** A launch refuses two regions whose
  seeds sit in the same space when a finer check confirms it; a case the
  detection cannot settle is a warning, not a block.
- **On Gmsh, the solids are the regions.** A CAD model's solids are drawn in
  their region colours on the Volume controls step and listed with their type
  and volume; each included solid is meshed as its own cell zone named after
  its region. A two-solid STEP (a pipe inside a jacket) meshes as a
  conjugate-heat-transfer case with no seeds to place. With the far-field box
  on, the solids are shown as the obstacle the box cuts out.
- **Fixes.**
  - A single STL whose regions share faces (a pipe inside a jacket) is read as
    those regions instead of being refused as not closed, on snappy; Gmsh
    names the remedy.
  - Concave cells alone no longer fail a snappy mesh; they are reported as an
    advisory note and the mesh is runnable.
  - Gmsh boundary layers on all eligible walls include the faces a STEP import
    left unnamed; an unnamed solid region is named `solid_N`.
  - A whole-pipeline run passes the same seed check as a single-stage run.
  - External flow round a closed body no longer reports a false count
    mismatch, and a base grid flush with a round body no longer splits the
    outside into pockets.
  - The regions table, the detection review and the docked region editor fit
    the settings column under the theme.
- **Known limitations.**
  - Overriding a region clash is available from the command line only
    (`allow_region_clash`).
  - Detection works on a voxel field and is approximate in very narrow
    passages; a region that runs through one may be proposed as two.

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
- **Viewport toolbar.** The toolbar holds tools rather than readouts: the
  whole-mesh cell count and the model size are shown on the overlay card, and
  the count returns to the toolbar only while a section is cut. The
  background is one button, painted with the current gradient, whose menu sets
  the top and bottom colours or resets them. Smaller buttons mean fewer tools
  go into the ⋯ menu (none at a 1600 px window).
- **Section plane.** The plane now goes through the model on screen. It used
  to stay at the centre of the first model opened, and could miss a later
  model altogether. Hidden, switched-off and empty parts no longer move it, and
  the cut stays visible while the plane is dragged or the view is turned.
- **Finding OpenFOAM and Gmsh.** On its first start FoamMesh looks through
  WSL for OpenFOAM 13 and Gmsh instead of assuming a distribution called
  `OpenFOAM13Runtime`. It tries `foamuser` first, then each distribution's
  own user, and Gmsh runs wherever OpenFOAM was found. Settings > Preferences
  has Find automatically.
- A mesh changed outside FoamMesh is no longer credited to the run that made
  the previous one, and a single-region mesh no longer reads "in 1 region ()".
- A timed-out runtime probe no longer leaves a `.pid` file in the folder the
  app was started from.

### Crash resilience

- **The window keeps going when a tool fails.** Mesh checks, the mesh
  preview, CAD import and repair, and Gmsh/MED/UNV/CGNS export run in a
  separate worker process with a memory cap. A worker that crashes or runs out
  of memory fails that one task with a reason and a Retry; the window and the
  mesh stay.
- **Big meshes.** Above 200,000 cells the viewport shows the boundary surface
  (simplified above 2 million triangles) and offers Load full volume. When a
  preview would not fit in memory the outline and patch list are shown instead,
  with Try again and Build anyway. Heavy tasks wait their turn rather than
  running together, and a check too big for the machine is refused up front
  with its estimated size. Reading a large mesh is about three times faster and
  uses about a quarter of the memory.
- **Lost WSL connection.** A job whose WSL connection drops says so, leaves no
  OpenFOAM processes behind and restores the previous mesh; Retry runs it
  again. A status bar shows when WSL stops answering and when it is back.
- **Crash reports and recovery.** If FoamMesh ever does close unexpectedly it
  writes a crash dump and a log, shows a banner at the next start, and offers
  to restore unsaved edits from its autosave journal. A job interrupted by a
  crash is recovered before a new one can start.
- **Graphics.** A graphics driver reset leaves a placeholder with Recreate
  viewport instead of closing the app; after two drawing crashes in a row
  FoamMesh starts in a safe mode (`--safe-mode`).
- **Installer.** The installer now asks for administrator rights, so it can
  switch on Windows crash dumps for FoamMesh.

### Sections, locked steps and OpenFOAM 13 controls

- **Rename always answers.** Right-click > Rename on a geometry row opens the
  rename dialog, straight after an import, a split or a re-tessellation too,
  and a box, sphere or cylinder added on the Geometry page renames like any
  other surface. When a rename cannot go ahead (nothing selected, several
  rows, a row no longer in the case) a message box says why, instead of a
  note under the list that was easy to miss.
- **A removed layer group stays removed.** Deleting the last snappy layer
  group leaves the list empty; Walls no longer comes straight back. Remove
  deletes the group that is highlighted, even after the rows have moved. A
  group the case refuses to remove keeps its row and a warning says why. On
  Gmsh, unticking every layer surface stays unticked, and All eligible walls
  says that it locks the list.
- **Proceed after an edit meshes again.** Go back to a meshed snappy step,
  change it (on its page, in a refinement, size-field or layer table, or on
  the older per-stage pages) and press Proceed: that step is meshed again
  with the new settings. A save that changed nothing keeps the mesh, a
  second quick press of Proceed joins the first, and meshing again marks
  every quality check that judged the old mesh as out of date, so Export
  never follows a report about a mesh that is gone.
- **Gmsh Check & Proceed reaches Export, or says why not.** The popup that
  stopped Quality is now titled "Quality check did not finish" and names what
  failed and which button to press. A missing Gmsh element report says to run
  Generate mesh again, instead of stopping Quality with no message. One check
  never runs twice at once, and a result for a mesh that has since been
  replaced can no longer move the step on.
- **A meshed step is locked, and can be unlocked.** Once a step has produced
  the mesh on screen, its settings are read-only under a banner that says so,
  and edits from the GUI, the command line or a script are refused; the
  geometry, region seeds and farfield are locked with it. To change it,
  right-click the step in the outline (or use the banner) and choose Unlock
  and discard later results…. The confirmation lists the steps that will be
  reset, the results that are set aside with their size on disk, the snapshot
  the rerun starts from and any exports that go out of date. Exported files
  are never moved or deleted; they are only marked as made from an older
  mesh.
- **Undo an unlock.** Restore previous mesh and settings puts the mesh, the
  settings and the step states back together. It stays available until a new
  run actually starts, so a launch that is refused does not use it up. If
  FoamMesh is closed or crashes half-way through an unlock or an undo, the
  case finishes or rolls it back when it is next opened, before anything can
  run.
- **Each snappy stage is kept.** Base grid, Castellation, Snap and Layers are
  each kept as a verified copy with the case. An edited stage starts from
  the stage before it (Snap from Castellation, Layers from Snap), never from
  its own old output; a missing earlier stage is rebuilt first. Settings >
  Preferences sets how much disk the copies may take (20 GiB by default) and
  how much free space to leave (1 GiB). A copy that does not fit is skipped
  and the step says what that means for a later rerun; a refusal for disk
  space says needed, free and reserve in plain words.
- **A section says what it shows.** When only the outer surfaces of a large
  mesh are loaded, the section panel says so (Boundary-only preview,
  Approximate cap — no cell data, Computing section, Exact section, Cap
  unavailable) instead of passing a hollow shell off as a cut. Surfaces cut
  by a plane are filled only where the cut forms closed loops: holes and
  separate bodies are kept, an open sheet is not filled, and a fill is
  trimmed by the other planes. Raising a plane never loads the whole volume
  by itself; Load full volume is offered, and refused above its limit.
- **Exact sections of large meshes.** Load cells for the cut reads the mesh
  in a background worker and draws only the cells the planes meet, with the
  real cells, not a shell. It works on meshes far past what fits on screen
  as a full volume (about 1 s at 1 M cells, 5–9 s at 8 M on the test
  machine). A new request replaces the one waiting, and the work is
  cancelled when the plane is cleared or deleted, the mesh is remeshed or
  unlocked, or the case is closed. A cut too dense to draw keeps the
  previous section on screen and says what to narrow.
- **Four section modes.** Slice (a flat section with cell outlines), Cut
  cells (every whole cell the plane passes through, on both sides), Clip (a
  smooth half) and Clip by whole cells (a stepped half) share one selector.
  Colour the cut by quality, cell type, refinement level or zone; the colours
  follow the cells they came from, a missing value is named rather than drawn
  as zero, and the scale holds still while the plane moves.
- **Place the plane by number.** Type the offset in the model's unit, step it
  with − and + or PgUp/PgDn (Shift for a tenth) by the cell size across the
  plane, flip which side is kept without moving the plane, and turn it by
  typed angles under Advanced. The keys act only while the view or the
  section panel has the keyboard, so they never steal a number you are
  typing. Look along plane turns the camera flat onto the section and puts it
  back when pressed again.
- **Dragging stays responsive.** On a large mesh a clip follows the mouse as a
  preview labelled "Preview while moving"; a slice or cut keeps its last
  result, labelled "Section at previous position", until the plane is let go.
- **Named sections, compare and freeze.** A section holds up to six named
  planes and is saved with the case by name, to be chosen again from the
  Saved list. Compare cuts the kept copy of each ticked snappy stage with the
  same planes, side by side under one legend, each labelled with its stage; a
  stage that was never kept is listed as unavailable, never replaced with the
  current mesh. Freeze cells keeps the cells of a cut on screen while the
  planes move; after a remesh they are shown from the kept stage, labelled,
  or dropped with the reason — never matched to cells of the new mesh.
- **Grading says where the small cells go.** Each axis of the base grid has a
  Grading ratio (1 or more; the largest cell over the smallest) and Fine
  cells at: − side (start), + side (end), Centre or Both edges. The page says
  what it will write, including the ratio a two-sided grading actually
  reaches, and refuses a grading with too few cells. Custom blocks get the
  same choice, plus Custom profile for typed segments; choosing a side
  rewrites the grading text, which stays editable.
- **The old "Fine end of X is the start" switch did the opposite.** On a
  custom block it put the small cells at the end, not the start, whenever the
  ratio was above 1 (measured on OpenFOAM 13: first cell 0.089, last 0.356
  for ratio 4). A turned-round grading with segments was also written
  unturned. Opening an existing project changes no mesh: every blockMeshDict
  is written exactly as before, and the choice is set to the side the small
  cells really were on. Where the switch was on, the base grid page shows a
  one-time note that the small cells were at the end and still are; choose
  Start to move them.
- **Farfield: box, sphere or cylinder, on both engines.** The outer boundary
  can be a box, a sphere or a cylinder around the model, with an automatic or
  typed centre, a radius, and for a cylinder a length and any axis. One
  setting serves both engines and survives switching between them. On Gmsh
  the bodies are cut out of the shape (a sphere's surface is `far_field`, a
  cylinder's is `far_field_side`, `far_field_inlet` and `far_field_outlet`
  along its axis); on snappy the shape becomes the outer boundary, one patch
  named `far_field`, and the background block grows to hold it. A farfield
  that would not hold the model is refused with a size that would. Measured
  on OpenFOAM 13, the fluid volume came within 0.4 % of the shape minus the
  body on every shape, the tilted cylinder included.
- **Farfield… on the Geometry page.** Beside Add, Farfield… opens the same
  setting: the controls follow the shape as it is chosen, a Farfield (shape)
  row lists it, Remove switches it off, and the view draws it with the patch
  names the chosen engine will give it. On Domain & regions it is drawn as a
  faint skin labelled "Outer boundary (farfield)", apart from the background
  block. A snappy launch refuses a fluid seed outside or on the farfield or
  inside a closed body, and warns when a seed sits in a sealed pocket.
- **Mesh quality and layer controls from OpenFOAM 13.** Min face flatness
  (strict and relaxed) is back on the quality settings, because v13 does use
  it; Max concavity of a merged layer face and Merge layer faces on one cell
  are on the layers page. Each writes nothing until it is set, so existing
  cases write the same dictionaries.
- **Exclude points.** Domain & regions takes points that name spaces to
  remove from the snappy mesh. Each has its own cube handle, drawn with the
  seeds, and the table says what the pre-launch check found. An exclude
  point in the same space as a seed, which OpenFOAM would silently ignore,
  refuses the launch once a finer check confirms it; before that it is a
  warning.
- **Change the core count of a mesh.** On the snappy route, Meshing resources
  offers Change core count of the mesh…. It shows what moves, what is not
  carried over (sets and refinement files) and what goes out of date, and
  runs only when confirmed. The change is made on a copy and checked (cells,
  faces, patches, zones and fields) before it replaces the live case, so a
  failure, a full disk or a crash leaves the old decomposition as it was.
  `foammesh redistribute preview|run` does the same from the command line.
- **See what checkMesh flagged.** Run every topology check, Run every geometry
  check and Write failed sets join Write the problem faces as a surface on
  both engines' check pages; the defaults run checkMesh exactly as before. After a check, Highlights from the last check lists what it
  wrote, one tick each, and draws a ticked result over the mesh: bad points
  as points, bad faces and cells as surfaces, each labelled with its check.
  Anything that cannot be drawn is greyed out with the reason.
- **Choose which feature edges are kept.** The Surface features page can keep
  only the edges inside or outside a box, or on one side of a plane, and can
  add edges from a feature file. It says when a subset kept no edges, and
  changing any of these makes extraction and every mesh after it out of date.
- **Fixes.**
  - A refreshed snappy dictionary no longer grows the background block a
    little more every time.
  - A real OpenFOAM 13 mesh that stores a list of equal values in short form
    now previews instead of being refused.
  - An added feature file is found by OpenFOAM 13 (the dictionary named it
    without its full file name).
  - A sphere or cylinder farfield on Gmsh no longer reports its faces as the
    sides of a box, or a box padding it never used.
  - A locked step's page stays read-only after a job ends or after its own
    run, and a locked region form or exclude-point table says under its
    fields why the edit was refused, instead of doing nothing or showing a
    bare "Operation failed".
  - The farfield is locked with the rest of a published mesh, from the
    Geometry page, the Gmsh panel and the command line alike.
- **Checked live on both engines.** A pass through the real windows walked
  the snappy and Gmsh routes on OpenFOAM 13: rename, removing layers,
  Proceed after an edit, Check & Proceed, unlock and undo, sections, plane
  placement, the new OpenFOAM 13 controls, farfields and changing the core
  count. It found the problems below, which are fixed.
  - A snappy mesh made on several cores can be unlocked and meshed again: the
    rerun starts from the restored stage instead of the old split mesh, and
    each regenerated stage is kept as its own result, not as the stage before
    it.
  - A case meshed on several cores reopens in its workflow, instead of as
    "External mesh" with the outline gone.
  - Adding a row to a table (a refinement, a layer group, a size field) keeps
    the values typed above it on the same page; before, they were dropped and
    never reached the mesher.
  - A layer group added on Boundary layers, or one that picks patches by a
    name pattern, is kept, instead of being deleted a moment after it was
    added.
  - Editing a refinement level no longer marks feature extraction out of
    date, and unlocking a step after deleting a box, sphere or cylinder no
    longer fails.
  - checkMesh highlights work when OpenFOAM runs in WSL; before, every
    highlight was greyed out as if the check could not write it.
  - Change core count of the mesh… works from the window: it used to refuse
    every click, and then its confirmation crashed. A change cut short by a
    crash, or by OpenFOAM being stopped, leaves the old decomposition as it was and
    no longer blocks the next change.
  - A Gmsh farfield's outer faces are published as patches, not walls, so a
    solver no longer treats the farfield as a solid wall. On snappy,
    detecting the fluid region with a farfield puts the seed inside the
    farfield.
  - The Farfield dialog grows to fit when a "Not accepted" line or the
    cylinder rows appear, instead of squashing the fields above them.
  - Rename offers the name the row shows; on an imported STL it offered the
    volume's name, so accepting it renamed the boundary after its volume.
  - After an unlock the view labels the mesh it keeps: "Previous mesh — edits
    to <step> not applied · revision N". A finished stage run says which
    revision it published.
- **Known limits.**
  - Keeping or refusing sealed cavities is a Gmsh setting only; on snappy a
    cavity is kept only if a region seed is placed in it, and a seed there is
    a warning.
  - On Gmsh a farfield needs CAD (STEP or IGES). With an STL model the
    Farfield dialog says why and keeps the setting, and the run meshes
    without a farfield and says so on the Quality page. On snappy the
    farfield is one patch, `far_field`, on every shape; it is not split into
    side, inlet and outlet.
  - There is no fixed limit on the number of cells. Loading, sections and
    meshing are refused only when the free memory cannot hold them, and the
    message says how much they need. Sections of 20 M cells take about
    4–5 s (a two-plane clip about 5.2 s); 50 M cells took 12–44 s using
    12–15 GB. Meshing runs inside WSL, whose own memory limit (about half the
    machine's RAM by default) is not yet counted.
  - The view uses the GPU that Windows gives it, of any make. If a faster
    GPU sits idle, the render note names it and says how to switch in
    Windows Settings › Graphics. While you rotate a large mesh a lighter copy
    is drawn, with full detail when you let go.
  - Dragging a section plane shows the plane moving; the exact cut is drawn
    when you let go, which still takes up to about half a second on a 5 M-cell
    volume.
  - On snappy, changing min face flatness or any other mesh quality limit
    on the Quality page re-runs the mesh from Snap, because snappyHexMesh
    uses these limits while it snaps and adds layers. The page says so
    before you edit. On Gmsh a change re-runs only the quality check.
  - A snappy mesh split into more separate regions than you placed seeds
    (for example by an exclude point in the wrong pocket) is now marked not
    ready to run, with the region count. This was checked against a real
    run's log but not yet in a live run.
  - FoamMesh closed unexpectedly once while a Gmsh mesh was run again with a
    section plane and a checkMesh highlight both on screen. It has not
    happened again in five attempts to repeat it.
  - The faster drag, the immediate "Unlocking…" and "Cancelling…" feedback
    and the cancelled Gmsh run keeping its mesh on screen were checked by
    tests but not yet timed in a live run.
  - Stage copies, Compare and a replayed rerun are snappy only; Gmsh meshes in
    one pass and reruns from its settings. Unlock keeps one undo.
  - A project meshed before this release has no publication record, so its
    steps stay unlocked until they are meshed again.
  - Change core count needs an already decomposed case on the snappy route;
    one-rank cases, the collated file format and multi-region meshes are
    refused with the reason.
  - The subset box and plane for feature edges are typed in; there is no
    handle in the view for them.

### Large models, parallel runs and resuming a run

- **Large far fields mesh quickly.** On snappy, a far field around small
  bodies is meshed coarse and refined back to the cell size you asked for
  around the bodies, so every size on the bodies is unchanged. A 0.5 m
  quadcopter in the default far field at 20 mm started from about 43,000
  background cells instead of 5.9 million, where castellation used to run for
  50 minutes and then fail.
- **See the cost before you run.** The base grid and Castellation pages show
  how many cells castellation starts from and how much memory they take, and
  warn when that will not fit in free memory. Settings > Preferences has a
  Stage time limit (0 means no limit); a stage stopped by it names the
  setting.
- **Auto uses the machine.** With the core count on Auto, snappy meshes on the
  WSL host's physical cores less one, limited only by free memory; before, it
  ran on one core. The Meshing resources page shows the count and how Auto
  reached it ("Auto: 16 physical cores on the WSL host, less 1; 64 GiB
  free"). Snap and Layers use the count Castellation ran on. Gmsh threads
  follow the same rule, and the old 64-thread cap is gone. More ranks than
  physical cores are started with `--oversubscribe` instead of failing.
- **A stopped Run to end can be resumed.** Stopping or losing a Run to end
  keeps every stage it finished, and Resume starts from the next one. A stage
  run on several cores is gathered and kept as soon as it ends. Save's
  tooltip says that finished stages are kept while a run is going.
- **A run meshes what is on the page.** A stage run uses the values typed on
  its page even before they are saved, and the base grid estimate follows
  geometry that is added, hidden or removed.
- **Importing big or messy models.** An OBJ whose faces each carry their own
  points is read as one shell, not one shell per triangle. The shell and
  self-intersection checks are much faster on large models. An import that
  runs out of memory fails with a message instead of hanging. The import
  dialog and the Geometry page say how big the whole model is.
- **Scale and the base grid.** Scale converts vertices you typed by hand, and
  the background block always surrounds the geometry, whatever scale an older
  project saved. Every base grid field states its unit and what it does;
  Standoff is a ratio; in target-size mode Castellation's base cell size is
  the target cell. Each refinement level says the cell size it makes, beside
  the field and in the table. Every visible axis of the domain box says its
  span.
- **Regions and features.** Accept all replaces the regions found before
  rather than adding to them, and Add starts a new seed inside the fluid.
  FoamMesh warns when the outside is meshed on a box flush with the model, on
  both sides of a surface, or around a duct. Solids with different feature
  levels each get their own feature file, the extracted-edges table lists
  feature pieces apart from their surface, and Castellation says which
  surfaces are written unrefined at feature level 0. OpenFOAM 13 has no
  separate curvature refinement; the help for the feature angle says how to
  get the same effect.
- **Fixes.**
  - Removing a geometry takes it out of the case completely: out of the
    readiness checks and the next prepared model, and its files are cleaned
    up when the case is next opened. A geometry removed but never saved comes
    back when the case is reopened, as the saved case still uses it.
  - A rerun after an unlock meshes the restored mesh, not an old split copy
    left from a parallel run.
  - An emptied layer group is kept; Remove on the older castellation page
    takes effect at once; an edit refused on a locked step says why; Delete
    removes the selected rows.
  - Kept stage copies that a newer copy replaces are pruned.
  - A patch list written without its count is read.
  - Settings rows sit one spacing apart, and hidden rows leave no gap.
- **Known limits.**
  - The parallel resume and the processor-folder fix were checked by tests
    and on a stand-in host, not yet in a live run on every machine size.
  - Restoring unsaved edits after a crash does not yet tidy geometry those
    edits had removed; it is tidied when the case is next opened.

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
