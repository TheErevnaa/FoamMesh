"""Which geometry rows a castellation refinement group refines.

DP-490 (audit MA-04). A surface refinement group is a ``refinementSurfaces``
entry only once some surface row names it in ``geometry/*/castellationGroup``,
and a volume refinement group a ``refinementRegions`` entry only once a volume
row does: that binding is the one thing the writer reads
(``CaseBuilder._refinement_surfaces`` / ``_refinement_regions``). The legacy
Castellation page set it through the "Select surfaces" / "Select volumes"
lists of ``SurfaceRefinementDialog`` and ``VolumeRefinementDialog``. Routing
the task through the shared child editor (759bc490) kept the levels and lost
the lists, so every group saved from the current route was bound to nothing:
MEASURED on the audit's S1 and S2 cases, the saved rows asked for levels 1/2
and inside-2, every ``castellationGroup`` was null, and the dictionary written
next carried ``level (0 0)`` and an empty ``refinementRegions``.

This is those lists again, over the same store, the same eligibility rules
and the same merged-boundary expansion -- shown inside the row editor, under
the group's name, where the decision is made.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.db.configurations_schema import GeometryType, Shape
from foammesh.view.facade_client import submit
from foammesh.view.geometry.display_name import readable_geometry_name
from foammesh.view.geometry.merged_boundaries import MergedBoundaries

SURFACE = GeometryType.SURFACE.value
VOLUME = GeometryType.VOLUME.value

#: The six faces a bounding Hex6 publishes as surface rows.
_PLATES = tuple(Shape.PLATES.value)
#: DP-578. The surface rows a surface refinement group can refine; DP-668
#: adds the closed surface of a box, sphere or cylinder.
_SURFACE_GROUP_SHAPES = (Shape.TRI_SURFACE_MESH.value, '', None,
                         Shape.HEX.value, Shape.SPHERE.value,
                         Shape.CYLINDER.value)


class _Row:
    def __init__(self, values: dict):
        self._values = values

    def value(self, name):
        return self._values.get(name)


class _GeometryView:
    """The one ``db`` call :class:`MergedBoundaries` makes, over a dict."""

    def __init__(self, geometry: dict):
        self._geometry = geometry

    def getElements(self, _path):
        return {key: _Row(value) for key, value in self._geometry.items()}


def _key(value):
    """A stored group id, or None; ids are compared as strings."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ('', 'None') else text


def _order(item):
    key = item[0]
    return (not key.isdigit(), int(key) if key.isdigit() else 0, key)


def geometry_rows(configuration) -> dict:
    """``{gId: stored fields}`` of the configuration's geometry rows."""
    geometry = (configuration or {}).get('geometry') or {}
    return {str(key): dict(value) for key, value in geometry.items()
            if isinstance(value, dict)}


def bound_groups(configuration, kind: str) -> set:
    """The group ids at least one *kind* row is bound to."""
    return {_key(row.get('castellationGroup'))
            for row in geometry_rows(configuration).values()
            if row.get('gType') == kind
            and _key(row.get('castellationGroup')) is not None}


class RefinementMembership(QWidget):
    """A checkable list of the geometry rows one refinement group refines.

    ``load(group)`` fills it for the row about to be edited -- ``None`` for a
    row not created yet -- and ``commit(group)`` writes the difference
    through ``geometry.items.patch`` once the group has its id.
    """

    def __init__(self, client, kind: str, parent=None):
        super().__init__(parent)
        self._client = client
        self._kind = kind
        self._loadedFor = None
        self._loaded = False
        self._initial: set = set()
        self._merged = MergedBoundaries(_GeometryView({}), rows=())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = (self.tr('Select surfaces') if kind == SURFACE
                 else self.tr('Select volumes'))
        self._title = QLabel(title)
        layout.addWidget(self._title)
        self.list = QListWidget()
        self.list.setObjectName(f'castellation{kind.title()}Membership')
        self.list.setAccessibleName(title)
        self.list.setToolTip(
            self.tr('The surfaces this group refines. A group that refines '
                    'no surface is not written to the mesher.')
            if kind == SURFACE else
            self.tr('The volumes this group refines. A group that refines '
                    'no volume is not written to the mesher.'))
        layout.addWidget(self.list)
        self._empty = QLabel(
            self.tr('There is no surface to refine yet.') if kind == SURFACE
            else self.tr('There is no volume to refine yet. Add one on the '
                         'Geometry page.'))
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)

    # -- reading ----------------------------------------------------------- #

    def _configuration(self) -> dict:
        try:
            return self._client.configuration() or {}
        except Exception:                                    # noqa: BLE001
            return {}

    def _eligible(self, configuration, group):
        """``(gId, label, checked)`` for every row this group may refine.

        The legacy dialogs' rules: rows of this kind, not the bounding Hex6
        or its faces, not bound to another group, and a merged boundary once
        under its merged name rather than once per row it folded.
        """
        rows = geometry_rows(configuration)
        hex6 = _key(((configuration or {}).get('baseGrid') or {})
                    .get('boundingHex6'))
        entries = []
        for gId, row in sorted(rows.items(), key=_order):
            if row.get('gType') != self._kind:
                continue
            if hex6 is not None:
                if self._kind == VOLUME and gId == hex6:
                    continue
                if (self._kind == SURFACE and row.get('shape') in _PLATES
                        and _key(row.get('volume')) == hex6):
                    continue
            if self._kind == SURFACE and self._merged.isFollower(gId):
                continue
            owner = _key(row.get('castellationGroup'))
            if owner is not None and owner != group:
                continue
            # DP-578. A surface group refines imported surfaces and (DP-668)
            # a box, sphere or cylinder surface; any other modelled surface
            # has no writer and a group bound
            # to it wrote nothing. Still listed when already bound, so an old
            # project can take it off.
            if (self._kind == SURFACE
                    and row.get('shape') not in _SURFACE_GROUP_SHAPES
                    and (group is None or owner != group)):
                continue
            parent = rows.get(_key(row.get('volume')) or '', {}).get('name')
            label = readable_geometry_name(
                row.get('name'), path=row.get('path'), parent=parent)
            label = self._merged.nameFor(gId, label)
            entries.append((gId, label, group is not None and owner == group))
        return entries

    def load(self, group=None) -> None:
        """Show the rows open to *group*, checking the ones bound to it."""
        group = _key(group)
        configuration = self._configuration()
        if self._kind == SURFACE:
            self._merged = MergedBoundaries(
                _GeometryView(geometry_rows(configuration)))
        self.list.clear()
        self._initial = set()
        for gId, label, checked in self._eligible(configuration, group):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, gId)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked
                               else Qt.CheckState.Unchecked)
            self.list.addItem(item)
            if checked:
                self._initial.add(gId)
        self._empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)
        self._loadedFor = group
        self._loaded = True

    def checked(self) -> set:
        return {str(self.list.item(index).data(Qt.ItemDataRole.UserRole))
                for index in range(self.list.count())
                if self.list.item(index).checkState() == Qt.CheckState.Checked}

    def setChecked(self, gIds) -> None:
        wanted = {str(gId) for gId in gIds}
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole)) in wanted
                else Qt.CheckState.Unchecked)

    # -- writing ----------------------------------------------------------- #

    def pending(self, group) -> dict:
        """``{gId: group or None}`` still to be written for *group*.

        Empty unless the list was loaded for this row: a list left over from
        another row must never be written onto this one.
        """
        group = _key(group)
        if not self._loaded or group is None:
            return {}
        if self._loadedFor is not None and self._loadedFor != group:
            return {}
        checked = self.checked()
        changes = {gId: group for gId in checked - self._initial}
        changes.update({gId: None for gId in self._initial - checked})
        if self._kind == SURFACE:
            changes = self._merged.expand(changes)
        return changes

    def commit(self, group, then=None) -> None:
        """Bind the checked rows to *group* and unbind the unchecked ones."""
        changes = self.pending(group)
        self._loaded = False
        self._initial = set()
        _patch_bindings(self._client, changes, then)


def _patch_bindings(client, changes: dict, then=None,
                    field: str = 'castellation_group') -> None:
    """Write ``{gId: group or None}`` as *field* patches on geometry rows.

    DP-524. The layer groups bind through ``layer_group`` and
    ``slave_layer_group`` by the same operation, so the field is a parameter.
    """
    items = sorted(changes.items())
    if not items:
        if then is not None:
            then()
        return
    last = len(items) - 1
    for index, (gId, group) in enumerate(items):
        done = None
        if then is not None and index == last:
            def done(_result, then=then):
                then()
        submit(client, 'geometry.items.patch',
               {'entity_id': str(gId), 'fields': {field: group}},
               then=done)


def unbind_group(client, kind: str, group, then=None) -> None:
    """Clear every *kind* row bound to a group that was just removed.

    Row ids are reused, so a binding left behind on a removed group is
    inherited by the next group created under the same id.
    """
    group = _key(group)
    try:
        configuration = client.configuration() or {}
    except Exception:                                        # noqa: BLE001
        configuration = {}
    changes = {gId: None for gId, row in geometry_rows(configuration).items()
               if row.get('gType') == kind
               and _key(row.get('castellationGroup')) == group}
    _patch_bindings(client, changes, then)
