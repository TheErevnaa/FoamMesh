#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The section controls: what the plane is, where it is, which way it moves.

What existed before this was one scalar per axis -- ``'{:.6g}'.format(
origin[normal])`` -- and an *Invert* checkbox. That is the whole positional
interface a section plane had: no normal, no way to say which direction the
plane should travel, and no reading of where the plane sits relative to the
model.

The panel is built in code rather than in Designer because it has two homes:
the Display Control dock, and the overlay pinned to the viewport itself. A
``.ui`` widget has one parent; this one is instantiated twice and both
instances drive the same section state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QSizePolicy, QSlider,
    QVBoxLayout, QWidget)

from foammesh.rendering.plane_widget import AXIS_NORMAL
from foammesh.view.theming.metrics import GAP


#: How many independent section planes a user can raise at once. Three is the
#: number that makes a corner cut possible, which is what people actually build
#: when they are chasing a boundary layer into a fillet.
MAX_PLANES = 3

#: The position slider is integer-valued; this is its resolution across the
#: model's extent along the plane normal.
SLIDER_STEPS = 1000


class CutType(Enum):
    CLIP  = auto()  # noqa: E221
    SLICE = auto()


@dataclass
class SectionPlaneState:
    """One section plane, in model space rather than in axis indices."""
    enabled: bool = False
    origin: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    normal: list = field(default_factory=lambda: [1.0, 0.0, 0.0])

    def normalised(self):
        length = sum(value * value for value in self.normal) ** 0.5
        if not length:
            return [1.0, 0.0, 0.0]
        return [value / length for value in self.normal]


def _field(width=72):
    edit = QLineEdit()
    edit.setValidator(QDoubleValidator())
    edit.setMaximumWidth(width)
    edit.setAlignment(Qt.AlignmentFlag.AlignRight)
    return edit


def _compactButton(text, tooltip, width=34):
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setMaximumWidth(width)
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return button


class SectionPanel(QWidget):
    """Editing surface for a list of :class:`SectionPlaneState`."""

    #: The section changed and should be re-applied to the scene.
    sectionChanged = Signal()
    #: The set of planes that want a visible gizmo changed.
    gizmosChanged = Signal()
    #: The user asked for the plane normal to follow the current camera.
    viewNormalRequested = Signal()
    #: A plane row was selected; the argument is its index.
    activePlaneChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._planes = [SectionPlaneState() for _ in range(MAX_PLANES)]
        self._active = 0
        self._bounds = None
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP)

        layout.addWidget(self._buildModeRow())
        layout.addWidget(self._buildPlaneRows())
        layout.addWidget(self._separator())
        layout.addWidget(self._buildPlacement())
        layout.addWidget(self._buildFooter())

        self._connectSignalsSlots()
        self._syncFromState()

    # -- construction ------------------------------------------------------ #

    def _separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _buildModeRow(self):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        self._clipRadio = QRadioButton(self.tr('Clip'))
        self._clipRadio.setChecked(True)
        self._clipRadio.setToolTip(
            self.tr('Remove everything on one side of the plane'))
        self._sliceRadio = QRadioButton(self.tr('Slice'))
        self._sliceRadio.setToolTip(
            self.tr('Keep only the surface where the plane meets the mesh'))
        self._typeGroup = QButtonGroup(self)
        self._typeGroup.addButton(self._clipRadio, CutType.CLIP.value)
        self._typeGroup.addButton(self._sliceRadio, CutType.SLICE.value)

        self._crinkle = QCheckBox(self.tr('Whole cells'))
        self._crinkle.setToolTip(self.tr(
            'Keep every cell the plane passes through intact instead of '
            'cutting it. Ragged, but cell shapes stay readable — this is the '
            'view that shows whether prism layers are there.'))

        row.addWidget(self._clipRadio)
        row.addWidget(self._sliceRadio)
        row.addStretch(1)
        row.addWidget(self._crinkle)
        return widget

    def _buildPlaneRows(self):
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)

        self._enableBoxes = []
        self._gizmoButtons = []
        self._selectButtons = []
        self._summaries = []
        self._gizmoGroup = QButtonGroup(self)
        self._gizmoGroup.setExclusive(False)
        self._selectGroup = QButtonGroup(self)

        for index in range(MAX_PLANES):
            enable = QCheckBox()
            enable.setToolTip(self.tr('Use plane {0}').format(index + 1))
            enable.setAccessibleName(self.tr('Enable plane {0}').format(index + 1))

            select = QPushButton(self.tr('Plane {0}').format(index + 1))
            select.setCheckable(True)
            select.setChecked(index == 0)
            select.setToolTip(self.tr('Edit this plane below'))
            self._selectGroup.addButton(select, index)

            summary = QLabel()
            summary.setToolTip(self.tr('Origin and normal of this plane'))

            gizmo = _compactButton(
                self.tr('◈'), self.tr('Show the draggable handle for this plane'))
            gizmo.setCheckable(True)
            self._gizmoGroup.addButton(gizmo, index)

            grid.addWidget(enable, index, 0)
            grid.addWidget(select, index, 1)
            grid.addWidget(summary, index, 2)
            grid.addWidget(gizmo, index, 3)

            self._enableBoxes.append(enable)
            self._selectButtons.append(select)
            self._summaries.append(summary)
            self._gizmoButtons.append(gizmo)

        grid.setColumnStretch(2, 1)
        return widget

    def _buildPlacement(self):
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)

        self._originFields = [_field() for _ in range(3)]
        self._normalFields = [_field() for _ in range(3)]
        for axis, edit in zip('XYZ', self._originFields):
            edit.setAccessibleName(self.tr('Origin {0}').format(axis))
        for axis, edit in zip('XYZ', self._normalFields):
            edit.setAccessibleName(self.tr('Normal {0}').format(axis))

        grid.addWidget(QLabel(self.tr('Origin')), 0, 0)
        originRow = QHBoxLayout()
        originRow.setContentsMargins(0, 0, 0, 0)
        for edit in self._originFields:
            originRow.addWidget(edit)
        originRow.addStretch(1)
        grid.addLayout(originRow, 0, 1)

        grid.addWidget(QLabel(self.tr('Normal')), 1, 0)
        normalRow = QHBoxLayout()
        normalRow.setContentsMargins(0, 0, 0, 0)
        for edit in self._normalFields:
            normalRow.addWidget(edit)
        normalRow.addStretch(1)
        grid.addLayout(normalRow, 1, 1)

        presets = QHBoxLayout()
        presets.setContentsMargins(0, 0, 0, 0)
        self._axisButtons = []
        for axis, label in enumerate(('X', 'Y', 'Z')):
            button = _compactButton(
                label, self.tr('Cut normal to {0}').format(label))
            presets.addWidget(button)
            self._axisButtons.append(button)
        self._viewNormalButton = QPushButton(self.tr('View normal'))
        self._viewNormalButton.setToolTip(
            self.tr('Cut along whatever you are currently looking down'))
        self._flipButton = QPushButton(self.tr('Flip'))
        self._flipButton.setToolTip(
            self.tr('Keep the other side of the plane instead'))
        presets.addWidget(self._viewNormalButton)
        presets.addWidget(self._flipButton)
        presets.addStretch(1)
        grid.addLayout(presets, 2, 1)

        grid.addWidget(QLabel(self.tr('Position')), 3, 0)
        positionRow = QHBoxLayout()
        positionRow.setContentsMargins(0, 0, 0, 0)
        self._positionSlider = QSlider(Qt.Orientation.Horizontal)
        self._positionSlider.setRange(0, SLIDER_STEPS)
        self._positionSlider.setValue(SLIDER_STEPS // 2)
        self._positionSlider.setToolTip(
            self.tr('Sweep the plane across the model along its normal'))
        self._positionSlider.setAccessibleName(self.tr('Plane position'))
        self._offsetLabel = QLabel()
        self._offsetLabel.setToolTip(
            self.tr('Distance from the centre of the model, in model units'))
        positionRow.addWidget(self._positionSlider, 1)
        positionRow.addWidget(self._offsetLabel)
        grid.addLayout(positionRow, 3, 1)

        grid.addWidget(QLabel(self.tr('Drag')), 4, 0)
        dragRow = QHBoxLayout()
        dragRow.setContentsMargins(0, 0, 0, 0)
        self._dragAxis = QComboBox()
        self._dragAxis.addItem(self.tr('Free'), None)
        self._dragAxis.addItem(self.tr('Along X'), 0)
        self._dragAxis.addItem(self.tr('Along Y'), 1)
        self._dragAxis.addItem(self.tr('Along Z'), 2)
        self._dragAxis.addItem(self.tr('Along normal'), AXIS_NORMAL)
        self._dragAxis.setCurrentIndex(4)
        self._dragAxis.setToolTip(
            self.tr('Which direction the handle is allowed to move in'))
        self._snapCentre = QPushButton(self.tr('Centre'))
        self._snapCentre.setToolTip(self.tr('Put the plane through the middle of the model'))
        self._snapOrigin = QPushButton(self.tr('Origin'))
        self._snapOrigin.setToolTip(self.tr('Put the plane through (0, 0, 0)'))
        # DP-695. A placed plane that any handle press can move is a plane
        # that moves while the user only meant to turn the model.
        self._lock = QCheckBox(self.tr('Lock'))
        self._lock.setToolTip(self.tr(
            'Keep the plane where it is: its handles stay visible but every '
            'drag in the viewport turns the camera. Ctrl+drag a handle to '
            'move the plane anyway; untick to drag handles without Ctrl.'))
        self._lock.setAccessibleName(self.tr('Lock section plane'))
        dragRow.addWidget(self._dragAxis)
        dragRow.addWidget(self._lock)
        dragRow.addWidget(self._snapCentre)
        dragRow.addWidget(self._snapOrigin)
        dragRow.addStretch(1)
        grid.addLayout(dragRow, 4, 1)

        grid.setColumnStretch(1, 1)
        return widget

    def _buildFooter(self):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        self._live = QCheckBox(self.tr('Live'))
        self._live.setChecked(True)
        self._live.setToolTip(self.tr(
            'Re-cut while the handle is dragged. Large meshes fall back to '
            'updating when the drag ends, and say so here.'))
        self._liveNote = QLabel()
        self._liveNote.setVisible(False)
        self._clearButton = QPushButton(self.tr('Clear'))
        self._clearButton.setToolTip(self.tr('Turn every plane off'))
        self._applyButton = QPushButton(self.tr('Apply'))
        self._applyButton.setToolTip(
            self.tr('Re-cut now. Only needed when Live is off.'))

        row.addWidget(self._live)
        row.addWidget(self._liveNote, 1)
        row.addStretch(1)
        row.addWidget(self._clearButton)
        row.addWidget(self._applyButton)
        return widget

    # -- public state ------------------------------------------------------ #

    def cutType(self) -> CutType:
        return CutType.CLIP if self._clipRadio.isChecked() else CutType.SLICE

    def isCrinkle(self) -> bool:
        return self._crinkle.isChecked()

    def isLive(self) -> bool:
        return self._live.isChecked()

    def dragAxis(self):
        return self._dragAxis.currentData()

    def isLocked(self) -> bool:
        return self._lock.isChecked()

    def setLocked(self, locked: bool):
        self._lock.setChecked(bool(locked))

    def activeIndex(self) -> int:
        return self._active

    def planes(self) -> list[SectionPlaneState]:
        return self._planes

    def enabledPlanes(self) -> list[SectionPlaneState]:
        return [plane for plane in self._planes if plane.enabled]

    def gizmoIndexes(self) -> list[int]:
        return [index for index, button in enumerate(self._gizmoButtons)
                if button.isChecked()]

    def setBounds(self, bounds):
        """Give the panel a model to place planes against.

        Until this arrives the position slider has no scale to work in, so the
        controls stay disabled with a reason rather than pretending.
        """
        self._bounds = bounds
        if bounds is not None:
            centre = bounds.center()
            for plane in self._planes:
                if plane.origin == [0.0, 0.0, 0.0]:
                    plane.origin = list(centre)
        self.setEnabled(bounds is not None)
        self.setToolTip('' if bounds is not None
                        else self.tr('Nothing is loaded to cut yet.'))
        self._syncFromState()

    def setPlaneGeometry(self, index, origin, normal):
        """Adopt a drag: called when the gizmo moves the plane in the viewport."""
        if not 0 <= index < len(self._planes):
            return
        plane = self._planes[index]
        plane.origin = [float(value) for value in origin]
        plane.normal = [float(value) for value in normal]
        self._syncFromState()

    def setDegradedNote(self, text: str):
        """Say on the control when live update has stepped down to on-release."""
        self._liveNote.setText(text)
        self._liveNote.setVisible(bool(text))

    def degradedNote(self) -> str:
        return self._liveNote.text()

    def clear(self):
        for plane in self._planes:
            plane.enabled = False
        for box in self._enableBoxes:
            box.setChecked(False)
        for button in self._gizmoButtons:
            button.setChecked(False)
        self._syncFromState()

    def adoptFrom(self, other: 'SectionPanel'):
        """Take another panel's state without echoing a change back at it.

        The dock panel and the viewport overlay are two views of one section,
        not two sections. Mirroring silently is what keeps them from fighting.
        """
        if other is None or other is self:
            return
        self._updating = True
        try:
            for mine, theirs in zip(self._planes, other.planes()):
                mine.enabled = theirs.enabled
                mine.origin = list(theirs.origin)
                mine.normal = list(theirs.normal)
            self._active = other.activeIndex()
            self._selectButtons[self._active].setChecked(True)
            self._clipRadio.setChecked(other.cutType() is CutType.CLIP)
            self._sliceRadio.setChecked(other.cutType() is CutType.SLICE)
            self._crinkle.setChecked(other.isCrinkle())
            self._live.setChecked(other.isLive())
            index = self._dragAxis.findData(other.dragAxis())
            if index >= 0:
                self._dragAxis.setCurrentIndex(index)
            self._lock.setChecked(other.isLocked())
            wanted = set(other.gizmoIndexes())
            for position, button in enumerate(self._gizmoButtons):
                button.setChecked(position in wanted)
        finally:
            self._updating = False
        self._syncFromState()

    # -- wiring ------------------------------------------------------------ #

    def _connectSignalsSlots(self):
        self._clipRadio.toggled.connect(self._emitChanged)
        self._crinkle.toggled.connect(self._emitChanged)
        self._applyButton.clicked.connect(self.sectionChanged)
        self._clearButton.clicked.connect(self._clearClicked)
        self._live.toggled.connect(lambda _checked: self.setDegradedNote(''))
        self._dragAxis.currentIndexChanged.connect(
            lambda _index: self.gizmosChanged.emit())
        self._lock.toggled.connect(self._lockToggled)
        self._viewNormalButton.clicked.connect(self.viewNormalRequested)
        self._flipButton.clicked.connect(self._flip)
        self._snapCentre.clicked.connect(self._snapToCentre)
        self._snapOrigin.clicked.connect(self._snapToOrigin)
        self._positionSlider.valueChanged.connect(self._positionChanged)
        self._selectGroup.idClicked.connect(self._selectPlane)
        self._gizmoGroup.idToggled.connect(
            lambda _index, _checked: self.gizmosChanged.emit())

        for index, box in enumerate(self._enableBoxes):
            box.toggled.connect(
                lambda checked, i=index: self._planeToggled(i, checked))
        for axis, button in enumerate(self._axisButtons):
            button.clicked.connect(lambda _c=False, a=axis: self._setAxis(a))
        for axis, edit in enumerate(self._originFields):
            edit.editingFinished.connect(
                lambda a=axis: self._originEdited(a))
        for axis, edit in enumerate(self._normalFields):
            edit.editingFinished.connect(
                lambda a=axis: self._normalEdited(a))

    def _emitChanged(self, *_args):
        if not self._updating:
            self.sectionChanged.emit()

    def _lockToggled(self, _checked):
        if not self._updating:
            self.gizmosChanged.emit()

    def _clearClicked(self):
        self.clear()
        self.sectionChanged.emit()
        self.gizmosChanged.emit()

    def _planeToggled(self, index, checked):
        self._planes[index].enabled = bool(checked)
        if checked:
            self._selectPlane(index)
        self._syncFromState()
        self._emitChanged()

    def _selectPlane(self, index):
        self._active = index
        self._selectButtons[index].setChecked(True)
        self._syncFromState()
        self.activePlaneChanged.emit(index)

    def _activePlane(self) -> SectionPlaneState:
        return self._planes[self._active]

    def _setAxis(self, axis):
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        self._activePlane().normal = normal
        self._syncFromState()
        self._emitChanged()

    def setViewNormal(self, normal):
        self._activePlane().normal = [float(value) for value in normal]
        self._syncFromState()
        self._emitChanged()

    def _flip(self):
        plane = self._activePlane()
        plane.normal = [-value for value in plane.normal]
        self._syncFromState()
        self._emitChanged()

    def _snapToCentre(self):
        if self._bounds is None:
            return
        self._activePlane().origin = list(self._bounds.center())
        self._syncFromState()
        self._emitChanged()

    def _snapToOrigin(self):
        self._activePlane().origin = [0.0, 0.0, 0.0]
        self._syncFromState()
        self._emitChanged()

    def _originEdited(self, axis):
        if self._updating:
            return
        try:
            value = float(self._originFields[axis].text())
        except ValueError:
            self._syncFromState()
            return
        self._activePlane().origin[axis] = value
        self._syncFromState()
        self._emitChanged()

    def _normalEdited(self, axis):
        if self._updating:
            return
        try:
            value = float(self._normalFields[axis].text())
        except ValueError:
            self._syncFromState()
            return
        normal = list(self._activePlane().normal)
        normal[axis] = value
        if not any(normal):
            self._syncFromState()
            return
        self._activePlane().normal = normal
        self._syncFromState()
        self._emitChanged()

    def _positionChanged(self, value):
        if self._updating or self._bounds is None:
            return
        plane = self._activePlane()
        centre = self._bounds.center()
        unit = plane.normalised()
        span = self._span(unit)
        offset = (value / SLIDER_STEPS - 0.5) * span
        plane.origin = [centre[axis] + offset * unit[axis] for axis in range(3)]
        self._syncFromState()
        self._emitChanged()

    # -- presentation ------------------------------------------------------ #

    def _span(self, unit):
        """Extent of the model measured along ``unit``."""
        if self._bounds is None:
            return 0.0
        size = self._bounds.size()
        return sum(abs(unit[axis]) * size[axis] for axis in range(3)) or 1.0

    def _offset(self, plane):
        if self._bounds is None:
            return 0.0
        centre = self._bounds.center()
        unit = plane.normalised()
        return sum((plane.origin[axis] - centre[axis]) * unit[axis]
                   for axis in range(3))

    def _syncFromState(self):
        self._updating = True
        try:
            plane = self._activePlane()
            for axis in range(3):
                self._originFields[axis].setText(
                    '{:.6g}'.format(plane.origin[axis]))
                self._normalFields[axis].setText(
                    '{:.6g}'.format(plane.normal[axis]))
            for index, state in enumerate(self._planes):
                self._enableBoxes[index].setChecked(state.enabled)
                unit = state.normalised()
                self._summaries[index].setText(
                    '{:.4g}, {:.4g}, {:.4g}  ⟂ {:.2g}, {:.2g}, {:.2g}'.format(
                        *state.origin, *unit))
                self._gizmoButtons[index].setEnabled(state.enabled)
                if not state.enabled:
                    self._gizmoButtons[index].setChecked(False)
                self._gizmoButtons[index].setToolTip(
                    self.tr('Show the draggable handle for plane {0}').format(
                        index + 1)
                    if state.enabled
                    else self.tr('Turn plane {0} on first').format(index + 1))

            offset = self._offset(plane)
            span = self._span(plane.normalised())
            self._offsetLabel.setText('{:+.4g}'.format(offset))
            self._offsetLabel.setToolTip(
                self.tr('{0} from the centre of the model, measured along the '
                        'plane normal').format('{:+.4g}'.format(offset)))
            if span:
                self._positionSlider.setValue(
                    int(round((offset / span + 0.5) * SLIDER_STEPS)))

            slicing = self.cutType() is CutType.SLICE
            self._crinkle.setEnabled(not slicing)
            self._crinkle.setToolTip(
                self.tr('A slice is already a surface; whole cells only apply '
                        'to a clip.') if slicing else self._crinkle.toolTip())
        finally:
            self._updating = False
