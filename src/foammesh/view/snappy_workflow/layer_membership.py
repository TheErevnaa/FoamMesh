"""Which boundaries a snappy layer group grows its layers on.

DP-524. A layer group that selects its patches by geometry (``patchSelector
geometry``, the schema default) is written into ``addLayersControls/layers``
only for the geometry rows that name it in ``geometry/*/layerGroup`` -- or, for
the far side of an interface, ``slaveLayerGroup``: that binding is all
``CaseBuilder._layer_surfaces`` reads. The legacy Boundary Layers page set it
through the "Select boundaries" list of ``BoundarySettingDialog``. The routed
Layers page edits groups in the shared child editor, which had no such list,
so a group added there on the default selector grew layers on nothing -- the
same gap DP-490 closed for the castellation refinement groups.

This is that list again, over the same store and the legacy dialog's rules:
boundary and interface rows, not the bounding Hex6's faces, not a row another
group holds, a merged boundary once under its merged name, and the slave side
of an interface as its own ``<name>_slave`` entry. It is shown in the row
editor under ``Selects patches by`` while that says ``geometry``; a group that
matches by pattern reads no binding, so saving one releases what it held.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.db.configurations_schema import CFDType
from foammesh.view.geometry.display_name import readable_geometry_name
from foammesh.view.geometry.merged_boundaries import MergedBoundaries

from .refinement_membership import (
    _PLATES, _GeometryView, _key, _order, _patch_bindings, geometry_rows,
)

#: The selector value under which the geometry binding is what is read.
BY_GEOMETRY = 'geometry'
BY_PATTERN = 'pattern'

#: The two stored fields, and the facade spelling each is patched under.
MASTER = ('layerGroup', 'layer_group')
SLAVE = ('slaveLayerGroup', 'slave_layer_group')

_LAYERED = (CFDType.BOUNDARY.value, CFDType.INTERFACE.value)


#: Plan 37 UF20. Layer groups written a moment ago whose geometry binding is
#: still on its way. A group is created (or edited) first and bound second, as
#: two writes; between them no geometry row names it, which is exactly how an
#: unused group looks to the legacy Boundary layer page's prune. MEASURED
#: live (snappy elbow): ``create meshing.layers.groups/1`` at 15:48:03.7, the
#: hidden legacy page's ``remove unused layer groups`` at 15:48:04.5, the
#: binding at 15:48:10.1 -- the user's group was gone and the page's default
#: "Walls" took its id.
_BINDING_PENDING: set[str] = set()


def hold_binding(group) -> None:
    """Mark *group* as written and about to be bound."""
    key = _key(group)
    if key is not None:
        _BINDING_PENDING.add(key)


def release_binding(group) -> None:
    """The binding for *group* has landed (or will not come)."""
    key = _key(group)
    if key is not None:
        _BINDING_PENDING.discard(key)


#: Group writes sent and not yet answered. MEASURED on the re-run: the case
#: announces the create before the page hears its answer, and the legacy page
#: repaints -- and prunes -- on that announcement, so while a write is out no
#: id is known yet and every unbound group has to be spared.
_WRITES_OUT = [0]


def begin_group_write() -> None:
    """A group create or edit has been sent; its id is not known yet."""
    _WRITES_OUT[0] += 1


def end_group_write() -> None:
    """The write sent by ``begin_group_write`` has been answered."""
    _WRITES_OUT[0] = max(0, _WRITES_OUT[0] - 1)


def binding_pending(group) -> bool:
    """Whether *group* is between its write and its binding."""
    if _WRITES_OUT[0]:
        return True
    key = _key(group)
    return key is not None and key in _BINDING_PENDING


def _entry(gId, slave: bool) -> str:
    return f'{gId}s' if slave else str(gId)


def _split(entry: str):
    """``(gId, slave)`` for one list entry."""
    entry = str(entry)
    if entry.endswith('s'):
        return entry[:-1], True
    return entry, False


def bound_layer_groups(configuration) -> set:
    """The layer group ids at least one geometry row is bound to."""
    groups = set()
    for row in geometry_rows(configuration).values():
        for stored, _field in (MASTER, SLAVE):
            group = _key(row.get(stored))
            if group is not None:
                groups.add(group)
    return groups


class LayerMembership(QWidget):
    """A checkable list of the boundaries one layer group grows layers on.

    ``load(group)`` fills it for the row about to be edited -- ``None`` for a
    row not created yet -- and ``commit(group)`` writes the difference through
    ``geometry.items.patch`` once the group has its id. *selector* returns the
    row's ``patch_selector`` as the editor holds it.
    """

    def __init__(self, client, selector=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._selector = selector
        self._loadedFor = None
        self._loaded = False
        self._initial: set = set()
        self._merged = MergedBoundaries(_GeometryView({}), rows=())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # The contents, not this widget, follow the selector: the dialog's
        # relevance rule shows this row whenever it shows the selector, and
        # showing a widget does not re-show a child hidden on purpose.
        self._body = QWidget(self)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        title = self.tr('Select boundaries')
        self._title = QLabel(title)
        body.addWidget(self._title)
        self.list = QListWidget()
        self.list.setObjectName('layerGroupMembership')
        self.list.setAccessibleName(title)
        self.list.setToolTip(self.tr(
            'The boundaries this group grows layers on. A group that covers '
            'no boundary is not written to the mesher.'))
        body.addWidget(self.list)
        self._empty = QLabel(self.tr(
            'There is no boundary to grow layers on yet.'))
        self._empty.setWordWrap(True)
        body.addWidget(self._empty)
        layout.addWidget(self._body)

    # -- following the selector -------------------------------------------- #

    def selector(self) -> str:
        if self._selector is None:
            return BY_GEOMETRY
        try:
            value = self._selector()
        except Exception:                                    # noqa: BLE001
            return BY_GEOMETRY
        value = getattr(value, 'value', value)
        return str(value or BY_GEOMETRY)

    def byGeometry(self) -> bool:
        return self.selector() != BY_PATTERN

    def syncSelector(self, *_args) -> None:
        self._body.setVisible(self.byGeometry())

    def showEvent(self, event):
        # The editors are loaded with their signals blocked, so the value the
        # dialog opens on is read here rather than waited for.
        self.syncSelector()
        super().showEvent(event)

    # -- reading ----------------------------------------------------------- #

    def _configuration(self) -> dict:
        try:
            return self._client.configuration() or {}
        except Exception:                                    # noqa: BLE001
            return {}

    def _eligible(self, configuration, group):
        """``(entry, label, checked)`` for every boundary this group may take."""
        rows = geometry_rows(configuration)
        hex6 = _key(((configuration or {}).get('baseGrid') or {})
                    .get('boundingHex6'))
        entries = []
        for gId, row in sorted(rows.items(), key=_order):
            cfd = row.get('cfdType')
            if cfd not in _LAYERED:
                continue
            if hex6 is not None and (
                    gId == hex6
                    or (row.get('shape') in _PLATES
                        and _key(row.get('volume')) == hex6)):
                continue
            if self._merged.isFollower(gId):
                continue
            parent = rows.get(_key(row.get('volume')) or '', {}).get('name')
            label = readable_geometry_name(
                row.get('name'), path=row.get('path'), parent=parent)
            label = self._merged.nameFor(gId, label)
            sides = [(False, MASTER)]
            if cfd == CFDType.INTERFACE.value:
                sides.append((True, SLAVE))
            for slave, (stored, _field) in sides:
                owner = _key(row.get(stored))
                if owner is not None and owner != group:
                    continue
                entries.append((_entry(gId, slave),
                                f'{label}_slave' if slave else label,
                                group is not None and owner == group))
        return entries

    def load(self, group=None) -> None:
        """Show the boundaries open to *group*, checking the ones it holds."""
        group = _key(group)
        configuration = self._configuration()
        self._merged = MergedBoundaries(
            _GeometryView(geometry_rows(configuration)))
        self.list.clear()
        self._initial = set()
        for entry, label, checked in self._eligible(configuration, group):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked
                               else Qt.CheckState.Unchecked)
            self.list.addItem(item)
            if checked:
                self._initial.add(entry)
        self._empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)
        self._loadedFor = group
        self._loaded = True
        self.syncSelector()

    def checked(self) -> set:
        return {str(self.list.item(index).data(Qt.ItemDataRole.UserRole))
                for index in range(self.list.count())
                if self.list.item(index).checkState() == Qt.CheckState.Checked}

    def setChecked(self, entries) -> None:
        wanted = {str(entry) for entry in entries}
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole)) in wanted
                else Qt.CheckState.Unchecked)

    # -- writing ----------------------------------------------------------- #

    def pending(self, group) -> dict:
        """``{facade field: {gId: group or None}}`` still to be written.

        Empty unless the list was loaded for this row. A group that matches
        by pattern reads no binding, so it keeps none: whatever it held is
        released, and those boundaries are offered to other groups again.
        """
        group = _key(group)
        if not self._loaded or group is None:
            return {}
        if self._loadedFor is not None and self._loadedFor != group:
            return {}
        checked = self.checked() if self.byGeometry() else set()
        decided = {entry: group for entry in checked - self._initial}
        decided.update({entry: None for entry in self._initial - checked})
        changes = {MASTER[1]: {}, SLAVE[1]: {}}
        for entry, value in decided.items():
            gId, slave = _split(entry)
            field = SLAVE[1] if slave else MASTER[1]
            # R137. A merged boundary stands for every row it covers; each of
            # them carries the group, or layers reach one side of the wall.
            changes[field].update(self._merged.expand({gId: value}))
        return {field: rows for field, rows in changes.items() if rows}

    def commit(self, group, then=None) -> None:
        """Bind the checked boundaries to *group*, release the unchecked."""
        changes = self.pending(group)
        self._loaded = False
        self._initial = set()
        _write(self._client, changes, then)


def _write(client, changes: dict, then=None) -> None:
    """Patch each field's ``{gId: group}`` in turn, then call *then*."""
    fields = [(field, rows) for field, rows in sorted(changes.items()) if rows]
    if not fields:
        if then is not None:
            then()
        return

    def step(index: int) -> None:
        if index == len(fields):
            if then is not None:
                then()
            return
        field, rows = fields[index]
        _patch_bindings(client, rows, then=lambda: step(index + 1),
                        field=field)

    step(0)


def unbind_layer_group(client, group, then=None) -> None:
    """Clear every row bound to a layer group that was just removed.

    Row ids are reused, so a binding left behind on a removed group is
    inherited by the next group created under the same id.
    """
    group = _key(group)
    try:
        configuration = client.configuration() or {}
    except Exception:                                        # noqa: BLE001
        configuration = {}
    changes = {}
    for stored, field in (MASTER, SLAVE):
        rows = {gId: None for gId, row in geometry_rows(configuration).items()
                if _key(row.get(stored)) == group}
        if rows:
            changes[field] = rows
    _write(client, changes, then)
