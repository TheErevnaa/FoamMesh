# Themes & Accessibility

## Themes

**Settings → Theme** offers System / Light / Dark. Themes apply live — the
viewport, icons, and failed-cell overlays retheme without a restart, and the
configured value persists. Theme tokens drive both widget styling and VTK
colours, so the two never disagree.

## Accessibility

What FoamMesh guarantees:

- **Keyboard**: New (Ctrl+N), Open (Ctrl+O), Save (Ctrl+S), Close (Ctrl+E),
  Undo/Redo (Ctrl+Z / Ctrl+Shift+Z), and all menu actions are reachable by
  keyboard; Mesh check runs from the Mesh menu without the mouse.
- **No colour-only verdicts**: Mesh check severity is always a word
  (pass/warning/fail/incomplete); failed-cell overlays are labelled
  (`Failed: <set>`), not just red.
- **Safe defaults**: every confirmation for a destructive action (repair,
  replace mesh, restore, clean case, negative scale) defaults to the safe
  choice — pressing Enter never destroys data.
- **Screen readers**: icon-only viewport controls expose accessible names
  (e.g. "Fit view to model", "Set rotation center").
- **Disabled ≠ silent**: a disabled action always carries the reason in its
  tooltip and status tip.

Items verified per release on real hardware (see the release checklist):
logical tab order and visible focus, dialogs at minimum resolution and 200 %
scale, and screen-reader output on each platform.
