"""Layers per patch (Plan 41 GN10, M-34/M-35).

One row per patch that grows its own stack: layer count, first height, ratio,
total, elements per layer and quads.  A patch with no row keeps the stack
above.  Rows are ``gmsh.boundary_layers.stacks``.  *Preview layers* draws the
ghost prisms (``core/gmsh/layer_preview``) of the rows in the viewport.

Adoptable by a task page (``pending_patch``/``dirtyChanged``/``reload``).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)

from foammesh.core import display_unit
from foammesh.core.gmsh import layers as stack_layers
from foammesh.core.quantities import count_text
from foammesh.view.workflow_controls.length_box import (
    LengthItemDelegate, length_header, mark_length_item,
)
from widgets.fit_to_text import FlowLayout

FIELD_ID = 'gmsh.boundary_layers.stacks'
#: DP-1344 (UXB1) retitled the layer switch ``gmsh.boundary_layers.quads``
#: from the dictionary word "Quads"; a stack's own switch is the same
#: setting per patch and carries the same name (DP-162).
COLUMNS = (('patch', 'Patch'), ('layerCount', 'Layers'),
           ('firstHeight', 'First height'), ('ratio', 'Ratio'),
           ('totalThickness', 'Total'), ('elementsPerLayer', 'Elements/layer'),
           ('quads', 'Prism and brick layer cells'), ('enabled', 'On'))
_NUMBERS = {'layerCount': int, 'firstHeight': float, 'ratio': float,
            'totalThickness': float, 'elementsPerLayer': int}
_FLAGS = ('quads', 'enabled')
#: UXB3c. The columns that hold a length in model units.
_LENGTHS = ('firstHeight', 'totalThickness')


class LayerStacksPanel(QWidget):
    """Edits ``gmsh.boundaryLayers.stacks``; adoptable by a task page."""

    dirtyChanged = Signal(bool)

    def __init__(self, client, page=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._page = page
        self._embedded = False
        self._rows: list = []
        self._dirty = False
        self._filling = False
        self._ghosts: list = []
        self.setObjectName('gmshLayerStacks')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName('gmshLayerStacksTable')
        self._table.setAccessibleName(self.tr('Layers per patch'))
        # Plan 33 section 6 check 1: what the table means is said on the
        # table, not as a paragraph standing in the settings column.
        explanation = self.tr(
            'A patch with a row grows that stack; every other patch keeps '
            'the stack above. Patches whose stacks differ may not touch.')
        self._table.setToolTip(explanation)
        self._table.setAccessibleDescription(explanation)
        self._table.setHorizontalHeaderLabels(self._headers())
        delegate = LengthItemDelegate(self._table, minimum=0.0)
        for column, (key, _label) in enumerate(COLUMNS):
            if key in _LENGTHS:
                self._table.setItemDelegateForColumn(column, delegate)
        display_unit.subscribe(self._on_display_unit)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(90)
        self._table.itemChanged.connect(self._onItemChanged)
        layout.addWidget(self._table)
        # DP-573: a row of controls wraps rather than standing wider than
        # the settings column (the compact column is 314 px).
        buttons = FlowLayout()
        self._add = QPushButton(self.tr('Add stack'), self)
        self._add.setObjectName('gmshLayerStackAdd')
        self._add.clicked.connect(lambda *_: self.addRow())
        self._remove = QPushButton(self.tr('Remove stack'), self)
        self._remove.setObjectName('gmshLayerStackRemove')
        self._remove.clicked.connect(self.removeSelected)
        self._preview = QCheckBox(self.tr('Preview layers'), self)
        self._preview.setObjectName('gmshLayerStackPreview')
        self._preview.toggled.connect(self.showPreview)
        for w in (self._add, self._remove, self._preview):
            buttons.addWidget(w)
        layout.addLayout(buttons)
        self._status = QLabel('', self)
        self._status.setObjectName('gmshLayerStackStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self.reload()

    # -- state -------------------------------------------------------------- #

    def _headers(self) -> list:
        return [length_header(self.tr(label)) if key in _LENGTHS
                else self.tr(label) for key, label in COLUMNS]

    def _on_display_unit(self) -> None:
        """The display unit changed: same stored heights, new words."""
        try:
            self._table.setHorizontalHeaderLabels(self._headers())
            self._table.viewport().update()
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

    def pending_patch(self) -> dict:
        if not self._dirty:
            return {}
        return {FIELD_ID: stack_layers.stacks_to_text(self._rows)}

    def reload(self, *, discard_pending: bool = True) -> None:
        if discard_pending:
            self._dirty = False
            self._rows = self._read_stored()
        self._fill()
        self.dirtyChanged.emit(self._dirty)

    def revert(self) -> None:
        self.reload(discard_pending=True)

    def _read_stored(self) -> list:
        try:
            raw = self._client.field_values((FIELD_ID,)).get(FIELD_ID)
            return stack_layers.stacks_from_text(getattr(raw, 'value', raw))
        except stack_layers.LayerError as error:
            self._status.setText(str(error))
        except Exception:                                # noqa: BLE001
            pass
        return []

    # -- editing ------------------------------------------------------------ #

    def _patchNames(self) -> list:
        try:
            return [name for name, _cat in self._page.preparedPatches()]
        except Exception:                                # noqa: BLE001
            return []

    def addRow(self, patch: str = '') -> dict:
        if not patch:
            taken = {r.get('patch') for r in self._rows}
            free = [n for n in self._patchNames() if n not in taken]
            patch = free[0] if free else ''
        row = {'patch': patch, 'enabled': True}
        self._rows.append(row)
        self._changed()
        return row

    def removeSelected(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            del self._rows[row]
            self._changed()

    def _changed(self) -> None:
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._fill()
        self._check()

    def derived(self) -> tuple:
        """The rows as the derivation reads them; raises ``LayerError``."""
        base = {'mode': 'first_and_ratio', 'layerCount': 3,
                'firstHeight': 0.001, 'ratio': 1.2, 'totalThickness': 0.0,
                'elementsPerLayer': 1, 'quads': True}
        return stack_layers._derive_stacks(self._rows, base, [], None)

    def _check(self) -> None:
        try:
            stacks = self.derived()
        except stack_layers.LayerError as error:
            self._status.setText(str(error))
            return
        self._status.setText(
            count_text(len(stacks), self.tr('layer stack')) + self.tr(' on.'))
        if self._preview.isChecked():
            self.showPreview(True)

    def _fill(self) -> None:
        self._filling = True
        try:
            self._table.setRowCount(len(self._rows))
            for r, row in enumerate(self._rows):
                for c, (key, _label) in enumerate(COLUMNS):
                    if key in _FLAGS:
                        item = QTableWidgetItem()
                        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                                      | Qt.ItemFlag.ItemIsEnabled)
                        on = row.get(key, key == 'enabled')
                        item.setCheckState(Qt.CheckState.Checked if on
                                           else Qt.CheckState.Unchecked)
                    else:
                        value = row.get(key, '')
                        if value in (None, ''):
                            text = ''
                        elif isinstance(value, float):
                            text = f'{value:g}'
                        else:
                            text = str(value)
                        item = QTableWidgetItem(text)
                        if key in _LENGTHS:
                            mark_length_item(item, unset_text=self.tr(
                                'Not set'))
                    self._table.setItem(r, c, item)
        finally:
            self._filling = False

    def _onItemChanged(self, item) -> None:
        if self._filling or not 0 <= item.row() < len(self._rows):
            return
        row = self._rows[item.row()]
        key = COLUMNS[item.column()][0]
        if key in _FLAGS:
            row[key] = item.checkState() == Qt.CheckState.Checked
        elif key == 'patch':
            row[key] = item.text().strip()
        else:
            text = item.text().strip()
            if not text:
                row.pop(key, None)
            else:
                try:
                    number = float(text)
                except ValueError:
                    self._status.setText(
                        self.tr('That column needs a number.'))
                    self._fill()
                    return
                row[key] = int(number) if _NUMBERS[key] is int else number
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._check()

    # -- ghost prisms ------------------------------------------------------- #

    def showPreview(self, on: bool = True) -> int:
        """Draw (or clear) the ghost shells; returns how many were drawn."""
        self.clearPreview()
        if not on:
            return 0
        try:
            stacks = self.derived()
        except stack_layers.LayerError:
            return 0
        from foammesh.app import app
        window = getattr(app, 'window', None)
        manager = getattr(window, 'geometryManager', None)
        display = getattr(window, 'displayControl', None)
        if manager is None or display is None or self._page is None:
            return 0
        identity = self._page.preparedIdentity()
        drawn = 0
        for stack in stacks:
            gid = identity.get(stack['patch'], ('', ''))[0]
            try:
                actor = self._ghostActor(manager.polyData(gid), stack)
            except Exception:                            # noqa: BLE001
                continue
            display.addOverlay(actor)
            self._ghosts.append(actor)
            drawn += 1
        return drawn

    @staticmethod
    def _ghostActor(data, stack):
        import numpy as np
        import vtk
        from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

        from foammesh.core.gmsh import layer_preview

        tri = vtk.vtkTriangleFilter()
        tri.SetInputData(data)
        tri.Update()
        mesh = tri.GetOutput()
        points = vtk_to_numpy(mesh.GetPoints().GetData())
        cells = vtk_to_numpy(mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
        shells = layer_preview.offset_shells(
            points, cells, [layer_preview.reach(stack)], points.mean(axis=0))
        out = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        pts.SetData(numpy_to_vtk(np.ascontiguousarray(shells[-1][1])))
        out.SetPoints(pts)
        out.SetPolys(mesh.GetPolys())
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(out)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.2, 0.6, 0.9)
        actor.GetProperty().SetOpacity(0.25)
        actor.PickableOff()
        return actor

    def clearPreview(self) -> None:
        if not self._ghosts:
            return
        from foammesh.app import app
        display = getattr(getattr(app, 'window', None), 'displayControl', None)
        for actor in self._ghosts:
            try:
                display.removeOverlay(actor)
            except Exception:                            # noqa: BLE001
                pass
        self._ghosts = []
