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

Plan 37 UF7 (DP-1045). Each plane is now the canonical state of
`foammesh.core.section.plane_state` -- unit normal, persisted reference
point, signed offset in metres, keep side -- and every control writes that
one state: the Offset field (typed, in the model's unit), the −/+ step
buttons and PgUp/PgDn, the slider, the gizmo. *Flip* keeps the plane where it
is and keeps the other half. One selector holds the four section modes
(DP-1046), and a plane's handle is shown as soon as the plane is raised.
"""
from __future__ import annotations

import math
from enum import Enum, auto

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSizePolicy, QSlider, QVBoxLayout,
    QWidget)

from foammesh.core.section.modes import MODE_LABELS, MODE_TOOLTIPS, SectionMode
from foammesh.core.section import section_colour
from foammesh.core.section.plane_state import (
    FINE_FRACTION, PlaneState, display_unit, domain_step, from_display,
    rotated_normal, to_display, unit)
from foammesh.rendering.plane_widget import AXIS_NORMAL
from foammesh.core.quantities import aligned
from foammesh.view.theming.metrics import GAP, GAP_TIGHT, UnitLabel


#: How many planes a section starts with. Three is the number that makes a
#: corner cut possible, which is what people actually build when they are
#: chasing a boundary layer into a fillet.
DEFAULT_PLANES = 3

#: Plan 37 UF11. The most planes one section holds (added, named, deleted on
#: the panel); a seventh is refused with the reason.
MAX_PLANES = 6

#: The position slider is integer-valued; this is its resolution across the
#: model's extent along the plane normal.
SLIDER_STEPS = 1000

#: Where the −/+ step comes from.
STEP_CELL = 'cell'
STEP_TYPED = 'typed'


class CutType(Enum):
    CLIP  = auto()  # noqa: E221
    SLICE = auto()


def _modeFor(cutType, crinkle) -> SectionMode:
    if cutType is CutType.SLICE:
        return SectionMode.SLICE
    return SectionMode.CLIP_WHOLE_CELLS if crinkle else SectionMode.CLIP


class SectionPlaneState:
    """One section plane: the canonical `PlaneState` plus a pivot on it.

    The pivot is the point the handle sits on and a turn of the normal holds
    still; it moves with the offset and stays through a flip. ``origin`` (the
    pivot) and ``normal`` (the normal VTK clips with, into the kept half) are
    the reading the viewport and the capture use; writing either keeps the
    reference and recomputes the offset.
    """

    def __init__(self, enabled=False, origin=None, normal=None,
                 reference=None, keep=1, name=''):
        self.enabled = bool(enabled)
        #: UF11. The user's name for the plane ('' = "Plane N").
        self.name = str(name or '')
        self._placed = origin is not None
        pivot = tuple(float(value) for value in (origin or (0.0, 0.0, 0.0)))
        try:
            vtkNormal = unit(normal if normal is not None else (1.0, 0.0, 0.0))
        except ValueError:
            vtkNormal = (1.0, 0.0, 0.0)
        n = tuple(keep * value for value in vtkNormal)
        self._state = PlaneState.through(pivot, n, reference, keep)
        self._pivot = pivot

    # -- the canonical state ----------------------------------------------- #

    @property
    def state(self) -> PlaneState:
        return self._state

    @property
    def reference(self):
        return list(self._state.reference)

    @property
    def distance(self) -> float:
        return self._state.distance

    @property
    def keep(self) -> int:
        return self._state.keep

    def isPlaced(self) -> bool:
        return self._placed

    def setState(self, state: PlaneState, pivot=None):
        """Take *state*; the pivot is *pivot* (or the old one) put on it."""
        self._state = state
        self._pivot = state.project(self._pivot if pivot is None else pivot)
        self._placed = True

    def setDistance(self, metres):
        """Move to offset *metres* from the reference, along n."""
        delta = float(metres) - self._state.distance
        state = self._state.with_distance(metres)
        self._pivot = tuple(p + delta * n
                            for p, n in zip(self._pivot, state.normal))
        self._state = state

    def move(self, delta):
        self.setDistance(self._state.distance + float(delta))

    def flip(self):
        """The other half kept; the plane and its pivot stay."""
        self._state = self._state.flipped()

    def setUnitNormal(self, n):
        """Turn the canonical n (not the VTK normal) about the pivot."""
        self._state = self._state.with_normal(n, self._pivot)

    def placeAt(self, centre):
        """A fresh plane through *centre*, measured from it (offset 0)."""
        centre = tuple(float(value) for value in centre)
        self._state = PlaneState(self._state.normal, centre, 0.0,
                                 self._state.keep)
        self._pivot = centre
        self._placed = True

    def assign(self, other: 'SectionPlaneState'):
        self.enabled = other.enabled
        self.name = getattr(other, 'name', '')
        self._state = other._state
        self._pivot = tuple(other._pivot)
        self._placed = other._placed

    def copy(self) -> 'SectionPlaneState':
        clone = SectionPlaneState()
        clone.assign(self)
        return clone

    def to_dict(self):
        """What a saved view would persist (the reference included)."""
        result = {'enabled': self.enabled, 'pivot': list(self._pivot),
                  **self._state.to_dict()}
        if self.name:
            result['name'] = self.name
        return result

    @classmethod
    def from_dict(cls, data):
        plane = cls()
        plane.enabled = bool(data.get('enabled', False))
        plane.name = str(data.get('name') or '')
        plane.setState(PlaneState.from_dict(data),
                       data.get('pivot') or None)
        return plane

    # -- the viewport's reading ------------------------------------------- #

    @property
    def origin(self):
        return list(self._pivot)

    @origin.setter
    def origin(self, point):
        pivot = tuple(float(value) for value in point)
        self._state = PlaneState.through(pivot, self._state.normal,
                                         self._state.reference,
                                         self._state.keep)
        self._pivot = pivot
        self._placed = True

    @property
    def normal(self):
        return list(self._state.vtk_normal())

    @normal.setter
    def normal(self, values):
        n = unit(values)
        self.setUnitNormal(tuple(self._state.keep * value for value in n))

    def normalised(self):
        return list(self._state.vtk_normal())

    def __repr__(self):
        return ('SectionPlaneState(enabled={0}, origin={1}, normal={2}, '
                'reference={3}, distance={4:.9g}, keep={5})').format(
                    self.enabled, self.origin, self.normal, self.reference,
                    self.distance, self.keep)


#: What a tilt or turn box starts at: no turn. A value, not a label -- the
#: box's name ('Tilt angle', 'Turn angle') says what the number is.
_NO_TURN = format(0, 'd')


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
    #: Plan 37 UF6. "Load full volume" was pressed on a boundary-only section.
    loadVolumeRequested = Signal()
    #: Plan 37 UF10. "Load cells for the cut" was pressed: compute the exact
    #: section of the cells the planes meet in the mesh worker.
    loadCellsRequested = Signal()
    #: Plan 37 UF7. *Look along plane* was turned on (True) or off.
    lookAlongRequested = Signal(bool)
    #: Plan 37 UF9. *Colour cut by* changed; the argument is the choice's key
    #: (`core.section.section_colour`).
    colourChanged = Signal(str)
    #: Plan 37 UF11. A plane was added, renamed or deleted.
    planesChanged = Signal()
    #: Plan 37 UF11. Save the section on screen under this name.
    saveSectionRequested = Signal(str)
    #: Plan 37 UF11. Put the saved section with this ID back on screen.
    loadSectionRequested = Signal(str)
    #: Plan 37 UF11. Delete the saved section with this ID.
    deleteSectionRequested = Signal(str)
    #: Plan 37 UF11. Cut the kept snapshots of these stages with the same
    #: planes and show them side by side.
    compareRequested = Signal(list)
    #: Plan 37 UF11. Take the stage comparison down.
    compareCleared = Signal()
    #: Plan 37 UF11. Freeze the cells the worker's section shows.
    freezeRequested = Signal()
    #: Plan 37 UF11. Let the frozen cells go.
    unfreezeRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._planes = [SectionPlaneState() for _ in range(DEFAULT_PLANES)]
        self._active = 0
        self._bounds = None
        self._updating = False
        #: UF7. Where the cell-scale step comes from (the section tool), the
        #: step a run of nudges holds fixed, and the last one that was found.
        self._stepProvider = None
        self._gestureStep = None
        self._lastStep = None
        #: While a handle is dragged the step note holds still: finding the
        #: cell-scale step reads every cell on screen (MEASURED 2026-10-01,
        #: 4.9 M hexes: 545 ms a move, 98 % of the drag frame).
        self._stepHeld = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP)

        layout.addWidget(self._buildModeRow())
        layout.addWidget(self._buildSaved())
        layout.addWidget(self._buildPlaneRows())
        layout.addWidget(self._separator())
        layout.addWidget(self._buildPlacement())
        layout.addWidget(self._buildAdvanced())
        layout.addWidget(self._buildStatus())
        layout.addWidget(self._buildColour())
        layout.addWidget(self._buildCompare())
        layout.addWidget(self._buildFreeze())
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

        self._modeBox = QComboBox()
        self._modeBox.setObjectName('sectionMode')
        self._modeBox.setAccessibleName(self.tr('Section mode'))
        for mode, label in MODE_LABELS.items():
            self._modeBox.addItem(self.tr(label), mode)
            self._modeBox.setItemData(
                self._modeBox.count() - 1, self.tr(MODE_TOOLTIPS[mode]),
                Qt.ItemDataRole.ToolTipRole)
        self._modeBox.setCurrentIndex(self._modeBox.findData(SectionMode.CLIP))

        row.addWidget(QLabel(self.tr('Show')))
        row.addWidget(self._modeBox, 1)
        return widget

    def _buildSaved(self):
        """Plan 37 UF11. Sections saved with the case, by name."""
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(GAP_TIGHT)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(self.tr('Saved'))
        self._savedBox = QComboBox()
        self._savedBox.setObjectName('sectionSaved')
        self._savedBox.setAccessibleName(self.tr('Saved sections'))
        self._savedBox.setToolTip(self.tr(
            'Sections saved with this case. Choosing one puts its planes, '
            'mode and colour back.'))
        label.setBuddy(self._savedBox)
        self._savedName = QLineEdit()
        self._savedName.setObjectName('sectionSavedName')
        self._savedName.setAccessibleName(self.tr('Section name'))
        self._savedName.setPlaceholderText(self.tr('Section name'))
        self._savedName.setMaxLength(60)
        self._saveButton = QPushButton(self.tr('Save'))
        self._saveButton.setObjectName('sectionSave')
        self._saveButton.setToolTip(self.tr(
            'Save these planes, the mode and the colour with the case under '
            'this name (a section of the same name is replaced)'))
        self._deleteSavedButton = QPushButton(self.tr('Delete'))
        self._deleteSavedButton.setObjectName('sectionDeleteSaved')
        self._deleteSavedButton.setToolTip(
            self.tr('Delete the chosen saved section'))
        row.addWidget(label)
        row.addWidget(self._savedBox, 1)
        row.addWidget(self._savedName, 1)
        row.addWidget(self._saveButton)
        row.addWidget(self._deleteSavedButton)
        self._savedNote = QLabel()
        self._savedNote.setObjectName('sectionSavedNote')
        self._savedNote.setWordWrap(True)
        self._savedNote.setVisible(False)
        column.addLayout(row)
        column.addWidget(self._savedNote)
        self._savedReadOnly = ''
        self.setSavedSections([])
        return widget

    def setSavedSections(self, sections, readOnly='', note='',
                         current=None):
        """*sections*: ``[(id, name)]``. *readOnly*: why saving is refused
        (``''``: it is not). *current*: the ID to show as chosen."""
        self._savedReadOnly = readOnly
        self._savedBox.blockSignals(True)
        try:
            self._savedBox.clear()
            self._savedBox.addItem(self.tr('Unsaved section'), None)
            for sectionId, name in sections:
                self._savedBox.addItem(name, sectionId)
            index = self._savedBox.findData(current) if current else 0
            self._savedBox.setCurrentIndex(max(index, 0))
        finally:
            self._savedBox.blockSignals(False)
        self._savedBox.setEnabled(bool(sections))
        text = readOnly or note
        self._savedNote.setText(text)
        self._savedNote.setVisible(bool(text))
        self._saveButton.setEnabled(not readOnly)
        self._saveButton.setToolTip(readOnly or self.tr(
            'Save these planes, the mode and the colour with the case under '
            'this name (a section of the same name is replaced)'))
        self._syncSavedActions()

    def savedSections(self):
        return [(self._savedBox.itemData(i), self._savedBox.itemText(i))
                for i in range(1, self._savedBox.count())]

    def savedNote(self) -> str:
        return self._savedNote.text()

    def currentSavedId(self):
        return self._savedBox.currentData()

    def _syncSavedActions(self):
        chosen = self._savedBox.currentData() is not None
        self._deleteSavedButton.setEnabled(chosen and not self._savedReadOnly)
        if chosen:
            self._savedName.setText(self._savedBox.currentText())

    def _savedPicked(self, _index):
        self._syncSavedActions()
        sectionId = self._savedBox.currentData()
        if sectionId is not None:
            self.loadSectionRequested.emit(str(sectionId))

    def _saveClicked(self):
        name = self._savedName.text().strip()
        if not name:
            self._savedNote.setText(self.tr('A saved section needs a name.'))
            self._savedNote.setVisible(True)
            return
        self.saveSectionRequested.emit(name)

    def _deleteSavedClicked(self):
        sectionId = self._savedBox.currentData()
        if sectionId is not None:
            self.deleteSectionRequested.emit(str(sectionId))

    def _buildPlaneRows(self):
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)

        self._enableBoxes = []
        self._gizmoButtons = []
        self._selectButtons = []
        self._summaries = []
        self._rowWidgets = []
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
            self._rowWidgets.append((enable, select, summary, gizmo))

        grid.addWidget(self._buildPlaneActions(), MAX_PLANES, 0, 1, 4)
        grid.setColumnStretch(2, 1)
        return widget

    def _buildPlaneActions(self):
        """Plan 37 UF11. Name the plane being edited; add or delete one."""
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(self.tr('Name'))
        self._nameEdit = QLineEdit()
        self._nameEdit.setObjectName('sectionPlaneName')
        self._nameEdit.setAccessibleName(self.tr('Plane name'))
        self._nameEdit.setToolTip(self.tr(
            'The name of the plane being edited. Press Enter to rename it.'))
        self._nameEdit.setMaxLength(40)
        label.setBuddy(self._nameEdit)
        self._addPlaneButton = QPushButton(self.tr('Add plane'))
        self._addPlaneButton.setObjectName('sectionAddPlane')
        self._deletePlaneButton = QPushButton(self.tr('Delete plane'))
        self._deletePlaneButton.setObjectName('sectionDeletePlane')
        self._planeNote = QLabel()
        self._planeNote.setObjectName('sectionPlaneNote')
        self._planeNote.setWordWrap(True)
        self._planeNote.setAccessibleName(self.tr('Plane note'))
        self._planeNote.setVisible(False)
        row.addWidget(label)
        row.addWidget(self._nameEdit, 1)
        row.addWidget(self._addPlaneButton)
        row.addWidget(self._deletePlaneButton)
        outer = QWidget()
        column = QVBoxLayout(outer)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(GAP_TIGHT)
        column.addWidget(widget)
        column.addWidget(self._planeNote)
        return outer

    # -- UF11: adding, naming and deleting planes ------------------------- #

    def planeName(self, index) -> str:
        """The plane's own name, or "Plane N" for one never named."""
        plane = self._planes[index]
        return plane.name or self.tr('Plane {0}').format(index + 1)

    def planeNames(self) -> list[str]:
        return [self.planeName(index) for index in range(len(self._planes))]

    def planeNote(self) -> str:
        return self._planeNote.text()

    def _note(self, text):
        self._planeNote.setText(text)
        self._planeNote.setVisible(bool(text))

    def _freeName(self):
        taken = set(self.planeNames())
        number = len(self._planes) + 1
        while self.tr('Plane {0}').format(number) in taken:
            number += 1
        return self.tr('Plane {0}').format(number)

    def addPlane(self, name=None):
        """A new, lowered plane, made the one being edited.

        ``(True, '')``, or ``(False, reason)`` with nothing changed.
        """
        if len(self._planes) >= MAX_PLANES:
            reason = self.tr(
                'A section holds at most {0} planes. Delete one to add '
                'another.').format(MAX_PLANES)
            self._note(reason)
            return False, reason
        text = self._freeName() if name is None else str(name).strip()
        refusal = self._nameRefusal(text, None)
        if refusal:
            self._note(refusal)
            return False, refusal
        plane = SectionPlaneState(name=text)
        if self._bounds is not None:
            plane.placeAt(self._bounds.center())
        self._planes.append(plane)
        self._note('')
        self._selectPlane(len(self._planes) - 1)
        self.planesChanged.emit()
        return True, ''

    def renamePlane(self, index, name):
        if not 0 <= index < len(self._planes):
            return False, self.tr('There is no plane {0}.').format(index + 1)
        text = str(name or '').strip()
        refusal = self._nameRefusal(text, index)
        if refusal:
            self._note(refusal)
            self._syncFromState()
            return False, refusal
        if text == self.planeName(index):
            return True, ''
        self._planes[index].name = text
        self._note('')
        self._syncFromState()
        self.planesChanged.emit()
        return True, ''

    def _nameRefusal(self, text, index):
        if not text:
            return self.tr('A plane needs a name.')
        for other, taken in enumerate(self.planeNames()):
            if other != index and taken == text:
                return self.tr('Another plane is already called '
                               '"{0}".').format(text)
        return ''

    def deletePlane(self, index=None):
        """Delete plane *index* (the one being edited when ``None``)."""
        index = self._active if index is None else int(index)
        if not 0 <= index < len(self._planes):
            return False, self.tr('There is no plane {0}.').format(index + 1)
        if len(self._planes) <= 1:
            reason = self.tr('A section keeps at least one plane.')
            self._note(reason)
            return False, reason
        wasCutting = self._planes[index].enabled
        # A plane keeps the name it was shown with: "Plane 3" does not
        # become "Plane 2" because the plane before it went.
        for position, plane in enumerate(self._planes):
            if not plane.name:
                plane.name = self.planeName(position)
        # The handle ticks belong to planes, not rows: they move up with them.
        wanted = [i - (i > index) for i in self.gizmoIndexes() if i != index]
        del self._planes[index]
        active = self._active
        if active > index or active >= len(self._planes):
            active -= 1
        self._active = max(active, 0)
        wasUpdating = self._updating
        self._updating = True
        try:
            for position, button in enumerate(self._gizmoButtons):
                button.setChecked(position in wanted)
            self._selectButtons[self._active].setChecked(True)
        finally:
            self._updating = wasUpdating
        self._gestureStep = None
        self._note('')
        self._syncFromState()
        self.planesChanged.emit()
        self.activePlaneChanged.emit(self._active)
        if wasCutting:
            self._emitChanged()
        self.gizmosChanged.emit()
        return True, ''

    def _nameEdited(self):
        if self._updating:
            return
        self.renamePlane(self._active, self._nameEdit.text())

    def _buildPlacement(self):
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)

        grid.addWidget(QLabel(self.tr('Normal')), 0, 0)
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
        self._flipButton.setToolTip(self.tr(
            'Keep the other side of the plane instead. The plane stays '
            'where it is.'))
        self._lookButton = QPushButton(self.tr('Look along plane'))
        self._lookButton.setObjectName('sectionLookAlong')
        self._lookButton.setCheckable(True)
        self._lookButton.setToolTip(self.tr(
            'Look straight at the cut, square on, without perspective. Press '
            'again to put the camera back.'))
        presets.addWidget(self._viewNormalButton)
        presets.addWidget(self._flipButton)
        presets.addWidget(self._lookButton)
        presets.addStretch(1)
        grid.addLayout(presets, 0, 1)

        grid.addWidget(QLabel(self.tr('Offset')), 1, 0)
        offsetRow = QHBoxLayout()
        offsetRow.setContentsMargins(0, 0, 0, 0)
        self._offsetEdit = _field(96)
        self._offsetEdit.setObjectName('sectionOffset')
        self._offsetEdit.setAccessibleName(self.tr('Plane offset'))
        self._offsetUnit = QLabel()
        self._offsetUnit.setObjectName('sectionOffsetUnit')
        self._stepDown = _compactButton(
            '−', self.tr('Move the plane back one step. Keyboard: PgDn, '
                         'or Shift+PgDn for a fine step.'))
        self._stepDown.setAccessibleName(self.tr('Step plane back'))
        self._stepUp = _compactButton(
            '+', self.tr('Move the plane on one step. Keyboard: PgUp, '
                         'or Shift+PgUp for a fine step.'))
        self._stepUp.setAccessibleName(self.tr('Step plane forward'))
        self._stepChoice = QComboBox()
        self._stepChoice.setObjectName('sectionStepChoice')
        self._stepChoice.setAccessibleName(self.tr('Step size'))
        self._stepChoice.addItem(self.tr('Cell-scale step'), STEP_CELL)
        self._stepChoice.addItem(self.tr('Typed step'), STEP_TYPED)
        self._stepChoice.setToolTip(self.tr(
            'Cell-scale: about one cell across the plane (the median '
            'thickness of the cells it cuts) — approximate, it can skip a '
            'cell where the mesh is graded. Typed: the distance you enter.'))
        self._stepEdit = _field(72)
        self._stepEdit.setObjectName('sectionStep')
        self._stepEdit.setAccessibleName(self.tr('Typed step'))
        self._stepEdit.setPlaceholderText(self.tr('step'))
        self._stepNote = QLabel()
        self._stepNote.setObjectName('sectionStepNote')
        offsetRow.addWidget(self._offsetEdit)
        offsetRow.addWidget(self._offsetUnit)
        offsetRow.addWidget(self._stepDown)
        offsetRow.addWidget(self._stepUp)
        offsetRow.addWidget(self._stepChoice)
        offsetRow.addWidget(self._stepEdit)
        offsetRow.addWidget(self._stepNote, 1)
        grid.addLayout(offsetRow, 1, 1)

        grid.addWidget(QLabel(self.tr('Position')), 2, 0)
        self._positionSlider = QSlider(Qt.Orientation.Horizontal)
        self._positionSlider.setRange(0, SLIDER_STEPS)
        self._positionSlider.setValue(SLIDER_STEPS // 2)
        self._positionSlider.setToolTip(
            self.tr('Sweep the plane across the model along its normal'))
        self._positionSlider.setAccessibleName(self.tr('Plane position'))
        grid.addWidget(self._positionSlider, 2, 1)

        grid.addWidget(QLabel(self.tr('Drag')), 3, 0)
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
        self._advancedButton = QPushButton(self.tr('Advanced'))
        self._advancedButton.setObjectName('sectionAdvanced')
        self._advancedButton.setCheckable(True)
        self._advancedButton.setToolTip(self.tr(
            'Type the normal and a point on the plane, or turn the normal by '
            'angles'))
        dragRow.addWidget(self._dragAxis)
        dragRow.addWidget(self._lock)
        dragRow.addWidget(self._snapCentre)
        dragRow.addWidget(self._snapOrigin)
        dragRow.addStretch(1)
        dragRow.addWidget(self._advancedButton)
        grid.addLayout(dragRow, 3, 1)

        grid.setColumnStretch(1, 1)
        return widget

    def _buildAdvanced(self):
        """Absolute entry: a point on the plane, the normal, relative turns."""
        widget = QWidget()
        widget.setObjectName('sectionAdvancedPane')
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(3)

        self._originFields = [_field() for _ in range(3)]
        self._normalFields = [_field() for _ in range(3)]
        for axis, edit in zip('XYZ', self._originFields):
            edit.setAccessibleName(self.tr('Point on plane {0}').format(axis))
        for axis, edit in zip('XYZ', self._normalFields):
            edit.setAccessibleName(self.tr('Normal {0}').format(axis))

        grid.addWidget(QLabel(self.tr('Point')), 0, 0)
        originRow = QHBoxLayout()
        originRow.setContentsMargins(0, 0, 0, 0)
        for edit in self._originFields:
            originRow.addWidget(edit)
        self._pointUnit = QLabel()
        originRow.addWidget(self._pointUnit)
        originRow.addStretch(1)
        grid.addLayout(originRow, 0, 1)

        grid.addWidget(QLabel(self.tr('Normal')), 1, 0)
        normalRow = QHBoxLayout()
        normalRow.setContentsMargins(0, 0, 0, 0)
        for edit in self._normalFields:
            normalRow.addWidget(edit)
        normalRow.addStretch(1)
        grid.addLayout(normalRow, 1, 1)

        grid.addWidget(QLabel(self.tr('Turn')), 2, 0)
        turnRow = QHBoxLayout()
        turnRow.setContentsMargins(0, 0, 0, 0)
        self._tiltField = _field(56)
        self._tiltField.setText(_NO_TURN)
        self._tiltField.setAccessibleName(self.tr('Tilt angle'))
        self._tiltField.setToolTip(self.tr(
            'Tilt: degrees about the plane\'s first in-plane axis '
            '(right-handed), applied first'))
        self._turnField = _field(56)
        self._turnField.setText(_NO_TURN)
        self._turnField.setAccessibleName(self.tr('Turn angle'))
        self._turnField.setToolTip(self.tr(
            'Degrees about the plane\'s second in-plane axis (right-handed), '
            'applied second'))
        self._rotateButton = QPushButton(self.tr('Turn normal'))
        self._rotateButton.setToolTip(self.tr(
            'Turn the normal by these angles about the point on the plane'))
        # DP-1103: the unit follows its box as everywhere else (DP-164).
        turnRow.addWidget(QLabel(self.tr('tilt')))
        turnRow.addWidget(self._tiltField)
        turnRow.addWidget(UnitLabel('deg'))
        turnRow.addWidget(QLabel(self.tr('then')))
        turnRow.addWidget(self._turnField)
        turnRow.addWidget(UnitLabel('deg'))
        turnRow.addWidget(self._rotateButton)
        turnRow.addStretch(1)
        grid.addLayout(turnRow, 2, 1)

        self._referenceLabel = QLabel()
        self._referenceLabel.setObjectName('sectionReference')
        self._referenceLabel.setToolTip(self.tr(
            'The offset is measured from this point, fixed when the section '
            'was made'))
        grid.addWidget(self._referenceLabel, 3, 0, 1, 2)
        grid.setColumnStretch(1, 1)

        widget.setVisible(False)
        self._advanced = widget
        return widget

    def _buildStatus(self):
        """Plan 37 UF6. What the section on screen is, and the one way on.

        A boundary-only preview cut by a plane looked like a section through
        cells. The line here says which picture it is (the words are
        `core.section.notices`), and on a boundary-only preview offers the
        full volume -- never loaded by raising a plane, only from here.
        UF10 adds "Load cells for the cut": only the cells the planes meet,
        cut exactly in the mesh worker, without loading the whole volume.
        """
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        self._statusLabel = QLabel()
        self._statusLabel.setObjectName('sectionStatus')
        self._statusLabel.setWordWrap(True)
        self._statusLabel.setAccessibleName(self.tr('Section status'))
        self._loadVolumeButton = QPushButton(self.tr('Load full volume'))
        self._loadVolumeButton.setObjectName('sectionLoadVolume')
        self._loadVolumeButton.setVisible(False)
        self._loadVolumeButton.clicked.connect(self._loadVolumeClicked)
        self._loadCellsButton = QPushButton(self.tr('Load cells for the cut'))
        self._loadCellsButton.setObjectName('sectionLoadCells')
        self._loadCellsButton.setToolTip(self.tr(
            'Cut only the cells the planes meet, exactly, without loading '
            'the whole volume'))
        self._loadCellsButton.setVisible(False)
        self._loadCellsButton.clicked.connect(self._loadCellsClicked)
        row.addWidget(self._statusLabel, 1)
        row.addWidget(self._loadCellsButton)
        row.addWidget(self._loadVolumeButton)
        return widget

    def _buildColour(self):
        """Plan 37 UF9. *Colour cut by*: quality, cell type, refinement
        level or region, read per cell -- a choice with no values here is
        disabled and says why, never drawn as zeros."""
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(self.tr('Colour cut by'))
        self._colourBox = QComboBox()
        self._colourBox.setObjectName('sectionColourBy')
        self._colourBox.setAccessibleName(self.tr('Colour cut by'))
        for item in section_colour.CHOICES:
            self._colourBox.addItem(self.tr(item.label), item.key)
        label.setBuddy(self._colourBox)
        self._colourBox.currentIndexChanged.connect(self._colourPicked)
        self._colourLegend = QLabel()
        self._colourLegend.setObjectName('sectionColourLegend')
        self._colourLegend.setWordWrap(True)
        self._colourLegend.setAccessibleName(self.tr('Colour legend'))
        row.addWidget(label)
        row.addWidget(self._colourBox)
        row.addWidget(self._colourLegend, 1)
        return widget

    def _colourPicked(self, index):
        key = self._colourBox.itemData(index)
        if key is not None:
            self.colourChanged.emit(str(key))

    def colourKey(self) -> str:
        return str(self._colourBox.currentData() or section_colour.NONE)

    def setColourKey(self, key: str):
        index = self._colourBox.findData(key)
        if index >= 0 and index != self._colourBox.currentIndex():
            self._colourBox.blockSignals(True)
            self._colourBox.setCurrentIndex(index)
            self._colourBox.blockSignals(False)

    def setColourChoices(self, states: dict):
        """*states*: {key: (allowed, reason)}; a refused choice is disabled
        with its reason as the tooltip."""
        model = self._colourBox.model()
        for index in range(self._colourBox.count()):
            key = self._colourBox.itemData(index)
            allowed, reason = states.get(key, (True, ''))
            item = model.item(index)
            if item is not None:
                item.setEnabled(bool(allowed))
            self._colourBox.setItemData(index, reason or None,
                                        Qt.ItemDataRole.ToolTipRole)

    def colourChoiceState(self, key: str):
        """``(enabled, tooltip)`` of the choice *key*."""
        index = self._colourBox.findData(key)
        item = self._colourBox.model().item(index)
        tip = self._colourBox.itemData(index, Qt.ItemDataRole.ToolTipRole)
        return bool(item.isEnabled()), tip or ''

    def setColourLegend(self, text: str, tooltip: str = ''):
        self._colourLegend.setText(text)
        self._colourLegend.setToolTip(tooltip)

    def colourLegend(self) -> str:
        return self._colourLegend.text()

    # -- UF11: compare the kept stages ------------------------------------ #

    #: The stages a comparison can cut, with their labels.
    COMPARE_STAGES = (('blockMesh', 'Base grid'),
                      ('castellation', 'Castellation'),
                      ('snap', 'Snap'), ('layers', 'Layers'))

    def _buildCompare(self):
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(GAP_TIGHT)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(self.tr('Compare')))
        self._compareBoxes = {}
        for stage, label in self.COMPARE_STAGES:
            box = QCheckBox(self.tr(label))
            box.setObjectName('sectionCompare' + stage[0].upper() + stage[1:])
            box.setChecked(stage != 'layers')
            box.setToolTip(self.tr(
                'Cut the kept {0} snapshot with the same planes').format(
                    self.tr(label)))
            self._compareBoxes[stage] = box
            row.addWidget(box)
        row.addStretch(1)
        self._compareButton = QPushButton(self.tr('Compare'))
        self._compareButton.setObjectName('sectionCompare')
        self._compareButton.setToolTip(self.tr(
            'Cut the kept snapshot of each ticked stage with these planes and '
            'show them side by side with one legend. A stage with no kept '
            'snapshot is listed as unavailable — never the current mesh.'))
        self._compareClearButton = QPushButton(self.tr('Clear'))
        self._compareClearButton.setObjectName('sectionCompareClear')
        self._compareClearButton.setToolTip(
            self.tr('Take the stage comparison down'))
        self._compareClearButton.setEnabled(False)
        row.addWidget(self._compareButton)
        row.addWidget(self._compareClearButton)
        self._compareResults = QLabel()
        self._compareResults.setObjectName('sectionCompareResults')
        self._compareResults.setWordWrap(True)
        self._compareResults.setAccessibleName(
            self.tr('Stage comparison results'))
        self._compareResults.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._compareResults.setVisible(False)
        self._compareLegend = QLabel()
        self._compareLegend.setObjectName('sectionCompareLegend')
        self._compareLegend.setWordWrap(True)
        self._compareLegend.setAccessibleName(
            self.tr('Stage comparison legend'))
        self._compareLegend.setVisible(False)
        #: Plan 37 UF11: a comparison cut at planes since moved says so.
        self._compareStale = QLabel(self.tr(
            'Plane moved — compare again to update.'))
        self._compareStale.setObjectName('sectionCompareStale')
        self._compareStale.setWordWrap(True)
        self._compareStale.setAccessibleName(
            self.tr('Stage comparison is out of date'))
        self._compareStale.setVisible(False)
        self._compareStaleState = False
        column.addLayout(row)
        column.addWidget(self._compareStale)
        column.addWidget(self._compareResults)
        column.addWidget(self._compareLegend)
        self._compareButton.clicked.connect(self._compareClicked)
        self._compareClearButton.clicked.connect(self.compareCleared.emit)
        return widget

    def compareStages(self) -> list[str]:
        return [stage for stage, box in self._compareBoxes.items()
                if box.isChecked()]

    def setCompareStages(self, stages):
        for stage, box in self._compareBoxes.items():
            box.setChecked(stage in stages)

    def _compareClicked(self):
        stages = self.compareStages()
        if not stages:
            self.setCompareResults([self.tr('Tick a stage to compare.')])
            return
        self.compareRequested.emit(stages)

    def setCompareResults(self, lines, legend='', busy=False, shown=False):
        """*lines*: one per stage (its label, or why it is unavailable)."""
        text = '\n'.join(lines)
        self._compareResults.setText(text)
        self._compareResults.setVisible(bool(text))
        self._compareLegend.setText(legend)
        self._compareLegend.setVisible(bool(legend))
        self._compareButton.setEnabled(not busy)
        self._compareClearButton.setEnabled(bool(shown or text) and not busy)

    def setCompareStale(self, stale: bool):
        """Say the comparison on screen was cut at planes that have moved
        since (or take that back), and offer *Compare again*."""
        stale = bool(stale)
        self._compareStaleState = stale
        self._compareStale.setVisible(stale)
        self._compareButton.setText(
            self.tr('Compare again') if stale else self.tr('Compare'))
        self._compareButton.setToolTip(
            self.tr('The planes moved after this comparison was cut: cut the '
                    'kept snapshots again at the planes as they are now.')
            if stale else self.tr(
                'Cut the kept snapshot of each ticked stage with these planes '
                'and show them side by side with one legend. A stage with no '
                'kept snapshot is listed as unavailable — never the current '
                'mesh.'))

    def isCompareStale(self) -> bool:
        return self._compareStaleState

    def compareResults(self) -> list[str]:
        text = self._compareResults.text()
        return text.split('\n') if text else []

    def compareLegend(self) -> str:
        return self._compareLegend.text()

    # -- UF11: a frozen cell layer ---------------------------------------- #

    def _buildFreeze(self):
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(GAP_TIGHT)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self._freezeButton = QPushButton(self.tr('Freeze cells'))
        self._freezeButton.setObjectName('sectionFreeze')
        self._freezeButton.setToolTip(self.tr(
            'Keep the cells the loaded cut shows on screen while the planes '
            'move. They belong to this mesh: after a re-mesh they are shown '
            'from the kept snapshot of this mesh, or not at all.'))
        self._unfreezeButton = QPushButton(self.tr('Unfreeze'))
        self._unfreezeButton.setObjectName('sectionUnfreeze')
        self._unfreezeButton.setToolTip(self.tr('Let the frozen cells go'))
        self._unfreezeButton.setEnabled(False)
        row.addWidget(self._freezeButton)
        row.addWidget(self._unfreezeButton)
        row.addStretch(1)
        self._frozenNote = QLabel()
        self._frozenNote.setObjectName('sectionFrozenNote')
        self._frozenNote.setWordWrap(True)
        self._frozenNote.setAccessibleName(self.tr('Frozen cells'))
        self._frozenNote.setVisible(False)
        column.addLayout(row)
        column.addWidget(self._frozenNote)
        self._freezeButton.clicked.connect(self.freezeRequested.emit)
        self._unfreezeButton.clicked.connect(self.unfreezeRequested.emit)
        return widget

    def setFrozenState(self, note='', frozen=False):
        """*note*: what is frozen and where it is drawn from, or why the
        freeze was refused or let go."""
        self._frozenNote.setText(note)
        self._frozenNote.setVisible(bool(note))
        self._unfreezeButton.setEnabled(bool(frozen))

    def frozenNote(self) -> str:
        return self._frozenNote.text()

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

    def mode(self) -> SectionMode:
        return self._modeBox.currentData()

    def setMode(self, mode: SectionMode):
        index = self._modeBox.findData(mode)
        if index >= 0:
            self._modeBox.setCurrentIndex(index)

    def cutType(self) -> CutType:
        return CutType.SLICE if self.mode() is SectionMode.SLICE \
            else CutType.CLIP

    def isCrinkle(self) -> bool:
        return self.mode() is SectionMode.CLIP_WHOLE_CELLS

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
                if button.isChecked() and index < len(self._planes)]

    def isLookingAlong(self) -> bool:
        return self._lookButton.isChecked()

    def setLookingAlong(self, looking: bool):
        """Show *looking* on the toggle without asking for it again."""
        self._lookButton.blockSignals(True)
        try:
            self._lookButton.setChecked(bool(looking))
        finally:
            self._lookButton.blockSignals(False)

    def stepChoice(self) -> str:
        return self._stepChoice.currentData()

    def typedStep(self) -> str:
        return self._stepEdit.text()

    def setStepProvider(self, provider):
        """*provider(plane)* gives the cell-scale step in metres, or None."""
        self._stepProvider = provider
        self._gestureStep = None

    def setBounds(self, bounds):
        """Give the panel a model to place planes against.

        Until this arrives the position slider has no scale to work in, so the
        controls stay disabled with a reason rather than pretending.
        """
        self._bounds = bounds
        self._gestureStep = None
        if bounds is not None:
            centre = bounds.center()
            for plane in self._planes:
                if not plane.isPlaced():
                    plane.placeAt(centre)
        self.setEnabled(bounds is not None)
        self.setToolTip('' if bounds is not None
                        else self.tr('Nothing is loaded to cut yet.'))
        self._syncFromState()

    def setPlaneGeometry(self, index, origin, normal):
        """Adopt a drag: called when the gizmo moves the plane in the viewport."""
        if not 0 <= index < len(self._planes):
            return
        plane = self._planes[index]
        try:
            plane.normal = [float(value) for value in normal]
        except ValueError:
            pass
        plane.origin = [float(value) for value in origin]
        self._gestureStep = None
        self._syncFromState()

    def holdStep(self, held: bool):
        """Hold the step note while a handle is dragged; letting go finds
        the step at the plane's new position."""
        held = bool(held)
        if held == self._stepHeld:
            return
        self._stepHeld = held
        if not held:
            self._gestureStep = None
            self._syncFromState()

    def isStepHeld(self) -> bool:
        return self._stepHeld

    def setDegradedNote(self, text: str):
        """Say on the control when live update has stepped down to on-release."""
        self._liveNote.setText(text)
        self._liveNote.setVisible(bool(text))

    def degradedNote(self) -> str:
        return self._liveNote.text()

    def setSectionStatus(self, notices, explanation=''):
        """Plan 37 UF6. Say what the section on screen is (``[]``: nothing)."""
        self._statusLabel.setText(' · '.join(notices))
        self._statusLabel.setToolTip(explanation)

    def sectionStatus(self) -> str:
        return self._statusLabel.text()

    def setLoadVolume(self, offered: bool, allowed: bool, reason: str = ''):
        """Offer "Load full volume" (*offered*), refusable with a *reason*."""
        self._loadVolumeButton.setVisible(bool(offered))
        self._loadVolumeButton.setEnabled(bool(offered and allowed))
        self._loadVolumeButton.setToolTip(reason)

    def setLoadCells(self, offered: bool, allowed: bool, reason: str = ''):
        """Plan 37 UF10. Offer "Load cells for the cut", refusable with a
        *reason* (shown as its tooltip)."""
        self._loadCellsButton.setVisible(bool(offered))
        self._loadCellsButton.setEnabled(bool(offered and allowed))
        self._loadCellsButton.setToolTip(reason or self.tr(
            'Cut only the cells the planes meet, exactly, without loading '
            'the whole volume'))

    def _loadCellsClicked(self):
        if self._loadCellsButton.isEnabled():
            self.loadCellsRequested.emit()

    def _loadVolumeClicked(self):
        if self._loadVolumeButton.isEnabled():
            self.loadVolumeRequested.emit()

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
            theirPlanes = list(other.planes())[:MAX_PLANES]
            del self._planes[len(theirPlanes):]
            while len(self._planes) < len(theirPlanes):
                self._planes.append(SectionPlaneState())
            for mine, theirs in zip(self._planes, theirPlanes):
                if isinstance(theirs, SectionPlaneState):
                    mine.assign(theirs)
                else:
                    mine.enabled = theirs.enabled
                    mine.name = getattr(theirs, 'name', '')
                    mine.origin = list(theirs.origin)
                    mine.normal = list(theirs.normal)
            self._active = min(other.activeIndex(), len(self._planes) - 1)
            self._selectButtons[self._active].setChecked(True)
            mode = getattr(other, 'mode', None)
            self.setMode(mode() if callable(mode)
                         else _modeFor(other.cutType(), other.isCrinkle()))
            self._live.setChecked(other.isLive())
            index = self._dragAxis.findData(other.dragAxis())
            if index >= 0:
                self._dragAxis.setCurrentIndex(index)
            self._lock.setChecked(other.isLocked())
            wanted = set(other.gizmoIndexes())
            for position, button in enumerate(self._gizmoButtons):
                button.setChecked(position in wanted)
            if isinstance(other, SectionPanel):
                self._stepChoice.setCurrentIndex(
                    self._stepChoice.findData(other.stepChoice()))
                self._stepEdit.setText(other.typedStep())
                self.setLookingAlong(other.isLookingAlong())
        finally:
            self._updating = False
        self._syncFromState()

    # -- wiring ------------------------------------------------------------ #

    def _connectSignalsSlots(self):
        self._modeBox.currentIndexChanged.connect(self._emitChanged)
        self._applyButton.clicked.connect(self.sectionChanged)
        self._clearButton.clicked.connect(self._clearClicked)
        self._live.toggled.connect(lambda _checked: self.setDegradedNote(''))
        self._dragAxis.currentIndexChanged.connect(
            lambda _index: self.gizmosChanged.emit())
        self._lock.toggled.connect(self._lockToggled)
        self._viewNormalButton.clicked.connect(self.viewNormalRequested)
        self._flipButton.clicked.connect(self._flip)
        self._lookButton.toggled.connect(self.lookAlongRequested)
        self._advancedButton.toggled.connect(self._advanced.setVisible)
        self._snapCentre.clicked.connect(self._snapToCentre)
        self._snapOrigin.clicked.connect(self._snapToOrigin)
        self._positionSlider.valueChanged.connect(self._positionChanged)
        self._offsetEdit.editingFinished.connect(self._offsetEdited)
        self._stepDown.clicked.connect(lambda: self.nudge(-1))
        self._stepUp.clicked.connect(lambda: self.nudge(+1))
        self._stepChoice.currentIndexChanged.connect(self._stepChoiceChanged)
        self._stepEdit.editingFinished.connect(self._stepEdited)
        self._rotateButton.clicked.connect(self._rotate)
        self._selectGroup.idClicked.connect(self._selectPlane)
        self._nameEdit.editingFinished.connect(self._nameEdited)
        self._savedBox.currentIndexChanged.connect(self._savedPicked)
        self._saveButton.clicked.connect(self._saveClicked)
        self._deleteSavedButton.clicked.connect(self._deleteSavedClicked)
        self._addPlaneButton.clicked.connect(lambda: self.addPlane())
        self._deletePlaneButton.clicked.connect(lambda: self.deletePlane())
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
        if self._updating:
            # The box is being set to what the state already says.
            return
        self._planes[index].enabled = bool(checked)
        if checked:
            # UF7. A raised plane shows its handle; the ◈ hides it.
            self._gizmoButtons[index].setChecked(True)
            self._selectPlane(index)
        self._syncFromState()
        self._emitChanged()

    def _selectPlane(self, index):
        self._active = index
        self._selectButtons[index].setChecked(True)
        self._gestureStep = None
        self._syncFromState()
        self.activePlaneChanged.emit(index)

    def _activePlane(self) -> SectionPlaneState:
        return self._planes[self._active]

    def activePlane(self) -> SectionPlaneState:
        return self._activePlane()

    def _changed(self):
        self._syncFromState()
        self._emitChanged()

    def _setAxis(self, axis):
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        self._activePlane().normal = normal
        self._gestureStep = None
        self._changed()

    def setViewNormal(self, normal):
        try:
            self._activePlane().normal = [float(value) for value in normal]
        except ValueError:
            return
        self._gestureStep = None
        self._changed()

    def _flip(self):
        self._activePlane().flip()
        self._gestureStep = None
        self._changed()

    def _rotate(self):
        try:
            tilt = float(self._tiltField.text() or 0)
            turn = float(self._turnField.text() or 0)
            plane = self._activePlane()
            n = rotated_normal(plane.state.normal, tilt, turn)
        except ValueError:
            self._syncFromState()
            return
        plane.setUnitNormal(n)
        self._gestureStep = None
        self._changed()

    def _snapToCentre(self):
        if self._bounds is None:
            return
        self._activePlane().origin = list(self._bounds.center())
        self._changed()

    def _snapToOrigin(self):
        self._activePlane().origin = [0.0, 0.0, 0.0]
        self._changed()

    def _originEdited(self, axis):
        """A point on the plane typed (in the display unit)."""
        if self._updating:
            return
        scale, _suffix = self._unit()
        edit = self._originFields[axis]
        plane = self._activePlane()
        if edit.text() == to_display(plane.origin[axis], scale):
            return
        try:
            value = from_display(edit.text(), scale)
        except ValueError:
            self._syncFromState()
            return
        point = plane.origin
        point[axis] = value
        plane.origin = point
        self._gestureStep = None
        self._changed()

    def _normalEdited(self, axis):
        if self._updating:
            return
        plane = self._activePlane()
        if self._normalFields[axis].text() == '{:.6g}'.format(
                plane.normal[axis]):
            return
        try:
            value = float(self._normalFields[axis].text())
        except ValueError:
            self._syncFromState()
            return
        normal = list(plane.normal)
        normal[axis] = value
        try:
            plane.normal = normal
        except ValueError:
            # A zero or unreadable normal is refused; the field goes back.
            self._syncFromState()
            return
        self._gestureStep = None
        self._changed()

    def _offsetEdited(self):
        """The offset typed, in the unit beside it."""
        if self._updating:
            return
        scale, _suffix = self._unit()
        plane = self._activePlane()
        text = self._offsetEdit.text()
        if text == to_display(plane.distance, scale):
            return
        try:
            metres = from_display(text, scale)
        except ValueError:
            self._syncFromState()
            return
        plane.setDistance(metres)
        self._gestureStep = None
        self._changed()

    def _positionChanged(self, value):
        if self._updating or self._bounds is None:
            return
        plane = self._activePlane()
        span = self._span(plane.state.normal)
        centre = self._centreOffset(plane)
        plane.setDistance(centre + (value / SLIDER_STEPS - 0.5) * span)
        self._gestureStep = None
        self._changed()

    # -- stepping ----------------------------------------------------------- #

    def _stepChoiceChanged(self, _index):
        self._gestureStep = None
        self._syncFromState()

    def _stepEdited(self):
        self._gestureStep = None
        self._syncFromState()

    def currentStep(self):
        """``(metres, source)`` for the next −/+ step, or ``(0.0, None)``.

        *source* is ``'typed'``, ``'cell'`` (median cell thickness along n),
        ``'kept'`` (the last cell-scale step, when the plane cuts no cells
        now) or ``'domain'`` (a hundredth of the model, labelled so).
        """
        plane = self._activePlane()
        key = (self._active, plane.state.normal, self.stepChoice(),
               self._stepEdit.text())
        if self._gestureStep is not None and self._gestureStep[0] == key:
            return self._gestureStep[1], self._gestureStep[2]
        step, source = self._findStep(plane)
        if step:
            self._gestureStep = (key, step, source)
        return step, source

    def _findStep(self, plane):
        scale, _suffix = self._unit()
        if self.stepChoice() == STEP_TYPED:
            try:
                step = from_display(self._stepEdit.text(), scale)
            except ValueError:
                return 0.0, None
            return (abs(step), 'typed') if step else (0.0, None)
        step = None
        if self._stepProvider is not None:
            try:
                step = self._stepProvider(plane)
            except Exception:
                step = None
        if step is not None and math.isfinite(step) and step > 0:
            self._lastStep = float(step)
            return float(step), 'cell'
        if self._lastStep:
            return self._lastStep, 'kept'
        fallback = domain_step(self._span(plane.state.normal))
        return (fallback, 'domain') if fallback else (0.0, None)

    def nudge(self, direction, fine=False) -> bool:
        """Move the active plane one step along n (*direction* ±1)."""
        plane = self._activePlane()
        if self._bounds is None or not plane.enabled:
            return False
        step, _source = self.currentStep()
        if not step:
            return False
        if fine:
            step *= FINE_FRACTION
        plane.move(math.copysign(step, direction))
        self._changed()
        return True

    # -- presentation ------------------------------------------------------ #

    def _unit(self):
        """``(scale, suffix)`` every length on the panel is shown in."""
        if self._bounds is None:
            return 1.0, 'm'
        return display_unit(max(self._bounds.size()))

    def _span(self, unitNormal):
        """Extent of the model measured along ``unitNormal``."""
        if self._bounds is None:
            return 0.0
        size = self._bounds.size()
        return sum(abs(unitNormal[axis]) * size[axis]
                   for axis in range(3)) or 1.0

    def _centreOffset(self, plane):
        """The offset that puts *plane* through the middle of the model."""
        if self._bounds is None:
            return 0.0
        centre = self._bounds.center()
        n = plane.state.normal
        reference = plane.reference
        return sum(n[axis] * (centre[axis] - reference[axis])
                   for axis in range(3))

    def _stepText(self, step, source):
        if not source:
            return ''
        scale, suffix = self._unit()
        amount = '{0} {1}'.format(to_display(step, scale), suffix)
        if source == 'cell':
            return self.tr('≈ {0} (cell scale)').format(amount)
        if source == 'kept':
            return self.tr('{0} (last cell scale)').format(amount)
        if source == 'domain':
            return self.tr('{0} (model ÷ 100: no cells cut)').format(amount)
        return amount

    def _syncPlaneActions(self):
        full = len(self._planes) >= MAX_PLANES
        self._addPlaneButton.setEnabled(not full)
        self._addPlaneButton.setToolTip(
            self.tr('A section holds at most {0} planes. Delete one to add '
                    'another.').format(MAX_PLANES) if full
            else self.tr('Add another plane (up to {0})').format(MAX_PLANES))
        alone = len(self._planes) <= 1
        self._deletePlaneButton.setEnabled(not alone)
        self._deletePlaneButton.setToolTip(
            self.tr('A section keeps at least one plane.') if alone
            else self.tr('Delete {0}').format(self.planeName(self._active)))
        if not self._nameEdit.hasFocus():
            self._nameEdit.setText(self.planeName(self._active))

    def _syncFromState(self):
        wasUpdating = self._updating
        self._updating = True
        try:
            scale, suffix = self._unit()
            plane = self._activePlane()
            for axis in range(3):
                self._originFields[axis].setText(
                    to_display(plane.origin[axis], scale))
                self._normalFields[axis].setText(
                    '{:.6g}'.format(plane.normal[axis]))
            self._pointUnit.setText(suffix)
            reference = ', '.join(to_display(value, scale)
                                  for value in plane.reference)
            self._referenceLabel.setText(
                self.tr('Offset measured from ({0}) {1}').format(
                    reference, suffix))
            count = len(self._planes)
            for index, widgets in enumerate(self._rowWidgets):
                for item in widgets:
                    item.setVisible(index < count)
                if index >= count:
                    self._enableBoxes[index].setChecked(False)
                    self._gizmoButtons[index].setChecked(False)
            self._syncPlaneActions()
            for index, state in enumerate(self._planes):
                name = self.planeName(index)
                self._selectButtons[index].setText(name)
                self._enableBoxes[index].setToolTip(
                    self.tr('Use {0}').format(name))
                self._enableBoxes[index].setAccessibleName(
                    self.tr('Enable {0}').format(name))
                self._enableBoxes[index].setChecked(state.enabled)
                unitNormal = state.normalised()
                self._summaries[index].setText(
                    '{0} {1}  ⟂ {2:.2g}, {3:.2g}, {4:.2g}'.format(
                        ', '.join(aligned(value * scale
                                          for value in state.origin)),
                        suffix, *unitNormal))
                self._gizmoButtons[index].setEnabled(state.enabled)
                if not state.enabled:
                    self._gizmoButtons[index].setChecked(False)
                self._gizmoButtons[index].setToolTip(
                    self.tr('Show the draggable handle for {0}').format(name)
                    if state.enabled
                    else self.tr('Turn {0} on first').format(name))

            offset = plane.distance
            self._offsetEdit.setText(to_display(offset, scale))
            self._offsetUnit.setText(suffix)
            self._offsetEdit.setToolTip(self.tr(
                'Signed distance of the plane from its reference point, '
                'along the normal, in {0}. Type a value to move it.').format(
                    suffix))
            span = self._span(plane.state.normal)
            if span and self._bounds is not None:
                position = ((offset - self._centreOffset(plane)) / span + 0.5)
                self._positionSlider.setValue(
                    int(round(min(max(position, 0.0), 1.0) * SLIDER_STEPS)))

            typed = self.stepChoice() == STEP_TYPED
            self._stepEdit.setEnabled(typed)
            steppable = plane.enabled and self._bounds is not None
            self._stepDown.setEnabled(steppable)
            self._stepUp.setEnabled(steppable)
            if self._stepHeld:
                pass        # the note follows when the drag is let go
            elif steppable:
                step, source = self.currentStep()
                self._stepNote.setText(self._stepText(step, source))
            else:
                self._stepNote.setText('')
            self._lookButton.setEnabled(plane.enabled)
        finally:
            self._updating = wasUpdating
