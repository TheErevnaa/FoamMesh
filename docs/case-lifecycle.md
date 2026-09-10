# FoamMesh case lifecycle

FoamMesh opens native OpenFOAM cases in place. Its owned state lives under
`foammesh/`; `constant/polyMesh` is never moved merely because a case is opened.

## Save choices

- **Save** atomically replaces the current FoamMesh state after checking that
  neither the sidecar nor `constant/polyMesh` changed outside FoamMesh.
- **Save FoamMesh State As…** creates a new case containing the current
  FoamMesh configuration, local settings, history, and a fresh project
  manifest. It deliberately does not copy native geometry, mesh, time, or
  solver files. The destination is committed by a sibling staging rename.
- **Save Project As…** makes a verified full case-directory copy. Disposable
  `foammesh/cache/` and live lock files are excluded. The copy is staged and renamed only after its
  file list, sizes, and checksums match.

When an external change is detected, Save offers Reload, Save Copy, or Cancel.
Mesh-changing jobs are recoverable artifact operations; they are recorded and
mark the case dirty, but do not enter the ordinary state-edit undo stack.
