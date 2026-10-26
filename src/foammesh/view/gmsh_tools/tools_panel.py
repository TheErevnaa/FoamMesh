"""The form of the Gmsh tool dock (Plan 48 GM11b).

One panel in the rail's popover.  Every control calls a method of
:class:`~foammesh.view.gmsh_tools.dock.GmshToolDock`, so the form and the
keys and the local API run the same code.  A control that opens a panel that
lives elsewhere is a link: *links* maps the tool id to a callable that
brings it forward.  Plan 48 DK: the Entities, Visibility and display and
Plugin views panels are *hosted* here -- the Gmsh pages mount their panel in
the section of the same key (:meth:`mountHosted`), closed until asked for;
the Model buttons open the section.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from foammesh.view.gmsh_tools import point_placement as placing
from foammesh.view.theming.metrics import (
    CompactDoubleSpinBox, FORM_MARGIN, GAP, GAP_TIGHT,
)

#: The hosted sections, in the order they are drawn: ``(key, title)``.
HOSTED = (
    ('entities', 'Entities'),
    ('visibility', 'Visibility and display'),
    ('plugin_views', 'Plugin views'),
)

HELP_ELEMENT_PICK = (
    'Reverse and Delete take element numbers: pick single elements in the '
    'viewport with the Cells filter of the Pick tool (offered while a mesh '
    'is on screen), pick entities, or fill the poor elements, on the '
    'Generate panel.')


def _scrollTo(scroll, box) -> None:
    try:
        scroll.ensureWidgetVisible(box, 0, 8)
    except RuntimeError:
        pass                                       # the section has gone


class GmshToolsPanel(QWidget):
    """Entities, display, bounding boxes, previews, placement and colours."""

    def __init__(self, dock, links=None, parent=None):
        super().__init__(parent)
        self._dock = dock
        self._links = dict(links or {})
        self._building = True
        self.setObjectName('gmshToolsPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP)

        layout.addWidget(self._linksGroup())
        self._hosted: dict = {}
        self._sections: dict = {}
        for key, title in HOSTED:
            layout.addWidget(self._section(key, self.tr(title)))
        layout.addWidget(self._boxesGroup())
        layout.addWidget(self._placementGroup())
        layout.addWidget(self._previewGroup())
        layout.addWidget(self._partitionGroup())
        self._status = QLabel('', self)
        self._status.setObjectName('gmshToolsStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        layout.addStretch(1)
        self._building = False
        dock.listeners.append(self.refresh)
        self.refresh()

    def addPanel(self, title: str, widget) -> QGroupBox:
        """A registered panel, in a group of its own before the status."""
        box, inner = self._group(title, 'gmshToolsPanel_' + (
            widget.objectName() or title.replace(' ', '_')))
        inner.addWidget(widget)
        layout = self.layout()
        layout.insertWidget(max(0, layout.count() - 2), box)
        return box

    # -- hosted panels (Plan 48 DK) ---------------------------------------- #

    def _section(self, key: str, title: str) -> QGroupBox:
        """A closed, checkable section a page fills with its panel."""
        box = QGroupBox(title, self)
        box.setObjectName('gmshToolsHost_' + key)
        box.setCheckable(True)
        box.setChecked(False)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        body = QWidget(box)
        body.setObjectName('gmshToolsHostBody_' + key)
        QVBoxLayout(body).setContentsMargins(0, 0, 0, 0)
        body.setVisible(False)
        outer.addWidget(body)
        box.toggled.connect(body.setVisible)
        box.setVisible(False)
        self._sections[key] = (box, body)
        return box

    def mountHosted(self, key: str, widget) -> bool:
        """Put *widget* in the section *key*; a panel held before is released.

        The section stays closed (as the closed tier it replaces was) and is
        shown from now on.  False for an unknown key.
        """
        if key not in self._sections:
            return False
        box, body = self._sections[key]
        self.unmountHosted(key, self._hosted.get(key))
        widget.setParent(body)
        body.layout().addWidget(widget)
        widget.setVisible(True)
        self._hosted[key] = widget
        box.setVisible(True)
        return True

    def unmountHosted(self, key: str, widget) -> None:
        """Release *widget* when it is the one held in *key*."""
        if widget is None or key not in self._sections:
            return
        box, _body = self._sections[key]
        try:
            if widget is self._hosted.get(key):
                del self._hosted[key]
                box.setVisible(False)
                box.setChecked(False)
            widget.setParent(None)
            widget.deleteLater()
        except RuntimeError:
            pass                                   # already gone

    def hosted(self, key: str):
        """The panel held in section *key*, or ``None``."""
        widget = self._hosted.get(key)
        try:
            if widget is not None:
                widget.objectName()
        except RuntimeError:
            self._hosted.pop(key, None)
            return None
        return widget

    def showSection(self, key: str) -> bool:
        """Open section *key* and scroll it into view; False when empty."""
        if self.hosted(key) is None:
            return False
        box, _body = self._sections[key]
        box.setChecked(True)
        scroll = self.parentWidget()
        while scroll is not None and not isinstance(scroll, QScrollArea):
            scroll = scroll.parentWidget()
        if scroll is not None:
            QTimer.singleShot(0, lambda: _scrollTo(scroll, box))
        return True

    def hideSection(self, key: str) -> None:
        if key in self._sections:
            self._sections[key][0].setChecked(False)

    def sectionOpen(self, key: str) -> bool:
        return key in self._sections and self._sections[key][0].isChecked()

    # -- groups ------------------------------------------------------------ #

    def _group(self, title: str, name: str) -> tuple:
        box = QGroupBox(title, self)
        box.setObjectName(name)
        inner = QVBoxLayout(box)
        inner.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        inner.setSpacing(GAP_TIGHT)
        return box, inner

    def _button(self, text, name, handler, tip='') -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(name)
        if tip:
            button.setToolTip(tip)
        button.clicked.connect(lambda _c=False: handler())
        return button

    def _linksGroup(self) -> QGroupBox:
        box, inner = self._group(self.tr('Model'), 'gmshToolsModel')
        for key, text, tip in (
                ('entities', self.tr('Entities…'),
                 self.tr('Open the Entities section: pick and name points, '
                         'curves, surfaces and volumes')),
                ('visibility', self.tr('Visibility and display…'),
                 self.tr('Hide and show entities, the display options and '
                         'the manipulator')),
                ('plugin_views', self.tr('Plugins…'),
                 self.tr('Run Gmsh plugins and keep their views'))):
            button = self._button(
                text, 'gmshTools_' + key, lambda k=key: self._link(k), tip)
            inner.addWidget(button)
        note = QLabel(self.tr(HELP_ELEMENT_PICK), self)
        note.setObjectName('gmshToolsElementPickNote')
        note.setWordWrap(True)
        inner.addWidget(note)
        return box

    def _link(self, key: str) -> bool:
        action = self._links.get(key)
        if action is None:
            self._dock._say(self.tr('Open a project on the Gmsh route first.'))
            return False
        opened = bool(action() is not False)
        self.showSection(key)
        return opened

    def _boxesGroup(self) -> QGroupBox:
        box, inner = self._group(self.tr('Bounding boxes'), 'gmshToolsBoxes')
        self._boxes = QCheckBox(self.tr('Show bounding boxes'), self)
        self._boxes.setToolTip(self.tr('Show or hide the bounding boxes (Alt+B)'))
        self._boxes.setObjectName('gmshToolsBoxesShow')
        self._boxes.toggled.connect(self._boxesToggled)
        inner.addWidget(self._boxes)
        inner.addWidget(self._button(
            self.tr('Fit to visible entities (Alt+Shift+1)'),
            'gmshToolsBoxesFit', self._dock.reset_to_visible,
            self.tr('Reset the view to the box around what is visible')))
        return box

    def _boxesToggled(self, on: bool) -> None:
        if not self._building:
            self._dock.set_bounding_boxes(on)

    def _placementGroup(self) -> QGroupBox:
        box, inner = self._group(self.tr('Place points'), 'gmshToolsPlacement')
        self._place = QCheckBox(self.tr('Click in the viewport to place points'),
                                self)
        self._place.setObjectName('gmshToolsPlaceActive')
        self._place.toggled.connect(self._placeToggled)
        inner.addWidget(self._place)
        row = QHBoxLayout()
        self._freeze = {}
        for axis in placing.AXES:
            check = QCheckBox(self.tr('Freeze %s') % axis, self)
            check.setObjectName('gmshToolsFreeze_' + axis)
            check.toggled.connect(
                lambda on, a=axis: self._freezeToggled(a, on))
            row.addWidget(check)
            self._freeze[axis] = check
        inner.addLayout(row)
        plane = QHBoxLayout()
        self._plane = QComboBox(self)
        self._plane.setObjectName('gmshToolsPlane')
        for name in placing.PLANES:
            self._plane.addItem(self.tr('%s plane') % name, name)
        self._plane.currentIndexChanged.connect(self._planeChanged)
        self._offset = CompactDoubleSpinBox(self)
        self._offset.setObjectName('gmshToolsPlaneOffset')
        self._offset.setRange(-1e9, 1e9)
        self._offset.setDecimals(6)
        self._offset.setToolTip(self.tr('Where the work plane cuts its axis'))
        self._offset.valueChanged.connect(self._planeChanged)
        plane.addWidget(self._plane)
        plane.addWidget(self._offset)
        inner.addLayout(plane)
        grid = QHBoxLayout()
        grid.addWidget(QLabel(self.tr('Snap'), self))
        self._grid = CompactDoubleSpinBox(self)
        self._grid.setObjectName('gmshToolsSnap')
        self._grid.setRange(0.0, 1e9)
        self._grid.setDecimals(6)
        self._grid.setToolTip(self.tr('Round each coordinate to a multiple '
                                      'of this step; 0 is off'))
        self._grid.valueChanged.connect(self._gridChanged)
        grid.addWidget(self._grid)
        grid.addWidget(QLabel(self.tr('Size'), self))
        self._size = CompactDoubleSpinBox(self)
        self._size.setObjectName('gmshToolsPointSize')
        self._size.setRange(0.0, 1e9)
        self._size.setDecimals(6)
        self._size.setToolTip(self.tr('Mesh size at each new point; 0 is none'))
        self._size.valueChanged.connect(self._sizeChanged)
        grid.addWidget(self._size)
        inner.addLayout(grid)
        return box

    def _placeToggled(self, on: bool) -> None:
        if self._building:
            return
        if on:
            if not self._dock.start_placement():
                self._building = True
                self._place.setChecked(False)
                self._building = False
        else:
            self._dock.stop_placement()

    def _freezeToggled(self, axis: str, on: bool) -> None:
        if not self._building:
            self._dock.placement.placement.freeze(axis, on)

    def _planeChanged(self, *_args) -> None:
        if self._building:
            return
        self._dock.placement.placement.set_plane(
            self._plane.currentData(), self._offset.value())

    def _gridChanged(self, value: float) -> None:
        if not self._building:
            self._dock.placement.placement.set_grid(value)

    def _sizeChanged(self, value: float) -> None:
        if not self._building:
            self._dock.placement.placement.set_size(value)

    def _previewGroup(self) -> QGroupBox:
        box, inner = self._group(self.tr('Previews'), 'gmshToolsPreviews')
        self._previewNote = QLabel('', self)
        self._previewNote.setObjectName('gmshToolsPreviewNote')
        self._previewNote.setWordWrap(True)
        inner.addWidget(self._previewNote)
        inner.addWidget(self._button(
            self.tr('Clear previews'), 'gmshToolsClearPreviews',
            self._dock.clear_previews,
            self.tr('Remove the sweep, transfinite and reclassify drawings')))
        return box

    def _partitionGroup(self) -> QGroupBox:
        box, inner = self._group(self.tr('Partitions'), 'gmshToolsPartitions')
        self._partition = QCheckBox(self.tr('Colour the mesh by partition'),
                                    self)
        self._partition.setObjectName('gmshToolsPartitionColour')
        self._partition.toggled.connect(self._partitionToggled)
        inner.addWidget(self._partition)
        self._ghost = QCheckBox(self.tr('Create ghost cells when partitioning'),
                                self)
        self._ghost.setObjectName('gmshToolsGhostCells')
        self._ghost.setToolTip(self.tr(
            'Mesh.PartitionCreateGhostCells: a ghost entity per partition '
            'with the elements that touch a neighbour'))
        self._ghost.toggled.connect(self._ghostToggled)
        inner.addWidget(self._ghost)
        return box

    def _partitionToggled(self, on: bool) -> None:
        if self._building:
            return
        if not self._dock.set_partition_colour(on):
            self._building = True
            self._partition.setChecked(self._dock.partition_colour())
            self._building = False

    def _ghostToggled(self, on: bool) -> None:
        if self._building:
            return
        if not self._dock.set_ghost_cells(on):
            self._building = True
            self._ghost.setChecked(self._dock.ghost_cells())
            self._building = False

    # -- state -------------------------------------------------------------- #

    def refresh(self) -> None:
        """Show the dock's state in the controls."""
        dock = self._dock
        self._building = True
        try:
            self._boxes.setChecked(dock.bounding.shown())
            self._place.setChecked(dock.placement.active())
            placement = dock.placement.placement
            for axis, check in self._freeze.items():
                check.setChecked(axis in placement.frozen)
            index = self._plane.findData(placement.plane)
            if index >= 0:
                self._plane.setCurrentIndex(index)
            self._offset.setValue(placement.offset)
            self._grid.setValue(placement.grid)
            self._size.setValue(placement.size)
            self._partition.setChecked(dock.partition_colour())
            self._previewNote.setText(self.tr(
                'Sweep: %d segments. Transfinite: %d entities. '
                'Reclassify: %d edges.') % (
                    dock.sweep.count, len(dock.qualify.painted),
                    dock.reclassify.count))
            self._status.setText(dock.status or dock.placement.status())
        finally:
            self._building = False
