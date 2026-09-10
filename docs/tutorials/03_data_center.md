# Tutorial 3 — Data-Center Outdoor CFD

Goal: external wind + thermal plume study around outdoor units (chillers, DG
exhaust) on a site.

## 1. Geometry
- Import the site assembly (STEP recommended via `[cad]` to keep per-equipment
  face names). Map CAD faces to patches: `chiller_intake`, `chiller_exhaust`,
  `dg_exhaust`, `building`, `ground`.

## 2. Domain
- A large atmospheric box: several building-heights up and to the sides, longer
  downwind. Ground is a wall; top/sides are far-field.

## 3. Refinement
- Surface level 3–4 on equipment, level 2 on buildings.
- Region boxes: refine around intakes/exhausts and the downwind recirculation zone.

## 4. Layers
- Ground + building walls: 3–5 layers (atmospheric BL is thick; modest y+).

## 5. Run & QA
```bash
foammesh mesh site.stl -o site_case/
```
Watch cell count — the estimator warns before oversized jobs. Use region
refinement rather than a high global level to keep counts manageable.

## Notes
Recirculation between exhaust and nearby intakes is the key risk; refine those
regions and verify mesh quality there in the QA dashboard (failed-cell highlight).
