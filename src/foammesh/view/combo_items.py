"""Enable and disable the entries of a combo box without a QStandardItem.

DP-1447. ``combo.model().item(i)`` hands Python a wrapper for a
``QStandardItem``.  That class is not a QObject, so nothing tells the wrapper
when the combo clears or rebuilds its entries; the stale wrapper stayed in
the binding's address map, and a later object allocated at the same address
was looked up as a ``QStandardItem`` -- ``combo.model()`` answered a
``QStandardItem`` and, at the end of the Gmsh guided walks, an event filter
was called with ``(QAbstractEventDispatcher, QStandardItem)`` (rc 139).  The
item's flags are the model's data in role ``UserRole - 1``, which needs no
item wrapper at all.
"""
from __future__ import annotations

from PySide6.QtCore import Qt

#: The role a QStandardItem keeps its flags in.
FLAGS_ROLE = Qt.ItemDataRole.UserRole - 1


def set_item_enabled(combo, index: int, enabled: bool) -> None:
    """Enable or grey the entry *index* of *combo*."""
    model = combo.model()
    cell = model.index(index, 0)
    if not cell.isValid():
        return
    flags = model.flags(cell)
    flags = (flags | Qt.ItemFlag.ItemIsEnabled) if enabled else (
        flags & ~Qt.ItemFlag.ItemIsEnabled)
    model.setData(cell, int(flags.value), FLAGS_ROLE)


def item_enabled(combo, index: int) -> bool | None:
    """Whether the entry *index* can be picked; None when there is none."""
    model = combo.model()
    cell = model.index(index, 0)
    if not cell.isValid():
        return None
    return bool(model.flags(cell) & Qt.ItemFlag.ItemIsEnabled)
