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

NOTE: snapshots cover the configuration state. Binary geometry data (imported
polyData held outside the configuration) is not yet captured by undo — see the
phase-02 plan; this is a documented limitation to be closed later.
"""
from __future__ import annotations

from typing import Any


class History:
    def __init__(self, max_depth: int = 50):
        self._undo: list[tuple[Any, str]] = []
        self._redo: list[tuple[Any, str]] = []
        self._max_depth = max_depth

    def record(self, snapshot, label: str = '') -> None:
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

    def undo(self, current):
        """Return the snapshot to restore, pushing *current* onto the redo stack."""
        if not self._undo:
            return None
        snapshot, label = self._undo.pop()
        self._redo.append((current, label))
        return snapshot

    def redo(self, current):
        if not self._redo:
            return None
        snapshot, label = self._redo.pop()
        self._undo.append((current, label))
        return snapshot

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
