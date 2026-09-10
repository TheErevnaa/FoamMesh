# Tutorial 2 — Duct Flow (Internal)

Goal: mesh the fluid volume inside a duct/manifold.

## 1. Geometry
- Import the duct surface (STL/OBJ, or STEP with `[cad]`). For internal flow the
  surface bounds the *fluid* region.
- Check watertightness — internal volumes must be closed for region detection.

## 2. Region
- Mark the enclosed volume as a **fluid** cell zone (Region step). FoamMesh uses a
  point inside the duct to seed the keepable region.

## 3. Base grid
- A tight bounding box around the duct; isotropic base cells sized to the smallest
  passage (aim ≥ 4–6 cells across the narrowest cross-section after refinement).

## 4. Castellation + snap
- Surface refinement level 2–3 on the walls; feature refinement on inlet/outlet rims.

## 5. Boundary layers
- Wall patches: 5 layers, expansion 1.2. Internal shear layers matter for pressure
  drop — use the y+ calculator with the bulk velocity and hydraulic diameter as L.

## 6. Run & export
```bash
foammesh mesh duct.stl -o duct_case/
foammesh checkmesh duct_case/log.checkMesh    # if you captured the log
```

## Agent shortcut
The **internal_duct** strategy proposes wall refinement + layers sized to the
passage. Accept or tweak per patch.
