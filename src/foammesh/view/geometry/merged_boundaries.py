#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""The boundaries the Repair page merged, as the later pages have to see them.

R137. MEASURED on annulus.stl: ``wall_shell`` and ``wall_bore`` were merged
into one boundary named ``wall`` on the Repair page, and the Repair tree, the
Boundaries table (``3 boundaries``, ``wall`` with 2 sub-surfaces) and the
geometry tree all agreed the merge had happened. The Castellation refinement
group's **Select Surfaces** dialog then offered ``Available (4): inlet,
outlet, wall_bore, wall_shell`` -- the pre-merge names, with no ``wall``
anywhere -- and so did the Boundary Layers **Select Boundaries** dialog.
Refining or layering "the wall" meant picking two entries the user had
already told the app were one.

**Two stores hold a boundary's name.** The Repair page edits the geometry
manifest through ``geometry.patches.*``, where a merge folds several records
into one that carries ``members`` and ``source_refs``. The selectors read the
configuration's ``geometry`` collection, which a merge never touches: it still
holds one row per imported solid under its pre-merge name. This module maps
one store onto the other by the solid name they already agree on --
``source_ref/original_name``, which is also what
``CaseBuilder._source_regions`` keys snappy's ``regions`` off (R141) -- so a
selector can offer the merged boundary once and write the group onto every
configuration row it covers.

The first covered row is the primary one, matching
``CaseBuilder._source_region``: that is the row a merged prepared group binds
to, so it is the one whose ``castellationGroup``/``layerGroup`` the dictionary
writer will read. The others are written too, because binding is by name and
a rule change there must not silently drop the group.
"""
from __future__ import annotations

from foammesh.core.facade.errors import FacadeError


def mergedPatchRows():
    """Every boundary the geometry manifest holds, merged ones included.

    Returns an empty tuple when there is no case open or the manifest cannot
    be read: a selector that cannot reach the merged view must still list the
    surfaces it can see, exactly as it did before. ``AttributeError`` is in
    the net for the same reason -- this is a second store read on behalf of a
    page that has its own, and no failure to reach it may stop that page
    loading.
    """
    from foammesh.app import app                # local: keeps this importable
    from foammesh.view.facade_client import query

    client = getattr(app, 'facadeClient', None)
    try:
        if client is None or not client.has_case():
            return ()
        payload = query(client, 'geometry.patches.list').payload
    except (FacadeError, AttributeError, KeyError, OSError, RuntimeError,
            TypeError, ValueError):
        return ()
    return tuple(payload.get('patches') or ())


class MergedBoundaries:
    """Which configuration geometry rows the Repair page folded together.

    Constructed with the rows already fetched in tests; in the product the
    caller leaves *rows* alone and the manifest is read for it.
    """

    def __init__(self, db, rows=None):
        self._primary = {}            # gId -> the gId that stands for it
        self._cover = {}              # primary gId -> every gId it covers
        self._name = {}               # primary gId -> the merged name
        self._build(db, mergedPatchRows() if rows is None else rows)

    # -- reading ----------------------------------------------------------- #

    def isFollower(self, gId) -> bool:
        """Whether this row is covered by another row's merged boundary.

        A follower must not be listed: it is half of a boundary the user has
        already named, and offering it is what made the merge decorative.
        """
        gId = str(gId)
        return self._primary.get(gId, gId) != gId

    def nameFor(self, gId, default=None):
        """The merged boundary's name, or *default* when nothing was merged."""
        return self._name.get(str(gId), default)

    def cover(self, gId) -> tuple:
        """Every configuration row this selector entry stands for."""
        gId = str(gId)
        return self._cover.get(gId, (gId,))

    def expand(self, assignments: dict) -> dict:
        """Spread one ``{gId: group}`` decision over the rows it covers."""
        out = {}
        for gId, group in assignments.items():
            for member in self.cover(gId):
                out[member] = group
        return out

    # -- internals --------------------------------------------------------- #

    def _build(self, db, rows) -> None:
        byName = self._rowsByName(db)
        for row in rows or ():
            if not isinstance(row, dict) or not row.get('merged'):
                continue
            members = []
            for ref in row.get('source_refs') or ():
                original = str((ref or {}).get('original_name') or '')
                for gId in byName.get(original, ()):
                    if gId not in members:
                        members.append(gId)
            # One row means the manifest merged sub-surfaces the configuration
            # never had as separate rows; there is nothing to fold, and
            # renaming a single row here would fight the geometry tree.
            if len(members) < 2:
                continue
            primary = members[0]
            self._cover[primary] = tuple(members)
            self._name[primary] = str(row.get('name') or '')
            for gId in members:
                self._primary[gId] = primary

    @staticmethod
    def _rowsByName(db) -> dict:
        byName = {}
        try:
            elements = db.getElements('geometry').items()
        except (AttributeError, KeyError, TypeError):
            return byName
        for gId, geometry in elements:
            try:
                name = str(geometry.value('name'))
            except (AttributeError, KeyError, TypeError):
                continue
            byName.setdefault(name, []).append(str(gId))
        return byName
