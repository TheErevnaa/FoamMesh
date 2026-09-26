# Opening a case made by an older FoamMesh

## What you will see

A case saved by a FoamMesh older than the provenance record opens with its mesh
on screen, in **External mesh** mode: the mesh is drawn, Mesh check and the
exports work, and the meshing steps that would have produced it are not offered.
Nothing is broken and nothing is lost — the mesh in `constant/polyMesh` is the
one you made.

## Why

Every FoamMesh case carries a sidecar at `foammesh/project.json`. It records
what produced the mesh: `workflow` (authored here, or opened from elsewhere),
`mesh_origin`, and `mesh_fingerprint` — the digest that says *this* mesh is the
one that workflow wrote.

Cases written before that record existed carry a sidecar with

```json
{"workflow": "none", "mesh_origin": "none", "mesh_fingerprint": null}
```

The sidecar is valid and it is ours, so the case is recognised as a FoamMesh
case. But there is no claim in it that FoamMesh authored the mesh sitting beside
it, and FoamMesh will not invent one: a mesh it cannot tie to a recorded run is
treated as somebody else's, which is the safe reading. Resolving that sidecar
gives **External mesh** with the reason *"mesh exists but metadata has no
workflow mode"*.

Measured on 2026-09-05 against `test_cases/snappyhexmesh/duct`,
`test_cases/snappyhexmesh/elbow` and `test_cases/gmsh/pipe`: all three classify
as a FoamMesh case, all three resolve to External mesh, none carries workflow
state. The classification now says so in its own words — `classify_case` returns
the reason `legacy sidecar: valid FoamMesh metadata with no workflow mode beside
an existing mesh; the case opens as External mesh with no workflow state`.

## What you can do with it

**Use the mesh.** Nothing about External mesh limits the mesh itself. View it,
run Mesh check on it, export it, convert it. This is the right choice when the
case is a finished result you want to keep exactly as it is.

**Re-author it.** To get the meshing steps back, re-import the geometry and set
the case up again in the current version, then mesh. The run writes the
provenance record itself, so from the first run onward the case opens in its
workflow. The old mesh is overwritten by the new one, which is the point: a
workflow that cannot claim the mesh on disk is a workflow you cannot trust the
next step of.

There is no in-place upgrade, deliberately. Stamping `workflow: authored` onto
an old sidecar would assert that settings in the case produced the mesh next to
it, which nothing in the file supports — and the first stale-mesh check after
that would be answering with a guess.

## A case saved before the workflow rows merged

September 2026 merged the outline into one guided workflow: three shared rows
in front -- **1. Geometry**, **2. Mesh setup**, **3. Preparation** -- and the
mesher's own rows numbered on from 4, ending at Export. `1. Mesh intent`,
`2. Execution` and the Gmsh `Describe geometry` row were folded into those
three; every question they asked is still asked, on **2. Mesh setup** and in
the Advanced section of **3. Preparation**.

A case saved before that opens on the new outline, and nothing you authored
and nothing you meshed is lost. What is reset is the task progress alone --
the ticks beside the rows. Saved progress is tied to the shape of the workflow
it was recorded against, and carrying a tick across a renumbering would mark a
row as done that nobody had seen; FoamMesh says so instead, in the status line:
*The meshing workflow changed, so saved task progress has been reset. Meshes
and reports are untouched.* Your geometry, every value on every page, the run
directories under `foammesh/runs/` and the published `constant/polyMesh` are
untouched, and the case was standing on a row that still exists when you left
it -- the rows that went away answer to the rows that replaced them, so a
reopen puts you back where you were rather than nowhere.

Walking the rows again settles the ticks. Where a row was already run, the
press is the one that runs it again; where it only records what you filled in,
the press records what is already on the page.

## Which files matter

| Path | What it is |
|---|---|
| `foammesh/project.json` | The provenance sidecar. Legacy ones say `workflow: none`. |
| `constant/polyMesh` | The mesh. Read and used in either mode. |
| `geometry_input/` | The sources the case was built from, if the case has them. |

Nothing here needs editing by hand. A sidecar edited to a state the case cannot
support is reported as invalid rather than repaired, so the recovery path stays
open — see `case_storage_recovery.md`.

## The run records inside an older case

The sidecar is not the only record a case carries. A Gmsh case holds one
directory per run under `foammesh/runs/`, with the job that was sent, the mesh
that came back and a manifest of what happened. Those documents changed in
September 2026, and older ones are read as follows.

| What the old record says | What FoamMesh does with it |
|---|---|
| A scope named by an index into one imported file | Rewritten to a source-aware entity id, but **only** when the prepared geometry it meshed is still in the case and still places that scope at the same index. |
| A run that recorded no output files | The files in the run directory are listed and hashed as found, marked `reconstructed`. |
| A run that recorded no result | The mesh in that run's own directory becomes the run's result; a mesh published into `constant/polyMesh` is listed as a derived copy, because a later run may have replaced it. |
| A boundary layer setting written before layers could be put on named patches | The setting is rewritten to say, in the current words, what it already meant: the whole boundary. A setting that says it layered named patches and names none is sent back to be authored again. |
| Layer heights computed by a calculation this build no longer contains | Kept as the record of what ran, and marked to be re-derived before they are compared with a current run. |
| `exit_code: null` | Left null. It was never recorded — see below. |
| A dictionary snapshot or mesh check naming no run | Kept and shown, and marked as evidence to re-take. |

To look at a case before opening it, or to migrate a copy of it:

```
python scripts/adopt_legacy_case.py <case>
python scripts/adopt_legacy_case.py <case> --migrate --copy-to <directory>
```

The first form only reads. The second copies the case and writes the adopted
records into `foammesh/migration/<timestamp>/` inside the copy. Neither form
ever rewrites an existing file, and neither touches a mesh.

## When re-preparation is required

Some provenance cannot be rebuilt from what is on disk, and FoamMesh says so
rather than approximating it:

- **A case that imported more than one CAD file before September 2026.** The
  scope indices recorded then were local to one file while the mesher resolved
  them globally, and nothing in the case says which file each index came from.
  Prepare the geometry again and re-run before trusting any refinement, layer
  or periodic control in that case.
- **A case whose prepared geometry revision has been deleted.** The scopes have
  nothing left to resolve against.
- **A case whose boundary layers were said to run on named patches without naming them.** Nothing on disk says which patches were meant, and reading it as the whole boundary would layer surfaces the run did not. Author the layer scope again before re-running. No case in the catalogue has this shape; the check exists so one would not pass silently.
- **A case whose geometry was re-prepared after the run.** If the current
  revision puts a scope at a different surface than the run recorded, the model
  changed under the result; the mesh is kept and the controls are not carried
  across.

In all three the mesh, the run directory and the published `constant/polyMesh`
are left exactly as they are. Losing an accepted mesh is worse than refusing to
re-label it.

## Evidence that cannot be completed

Three things in an older case are missing rather than wrong, and no migration
supplies them:

- **`exit_code`.** Every run manifest written before September 2026 records
  `null` here, successes and failures alike, because the recorder read a key
  the payload never had. Re-run to obtain it.
- **The target and element order of a Gmsh run** older than the export record.
  The MSH version is recoverable — the mesh file states it in its own header —
  but which solver the run was aimed at, and at what element order, was never
  written down. Reading it from the case's current settings would report
  today's setting as though the run had chosen it.
- **The tie between a snappy dictionary snapshot and the mesh beside it.** The
  snapshot records what was written, not which mesh came out of it.

A case in this state opens, meshes, checks and exports normally. What it will
not do is quote an old verdict as though it were about the mesh in front of you.
