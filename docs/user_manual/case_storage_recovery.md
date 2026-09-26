# Case Storage & Recovery

## Where FoamMesh keeps its state

A FoamMesh case is a normal OpenFOAM case directory plus one sidecar folder:

```
<case>/
  0/  constant/  system/          # standard OpenFOAM content
  constant/polyMesh/              # the mesh artifact
  foammesh/                       # FoamMesh sidecar (safe to inspect, do not hand-edit)
    project.json                  # case metadata: workflow mode, mesh origin,
                                  # mesh fingerprint, provenance
    artifact_history.json         # append-only history of mesh mutations,
                                  # imports, exports, and restores
    quality/latest.json           # last structured Mesh check report
    logs/                         # raw utility logs (checkMesh, transformPoints, ...)
    recovery/<id>/                # verified polyMesh recovery points
```

Deleting `foammesh/` never damages the OpenFOAM case — you lose history,
recovery points, and workflow provenance, not the mesh.

## The mesh fingerprint

Every mutation records a SHA-256 fingerprint of `constant/polyMesh`.
FoamMesh uses it to:

- mark Mesh check results **stale** the moment the mesh no longer matches;
- decide whether a case can reopen in the authored workflow (provenance must
  agree with the mesh on disk, otherwise the case degrades safely to
  external-mesh mode with an explanation);
- verify recovery points before restoring them.

## Recovery points

Mesh-changing operations (transform, repair, converter import, format
conversion) snapshot `constant/polyMesh` first:

- **Failure or cancel** → the snapshot is restored automatically; the history
  records `restored`.
- **Success** → the snapshot is kept as an *available* recovery point.

**Mesh → Restore previous mesh...** replaces the current mesh with the most
recent verified recovery point. This is an explicit artifact operation — it is
*not* Edit → Undo — and it creates its own history event. A point whose backup
fails fingerprint verification is never offered.

Headless equivalents: `foammesh restore <case>` and semantic operation
`mesh.restore` through `/api/v1`.

## Transaction history

**Edit → Transaction history** shows one auditable timeline of:

- reversible state edits (also reachable through Undo/Redo), and
- artifact events — mesh mutations, imports, exports, restores — with command,
  before/after fingerprints, and recovery status. These never enter the
  ordinary undo stack.

`foammesh history <case>` prints the same data as JSON.
