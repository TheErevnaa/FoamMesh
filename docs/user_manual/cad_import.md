# CAD Import (STEP / IGES / BREP)

FoamMesh imports native CAD via OpenCASCADE while retaining the exact CAD
shape as the source for controlled tessellation and prepared face groups.

## Enable

```bash
pip install "foammesh[cad]"     # pythonocc-core / OCCT
```

Without the extra, CAD menu actions report a clear "install CAD support" message;
the rest of the app is unaffected.

## What you get

- **Formats**: `.step` / `.stp`, `.iges` / `.igs`, `.brep`.
- **Assembly tree**: products / bodies / solids / faces, with CAD names preserved.
- **Units**: read from STEP/IGES and mapped to SI (no guessing for CAD).
- **Controlled tessellation**: linear (chord) deflection and angular deflection
  drive facet density. The CAD shape stays the source; the tri-surface is
  regenerated when you change parameters (no re-import).
- **Per-face patch tags**: triangles carry a `cadFaceId`, so patches map to CAD
  faces/bodies rather than picking STL triangles.
- **Healing**: sewing + `ShapeFix` clean gaps / small edges / duplicate faces; the
  report is recorded as a transaction.

## Tessellation guidance

| Parameter | Effect | Typical |
|-----------|--------|---------|
| linear deflection | max chord error (model units) | 0.1–1.0 mm for cm/mm parts |
| angular deflection | max facet turn | 10–30° |

Finer values → more triangles → better surface capture but heavier snappy.

## Workflow

1. Import the CAD file → assembly tree appears.
2. Confirm/adjust the unit.
3. Set tessellation parameters and preview facet count.
4. (Optional) Heal the shape.
5. Assign patch names from CAD faces/bodies.
6. Continue with base grid → castellation → snap → layers as usual.
