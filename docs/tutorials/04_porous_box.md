# Tutorial 4 — Porous / Perforated Box

Goal: mesh a domain containing a porous zone (filter, louver, perforated panel)
represented as a cell zone rather than resolving every hole.

## 1. Geometry
- Import the enclosure plus a closed surface delimiting the porous region.

## 2. Region & cell zone
- Create a **cellZone** for the porous block (Region step). The zone will carry a
  porosity/Darcy-Forchheimer model later in the solver — here we just mesh it.

## 3. Base grid + refinement
- Uniform base grid; refine the porous-zone interface (level 2–3) so the zone
  boundary is captured cleanly.
- `topoSet` defines the cellZone from the delimiting surface.

## 4. Snap + layers
- Snap to the enclosure walls; layers on solid walls only (not on the porous
  interface).

## 5. Run & export
```bash
foammesh mesh enclosure.stl -o porous_case/
```
Confirm the cellZone exists in `constant/polyMesh` (cellZones file) before export.
