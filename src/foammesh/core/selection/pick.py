"""One pick request for every engine (Plan 42 SG13, decision D7).

A page that needs entities from the viewport - transfinite corners, points to
give a size, an operand of a Boolean - used to own a hidden entity panel of
its own, and each panel held a second picker over a second entity map. They
now ask the one :class:`~foammesh.core.selection.service.SelectionService`
for a pick: a :class:`PickRequest` says which kinds may be picked, whether the
order matters and what to do with the answer, and the one picker in the
viewport serves it.

No Qt, no Gmsh.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

__all__ = ['PickRequest', 'PickRefusal', 'pick_filters', 'GMSH_ONLY_FILTERS',
           'MESH_FILTERS']

#: Kinds that exist only in a Gmsh model: points and curves have no
#: counterpart in the shared prepared geometry.
GMSH_ONLY_FILTERS = ('Points', 'Curves')
#: Kinds that exist whenever a mesh is on screen, whichever engine made it.
MESH_FILTERS = ('Cells', 'Patches')
_COMMON = ('Surfaces', 'Volumes')


class PickRefusal(ValueError):
    """The request cannot be served; the message says why."""


def pick_filters(engine: str, *, mesh_on_screen: bool = False) -> tuple:
    """The kinds the Pick tool offers for *engine*, in display order.

    Points and Curves appear only for the Gmsh engine; Cells and Patches only
    while a mesh is on screen (of either engine).
    """
    names = []
    if str(engine or '').lower() == 'gmsh':
        names.extend(GMSH_ONLY_FILTERS)
    names.extend(_COMMON)
    if mesh_on_screen:
        names.extend(MESH_FILTERS)
    return tuple(names)


@dataclass(frozen=True)
class PickRequest:
    """What a page asks the Pick tool for."""

    filters: tuple
    #: ``'many'`` or ``'one'``.
    multiplicity: str = 'many'
    #: True when the order of the answer matters (transfinite corners).
    ordered: bool = False
    requester: str = ''
    on_accept: Callable | None = field(default=None, compare=False)
    on_cancel: Callable | None = field(default=None, compare=False)
    #: Keys that finish and abandon the pick.
    accept_keys: tuple = ('Return', 'Enter', 'e')
    cancel_keys: tuple = ('Escape', 'q')
    #: What a modifier means while this request is live; the volume owner
    #: and the Gmsh picker read shift differently.
    key_map: tuple = (('shift', 'extend'), ('ctrl', 'toggle'))

    def __post_init__(self):
        filters = tuple(dict.fromkeys(str(f) for f in self.filters))
        if not filters:
            raise PickRefusal('a pick request names at least one kind')
        if self.multiplicity not in ('one', 'many'):
            raise PickRefusal('multiplicity is "one" or "many"')
        object.__setattr__(self, 'filters', filters)
