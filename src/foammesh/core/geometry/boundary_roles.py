#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Which boundaries grow layers, decided once from the role each one has.

Plan 32 W4: *default layer targets from boundary roles*, and §4.5's
"highlight selected walls and exclude known inlet/outlet/symmetry surfaces by
default". The product already knows the role of every boundary -- it is the
``BoundaryCategory`` the Geometry step authors, carried on each prepared
group and re-read from the patch name by
:func:`~foammesh.core.geometry.patches.ops.boundary_category_for_name`. What
was missing was anything that turned that knowledge into the layer targets,
so the answer was typed again by hand on every case.

MEASURED at HEAD d15414db, with layers enabled and nothing selected:

* Gmsh grew layers on **everything**. ``core/gmsh/layers.py`` reads an empty
  ``patches`` as ``SCOPE = 'all_boundary_surfaces'``, so the inlet and the
  outlet carried prisms across the flow face.
* snappy grew layers on **nothing**. ``case_builder._layer_surfaces`` builds
  ``addLayersControls { layers { } }`` out of the layer groups alone, and a
  case with no group writes an empty sub-dictionary; v13 reads that as a
  request for nothing and exits zero (DP-112).

Two engines, two opposite wrong answers, from one fact neither of them asked
for. The rule lives here, once, and both layers pages read it rather than
deriving a second opinion about it.

The sentence a page shows is here too, for the same reason: the two pages
have to name the defaulted targets in the same words, and the words follow
the rule that chose them.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

from .patches.ops import BOUNDARY_CATEGORIES, boundary_category_for_name

#: The roles that grow boundary layers when nothing was chosen by hand. A
#: wall is where the boundary layer of the flow is; every other role in
#: ``BOUNDARY_CATEGORIES`` is a place a prism cell would sit across the flow
#: (inlet, outlet, far_field), across a plane of symmetry (symmetry, wedge),
#: or on a face that is not a boundary of this volume at all (interface).
LAYER_ROLES = ('wall',)

#: The roles a default selection deliberately leaves flat. Kept explicit so a
#: new category added to ``BoundaryCategory`` shows up as a decision to make
#: rather than silently joining or missing the default.
FLAT_ROLES = tuple(role for role in BOUNDARY_CATEGORIES
                   if role not in LAYER_ROLES)

#: The role of a boundary nothing has classified. It is NOT ``wall``: a
#: boundary whose role nobody stated must not be handed layers on a guess.
UNKNOWN_ROLE = 'unclassified'


def boundary_role(name, category='') -> str:
    """The engine-neutral role of one boundary.

    The stored category wins whenever it says something. When it is absent or
    still ``unclassified`` the name is read, which is the same rule the
    publication step applies to give a patch its OpenFOAM type -- so the layer
    default and the published patch type can never disagree about which
    boundary is a wall.
    """
    token = str(getattr(category, 'value', category) or '')
    token = token.strip().lower().split('.')[-1]
    if token and token != UNKNOWN_ROLE and token in set(BOUNDARY_CATEGORIES):
        return token
    return boundary_category_for_name(name, default=UNKNOWN_ROLE)


def _pair(entry) -> tuple[str, str]:
    """``(name, category)`` out of whatever shape the caller holds."""
    if isinstance(entry, Mapping):
        name = (entry.get('solver_name') or entry.get('display_name')
                or entry.get('name') or '')
        return str(name).strip(), str(entry.get('category') or '')
    if isinstance(entry, (tuple, list)) and entry:
        category = entry[1] if len(entry) > 1 else ''
        return str(entry[0]).strip(), str(category or '')
    return str(entry or '').strip(), ''


def boundaries_from_manifest(manifest) -> tuple[tuple[str, str], ...]:
    """``(name, category)`` for each group of a prepared group manifest."""
    groups = (manifest or {}).get('groups') if isinstance(manifest, Mapping) \
        else None
    result, seen = [], set()
    for group in groups or ():
        name, category = _pair(group)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append((name, category))
    return tuple(result)


def default_layer_targets(boundaries: Iterable) -> tuple[str, ...]:
    """The boundaries layers grow on when nothing was chosen by hand.

    *boundaries* is any iterable of ``(name, category)`` pairs, of prepared
    group mappings, or of bare names. Order is kept: a page that lists its
    patches in prepared order proposes them in that order.
    """
    targets, seen = [], set()
    for entry in boundaries:
        name, category = _pair(entry)
        if not name or name in seen:
            continue
        seen.add(name)
        if boundary_role(name, category) in LAYER_ROLES:
            targets.append(name)
    return tuple(targets)


def flat_boundaries(boundaries: Iterable) -> tuple[str, ...]:
    """The boundaries the default deliberately leaves without layers."""
    targets = set(default_layer_targets(boundaries))
    names, seen = [], set()
    for entry in boundaries:
        name, _category = _pair(entry)
        if not name or name in seen or name in targets:
            continue
        seen.add(name)
        names.append(name)
    return tuple(names)


def default_layer_pattern(names: Sequence[str]) -> str:
    """The ``addLayersControls/layers`` key that selects exactly *names*.

    ``layerParameters.C:265-282`` turns a quoted key into a ``wordRe`` and
    ``Foam::regExp::match`` requires the expression to consume the whole patch
    name, so an alternation of the escaped names selects those names and
    nothing that merely starts with one of them. The writer quotes it; see
    ``core/layer_patterns.quoted_key``.
    """
    return '|'.join(re.escape(str(name)) for name in names if str(name))


def defaulted_targets_sentence(targets: Sequence[str],
                               flat: Sequence[str] = ()) -> str:
    """What a layers page says about targets it chose rather than was given.

    Plan 32 W4 asks for the defaulted targets to be highlighted. A colour is
    not a statement, so the mark is this sentence, naming every boundary the
    role rule picked and every boundary it left flat.
    """
    if not targets:
        return ''
    text = ('Nothing was chosen, so the wall boundaries are the default and '
            'have been saved for this case: %s.' % ', '.join(targets))
    if flat:
        text += (' %s grow no layers, because their role is not wall.'
                 % ', '.join(flat))
    return text
