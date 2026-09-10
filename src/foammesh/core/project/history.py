#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Undo/redo over project-state snapshots.

The engine takes a snapshot (the project's serialized YAML configuration) before
each committed change. Undo restores the previous snapshot; redo re-applies an
undone one. A bounded depth keeps memory in check on large projects.

NOTE: snapshots cover the configuration state (the YAML). Binary geometry data
(imported polyData held outside the YAML) is not yet captured by undo — see the
phase-02 plan; this is a documented limitation to be closed later.
"""
from __future__ import annotations


class History:
    def __init__(self, max_depth: int = 50):
        self._undo: list[tuple[str, str]] = []
        self._redo: list[tuple[str, str]] = []
        self._max_depth = max_depth

    def record(self, snapshot: str, label: str = '') -> None:
        """Record the state *before* a change so it can be undone to."""
        self._undo.append((snapshot, label))
        if len(self._undo) > self._max_depth:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][1] if self._undo else ''

    def redo_label(self) -> str:
        return self._redo[-1][1] if self._redo else ''

    def undo(self, current: str) -> str | None:
        """Return the snapshot to restore, pushing *current* onto the redo stack."""
        if not self._undo:
            return None
        snapshot, label = self._undo.pop()
        self._redo.append((current, label))
        return snapshot

    def redo(self, current: str) -> str | None:
        if not self._redo:
            return None
        snapshot, label = self._redo.pop()
        self._undo.append((current, label))
        return snapshot

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
