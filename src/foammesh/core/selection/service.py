"""Stable, engine-neutral scope selection and viewport coordination."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Iterable
from uuid import uuid4


class SelectionError(ValueError):
    pass


class SelectionKind(str, Enum):
    GEOMETRY_SOURCE = 'geometry_source'
    SURFACE_GROUP = 'surface_group'
    CLOSED_VOLUME = 'closed_volume'
    REGION = 'region'
    CELL_ZONE = 'cell_zone'
    FACE_ZONE = 'face_zone'
    CANONICAL_GROUP = 'canonical_group'


class SelectionStatus(str, Enum):
    VALID = 'valid'
    STALE = 'stale'
    ORPHAN = 'orphan'


@dataclass(frozen=True)
class SelectionEntity:
    stable_id: str
    label: str
    kind: SelectionKind
    geometry_ids: tuple[str, ...] = ()
    status: SelectionStatus = SelectionStatus.VALID
    owner: str = 'project'
    removable: bool = True
    metadata: tuple[tuple[str, object], ...] = ()

    def __post_init__(self):
        stable_id = str(self.stable_id).strip()
        if not stable_id:
            raise SelectionError('selection entity requires a stable ID')
        object.__setattr__(self, 'stable_id', stable_id)
        object.__setattr__(
            self, 'geometry_ids',
            tuple(dict.fromkeys(str(value) for value in self.geometry_ids)))

    @property
    def status_label(self) -> str:
        return {
            SelectionStatus.VALID: 'Available',
            SelectionStatus.STALE: 'Stale',
            SelectionStatus.ORPHAN: 'Missing geometry',
        }[self.status]

    def to_dict(self) -> dict:
        return {
            'stable_id': self.stable_id,
            'label': self.label,
            'kind': self.kind.value,
            'geometry_ids': list(self.geometry_ids),
            'status': self.status.value,
            'status_label': self.status_label,
            'owner': self.owner,
            'removable': self.removable,
            'metadata': dict(self.metadata),
        }


@dataclass(frozen=True)
class SelectionSnapshot:
    selected_ids: tuple[str, ...] = ()
    preview_ids: tuple[str, ...] = ()
    hidden_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            'selected_ids': list(self.selected_ids),
            'preview_ids': list(self.preview_ids),
            'hidden_ids': list(self.hidden_ids),
        }


class SelectionSignal:
    """Small dependency-free signal used by core and straightforward tests."""

    def __init__(self):
        self._listeners: list[Callable] = []

    def connect(self, listener: Callable) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def disconnect(self, listener: Callable) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self, *args) -> None:
        for listener in tuple(self._listeners):
            listener(*args)


class SelectionService:
    """Single writer of selection, preview, visibility, and orphan state."""

    def __init__(self):
        self.selection_changed = SelectionSignal()
        self.preview_changed = SelectionSignal()
        self.visibility_changed = SelectionSignal()
        self.orphan_changed = SelectionSignal()
        self.state_changed = SelectionSignal()
        self._entities: dict[str, SelectionEntity] = {}
        self._snapshot = SelectionSnapshot()
        self._renderer: Callable | None = None
        self._active_editor: tuple[
            str, Callable[[tuple[str, ...]], None], frozenset[str]] | None = None
        self._applying_renderer = False

    def entities(
            self, *, kinds: Iterable[SelectionKind | str] | None = None,
            include_invalid: bool = True) -> tuple[SelectionEntity, ...]:
        allowed = (
            {SelectionKind(value) for value in kinds}
            if kinds is not None else None)
        return tuple(
            entity for entity in self._entities.values()
            if (allowed is None or entity.kind in allowed)
            and (include_invalid or entity.status is SelectionStatus.VALID))

    def entity(self, stable_id: str) -> SelectionEntity | None:
        return self._entities.get(str(stable_id))

    def register(self, entity: SelectionEntity) -> SelectionEntity:
        self._entities[entity.stable_id] = entity
        if entity.status is not SelectionStatus.VALID:
            self.orphan_changed.emit(entity.to_dict())
        self._apply()
        return entity

    def synchronize(
            self, entities: Iterable[SelectionEntity], *, owner: str) -> None:
        incoming = {item.stable_id: item for item in entities}
        for stable_id, entity in tuple(self._entities.items()):
            if entity.owner == owner and stable_id not in incoming:
                self._entities[stable_id] = replace(
                    entity, status=SelectionStatus.ORPHAN)
                self.orphan_changed.emit(
                    self._entities[stable_id].to_dict())
        for entity in incoming.values():
            if entity.owner != owner:
                entity = replace(entity, owner=owner)
            self._entities[entity.stable_id] = entity
        self._apply()

    def remove_owner(self, owner: str) -> None:
        removed = {
            stable_id for stable_id, entity in self._entities.items()
            if entity.owner == owner}
        for stable_id in removed:
            del self._entities[stable_id]
        self._snapshot = SelectionSnapshot(
            tuple(value for value in self._snapshot.selected_ids
                  if value not in removed),
            tuple(value for value in self._snapshot.preview_ids
                  if value not in removed),
            tuple(value for value in self._snapshot.hidden_ids
                  if value not in removed),
        )
        self._apply()

    def set_status(
            self, stable_id: str, status: SelectionStatus | str) -> None:
        stable_id = str(stable_id)
        if stable_id not in self._entities:
            raise SelectionError(f'unknown stable selection ID: {stable_id}')
        self._entities[stable_id] = replace(
            self._entities[stable_id], status=SelectionStatus(status))
        self.orphan_changed.emit(self._entities[stable_id].to_dict())
        self._apply()

    def snapshot(self) -> SelectionSnapshot:
        return self._snapshot

    def restore(self, snapshot: SelectionSnapshot) -> None:
        if not isinstance(snapshot, SelectionSnapshot):
            raise TypeError('selection snapshot is required')
        self._snapshot = snapshot
        self.selection_changed.emit(snapshot.selected_ids)
        self.preview_changed.emit(snapshot.preview_ids)
        self.visibility_changed.emit(snapshot.hidden_ids)
        self._apply()

    def select(self, stable_ids: Iterable[str]) -> None:
        values = self._validated(stable_ids)
        if values == self._snapshot.selected_ids:
            return
        self._snapshot = replace(self._snapshot, selected_ids=values)
        self.selection_changed.emit(values)
        self._apply()

    def preview(self, stable_ids: Iterable[str]) -> None:
        values = self._validated(stable_ids)
        if values == self._snapshot.preview_ids:
            return
        self._snapshot = replace(self._snapshot, preview_ids=values)
        self.preview_changed.emit(values)
        self._apply()

    def clear_preview(self) -> None:
        self.preview(())

    def set_hidden(self, stable_ids: Iterable[str]) -> None:
        values = self._validated(stable_ids, allow_invalid=True)
        if values == self._snapshot.hidden_ids:
            return
        self._snapshot = replace(self._snapshot, hidden_ids=values)
        self.visibility_changed.emit(values)
        self._apply()

    def attach_editor(
            self, callback: Callable[[tuple[str, ...]], None],
            allowed_ids: Iterable[str]) -> str:
        token = uuid4().hex
        self._active_editor = (
            token, callback, frozenset(str(value) for value in allowed_ids))
        return token

    def detach_editor(self, token: str) -> None:
        if self._active_editor and self._active_editor[0] == token:
            self._active_editor = None

    def viewport_picked(self, geometry_ids: Iterable[str], *,
                        expand: str | None = None,
                        toggle: bool = False) -> tuple[str, ...]:
        """Turn a set of picked geometry ids into a selection.

        Matching on any intersection was what made one face of the imported
        duct light up all six: the closed-volume entity carries every face,
        so touching one of them touched the volume. An entity now matches
        only when the pick covers *all* of its geometry, which makes the
        finest thing under the cursor the answer; the volume is reached by
        picking every face of it, or by asking for it with ``expand``.

        :param expand: ``'volume'`` selects the closed volume owning the
            pick - what Shift+click sends.
        :param toggle: fold the match into the current selection instead of
            replacing it - what Ctrl+click sends.
        """
        if self._applying_renderer:
            return self._snapshot.selected_ids
        picked = {str(value) for value in geometry_ids}
        valid = [entity for entity in self._entities.values()
                 if entity.status is SelectionStatus.VALID]
        matched = [entity for entity in valid
                   if self._covered_by(entity, picked)]
        if expand == 'volume':
            owners = [entity for entity in valid
                      if entity.kind is SelectionKind.CLOSED_VOLUME
                      and picked.intersection(entity.geometry_ids)]
            if owners:
                matched = owners
        matches = self._coarsest(matched)
        if toggle:
            matches = self._toggled(matches)
        if self._active_editor:
            _token, callback, allowed = self._active_editor
            matches = tuple(value for value in matches if value in allowed)
            callback(matches)
        self.select(matches)
        return matches

    def selected_actor_ids(self) -> tuple[str, ...]:
        """Every geometry id the current selection stands for.

        Actors are keyed by geometry id, entities by stable id, and a volume
        is one entity over many actors. Isolate, zoom and highlight all need
        the actor side of that, from whatever source made the selection.
        """
        actors: list[str] = []
        for stable_id in self._snapshot.selected_ids:
            entity = self._entities.get(stable_id)
            if entity is None:
                continue
            actors.extend(entity.geometry_ids or (entity.stable_id,))
        return tuple(dict.fromkeys(actors))

    def governed_actor_ids(self) -> frozenset[str]:
        """Every actor id some entity stands for.

        The display control holds parts this service has no entity for - a
        mesh boundary, for one - and those rows are the user's to keep.
        Measured: applying a snapshot cleared the row list wholesale, so a
        boundary row lost its selection the moment a geometry row joined it
        and the two readers disagreed about what was selected. Saying what
        is governed lets the control leave the rest alone.
        """
        actors: set[str] = set()
        for entity in self._entities.values():
            actors.update(entity.geometry_ids or (entity.stable_id,))
        return frozenset(actors)

    @staticmethod
    def _covered_by(entity: SelectionEntity, picked: set[str]) -> bool:
        if entity.geometry_ids:
            return set(entity.geometry_ids).issubset(picked)
        return entity.stable_id in picked

    @staticmethod
    def _coarsest(entities: list[SelectionEntity]) -> tuple[str, ...]:
        """Drop anything wholly contained in another match.

        A pick that covers all six faces is a pick of the duct, not of six
        faces and the duct; keeping both would highlight the same geometry
        twice and make the tree show seven selected rows.
        """
        spans = {entity.stable_id: (set(entity.geometry_ids)
                                    or {entity.stable_id})
                 for entity in entities}
        return tuple(
            entity.stable_id for entity in entities
            if not any(other != entity.stable_id
                       and spans[entity.stable_id] < span
                       for other, span in spans.items()))

    def _toggled(self, matches: tuple[str, ...]) -> tuple[str, ...]:
        current = self._snapshot.selected_ids
        kept = [value for value in current if value not in matches]
        added = [value for value in matches if value not in current]
        return tuple(dict.fromkeys(kept + added))

    def bind_renderer(self, callback: Callable | None) -> None:
        self._renderer = callback
        self._apply()

    def synchronize_prepared_case(self, case_path, db) -> tuple[SelectionEntity, ...]:
        """Publish prepared patch UUIDs as the typed engine scope catalogue."""
        from foammesh.core.geometry import PreparedGeometryStore
        store = PreparedGeometryStore(case_path)
        prepared = store.current()
        status = SelectionStatus.VALID
        if prepared is None:
            prepared = store.current(require_source_match=False)
            status = SelectionStatus.STALE
        if prepared is None:
            self.synchronize((), owner='prepared-groups')
            self.synchronize((), owner='prepared-regions')
            return ()
        names: dict[str, list[str]] = {}
        try:
            for geometry_id, geometry in db.getElements('geometry').items():
                names.setdefault(
                    str(geometry.value('name')).casefold(), []).append(
                        str(geometry_id))
        except Exception:
            names = {}
        entities = []
        patch_actor_ids = {}
        for group in prepared.group_manifest.get('groups', ()):
            display_name = str(group.get('display_name') or '')
            geometry_ids = tuple(names.get(display_name.casefold(), ()))
            group_status = status
            if status is SelectionStatus.VALID and names and not geometry_ids:
                group_status = SelectionStatus.ORPHAN
            entity = SelectionEntity(
                str(group['patch_uuid']), display_name,
                SelectionKind.SURFACE_GROUP, geometry_ids,
                group_status, owner='prepared-groups',
                metadata=(
                    ('solver_name', group.get('solver_name')),
                    ('native_token', group.get('native_token')),
                    ('category', group.get('category')),
                ))
            entities.append(entity)
            patch_actor_ids[entity.stable_id] = entity.geometry_ids
        self.synchronize(entities, owner='prepared-groups')
        region_entities = []
        for region in prepared.group_manifest.get('regions', ()):
            actor_ids = tuple(dict.fromkeys(
                actor_id
                for patch_uuid in region.get('boundary_patch_uuids', ())
                for actor_id in patch_actor_ids.get(str(patch_uuid), ())))
            region_entities.append(SelectionEntity(
                str(region['region_uuid']),
                str(region.get('display_name') or region['region_uuid']),
                SelectionKind.REGION, actor_ids, status,
                owner='prepared-regions',
                metadata=(
                    ('solver_name', region.get('solver_name')),
                    ('native_token', region.get('native_token')),
                    ('region_type', region.get('region_type')),
                )))
        self.synchronize(region_entities, owner='prepared-regions')
        return tuple(entities + region_entities)

    def _validated(
            self, values: Iterable[str], *,
            allow_invalid: bool = False) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(str(value) for value in values))
        missing = [value for value in result if value not in self._entities]
        invalid = [
            value for value in result
            if value in self._entities
            and self._entities[value].status is not SelectionStatus.VALID]
        if missing:
            raise SelectionError(
                'unknown stable selection IDs: ' + ', '.join(missing))
        if invalid and not allow_invalid:
            raise SelectionError(
                'orphaned or stale selection IDs: ' + ', '.join(invalid))
        return result

    def _apply(self) -> None:
        self.state_changed.emit(self._snapshot.to_dict())
        if self._renderer is None or self._applying_renderer:
            return
        self._applying_renderer = True
        try:
            self._renderer(self._snapshot, dict(self._entities))
        finally:
            self._applying_renderer = False
