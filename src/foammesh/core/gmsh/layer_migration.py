"""What a case saved before the layer choice existed means now.

Plan 33 section 1.1. An empty layer selection used to mean "every boundary
surface", so a project saved with layers enabled and nothing named grew
prisms on its inlets and outlets. That meaning is gone, and a migration that
simply left the selection empty would silently turn those layers off: the
case would mesh, report success, and have no near-wall resolution anywhere.

So a saved case with layers on and no named surface is read as an explicit
``All eligible walls``, which is the closest honest reading of what it grew:
the same boundaries minus the inlets and outlets that should never have had
prisms in the first place. The document level pass can only set the choice,
because a document carries no geometry; the page completes it against the
prepared boundaries and shows the set that resulted, so the change is on
screen rather than buried in a file.
"""

from __future__ import annotations

from .layer_targets import (
    MODE_ALL_WALLS,
    MODE_SELECTED,
    MODE_UNSET,
    eligible_wall_names,
    normalise_mode,
    patch_names,
)

#: The schema leaf the choice is saved under.
PATCH_MODE_KEY = 'patchMode'


def migrated_mode(values) -> str:
    """The choice a saved layer block implies, without touching it.

    Returns the empty string when the block already carries a choice, so a
    caller can tell a migration from a no change.
    """
    values = values or {}
    if normalise_mode(values.get(PATCH_MODE_KEY)) != MODE_UNSET:
        return MODE_UNSET
    if not bool(values.get('enabled', False)):
        return MODE_SELECTED
    if patch_names(values.get('patches')):
        return MODE_SELECTED
    return MODE_ALL_WALLS


def migrate_layer_selection(values, boundaries=()) -> dict:
    """Read a saved layer block against the geometry that is prepared.

    The answer is a plain record rather than a rewrite, because the caller
    knows whether it may write: the schema pass may, a page refresh may not
    until the user is looking at it.

    ``note`` is empty when nothing changed. When something did, it names the
    boundaries the layer now covers, because a migration a user cannot see is
    a mesh that quietly changed shape.
    """
    values = values or {}
    mode = migrated_mode(values)
    if mode == MODE_UNSET:
        settled = normalise_mode(values.get(PATCH_MODE_KEY))
        return {'mode': settled, 'patches': patch_names(values.get('patches')),
                'note': '', 'changed': False}
    if mode == MODE_SELECTED:
        return {'mode': mode, 'patches': patch_names(values.get('patches')),
                'note': '', 'changed': True}
    effective = eligible_wall_names(boundaries)
    note = ''
    if effective:
        note = (
            'This case was saved when an empty selection meant every '
            'boundary, so its layers also stood on the inlets and outlets. '
            'They now grow on the walls only: '
            + ', '.join(effective) + '.')
    return {'mode': mode, 'patches': effective, 'note': note, 'changed': True}


def migrate_document(document) -> bool:
    """Give a saved document the choice it predates. Returns whether it did.

    Only the choice, never the names: the boundaries live in the geometry,
    which this pass cannot read.
    """
    if not isinstance(document, dict):
        return False
    gmsh = document.get('gmsh')
    if not isinstance(gmsh, dict):
        return False
    layers = gmsh.get('boundaryLayers')
    if not isinstance(layers, dict):
        return False
    mode = migrated_mode(layers)
    if mode == MODE_UNSET:
        return False
    layers[PATCH_MODE_KEY] = mode
    return True
