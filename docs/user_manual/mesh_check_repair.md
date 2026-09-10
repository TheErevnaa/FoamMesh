# Mesh Check & Repair

## Mesh Info (Mesh → Info)

Reads `constant/polyMesh` directly — no solver launch. Reports point/face/
internal-face/cell counts, bounding box (with display unit), patches (name,
type, faces, start face), zones, per-file format/compression metadata, and
the last Mesh Check verdict with its staleness. Parser limits (for example a
binary points file) are reported as warnings — FoamMesh never fabricates
zero counts. **Copy Summary** and **Save Report...** (JSON/CSV) are built in.

Headless: `foammesh info <case> [--json|--report out.json]`, or semantic
operation `mesh.info` through `/api/v1`.

## Mesh Check (Mesh → Mesh Check)

Runs the configured `checkMesh` with `-allTopology -allGeometry` (the exact
options are probed from the utility's own `-help` output, never assumed).
Output streams live into the console; the raw log is kept under
`foammesh/logs/`.

The dashboard shows **Verdict · Key Metrics · Failed Checks · Patches/Sets ·
Recommendations · Raw Log**. Verdicts are words (`pass / warning / fail /
incomplete`) — never colour alone. A cancelled or truncated run is reported
as *incomplete*, not as a pass.

The structured report is stored with the mesh fingerprint; any later mesh
change makes it visibly **stale**. Selecting a failed cell set highlights it
in the viewport with a labelled high-contrast overlay; overlays are cleared
whenever the case, mesh, or result changes.

## Repair

Repair is a staged toolbox, not a universal "Fix Mesh" button.

**Pre-mesh surface repair** (no mesh yet): clean duplicates/degenerate cells,
fill holes, recompute consistent normals — always with before/after health
scores, and the choice to replace the working surface, **save a repaired
copy**, or keep everything unchanged.

**Post-mesh operations** — each is offered only when its utility exists in
the configured environment *and* its case prerequisites are satisfied:

| Operation | Utility | Prerequisite / safety |
|---|---|---|
| Renumber cells | `renumberMesh` | recovery point; counts compared |
| Collapse configured edges | `collapseEdges` | reviewed `system/collapseDict` |
| Extract cell set to a new case | `subsetMesh` | destructive — always runs in a new case copy |
| Rebuild patch definitions | `createPatch` | reviewed `system/createPatchDict` |
| Combine patch faces | `combinePatchFaces` | reviewed `system/combinePatchFacesDict` |

Every run creates a verified recovery point first, validates the produced
polyMesh, reruns Mesh Check when available, and reports the before/after
byte/patch counts plus the readiness delta. A failed repair restores the
previous mesh automatically; a successful one can still be reverted with
**Mesh → Restore Previous Mesh...** until a newer recovery point replaces it.
