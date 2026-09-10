#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Boundary-patch rename / merge / split as pure, serializable operations.

A ``PatchSet`` is the authoritative list of named patches and the membership of
source sub-surfaces. Operations return new state and a description so the caller
(GUI/API) can wrap each in a ProjectState transaction; the PatchSet itself is
headless and unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Names OpenFOAM keeps for itself. `internalMesh` is the cell zone the mesh
#: writer emits, and `zones`/`boundary` are polyMesh files; a boundary carrying
#: one of them collides with something the solver already wrote. The rule lived
#: only in the edit dialogs, so the Repair page could set a name the dialog
#: would have refused. It lives here now, beside the PatchSet the solver name
#: actually comes from, and the view imports it from here.
RESERVED_NAMES = ('internalMesh', 'zones', 'boundary')


#: The engine-neutral categories a patch name can announce. Kept as a literal
#: tuple so this module stays importable without the Qt/db stack behind it;
#: ``test_geometry_boundary_category.py`` pins it against
#: ``BoundaryCategory`` so the two can never drift apart.
BOUNDARY_CATEGORIES = (
    'wall', 'inlet', 'outlet', 'symmetry', 'wedge', 'far_field', 'interface',
    'unclassified',
)


def boundary_category_for_name(name: str, default: str = 'wall') -> str:
    """Read the boundary category a patch name announces.

    R69. The Geometry list's Type column reads ``Boundary`` for every surface,
    so the wall/inlet/outlet category that decides the published
    ``constant/polyMesh/boundary`` type is shown nowhere on the page. MEASURED
    on a five-patch tee (``inlet``, ``outlet_top``, ``outlet_branch``,
    ``wall_main``, ``wall_branch``): the list said ``Boundary`` five times and
    the only way to learn that ``outlet_top`` had published as a wall was to
    read ``constant/polyMesh/boundary`` by hand.

    This is the same rule the publication step applies
    (``domain_operations._category_for_name``), lifted to a place the view can
    import: leading word decides, two-word categories such as ``far_field``
    match as a whole, anything else takes *default*.
    """
    text = str(name or '').strip().lower()
    known = set(BOUNDARY_CATEGORIES)
    if text in known:
        return text
    tokens = [token for token in re.split(r'[^a-z0-9]+', text) if token]
    for size in (2, 1):
        # Two words first: ``far_field_west`` is a far field, not a ``far``.
        head = '_'.join(tokens[:size])
        if head and head in known:
            return head
    return default


@dataclass
class Patch:
    name: str
    members: list[str] = field(default_factory=list)   # source sub-surface ids

    def to_dict(self) -> dict:
        return {'name': self.name, 'members': list(self.members)}


class PatchError(ValueError):
    pass


class PatchSet:
    def __init__(self, patches: list[Patch] | None = None):
        self._patches: list[Patch] = list(patches or [])

    # queries --------------------------------------------------------------
    def names(self) -> list[str]:
        return [p.name for p in self._patches]

    def get(self, name: str) -> Patch | None:
        return next((p for p in self._patches if p.name == name), None)

    def __len__(self) -> int:
        return len(self._patches)

    def add(self, name: str, members: list[str] | None = None) -> Patch:
        if self.get(name):
            raise PatchError(f'patch {name!r} already exists')
        p = Patch(name, list(members or []))
        self._patches.append(p)
        return p

    # operations -----------------------------------------------------------
    def rename(self, old: str, new: str) -> tuple[str, dict]:
        p = self.get(old)
        if p is None:
            raise PatchError(f'no such patch: {old!r}')
        if old != new and self.get(new):
            raise PatchError(f'patch {new!r} already exists')
        p.name = new
        return ('rename_patch', {'before': old, 'after': new})

    def merge(self, names: list[str], into: str) -> tuple[str, dict]:
        if len(names) < 2:
            raise PatchError('merge needs at least two patches')
        targets = []
        for n in names:
            p = self.get(n)
            if p is None:
                raise PatchError(f'no such patch: {n!r}')
            targets.append(p)
        members: list[str] = []
        for p in targets:
            members.extend(p.members)
        self._patches = [p for p in self._patches if p.name not in names]
        self._patches.append(Patch(into, members))
        return ('merge_patches', {'merged': list(names), 'into': into})

    def split(self, name: str, groups: dict[str, list[str]]) -> tuple[str, dict]:
        """Split *name* into new patches; *groups* maps new-name -> member ids."""
        p = self.get(name)
        if p is None:
            raise PatchError(f'no such patch: {name!r}')
        all_members = set(p.members)
        assigned = {m for ms in groups.values() for m in ms}
        if not assigned <= all_members:
            raise PatchError('split groups reference members not in the patch')
        for new_name in groups:
            if new_name != name and self.get(new_name):
                raise PatchError(f'patch {new_name!r} already exists')
        self._patches = [q for q in self._patches if q.name != name]
        for new_name, members in groups.items():
            self._patches.append(Patch(new_name, list(members)))
        return ('split_patch', {'source': name, 'into': list(groups.keys())})

    # persistence ----------------------------------------------------------
    def to_list(self) -> list[dict]:
        return [p.to_dict() for p in self._patches]

    @classmethod
    def from_list(cls, items: list[dict]) -> 'PatchSet':
        return cls([Patch(i['name'], list(i.get('members', []))) for i in (items or [])])
