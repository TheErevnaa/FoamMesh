# Tutorial 5 — Rotating Fan (MRF)

Goal: mesh a fan/impeller with a rotating cell zone for an MRF (Multiple Reference
Frame) simulation.

## 1. Geometry
- Import the fan blades + housing. Add a closed cylinder around the rotor to define
  the **MRF zone** (a cellZone, not a moving mesh here).

## 2. Region & cell zone
- Create a cellZone `rotor` from the cylinder (topoSet). The blades are walls.

## 3. Base grid + refinement
- Base grid sized to the housing. Surface refinement level 4–5 on the blades
  (curvature + tip gaps), feature refinement on blade edges.
- Region-refine the MRF cylinder so the rotating/stationary interface is clean.

## 4. Snap + layers
- Snap to blades and housing. Boundary layers on blade walls (thin → consider
  `displacementMotionSolver` for the tight blade gaps); check the layer-coverage
  report afterwards.

## 5. Run & QA
```bash
foammesh mesh fan.stl -o fan_case/
```
Blade tips and the MRF interface are the quality hot spots — verify
non-orthogonality/skewness there in the dashboard.
