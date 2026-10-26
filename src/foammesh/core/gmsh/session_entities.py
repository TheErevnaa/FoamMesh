"""Entity picking, labels and physical groups on the Gmsh session (Plan 41 GN4).

Gmsh's own GUI picks points, curves, surfaces and volumes in the model view
(``Geometry > Physical groups > Add``: click, shift-click, ``e`` to end, ``q`` to
abort).  This module is that interaction without a window, so it can be tested
and so the viewport only has to feed it clicks:

* :class:`EntityMap` reads the per-cell entity tags the session writes into
  ``geometry.vtp`` (``gmshEntityDim`` / ``gmshEntityTag``) and the entity table
  of ``session.entities``; a rendered cell id becomes an entity.
* :class:`EntityPicker` is the state machine: a selection filter (Points,
  Curves, Surfaces, Volumes), hover, click, shift-click, accept, abort, and the
  chip list.
* :class:`PhysicalGroupEditor` adds, renames, re-fills and removes physical
  groups over picked entities through the session, and reads them back.
* :func:`entity_labels` is the text and anchor of the "Entity labels" toggle.
* :func:`coherence` is Geometry > Coherence as an explicit action.

Nothing here imports Qt or VTK except :meth:`EntityMap.from_vtp`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

#: Selection filter -> entity dimension.  ``Cells`` is the mesh element
#: filter of the same chip row (Plan 48 PK, DP-4503; GN13 called it
#: ``Elements``); the mesh viewport carries it and a picked element is the
#: pair ``(ELEMENT_DIM, element tag)``.
FILTERS = {'Points': 0, 'Curves': 1, 'Surfaces': 2, 'Volumes': 3}
CELLS = 'Cells'
ELEMENT_DIM = 4
FILTER_ORDER = tuple(FILTERS) + (CELLS,)
_PREFIX = {0: 'P', 1: 'C', 2: 'S', 3: 'V', ELEMENT_DIM: 'E'}
_DIM_NAME = {0: 'point', 1: 'curve', 2: 'surface', 3: 'volume',
             ELEMENT_DIM: 'element'}

Entity = tuple  # (dim, tag)


class PickRefusal(ValueError):
    """The operation cannot be done; the message says why."""


def chip_text(entity: Entity, name: str = '') -> str:
    """The chip for one entity: ``S12`` or ``S12 inlet``."""
    dim, tag = entity
    base = '%s%d' % (_PREFIX[dim], tag)
    return '%s %s' % (base, name) if name else base


# --------------------------------------------------------------------------- #
# The model's entities and the cell -> entity map
# --------------------------------------------------------------------------- #

@dataclass
class EntityRecord:
    dim: int
    tag: int
    name: str = ''
    physical: tuple = ()
    centre: tuple = (0.0, 0.0, 0.0)
    xyz: tuple | None = None
    up: tuple = ()

    @property
    def key(self) -> Entity:
        return (self.dim, self.tag)


class EntityMap:
    """Rendered cell id -> Gmsh entity, plus the entity table."""

    def __init__(self, cell_dim, cell_tag, records: Iterable[EntityRecord] = (),
                 cell_centres=None):
        self.cell_dim = np.asarray(cell_dim, dtype=np.int64)
        self.cell_tag = np.asarray(cell_tag, dtype=np.int64)
        self.records = {r.key: r for r in records}
        self.cell_centres = (None if cell_centres is None
                             else np.asarray(cell_centres, dtype=float))
        self._cells: dict = {}
        for index in range(len(self.cell_dim)):
            self._cells.setdefault(
                (int(self.cell_dim[index]), int(self.cell_tag[index])),
                []).append(index)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_session(cls, rows: Iterable[dict], cell_dim=(), cell_tag=(),
                     cell_centres=None) -> 'EntityMap':
        records = [EntityRecord(
            dim=int(r['dim']), tag=int(r['tag']), name=r.get('name') or '',
            physical=tuple(r.get('physical') or ()),
            centre=tuple(r.get('centre') or (0.0, 0.0, 0.0)),
            xyz=tuple(r['xyz']) if r.get('xyz') is not None else None,
            up=tuple(r.get('up') or ())) for r in rows]
        return cls(cell_dim, cell_tag, records, cell_centres)

    @classmethod
    def from_vtp(cls, path, rows: Iterable[dict] = ()) -> 'EntityMap':
        """Read ``geometry.vtp`` written by ``session.handover``."""
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy

        reader = vtk.vtkXMLPolyDataReader()
        reader.SetFileName(str(path))
        reader.Update()
        data = reader.GetOutput()
        cell_data = data.GetCellData()
        dim = cell_data.GetArray('gmshEntityDim')
        tag = cell_data.GetArray('gmshEntityTag')
        if dim is None or tag is None:
            raise PickRefusal('%s carries no gmshEntityDim/gmshEntityTag cell '
                              'arrays; it was not written by the Gmsh '
                              'session' % path)
        centres = vtk.vtkCellCenters()
        centres.SetInputData(data)
        centres.Update()
        points = vtk_to_numpy(centres.GetOutput().GetPoints().GetData())
        return cls.from_session(rows, vtk_to_numpy(dim), vtk_to_numpy(tag),
                                points)

    # -- lookups ----------------------------------------------------------- #

    def entity_of_cell(self, cell: int) -> Entity | None:
        if cell < 0 or cell >= len(self.cell_dim):
            return None
        return (int(self.cell_dim[cell]), int(self.cell_tag[cell]))

    def cells_of(self, entity: Entity) -> list:
        return list(self._cells.get(tuple(entity), ()))

    def entities(self, dim: int | None = None) -> list:
        keys = set(self.records) | set(self._cells)
        return sorted(k for k in keys if dim is None or k[0] == dim)

    def name_of(self, entity: Entity) -> str:
        record = self.records.get(tuple(entity))
        return record.name if record else ''

    def anchor(self, entity: Entity):
        """Where a label sits: a point's coordinates, a curve or surface's
        middle cell centre (on the entity, unlike a bounding-box centre), a
        volume's bounding-box centre."""
        entity = tuple(entity)
        record = self.records.get(entity)
        if entity[0] == 0 and record is not None and record.xyz is not None:
            return tuple(record.xyz)
        cells = self._cells.get(entity)
        if cells and self.cell_centres is not None:
            return tuple(self.cell_centres[cells[len(cells) // 2]])
        if record is not None:
            return tuple(record.centre)
        return None


# --------------------------------------------------------------------------- #
# The picker
# --------------------------------------------------------------------------- #

class EntityPicker:
    """Selection filter, hover, add / extend, accept, abort.

    ``begin`` starts a pick for a purpose (a physical group, a transform, an
    entity-list field); ``hover`` and ``click`` take what the viewport's cell
    picker returned (a cell id, or a world point for a point entity);
    ``accept`` (Enter, ``e``) hands the chips to the purpose; ``abort``
    (Escape, ``q``) drops them and says the pick was cancelled.
    """

    def __init__(self, entity_map: EntityMap, point_tolerance: float = 0.0):
        self.map = entity_map
        self.point_tolerance = float(point_tolerance)
        self._filter = 'Surfaces'
        self._hover: Entity | None = None
        self._chosen: list = []
        self._on_accept: Callable | None = None
        self._on_abort: Callable | None = None
        self._listeners: list = []
        self._active = False

    # -- state ------------------------------------------------------------- #

    @property
    def active(self) -> bool:
        return self._active

    @property
    def filter(self) -> str:
        return self._filter

    @property
    def hovered(self) -> Entity | None:
        return self._hover

    @property
    def selection(self) -> tuple:
        return tuple(self._chosen)

    def chips(self) -> list:
        return [chip_text(e, self.map.name_of(e)) for e in self._chosen]

    def subscribe(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def _notify(self, what: str) -> None:
        for listener in list(self._listeners):
            listener(what)

    # -- the interaction --------------------------------------------------- #

    def set_filter(self, name: str) -> None:
        if name not in FILTER_ORDER:
            raise PickRefusal('selection filter %r is not one of %s'
                              % (name, ', '.join(FILTER_ORDER)))
        if name != self._filter:
            self._filter = name
            self._hover = None
            # Picks of another kind are not mixed into one list.
            dim = ELEMENT_DIM if name == CELLS else FILTERS[name]
            self._chosen = [e for e in self._chosen if e[0] == dim]
            self._notify('filter')

    def begin(self, filter_name: str | None = None, *,
              on_accept: Callable | None = None,
              on_abort: Callable | None = None) -> None:
        if filter_name:
            self.set_filter(filter_name)
        self._chosen = []
        self._hover = None
        self._on_accept, self._on_abort = on_accept, on_abort
        self._active = True
        self._notify('begin')

    def resolve(self, cell: int | None = None, world=None) -> Entity | None:
        """The entity under the cursor for the current filter, or None."""
        if self._filter == CELLS:
            return None             # the mesh viewport answers: click_element
        dim = FILTERS[self._filter]
        if dim == 0:
            return self._nearest_point(world)
        if cell is None:
            return None
        found = self.map.entity_of_cell(int(cell))
        if found is None:
            return None
        if found[0] == dim:
            return found
        if dim == 3 and found[0] == 2:
            record = self.map.records.get(found)
            if record is not None and record.up:
                return (3, int(record.up[0]))
        return None

    def _nearest_point(self, world) -> Entity | None:
        if world is None:
            return None
        target = np.asarray(world, dtype=float)
        best, best_distance = None, None
        for key, record in self.map.records.items():
            if key[0] != 0 or record.xyz is None:
                continue
            distance = float(np.linalg.norm(np.asarray(record.xyz) - target))
            if best_distance is None or distance < best_distance:
                best, best_distance = key, distance
        if best is None:
            return None
        if self.point_tolerance and best_distance > self.point_tolerance:
            return None
        return best

    def hover(self, cell: int | None = None, world=None) -> Entity | None:
        found = self.resolve(cell, world) if self._active else None
        if found != self._hover:
            self._hover = found
            self._notify('hover')
        return found

    def click(self, cell: int | None = None, world=None, *,
              shift: bool = False) -> Entity | None:
        """A click replaces the pending list; shift-click extends it (and
        removes the entity when it is already in the list)."""
        if not self._active:
            return None
        found = self.resolve(cell, world)
        if found is None:
            return None
        if shift:
            if found in self._chosen:
                self._chosen.remove(found)
            else:
                self._chosen.append(found)
        else:
            self._chosen = [found]
        self._notify('selection')
        return found

    def hover_element(self, tag: int | None) -> Entity | None:
        """The mesh viewport's hover under the Cells filter."""
        found = ((ELEMENT_DIM, int(tag)) if self._active
                 and self._filter == CELLS and tag is not None else None)
        if found != self._hover:
            self._hover = found
            self._notify('hover')
        return found

    def click_element(self, tag: int | None, *,
                      shift: bool = False) -> Entity | None:
        """A click on a mesh element (its tag) under the Cells filter; the
        same rules as :meth:`click`."""
        if not self._active or self._filter != CELLS or tag is None:
            return None
        found = (ELEMENT_DIM, int(tag))
        if shift:
            if found in self._chosen:
                self._chosen.remove(found)
            else:
                self._chosen.append(found)
        else:
            self._chosen = [found]
        self._notify('selection')
        return found

    def remove_chip(self, entity: Entity) -> None:
        entity = tuple(entity)
        if entity in self._chosen:
            self._chosen.remove(entity)
            self._notify('selection')

    def accept(self) -> tuple:
        if not self._active:
            raise PickRefusal('there is no pick in progress')
        if not self._chosen:
            raise PickRefusal('nothing is picked; click an entity, or abort')
        chosen = tuple(self._chosen)
        callback = self._on_accept
        self._finish()
        self._notify('accept')
        if callback is not None:
            callback(chosen)
        return chosen

    def abort(self) -> None:
        if not self._active:
            return
        callback = self._on_abort
        self._finish()
        self._notify('abort')
        if callback is not None:
            callback()

    def _finish(self) -> None:
        self._active = False
        self._hover = None
        self._chosen = []
        self._on_accept = self._on_abort = None

    def key(self, key: str) -> bool:
        """Gmsh's keys: ``e`` / Enter ends, ``q`` / Escape aborts."""
        if not self._active:
            return False
        if key in ('e', 'E', 'Return', 'Enter'):
            try:
                self.accept()
            except PickRefusal:
                return False
            return True
        if key in ('q', 'Q', 'Escape'):
            self.abort()
            return True
        return False


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Label:
    entity: Entity
    text: str
    xyz: tuple


def entity_labels(entity_map: EntityMap, kinds: Iterable[str] = (
        'Points', 'Curves', 'Surfaces', 'Volumes'), *,
        with_names: bool = True) -> list:
    """The labels of the "Entity labels" toggle, one per entity of the kinds."""
    wanted = {FILTERS[k] for k in kinds if k in FILTERS}
    out = []
    for entity in entity_map.entities():
        if entity[0] not in wanted:
            continue
        where = entity_map.anchor(entity)
        if where is None:
            continue
        name = entity_map.name_of(entity) if with_names else ''
        out.append(Label(entity, chip_text(entity, name), tuple(where)))
    return out


# --------------------------------------------------------------------------- #
# Physical groups
# --------------------------------------------------------------------------- #

@dataclass
class PhysicalGroup:
    dim: int
    tag: int
    name: str
    entities: tuple

    @property
    def title(self) -> str:
        return self.name or ('%s group %d' % (_DIM_NAME[self.dim], self.tag))


class PhysicalGroupEditor:
    """Add / remove / rename / re-fill physical groups over picked entities.

    *session* is the :class:`GmshSession` client (or anything with
    ``call(op, *args, **kwargs)``).  Every change is a plain Gmsh API call, so
    it is logged and replayed with the rest of the model.
    """

    def __init__(self, session):
        self._session = session

    def groups(self) -> list:
        reply = self._session.call('session.physical_groups')
        return [PhysicalGroup(g['dim'], g['tag'], g['name'],
                              tuple(g['entities'])) for g in reply['groups']]

    def _find(self, dim: int, tag: int) -> PhysicalGroup:
        for group in self.groups():
            if (group.dim, group.tag) == (dim, tag):
                return group
        raise PickRefusal('there is no %s physical group %d'
                          % (_DIM_NAME.get(dim, dim), tag))

    @staticmethod
    def _members(entities) -> tuple:
        entities = [tuple(e) for e in entities]
        if not entities:
            raise PickRefusal('pick at least one entity first')
        dims = {e[0] for e in entities}
        if ELEMENT_DIM in dims:
            raise PickRefusal('mesh elements cannot be put in a physical '
                              'group; pick points, curves, surfaces or '
                              'volumes')
        if len(dims) != 1:
            raise PickRefusal(
                'a physical group holds one kind of entity; the pick mixes %s'
                % ', '.join(_DIM_NAME[d] + 's' for d in sorted(dims)))
        return dims.pop(), tuple(sorted({e[1] for e in entities}))

    def add(self, name: str, entities, tag: int = -1) -> PhysicalGroup:
        name = (name or '').strip()
        if not name:
            raise PickRefusal('a physical group needs a name; it is what the '
                              'mesh files carry')
        dim, tags = self._members(entities)
        for group in self.groups():
            if group.dim == dim and group.name == name:
                raise PickRefusal(
                    'a %s physical group named %r already exists (tag %d); '
                    'rename it or use Set members' % (_DIM_NAME[dim], name,
                                                      group.tag))
        made = self._session.call('model.addPhysicalGroup', dim, list(tags),
                                  tag=int(tag), name=name)
        return PhysicalGroup(dim, int(made), name, tags)

    def set_members(self, dim: int, tag: int, entities) -> PhysicalGroup:
        """Replace a group's members (Gmsh re-adds a group under its tag)."""
        old = self._find(dim, tag)
        new_dim, tags = self._members(entities)
        if new_dim != dim:
            raise PickRefusal('group %d holds %ss; the pick is %ss'
                              % (tag, _DIM_NAME[dim], _DIM_NAME[new_dim]))
        self._replace(dim, tag, old.name, tags)
        return PhysicalGroup(dim, tag, old.name, tags)

    def rename(self, dim: int, tag: int, name: str) -> PhysicalGroup:
        name = (name or '').strip()
        if not name:
            raise PickRefusal('a physical group needs a name')
        old = self._find(dim, tag)
        for group in self.groups():
            if (group.dim == dim and group.name == name
                    and group.tag != tag):
                raise PickRefusal('a %s physical group named %r already '
                                  'exists' % (_DIM_NAME[dim], name))
        self._replace(dim, tag, name, old.entities)
        return PhysicalGroup(dim, tag, name, old.entities)

    def _replace(self, dim: int, tag: int, name: str, tags) -> None:
        """Re-create a group under its own tag.

        Measured on Gmsh 4.15.2: ``setPhysicalName`` keeps the first name it
        was given, and ``addPhysicalGroup`` with a tag in use is refused
        ("Physical surface N already exists"), so a rename or a new member
        list is a remove followed by an add under the same tag.
        """
        self._session.call('model.removePhysicalGroups', [[dim, tag]])
        self._session.call('model.addPhysicalGroup', dim, list(tags),
                           tag=int(tag), name=name)

    def remove(self, dim: int, tag: int) -> None:
        self._find(dim, tag)
        self._session.call('model.removePhysicalGroups', [[dim, tag]])


def coherence(session) -> dict:
    """Geometry > Coherence: remove duplicate entities on the open model."""
    return session.call('session.coherence')


def entity_rows(session) -> list:
    return session.call('session.entities')['entities']
