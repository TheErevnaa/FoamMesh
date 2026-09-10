# Tutorial 1 — Ahmed Body (External Aerodynamics)

Goal: mesh a classic bluff body in a far-field domain.

## 1. Geometry
- Import `ahmed.stl` (Geometry step). Confirm the unit (the Ahmed body is ~1.044 m
  long — if the bounding box reads ~1044, choose **mm**).
- Run diagnostics: it should be watertight with no open edges.

## 2. Domain (base grid)
- Far-field box ≈ 3 lengths upstream, 6 downstream, 2–3 lengths sides/top.
- Base cells: start coarse (e.g. 120 × 40 × 30) — snappy refines locally.

## 3. Castellation
- Surface refinement on the body: level 4–5.
- Feature refinement at 150° to capture the slant edges.
- Region (box) refinement in the wake behind the body: level 2.

## 4. Snap
- Defaults are usually fine; increase `nFeatureSnapIter` if edges look rounded.

## 5. Boundary layers
- Wall patch (`ahmed`): 5–8 layers, expansion 1.2.
- Use the y+ calculator: U≈40 m/s, L≈1.044 m, ν≈1.5e-5, target y+≈30 (wall
  functions) → first layer height.

## 6. Run & check
```bash
foammesh mesh ahmed.stl -o ahmed_case/
```
Inspect the QA dashboard: max non-orthogonality < 65, max skewness < 4.

## Agent shortcut
Ask the agent for an **external_aero** strategy; it proposes the far-field box,
body refinement, wake box, feature edges, and layers — review and accept.
