#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Rename, merge and split boundary patches in the geometry manifest.

Plan 28 WP5. :mod:`.ops` has held a clean, tested ``PatchSet`` since Plan 12 and
nothing has ever called it: the names a CAD import invents (``Face_1`` ..
``Face_212``) travelled all the way to the solver, and a user who wanted an
``inlet`` had no way to say so. This module is the missing half -- it reads the
patch records the geometry store persists, runs the operation through
``PatchSet`` so the rules stay in one place, and writes the result back.

**A patch is a named set of source sub-surfaces.** That is what ``PatchSet``
already says, and what the prepared model needed to learn. Before this, one
prepared group was one imported sub-surface and nothing could be merged; now a
record may carry ``members``, and a merged record's ``source_refs`` names every
sub-surface it covers so nothing about the original geometry is lost.

**Identity is derived from membership, not from the name.** A merged patch's
uuid is a digest of its members' uuids, so merging and then renaming leaves the
identity alone -- a fidelity section keyed to that boundary survives the rename,
which is the whole reason patch uuids exist. Splitting restores the members'
original uuids, so a merge followed by a split is a round trip rather than a
new set of boundaries with the same shapes.

**One geometry at a time.** Merging patches from two imported files is refused.
A prepared group carries a single ``geometry_id`` and each imported surface is
published as its own file, so a cross-file group could not be written down
without inventing a geometry that does not exist. The refusal says so.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

from ..store import ordered_entries

from .ops import PatchError, Patch, PatchSet

#: Prefix for the uuid of a patch that covers more than one sub-surface.
MERGED_PREFIX = 'patch-merged-'


def merged_uuid(member_uuids: Iterable[str]) -> str:
    """A stable uuid for the set of sub-surfaces a merged patch covers.

    Deliberately not a function of the name: renaming a merged patch must not
    change what it *is*, or every artifact keyed to it would be orphaned by a
    typo correction.
    """
    joined = '|'.join(sorted(str(value) for value in member_uuids))
    return MERGED_PREFIX + hashlib.sha256(joined.encode()).hexdigest()[:24]


def records(entry: dict) -> list[dict]:
    """This geometry's patch records, materialising the implicit one.

    A tessellated import (STL, OBJ) has no ``patches`` list -- it is one
    surface, and the store records its identity on the entry itself. Every
    caller here would otherwise have to special-case that, and the prepared
    store already has the same fallback; this is the one place it belongs.
    """
    existing = entry.get('patches')
    if existing:
        return [dict(item) for item in existing]
    return [{
        'patch_uuid': entry.get('patch_uuid'),
        'name': entry.get('name') or entry.get('geometry_id'),
        'source_ref': dict(entry.get('source_ref') or {}),
    }]


def members_of(record: dict) -> list[dict]:
    """The sub-surfaces one record covers: itself, unless it was merged."""
    members = record.get('members')
    if members:
        return [dict(item) for item in members]
    return [{'patch_uuid': record.get('patch_uuid'),
             'name': record.get('name'),
             'source_ref': dict(record.get('source_ref') or {})}]


def rows(entries: Iterable[dict]) -> list[dict]:
    """A flat, display-ready list of every boundary the case would produce."""
    out: list[dict] = []
    # DP-380. Ordered by the same key the prepared set uses, not by the
    # import uuid: this is the list the user reads, and it has to agree
    # with the order the mesher merges in.
    for entry in ordered_entries(entries):
        for record in records(entry):
            member_list = members_of(record)
            out.append({
                'geometry_id': entry.get('geometry_id'),
                'geometry_name': entry.get('name'),
                'patch_uuid': record.get('patch_uuid'),
                'name': record.get('name'),
                'merged': bool(record.get('members')),
                'member_count': len(member_list),
                'members': [item.get('patch_uuid') for item in member_list],
                'source_refs': [item.get('source_ref') or {}
                                for item in member_list],
            })
    return out


class PatchGroupEditor:
    """Boundary-patch editing against a :class:`GeometryArtifactStore`.

    The store owns persistence; this owns the rules. Each method returns the
    ``(action, detail)`` pair :mod:`.ops` produces so the facade can put a
    truthful description into the transaction log rather than inventing one.
    """

    def __init__(self, store):
        self._store = store

    # -- reading ----------------------------------------------------------- #

    def rows(self) -> list[dict]:
        return rows(self._store.entries())

    # -- editing ----------------------------------------------------------- #

    def rename(self, patch_uuid: str, name: str) -> tuple[str, dict]:
        """Give one boundary the name the solver will see."""
        name = str(name or '').strip()
        if not name:
            raise PatchError('a patch name cannot be empty')
        entries = self._store.entries()
        entry, current = self._locate(entries, patch_uuid)
        patch_set = self._patch_set(current)
        before = self._record_of(current, patch_uuid)['name']
        action = patch_set.rename(before, name)
        self._write(entries, entry, self._apply(current, patch_set))
        return action

    def merge(self, patch_uuids: list[str], name: str) -> tuple[str, dict]:
        """Fold several sub-surfaces into one named boundary."""
        wanted = [str(value) for value in patch_uuids or ()]
        if len(set(wanted)) < 2:
            raise PatchError('merge needs at least two patches')
        name = str(name or '').strip()
        if not name:
            raise PatchError('a patch name cannot be empty')
        entries = self._store.entries()
        entry, current = self._locate(entries, wanted[0])
        chosen = []
        for value in wanted:
            record = self._record_of(current, value)
            if record is None:
                raise PatchError(
                    'patches from different imported geometries cannot be one '
                    'boundary: each imported surface is published as its own '
                    'file, so the group would have no geometry to belong to')
            chosen.append(record)

        # PatchSet holds the rules; running the merge through it keeps the
        # duplicate-name and arity checks in the one place they are tested.
        patch_set = self._patch_set(current)
        action = patch_set.merge([item['name'] for item in chosen], name)

        members = [member for item in chosen for member in members_of(item)]
        merged = {
            'patch_uuid': merged_uuid(item['patch_uuid'] for item in members),
            'name': name,
            'members': members,
            'source_ref': dict(members[0].get('source_ref') or {}),
            'source_refs': [dict(item.get('source_ref') or {})
                            for item in members],
        }
        self._write(entries, entry,
                    self._replace(current, set(wanted), [merged]),
                    remap={value: [merged['patch_uuid']] for value in wanted})
        return action

    def split(self, patch_uuid: str) -> tuple[str, dict]:
        """Undo a merge: every sub-surface becomes its own boundary again.

        Splitting a patch that was never merged is refused rather than
        silently doing nothing. One imported sub-surface is the finest grain
        the geometry itself offers; cutting a surface into new pieces is
        ``geometry.split``, which is a different operation on a different
        object and says so.
        """
        entries = self._store.entries()
        entry, current = self._locate(entries, patch_uuid)
        record = self._record_of(current, patch_uuid)
        members = record.get('members') or ()
        if len(members) < 2:
            raise PatchError(
                f'{record.get("name")!r} covers one imported sub-surface, so '
                'there is nothing to split it into. Use geometry.split to '
                'divide the surface itself.')
        restored = [{'patch_uuid': item.get('patch_uuid'),
                     'name': item.get('name'),
                     'source_ref': dict(item.get('source_ref') or {})}
                    for item in members]

        patch_set = self._patch_set(current)
        action = patch_set.split(record['name'], {
            str(item['name']): [str(item['patch_uuid'])] for item in restored})

        self._write(entries, entry,
                    self._replace(current, {patch_uuid}, restored),
                    remap={patch_uuid: [item['patch_uuid']
                                        for item in restored]})
        return action

    # -- internals --------------------------------------------------------- #

    @staticmethod
    def _replace(current: list[dict], consumed: set,
                 produced: list[dict]) -> list[dict]:
        """Put the new records where the ones they replace stood.

        R166. Appending them instead and re-sorting the whole list by name
        moved every other row too, which is how a rename came to land on the
        wrong face. A merged boundary belongs where its first member was; the
        pieces a split restores belong where the merged one was.
        """
        out: list[dict] = []
        for record in current:
            if record.get('patch_uuid') not in consumed:
                out.append(record)
                continue
            if produced:
                out.extend(produced)
                produced = []
        return out + list(produced)

    @staticmethod
    def _patch_set(current: list[dict]) -> PatchSet:
        return PatchSet([
            Patch(str(item.get('name')),
                  [str(member.get('patch_uuid'))
                   for member in members_of(item)])
            for item in current])

    @staticmethod
    def _record_of(current: list[dict], patch_uuid: str) -> dict | None:
        return next((item for item in current
                     if item.get('patch_uuid') == patch_uuid), None)

    def _locate(self, entries: list[dict],
                patch_uuid: str) -> tuple[dict, list[dict]]:
        for entry in entries:
            current = records(entry)
            if self._record_of(current, patch_uuid) is not None:
                return entry, current
        raise PatchError(f'no such patch: {patch_uuid!r}')

    @staticmethod
    def _apply(current: list[dict], patch_set: PatchSet) -> list[dict]:
        """Carry the names ``PatchSet`` settled back onto the records.

        Matched by membership rather than by name, because the name is
        precisely what may have changed.
        """
        by_members = {tuple(sorted(patch.members)): patch.name
                      for patch in patch_set._patches}       # noqa: SLF001
        out = []
        for record in current:
            key = tuple(sorted(str(member.get('patch_uuid'))
                               for member in members_of(record)))
            out.append({**record, 'name': by_members.get(key, record['name'])})
        return out

    def _write(self, entries: list[dict], entry: dict, patches: list[dict], *,
               remap: dict[str, list[str]] | None = None) -> None:
        """Persist the new patch list, keeping the regions pointing at it.

        A region's ``boundary_patch_uuids`` is the list of boundaries that
        bound it. Leaving a merged-away uuid in it would leave the region
        naming a boundary the mesh will never have -- the kind of dangling
        reference `_validate_regions` cannot see and publication would carry
        all the way to the solver. ``remap`` says what each old uuid became:
        one uuid for a merge, several for a split.
        """
        # R166. This sorted by name on every write, so the row being edited
        # moved the moment its new name was committed: renaming `tee_1` to
        # `wall_main` sent it down the table, the row under the cursor became
        # a different face, and five renames typed top to bottom landed on
        # three faces with two names never applied at all -- silently, since
        # nothing on screen says which face a row is. Order is now whatever
        # the caller settled on: rename keeps every record where it was,
        # merge and split put the new records where the old ones stood.
        entry['patches'] = list(patches)
        regions = entry.get('regions')
        if regions and remap:
            for region in regions:
                uuids: list[str] = []
                for value in region.get('boundary_patch_uuids') or ():
                    for candidate in remap.get(value, [value]):
                        if candidate not in uuids:
                            uuids.append(candidate)
                region['boundary_patch_uuids'] = uuids
        self._store.save_entries(entries)
