"""Size at points, picked in the viewport (Plan 41 GN7, M-01).

Gmsh's Mesh > Define > Size at points.  **Pick points** starts a pick on the
model with the Points filter; each accepted point becomes a row carrying the
size typed beside the button.  A row can be switched off, its size edited, or
removed.  The rows are ``gmsh.point_sizes.rows`` (``core/gmsh/point_sizes``);
they apply whatever "From points" says on Sizing, because a row is an
explicit request for that point.

Adoptable by a task page (``pending_patch``/``dirtyChanged``/``reload``).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core import display_unit
from foammesh.core.gmsh import point_sizes as points
from foammesh.core.quantities import count_text
from foammesh.view.workflow_controls.length_box import (
    LengthItemDelegate, ModelLengthBox, UnitLabel, length_header,
    mark_length_item,
)

COLUMN_NAME, COLUMN_POINT, COLUMN_SIZE, COLUMN_ON = range(4)


class PointSizesPanel(QWidget):
    """Edits ``gmsh.pointSizes.rows``; adoptable by a task page."""

    dirtyChanged = Signal(bool)

    def __init__(self, client, entity_panel=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._entityPanel = entity_panel
        self._embedded = False
        self._rows: list = []
        self._stored: list = []
        self._dirty = False
        self._filling = False
        self.setObjectName('gmshPointSizes')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        pick = QHBoxLayout()
        # UXB3c. A length: the box shows the project display unit, the rows
        # (and Gmsh) keep model units.
        self._size = ModelLengthBox(self, minimum=1e-9, maximum=1e9,
                                    step=0.01)
        self._size.setObjectName('gmshPointSizeValue')
        self._size.setAccessibleName(self.tr('Size for the points to pick'))
        self._size.setModelValue(0.1)
        self._sizeUnit = UnitLabel(display_unit.current())
        self._sizeUnit.setObjectName('gmshPointSizeUnit')
        self._size.unit_label = self._sizeUnit
        self._pick = QPushButton(self.tr('Pick points'), self)
        self._pick.setObjectName('gmshPointSizePick')
        self._pick.setToolTip(self.tr(
            'Click points in the viewport; shift-click adds more; Enter '
            'accepts. Each gets the size beside this button.'))
        self._pick.clicked.connect(self.beginPick)
        pick.addWidget(self._size, 1)
        pick.addWidget(self._sizeUnit)
        pick.addWidget(self._pick)
        layout.addLayout(pick)

        self._table = QTableWidget(0, 4, self)
        self._table.setObjectName('gmshPointSizesTable')
        self._table.setAccessibleName(self.tr('Sizes at points'))
        self._table.setHorizontalHeaderLabels(
            [self.tr('Name'), self.tr('Point'),
             length_header(self.tr('Size')), self.tr('On')])
        self._table.setItemDelegateForColumn(COLUMN_SIZE, LengthItemDelegate(
            self._table))
        display_unit.subscribe(self._on_display_unit)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_NAME, QHeaderView.ResizeMode.Stretch)
        for column in (COLUMN_POINT, COLUMN_SIZE, COLUMN_ON):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(90)
        self._table.itemChanged.connect(self._onItemChanged)
        layout.addWidget(self._table)

        self._remove = QPushButton(self.tr('Remove point size'), self)
        self._remove.setObjectName('gmshPointSizeRemove')
        self._remove.clicked.connect(self.removeSelected)
        layout.addWidget(self._remove)
        self._status = QLabel('', self)
        self._status.setObjectName('gmshPointSizeStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        if entity_panel is not None:
            entity_panel.pickAccepted.connect(self._onPicked)
        self.reload()

    # -- state -------------------------------------------------------------- #

    def _on_display_unit(self) -> None:
        """The display unit changed: same stored sizes, new words."""
        try:
            self._table.setHorizontalHeaderLabels(
                [self.tr('Name'), self.tr('Point'),
                 length_header(self.tr('Size')), self.tr('On')])
            self._fill()
        except RuntimeError:
            pass

    def setEmbedded(self, embedded: bool = True) -> None:
        self._embedded = bool(embedded)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def rows(self) -> list:
        return [dict(r) for r in self._rows]

    def status(self) -> str:
        return self._status.text()

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def pending_patch(self) -> dict:
        if not self._dirty:
            return {}
        return {points.FIELD_ID: points.rows_to_text(self._rows)}

    def reload(self, *, discard_pending: bool = True) -> None:
        if discard_pending:
            self._dirty = False
        self._stored = self._read_stored()
        if discard_pending:
            self._rows = [dict(r) for r in self._stored]
        self._fill()
        self.dirtyChanged.emit(self._dirty)

    def revert(self) -> None:
        self.reload(discard_pending=True)

    def _read_stored(self) -> list:
        try:
            values = self._client.field_values((points.FIELD_ID,))
            raw = values.get(points.FIELD_ID)
            return points.rows_from_text(getattr(raw, 'value', raw))
        except points.PointSizeRefusal as error:
            self._say(str(error))
        except Exception:                                # noqa: BLE001
            pass
        return []

    # -- editing ------------------------------------------------------------ #

    def _changed(self) -> None:
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._fill()
        self._check()

    def _check(self) -> None:
        try:
            points.validate(self._rows)
        except points.PointSizeRefusal as error:
            self._say(str(error))
        else:
            n = len([r for r in self._rows if r.get('enabled', True)])
            self._say(count_text(n, self.tr('point size')) + self.tr(' on.'))

    def add_point(self, tag: int, size: float, xyz=None) -> dict:
        """One row for point *tag*; a point that already has one is updated."""
        for row in self._rows:
            if int(row.get('tag', 0)) == int(tag):
                row['size'] = float(size)
                row['enabled'] = True
                self._changed()
                return row
        taken = {r['name'] for r in self._rows}
        name = f'point{int(tag)}'
        while name in taken:
            name += '_'
        row = {'name': name, 'tag': int(tag), 'size': float(size),
               'enabled': True}
        if xyz is not None:
            row.update(x=float(xyz[0]), y=float(xyz[1]), z=float(xyz[2]))
        self._rows.append(row)
        self._changed()
        return row

    def removeSelected(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            del self._rows[row]
            self._changed()

    def _fill(self) -> None:
        self._filling = True
        try:
            self._table.setRowCount(len(self._rows))
            for index, row in enumerate(self._rows):
                cells = (
                    (COLUMN_NAME, str(row['name']), True),
                    (COLUMN_POINT, str(row['tag']), False),
                    (COLUMN_SIZE, f'{float(row["size"]):g}', True))
                for column, text, editable in cells:
                    item = QTableWidgetItem(text)
                    flags = item.flags()
                    if not editable:
                        flags &= ~Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    if column == COLUMN_SIZE:
                        mark_length_item(item)
                    self._table.setItem(index, column, item)
                if row.get('x') is not None:
                    self._table.item(index, COLUMN_POINT).setToolTip(
                        '(%s) %s' % (', '.join(
                            display_unit.significant(
                                display_unit.to_display(row[axis]))
                            for axis in 'xyz'), display_unit.current()))
                on = QTableWidgetItem()
                on.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                            | Qt.ItemFlag.ItemIsEnabled)
                on.setCheckState(Qt.CheckState.Checked
                                 if row.get('enabled', True)
                                 else Qt.CheckState.Unchecked)
                self._table.setItem(index, COLUMN_ON, on)
        finally:
            self._filling = False

    def _onItemChanged(self, item) -> None:
        if self._filling or not 0 <= item.row() < len(self._rows):
            return
        row = self._rows[item.row()]
        if item.column() == COLUMN_NAME:
            row['name'] = item.text().strip() or row['name']
        elif item.column() == COLUMN_SIZE:
            try:
                row['size'] = float(item.text())
            except ValueError:
                self._say(self.tr('The size needs a number.'))
                self._fill()
                return
        elif item.column() == COLUMN_ON:
            row['enabled'] = item.checkState() == Qt.CheckState.Checked
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._check()

    # -- picking ------------------------------------------------------------ #

    def beginPick(self) -> None:
        if self._entityPanel is None:
            self._say(self.tr('There is no viewport to pick in.'))
            return
        if not self._entityPanel.pickFor('Points'):
            self._say(self._entityPanel.status())
            return
        self._say(self.tr('Click points in the viewport; Enter accepts.'))

    def _onPicked(self, entities_) -> None:
        picker = self._entityPanel.picker() if self._entityPanel else None
        added = 0
        for dim, tag in entities_:
            if int(dim) != 0:
                continue
            xyz = None
            if picker is not None:
                record = picker.map.records.get((0, int(tag)))
                xyz = getattr(record, 'xyz', None)
            self.add_point(int(tag), self._size.modelValue(), xyz)
            added += 1
        if added:
            self._say(count_text(added, self.tr('point'))
                      + self.tr(' added.'))
