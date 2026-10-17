#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Undo/redo over project-state snapshots.

The engine takes a snapshot of the project's configuration before each
committed change. Undo restores the previous snapshot; redo re-applies an
undone one. A bounded depth keeps memory in check on large projects.

A snapshot is opaque here: this class stores it and hands it back, and only
the state engine that produced it knows what it holds. Today that is a copy of
the configuration document. It used to be that document serialized to YAML
text, which cost about forty times as much to take — DP-72.

A snapshot covers the configuration state only. What lives outside it -- a mesh
stage run, a geometry operation, an accepted step -- rides in the entry's
``companion`` (UXD7), which the facade applies in the same move.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

_ids = itertools.count(1)


class _Entry:
    """One step of the history: the state before it, its name, and what else
    moves with it.

    ``companion`` is the part of the change the settings snapshot cannot hold
    (UXD7). It is ``{'kind': ..., 'undo': <action>, 'redo': <action>}`` and the
    facade applies the action for the direction taken, together with the
    settings snapshot. The kinds are ``stage`` (a mesh stage run: the mesh on
    disk), ``geometry`` (a geometry operation: the geometry manifest) and
    ``task`` (an accepted or finished step: the task-state documents).
    """
    __slots__ = ('snapshot', 'label', 'companion', 'entry_id', 'at')

    def __init__(self, snapshot, label, companion=None):
        self.snapshot = snapshot
        self.label = label
        self.companion = companion
        self.entry_id = next(_ids)
        self.at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def describe(self) -> dict:
        kind = (self.companion or {}).get('kind', 'stage') if self.companion else ''
        return {'id': self.entry_id, 'label': self.label, 'at': self.at,
                'stage': kind == 'stage', 'kind': kind}


class History:
    def __init__(self, max_depth: int = 50):
        self._undo: list[_Entry] = []
        self._redo: list[_Entry] = []
        self._max_depth = max_depth

    def record(self, snapshot, label: str = '', companion: dict | None = None) -> None:
        """Record the state *before* a change so it can be undone to."""
        self._undo.append(_Entry(snapshot, label, companion))
        if len(self._undo) > self._max_depth:
            self._undo.pop(0)
        self._redo.clear()

    def mark(self) -> int:
        """Where the history stands now, for :meth:`squash_since`."""
        return self._undo[-1].entry_id if self._undo else 0

    def squash_since(self, mark: int, snapshot, label: str,
                     companion: dict | None) -> None:
        """Make every step taken since *mark* one step (UXD7).

        An operation that moves something the settings cannot hold often also
        commits settings as it goes (a geometry operation reopens the
        preparation decision). The user did one thing, so Undo takes it back
        in one move: the state before the first folded step is the state
        before the operation, and *snapshot* is that state when nothing was
        committed in between.
        """
        first = next((index for index, entry in enumerate(self._undo)
                      if entry.entry_id > mark), len(self._undo))
        folded = self._undo[first:]
        del self._undo[first:]
        if folded:
            snapshot = folded[0].snapshot
        self._undo.append(_Entry(snapshot, label, companion))
        if len(self._undo) > self._max_depth:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ''

    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ''

    def undo_companion(self) -> dict | None:
        return self._undo[-1].companion if self._undo else None

    def redo_companion(self) -> dict | None:
        return self._redo[-1].companion if self._redo else None

    def undo(self, current):
        """Return the snapshot to restore, pushing *current* onto the redo stack."""
        if not self._undo:
            return None
        entry = self._undo.pop()
        snapshot = entry.snapshot
        entry.snapshot = current
        self._redo.append(entry)
        return snapshot

    def redo(self, current):
        if not self._redo:
            return None
        entry = self._redo.pop()
        snapshot = entry.snapshot
        entry.snapshot = current
        self._undo.append(entry)
        return snapshot

    def entries(self) -> dict:
        """What the History panel lists, oldest first.

        ``done`` are the steps taken, oldest first, the last being the one
        Undo would take back; ``undone`` are the steps Redo would re-apply,
        the next one first.
        """
        return {'done': [entry.describe() for entry in self._undo],
                'undone': [entry.describe() for entry in reversed(self._redo)]}

    def companions(self) -> list[dict]:
        """Every companion still reachable by undo or redo."""
        return [entry.companion for entry in (*self._undo, *self._redo)
                if entry.companion]

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
