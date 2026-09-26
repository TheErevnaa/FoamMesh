#!/usr/bin/env python3
"""Which boundaries receive layers, decided once for the page and the runner.

Plan 33 section 1.1. The shipped meaning of an empty layer selection was
"every boundary surface", so a user who ticked nothing got prisms standing on
the inlet and the outlet planes -- wrong for every flow case, and invisible
until the mesh was opened. The selection is now a choice with two spellings:
name the surfaces, or ask for every eligible wall.

That second spelling needs a rule, and a rule stated twice is a rule that
drifts. This module holds it once. It carries no imports beyond the standard
library so the runner, which executes inside WSL with nothing of the
application on its path, can import it as a sibling the way it already
imports ``shell_topology`` and ``field_graph``; the host reaches the same file
through ``foammesh.core.gmsh.layer_targets``, which loads it out of the
resource tree rather than keeping a second copy.

The rule itself is the one the publication step already applies to decide a
patch's ``constant/polyMesh/boundary`` type: a stored category wins, and
without one the patch name's leading word decides. The runner only ever has
the name, because a job carries surface names and no categories; the page has
both, and passes what it has.
"""

from __future__ import annotations

import re

#: The category whose boundaries grow layers. A layer on anything else is a
#: prism standing across the flow.
LAYER_CATEGORY = 'wall'

#: Every category a boundary name can announce. Kept in step with
#: ``foammesh.core.geometry.patches.ops.BOUNDARY_CATEGORIES``; a gate compares
#: the two so the runner and the host cannot drift apart.
BOUNDARY_CATEGORIES = (
    'wall', 'inlet', 'outlet', 'symmetry', 'wedge', 'far_field', 'interface',
    'unclassified',
)

#: The categories whose OpenFOAM patch type is a property of the *mesh*
#: rather than of the surface that names it. A ``wedge`` patch is one half of
#: an axisymmetric pair on a mesh one cell thick and an ``empty`` patch is one
#: of that mesh's two flat faces; neither contract can be satisfied by a
#: three-dimensional tetrahedral or hex-dominant mesh, which is what this
#: pipeline publishes everywhere except the section extrusion. Kept in step
#: with ``foammesh.openfoam.case_builder._MESH_CONSTRAINED_PATCH_TYPES``, the
#: snappy pipeline's copy of the same set; a gate compares the two the way one
#: already compares ``BOUNDARY_CATEGORIES`` against ``patches/ops.py``.
MESH_CONSTRAINED_CATEGORIES = ('wedge', 'empty')

#: Layers grow on the surfaces the plan names, and on nothing else. An empty
#: selection under this mode grows no layer at all.
MODE_SELECTED = 'selected'

#: Layers grow on every boundary this rule calls a wall, resolved against the
#: geometry the run actually imported.
MODE_ALL_WALLS = 'all_eligible_walls'

MODES = (MODE_SELECTED, MODE_ALL_WALLS)

#: What a saved case written before the choice existed carries.
MODE_UNSET = ''


def normalise_mode(value) -> str:
    """Read a stored mode, tolerating an enum, a label or nothing at all."""
    token = str(getattr(value, 'value', value) or '').strip().lower()
    token = token.split('.')[-1].replace(' ', '_').replace('-', '_')
    if token in MODES:
        return token
    return MODE_UNSET


def boundary_category(name, category='') -> str:
    """The category a boundary belongs to: what is stored, else what it says.

    The same reading as
    ``foammesh.core.geometry.patches.ops.boundary_category_for_name``: an
    exact name wins, then a two-word head such as ``far_field``, then the
    leading word. Anything else is unclassified, which is not a wall: a
    surface nobody has named is not one this rule will put prisms on.
    """
    stored = str(category or '').strip().lower()
    if stored in BOUNDARY_CATEGORIES:
        return stored
    text = str(name or '').strip().lower()
    if text in BOUNDARY_CATEGORIES:
        return text
    tokens = [token for token in re.split(r'[^a-z0-9]+', text) if token]
    for size in (2, 1):
        head = '_'.join(tokens[:size])
        if head and head in BOUNDARY_CATEGORIES:
            return head
    return 'unclassified'


def is_mesh_constrained(category) -> bool:
    """Whether this category names a patch type the *mesh* has to earn."""
    return str(category or '').strip().lower() in MESH_CONSTRAINED_CATEGORIES


def publishable_category(category):
    """The category a boundary will actually publish with.

    DP-444 and DP-445, which are one rule asked at two places. A surface
    called ``wedge_wall3`` announces the ``wedge`` category off its leading
    word, and that category is read twice: once by the publication step, to
    write ``type wedge`` into ``constant/polyMesh/boundary``, and once by
    this module, to decide that a surface which is not a wall grows no layer.
    Both readings are wrong for the same reason -- the surface is a wall, on
    a model that happens to be called ``wedge`` -- and both are repaired by
    asking what the patch will publish as rather than what its name says.

    A mesh-constrained category is answered ``wall``; everything else comes
    back exactly as it was given, so a caller can compare the two and know
    whether a substitution happened. ``boundary_category`` is deliberately
    left alone: the repository settled on keeping the name rule and guarding
    at the consumer, and ``test_a_wall_named_wedge_is_not_a_wedge_patch``
    pins that reading on purpose.
    """
    return LAYER_CATEGORY if is_mesh_constrained(category) else category


def _pair(entry) -> tuple[str, str]:
    """A boundary, however the caller happens to hold one.

    The page has manifest groups, the runner has bare names, and the tests
    have tuples. All three mean a name and possibly a category.
    """
    if isinstance(entry, dict):
        name = (entry.get('solver_name') or entry.get('display_name')
                or entry.get('name') or '')
        return str(name).strip(), str(entry.get('category') or '').strip()
    if isinstance(entry, (tuple, list)):
        items = list(entry) + ['', '']
        return str(items[0] or '').strip(), str(items[1] or '').strip()
    return str(entry or '').strip(), ''


def eligible_wall_names(boundaries) -> tuple[str, ...]:
    """The boundaries a layer may grow on, in the order they were given.

    This is what ``all_eligible_walls`` means, and the only place it is
    decided. Duplicates are dropped; an unnamed entry is skipped.
    """
    names, seen = [], set()
    for entry in (boundaries or ()):
        name, category = _pair(entry)
        if not name or name in seen:
            continue
        seen.add(name)
        # DP-444. The category a layer is judged by is the one the patch
        # will publish with, not the one its name announces. MEASURED on the
        # W-G campaign: `wedge/gmsh/flat` refused after 45.6 s because the
        # seven surfaces it offered were named `wedge_wall1 .. wedge_wall7`,
        # every one of them read as the `wedge` category off its leading
        # word, and this test disqualified all seven -- so boundary layers
        # were on with nothing to grow them on. `duct_wall1`, identical in
        # every other respect, meshed. The publication step substitutes
        # `wall` for a mesh-constrained category (DP-445), so that is what
        # these surfaces are, and this is where the page has to agree.
        if publishable_category(
                boundary_category(name, category)) == LAYER_CATEGORY:
            names.append(name)
    return tuple(names)


def patch_names(value) -> tuple[str, ...]:
    """A stored selection, whether it is a list or a comma separated string."""
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = value.replace('\n', ',').split(',')
    else:
        candidates = list(value)
    names, seen = [], set()
    for candidate in candidates:
        name = str(candidate).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return tuple(names)


def resolve_layer_patches(mode, patches, boundaries=()) -> tuple[str, ...]:
    """The boundaries that receive layers under *mode*.

    ``selected`` takes the plan at its word, including when the plan names
    nothing: growing on nothing is a state a user can ask for, and it is not
    the same as growing on everything. ``all_eligible_walls`` ignores the
    stored names and reads the geometry in front of it, so a case that gains
    a wall gains a layer without anyone editing a list.
    """
    if normalise_mode(mode) == MODE_ALL_WALLS:
        return eligible_wall_names(boundaries)
    return patch_names(patches)
