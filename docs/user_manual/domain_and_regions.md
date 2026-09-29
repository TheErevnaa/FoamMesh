# Domain & Regions

A snappyHexMesh mesh keeps the cells that can be reached from a **seed**: a
point you place inside the volume to be meshed. A seed on the wrong side of a
surface meshes the wrong side. The **Domain & Regions** step is where seeds
are placed, one per region, and where FoamMesh shows you what each one will
keep.

This page walks through it with the **annulus**: a pipe wall with an inner
radius of 0.06 m, an outer radius of 0.1 m and a length of 0.5 m. The fluid
is the 40 mm gap between the two walls. The pipe's core is open at both ends,
so it is part of the outside.

## What the page shows

- **The domain box.** The background mesh fills this box, and a seed only
  counts if it is inside it. The box is drawn while the page is open. If the
  domain is not a box, it is drawn block by block.
- **The regions table.** Each row is one region: its name, its type (Fluid
  or Solid) and its seed. A colour chip marks each row in the colour the
  viewport uses for that region. The row's tooltip also gives the volume of
  the space the seed is in, so the colour is never the only way to tell
  regions apart.
- **A warning line under the table.** It appears when two seeds are in one
  space, or when a Fluid seed and a Solid seed share a space, which snappy
  cannot mesh.

## Detect: let FoamMesh find the spaces

**Detect…** (Alt+T) asks how many fluid regions there are and whether the
flow is external. Then it finds the spaces the surfaces close off.

On the annulus:

1. Press **Detect…**, leave the count at **1** and press **Detect**. The
   detection runs in the background: the view keeps answering, and
   **Cancel** stops it.
2. The review lists one space of about 0.0100 m³. It is ticked and named
   `fluid_1`. In the view, the gap is drawn as a translucent ring labelled
   `fluid_1 · 0.0100 m³`. Its seed is at a radius of about 0.08 m, the point
   furthest from both walls.
3. Press **Accept all** (Ctrl+Enter). The region is written.

Now ask for **2** instead. FoamMesh cannot find a second space and says why:

> You asked for 2 fluid regions. The geometry encloses 1 space (0.0100 m³).
> The pipe's
> core is open at both ends, so it is part of the outside.

It then offers **Use 1** and **Include the outside as external flow**.

### Tidying the review

- **Rename or retype a row** in the table itself. Untick a row to leave it
  out.
- **Adjust…** (Alt+J) opens the selected row in the regions editor. There
  you can drag its seed, rename it or change its type. **OK** keeps the
  change in the review; nothing is written until **Accept all**.
- **Merge** (Alt+G) takes two selected rows (Ctrl+click, or Shift and an
  arrow key) and drops the second seed. It does this **only when FoamMesh
  can confirm that both seeds are in the same space**. If they are in
  different spaces, each needs its own seed, so nothing is merged and the
  panel says why. The same happens when the spaces cannot be confirmed yet.

### One Undo takes it all back

**Accept all** writes every ticked row in one step. **Edit ▸ Undo create
fluid regions** (Ctrl+Z) removes them all at once, and the note under the
table reminds you of this.

## Placing a seed by hand

**Add…** opens the regions editor under the table. If the settings column is
too narrow, the editor opens beside it instead. While the editor is open,
the rest of the page is locked. The viewport stays live.

The seed is drawn as a handle:

- A **solid ball** means the seed is inside the geometry.
- A **crossed ball** means it is outside, on a surface, or in a space that is
  open to the outside.
- A **hollow ball** means FoamMesh cannot yet tell.

A line under the coordinates says the same thing in words. Colour is never
the only signal. The space the seed is in is drawn in the region's colour.

On the annulus, type `0.08, 0, 0.25`. The ball is solid and the gap is
drawn. Change X to `0`: the seed is now in the core, the ball is crossed and
the line says *Outside the geometry*.

### Moving the seed

| To do this | Use |
|---|---|
| Move along one axis | Drag an arrow |
| Move within a plane | Drag a square |
| Move in the plane facing you | Drag the ball |
| Snap to one base-grid cell | Hold **Ctrl** while dragging |
| Put the seed back mid-drag | **Esc** |
| Nudge along X / Y | **Alt+←/→**, **Alt+↑/↓** in the editor, or the arrow keys in the view |
| Nudge along Z | **Alt+Page Up / Page Down** |
| Nudge ten steps | Add **Shift** |
| Undo / redo a placement | **Ctrl+Z** / **Ctrl+Y** (or Ctrl+Shift+Z) |

A seed never leaves the domain box, and it is kept off every face the mesh
will have at any refinement level.

Every drag, nudge or typed coordinate counts as one **placement**. While the
editor is open, Ctrl+Z moves the handle and the coordinates back one
placement at a time. Once the editor closes, Ctrl+Z is the main window's
Undo again.

### Section: seeing into the geometry

When the seed has to go inside something, such as the annulus gap seen from
outside the pipe, press **Section** (Alt+O). The model is cut on a plane
through the seed, across the view. The seed stays on the plane:

- push the plane to move the seed deeper;
- double-click on the plane to drop the seed there.

On the annulus, a section across the pipe shows the gap as a filled ring.
Double-click in the ring and the seed is placed.

### Finishing

Press **Enter** (OK) to keep the region, or **Esc** (Cancel) to leave it as
it was.

## Merging seeds already in the table

When the warning line says *Same space as fluid_1: this seed adds nothing*,
select the extra row and press **Merge** (Alt+G). FoamMesh drops that row
only if the fluid-space check confirms that its seed shares a space with
another row. If the seed is the only one in its space, or its space is not
known yet, nothing is removed and the note under the table says why.

## Keyboard and screen readers

- The focus order is: the regions table, **Add…**, **Edit…**, **Remove**,
  **Detect…**, **Merge**, then the editor when it is open.
- Every button's tooltip names its shortcut.
- The seed line is announced when a drag or nudge **ends**, not at every
  mouse move. It gives the position and whether the seed is inside.
- Each detection row describes its space in words (for example, "Kept,
  Space 1, 0.0100 m³"), as well as by its colour chip.
- The viewport labels use the theme's tooltip colours, which have at least
  4.5:1 contrast in both the light and the dark theme.

See also [Themes & accessibility](themes_accessibility.md) and
[Troubleshooting](troubleshooting.md).
