# OpenFOAM Environment

FoamMesh targets **OpenFOAM Foundation v13** and discovers utilities from the
environment it was launched in (`PATH`). It never assumes a utility exists:

- Every menu action that needs a utility (`checkMesh`, `transformPoints`,
  converters, `foamMeshToFluent`, `foamFormatConvert`, repair utilities,
  ParaView) is **capability-gated**. Missing utilities leave the action
  visible but disabled, with the reason in the tooltip/status tip.
- Before running, FoamMesh probes the utility's own `-help` output and builds
  argv against the *actual* contract (for example the Foundation-v13
  single-expression `transformPoints "Rz=45"` form, or whether `checkMesh`
  advertises `-writeSets`). If the configured utility does not advertise the
  required contract, the action explains that instead of running blind.

## Setting up

Launch FoamMesh from a shell where your OpenFOAM environment is active
(e.g. after `source <openfoam>/etc/bashrc` on Linux, or from an OpenFOAM
terminal on Windows/WSL setups). **External Tools → Terminal Here** opens a
terminal in the case directory with the same environment.

Changing **Parallel → Environment** re-probes capabilities; the menus update
immediately.

## What each target needs

A "target" here is the solver a mesh is being made for. Choosing one decides
which utilities must be installed and what the run may produce.

| Target | Needs installed | Produces |
|---|---|---|
| **OpenFOAM 13** (snappyHexMesh) | the OpenFOAM 13 environment on `PATH`, including `snappyHexMesh`, `blockMesh`, `surfaceFeatures` and `checkMesh` | `constant/polyMesh`, first order |
| **OpenFOAM 13** (Gmsh route) | the same environment, plus Gmsh in the runtime: `wsl -d OpenFOAM13Runtime -u root -- pip3 install gmsh` | the run's `mesh.msh` (MSH 2.2) and a published `constant/polyMesh`, first order |
| **SU2** (Gmsh route) | Gmsh in the runtime; nothing SU2-specific, and no extra Python package | the run's `mesh.msh` and `mesh.su2`, first order; no polyMesh is published |
| **No target** (Gmsh route) | Gmsh in the runtime | the run's `mesh.msh`, first or second order, with a polyMesh published so the viewport and `checkMesh` can open it |

Gmsh must be at least **4.11** (override with `FOAMMESH_GMSH_MINIMUM_VERSION`);
the measurements in this manual were taken on **4.15.2**. The Meshing Method
step probes the runtime and distinguishes a missing distribution, a missing
package and a version below the minimum, because the fix differs. See
[meshing with Gmsh](gmsh_workflow.md) for the full workflow, and
[export formats](export_formats.md) for what each target may export.

Optional Python extras: `[cad]` for STEP/IGES/BREP import, `[export]` for the
Gmsh `.msh` export, and a VTK build carrying `vtkCGNSWriter` for CGNS.

## Verifying

- `foammesh formats` — prints which converters/exporters were found and the
  reason for anything unavailable.
- The live validation matrix (release checklist) records tool versions and
  fixture results on the validation machine.
