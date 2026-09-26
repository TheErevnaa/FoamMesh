#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The export form: which format, what name, which folder, and how thick.

Plan 33 EXPORT-04 and OF-09. Every setting an export needs used to live in a
modal window that opened on top of the settings column, and the column that
exists to show a step's settings showed a paragraph, a record sentence and
three buttons instead. The form is the thing; this module holds it once, so
the Export step mounts it inline and the menu route hosts the same object in
its dialog rather than keeping a second copy of what an export asks for.

EXPORT-02 lives here too: the destination rows state a floor measured in the
font they are drawn in, and they state it again whenever that font changes,
because a row with no floor is a row a cramped layout is free to take the
missing pixels out of.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
    QWidget)

from widgets.new_project_widget import NewProjectWidget
from widgets.selector_dialog import SelectorDialog, SelectorItem
from widgets.typed_edit import FloatEdit

from foammesh.app import app
from foammesh.core.case.scratch import is_scratch_case, suggested_name
from foammesh.core.mesh.extrusion_options import ExtrudeModel, ExtrudeOptions
from foammesh.db.configurations_schema import CFDType
from foammesh.view.theming.metrics import (
    FORM_MARGIN, align_form_columns, apply_form_metrics, place_unit)

from .export_2D_region_widgets import (
    Export2DPlaneRegionWidget, Export2DWedgeRegionWidget)


#: Plan 28 WP3 / Plan 31 FC-F. The mesh-file formats the product offers, and
#: the operation that writes each one. Both lists used to be attributes of the
#: legacy page; they are here because the form is what reads them now and the
#: page imports them back, so there is still exactly one of each.
MESH_FORMATS = ('openfoam', 'su2', 'gmsh', 'vtk', 'cgns', 'med', 'unv')

#: DP-680. The formats whose export can replace an existing destination.
OVERWRITABLE = ('openfoam', 'su2')

EXPORT_OPERATIONS = {
    'openfoam': 'case.export.authored',
    'su2': 'case.export.su2',
    'gmsh': 'case.export.gmsh',
    'vtk': 'case.export.vtk',
    'cgns': 'case.export.cgns',
    'med': 'case.export.med',
    'unv': 'case.export.unv',
}

#: What the form offers when the facade could not be asked. The OpenFOAM case
#: is the one destination this application could always write, so a registry
#: that did not answer leaves the reader with a form, not with a blank column.
DEFAULT_ENTRIES = (
    {'entry_id': 'openfoam', 'label': 'OpenFOAM case', 'available': True,
     'reason': '', 'notes': '', 'destination': 'directory', 'suffix': ''},
)

#: The three shapes an export can take. The two 2D ones used to be two
#: buttons beside the 3D one, which reads as three acts; they are one
#: setting with three values, and the inputs each value needs appear with it.
THREE_D = ''
PLANE = 'plane'
WEDGE = 'wedge'

DIMENSIONALITIES = (
    (THREE_D, 'Three dimensional'),
    (PLANE, 'Two dimensional, extruded from a plane'),
    (WEDGE, 'Two dimensional, a wedge about an axis'),
)

#: The entry whose destination an extrusion can be written into. Extrusion
#: rewrites an OpenFOAM case; no single-file writer takes one.
EXTRUDABLE = 'openfoam'

#: EXPORT-02. A border above and below the text of a row. Qt draws a framed
#: line edit with rather more than this, so it is a floor and not a size.
ROW_PADDING = 4

#: How much of a path the location field asks to be able to show, at worst.
#: R17 made this 48 characters for the Save case dialog, where a window can
#: be as wide as the path. MEASURED here at 150 % scaling: 48 characters is
#: 816 px, the settings column is 520, and the row label and Browse want 242
#: of those between them -- so the field asked for more than the column has
#: and the rows beside it paid for it. Twelve characters is a floor, not a
#: size: the field still takes the width the layout can spare, the whole path
#: is in the sentence under it, elided in the middle, and in its tooltip.
LOCATION_CHARACTERS = 12

#: The controls of the destination form, by the names Designer gives them.
DESTINATION_ROWS = ('label', 'projectName', 'label_2', 'projectLocation',
                    'select')


#: The Export step's own copy of this form, for the route the footer takes.
#: A module-level slot rather than an import between the two pages: the step
#: page and the legacy page already reach each other through the window, and a
#: second import edge between them is a cycle waiting to be written.
_INLINE_PAGE: list = []


#: DP-536. What the name field's validator refuses, so an offered name is
#: one the field will take.
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|]')


def register_inline_export(page) -> None:
    """Remember the step page that is showing this form inline."""
    import weakref

    _INLINE_PAGE[:] = [weakref.ref(page)] if page is not None else []


def inline_export_page():
    """The step page whose form is on screen, or ``None``.

    EXPORT-04. The footer's press arrives at the legacy page, because that is
    what the step manager holds. When the step is showing the form, the press
    is an export of what the reader can see, and opening a window to ask the
    same questions again is the fault this package exists to remove.
    """
    reference = _INLINE_PAGE[0] if _INLINE_PAGE else None
    page = reference() if reference is not None else None
    if page is None:
        return None
    try:
        return page if page.isVisible() else None
    except RuntimeError:                                     # pragma: no cover
        # The C++ side of a deleted page. Nothing is on screen.
        _INLINE_PAGE.clear()
        return None


def extrude_payload(options) -> dict:
    """The extrusion, as the facade's authored export route takes it.

    One spelling, read by the dialog route and by the inline step, because an
    extrusion written two ways is two chances to send a solver a wedge with
    somebody else's angle in it.
    """
    return {
        'model': options.model.value, 'thickness': options.thickness,
        'point': options.point, 'axis': options.axis, 'angle': options.angle,
    }


def _row_floor(widget: QWidget) -> None:
    """State how short this control may be drawn, in the font it draws in."""
    metrics = QFontMetrics(widget.font())
    widget.setMinimumHeight(metrics.height() + ROW_PADDING)


class _RowFloors(QObject):
    """Keep the destination rows' floors true after a font change.

    EXPORT-02. A floor worked out once at construction is a floor for the
    font the widget happened to start with. Display scaling, the appearance
    settings and the application's own font installation all change that font
    afterwards, and the measured clipping was worst at 125 % and 150 %.
    """

    def __init__(self, host: QWidget, characters: int = 0) -> None:
        super().__init__(host)
        self._host = host
        self._characters = int(characters or 0)
        self.apply()
        for name in DESTINATION_ROWS:
            child = host.findChild(QWidget, name)
            if child is not None:
                child.installEventFilter(self)

    def apply(self) -> None:
        for name in DESTINATION_ROWS:
            child = self._host.findChild(QWidget, name)
            if child is not None:
                _row_floor(child)
        if self._characters:
            location = self._host.findChild(QLineEdit, 'projectLocation')
            if location is not None:
                location.setMinimumWidth(
                    QFontMetrics(location.font()).averageCharWidth()
                    * self._characters)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FontChange:
            self.apply()
        return False


def hold_destination_rows_open(widget: QWidget, characters: int = 0):
    """Give the destination rows of ``widget`` a floor they keep.

    EXPORT-02, and the one fix serves both hosts: the dialog and the inline
    form show the same destination widget, so a floor stated in only one of
    them would be a fix for one of the two places the rows were clipped.
    """
    return _RowFloors(widget, characters)


class ExportForm(QWidget):
    """What an export needs asked, in the order a reader answers it."""

    #: The destination became valid or stopped being valid.
    pathChanged = Signal(object)
    #: A different format was chosen; carries its entry id.
    formatChanged = Signal(str)

    def __init__(self, parent=None, entries=(), recommended: str = 'openfoam'):
        super().__init__(parent)
        self._entries = tuple(entries) or DEFAULT_ENTRIES
        # DP-536. The destination this form offered, and whether the reader
        # has since typed over it. See `_offerDestination`.
        self._offering = False
        self._destinationOwned = False
        self._offeredFor = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        chooser = QFormLayout()
        apply_form_metrics(chooser)
        layout.addLayout(chooser)
        self._chooserForm = chooser
        self._format = QComboBox(self)
        self._format.setObjectName('exportFormat')
        self._format.setAccessibleName(self.tr('Format'))
        self._format.setAccessibleDescription(
            self.tr('The file format this mesh is written in.'))
        formatLabel = QLabel(self)
        formatLabel.setText(self.tr('Format'))
        # W-O1. The label is built by hand rather than by `addRow(text, ...)`,
        # so nothing had told Qt which control it names. A census of the
        # settings column counted it as a standing line of prose for that
        # reason, and a screen reader had no association to follow either.
        formatLabel.setBuddy(self._format)
        chooser.addRow(formatLabel, self._format)

        # EXPORT-03. The three sentences of provenance that used to sit here
        # said which run the mesh came from and what had been read back out of
        # it. That is a claim about the history, not a setting, and it is on
        # the Details route of the step. What stays on the form is the one
        # thing a reader can act on: the reason a format cannot be written.
        self._refusal = QLabel('', self)
        self._refusal.setObjectName('exportRefusal')
        self._refusal.setWordWrap(True)
        self._refusal.setProperty('foammeshStatus', 'warning')
        self._refusal.setVisible(False)
        layout.addWidget(self._refusal)

        self._pathWidget = NewProjectWidget(self, suffix=None)
        # DP-154. The destination rows come from a Designer form with its
        # own margins and spacing; one metric for every form on the widget,
        # or the name row's editor sits a few px right of the format row's.
        apply_form_metrics(self._pathWidget.formLayout())
        layout.addWidget(self._pathWidget)
        self._floors = hold_destination_rows_open(self._pathWidget,
                                                  LOCATION_CHARACTERS)
        # DP-680. The one way to write where an export already is. Off by
        # default, so an existing export is still refused and the next free
        # name offered (DP-562); on, the named destination is accepted, the
        # page confirms before replacing it, and the export replaces only it.
        self._overwrite = QCheckBox(self.tr('Overwrite existing'), self)
        self._overwrite.setObjectName('exportOverwrite')
        self._overwrite.setChecked(False)
        self._pathWidget.formLayout().addRow(self._overwrite)

        shape = QFormLayout()
        apply_form_metrics(shape)
        layout.addLayout(shape)
        self._shapeForm = shape
        self._dimension = QComboBox(self)
        self._dimension.setObjectName('exportDimensionality')
        # DP-218. The row used to be called Dimensionality, which is what the
        # code calls the idea and not a word this product prints anywhere: a
        # listener hearing it has nothing on screen to match it against. What
        # the row asks is how the mesh leaves the case, so it says that.
        self._dimension.setAccessibleName(self.tr('Written as'))
        self._dimension.setAccessibleDescription(
            self.tr('Whether the mesh is written as it is or extruded from '
                    'one surface into a two dimensional case.'))
        for value, text in DIMENSIONALITIES:
            self._dimension.addItem(self.tr(text), value)
        self._dimensionLabel = QLabel(self)
        self._dimensionLabel.setText(self.tr('Written as'))
        # W-O1, as above: a name with no buddy is not a name to Qt.
        self._dimensionLabel.setBuddy(self._dimension)
        shape.addRow(self._dimensionLabel, self._dimension)

        self._twoD = QWidget(self)
        self._buildTwoDimensional(self._twoD)
        layout.addWidget(self._twoD)
        self._twoD.setVisible(False)

        self._fillFormats(recommended)
        self._connectSignalsSlots()
        self._dimensionChanged()
        align_form_columns(self.forms())

    def forms(self) -> tuple:
        """Every form on this widget, top to bottom, for one label column.

        DP-154. The format row, the destination rows, the shape row and the
        two dimensional rows are each a `QFormLayout` of their own, and each
        sizes its label column from its own longest label. MEASURED on the
        export step with the product fonts: editors at three x positions
        (73, 91 and 127) down one column. A host that stacks this widget
        under forms of its own aligns them all through this tuple.
        """
        return (self._chooserForm, self._pathWidget.formLayout(),
                self._shapeForm, self._planeForm, self._wedgeForm)

    def setDestinationShown(self, shown: bool) -> None:
        """Draw the derived destination sentence, or leave it off the form.

        W-O1. One form, two hosts: the dialog shows the sentence and the
        inline step does not, because the step sits in the settings column
        and the sentence is a readout of the two fields above it.
        """
        self._pathWidget.setDestinationShown(shown)

    # -- construction ------------------------------------------------------ #

    def _buildTwoDimensional(self, host: QWidget) -> None:
        """The inputs an extrusion needs, one set per extrusion."""
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, FORM_MARGIN, 0, 0)

        self._boundaries = []
        self._planeRegions = []
        self._wedgeRegions = []

        self._planeBox = QWidget(host)
        planeLayout = QVBoxLayout(self._planeBox)
        planeLayout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._planeBox)

        self._wedgeBox = QWidget(host)
        wedgeLayout = QVBoxLayout(self._wedgeBox)
        wedgeLayout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._wedgeBox)

        for region in self._regionNames():
            plane = Export2DPlaneRegionWidget(region)
            plane.boundarySelectClicked.connect(self._selectBoundary)
            planeLayout.addWidget(plane)
            self._planeRegions.append(plane)
            wedge = Export2DWedgeRegionWidget(region)
            wedge.p1SelectClicked.connect(self._selectBoundary)
            wedge.p2SelectClicked.connect(self._selectBoundary)
            wedgeLayout.addWidget(wedge)
            self._wedgeRegions.append(wedge)

        planeForm = QFormLayout()
        apply_form_metrics(planeForm)
        planeLayout.addLayout(planeForm)
        self._planeForm = planeForm
        self._thickness = FloatEdit(self._planeBox)
        self._thickness.setObjectName('exportThickness')
        self._thickness.setText('1')
        planeForm.addRow(QLabel(self.tr('Thickness'), self._planeBox),
                         self._thickness)

        wedgeForm = QFormLayout()
        apply_form_metrics(wedgeForm)
        wedgeLayout.addLayout(wedgeForm)
        self._wedgeForm = wedgeForm
        self._angle = FloatEdit(self._wedgeBox)
        self._angle.setObjectName('exportAngle')
        self._angle.setText('5')
        wedgeForm.addRow(QLabel(self.tr('Angle'), self._wedgeBox), self._angle)
        # DP-198. The unit goes beside the box, not into the row's name, so
        # the refusal and the label can go on saying the same word.
        place_unit(self._angle, 'deg')
        self._origin = []
        self._direction = []
        for axis in ('X', 'Y', 'Z'):
            edit = FloatEdit(self._wedgeBox)
            edit.setObjectName(f'exportOrigin{axis}')
            edit.setText('0')
            wedgeForm.addRow(
                QLabel(self.tr('Origin {0}').format(axis), self._wedgeBox),
                edit)
            self._origin.append(edit)
        for index, axis in enumerate(('X', 'Y', 'Z')):
            edit = FloatEdit(self._wedgeBox)
            edit.setObjectName(f'exportDirection{axis}')
            edit.setText('1' if index == 0 else '0')
            wedgeForm.addRow(
                QLabel(self.tr('Direction {0}').format(axis), self._wedgeBox),
                edit)
            self._direction.append(edit)

    def _regionNames(self) -> tuple:
        """The regions of this case, or none if there is no case to read.

        The form is built by a page that exists before a project is open and
        by gates that have no working copy at all, so a reader that cannot
        answer leaves the extrusion asking for nothing rather than raising
        through the constructor of the whole step.
        """
        try:
            db = app.facadeClient.checkout()
            names = tuple(region.value('name')
                          for region in db.getElements('region').values())
            self._boundaries = [
                SelectorItem(geometry.value('name'), geometry.value('name'),
                             gId)
                for gId, geometry in db.getElements(
                    'geometry',
                    lambda i, e: e['cfdType'] == CFDType.BOUNDARY.value).items()]
            return names
        except Exception:                                    # noqa: BLE001
            return ()

    def _connectSignalsSlots(self) -> None:
        self._pathWidget.pathChanged.connect(self._pathWasChanged)
        for name in ('projectName', 'projectLocation'):
            field = self._pathWidget.findChild(QLineEdit, name)
            if field is not None:
                field.textChanged.connect(self._destinationWasTyped)
        self._format.currentIndexChanged.connect(self._formatWasChanged)
        self._dimension.currentIndexChanged.connect(self._dimensionChanged)
        self._overwrite.toggled.connect(self._pathWidget.setOverwriteAllowed)

    # -- formats ----------------------------------------------------------- #

    def _fillFormats(self, recommended: str) -> None:
        """Every format stays listed; an unusable one is disabled, not hidden.

        Hiding SU2 from a snappyHexMesh project would leave the reader hunting
        for a format that is not missing but refused, and never reading why.
        """
        chosen = -1
        self._format.clear()
        for index, entry in enumerate(self._entries):
            label = entry['label']
            if not entry.get('available'):
                label = self.tr('{0}  (not available)').format(label)
            self._format.addItem(label, entry['entry_id'])
            item = self._format.model().item(index)
            if item is not None and not entry.get('available'):
                item.setEnabled(False)
            self._format.setItemData(
                index, entry.get('reason') or entry.get('notes') or '',
                Qt.ItemDataRole.ToolTipRole)
            if entry['entry_id'] == recommended and entry.get('available'):
                chosen = index
        if chosen < 0:
            chosen = next((index for index, entry in enumerate(self._entries)
                           if entry.get('available')), 0)
        self._format.setCurrentIndex(chosen)
        self._formatWasChanged(chosen)

    def setEntries(self, entries, recommended: str = '') -> None:
        """Take a fresh answer from the facade's export registry."""
        entries = tuple(entries)
        if not entries or entries == self._entries:
            return
        self._entries = entries
        self._fillFormats(recommended or self.selectedFormat())

    def entries(self) -> tuple:
        return self._entries

    def destinationWidget(self):
        """The name and location rows, for a host that has to reach them."""
        return self._pathWidget

    def formatBox(self):
        return self._format

    def refusalLabel(self):
        return self._refusal

    def selectedEntry(self):
        entry_id = self._format.currentData()
        for entry in self._entries:
            if entry['entry_id'] == entry_id:
                return entry
        return None

    def selectedFormat(self) -> str:
        entry = self.selectedEntry()
        return entry['entry_id'] if entry else 'openfoam'

    def operation(self) -> str:
        """The facade route that writes what this form describes."""
        if self.dimensionality():
            # An extrusion rewrites an OpenFOAM case whatever the list says.
            return 'case.export.authored'
        return EXPORT_OPERATIONS.get(self.selectedFormat(),
                                     'case.export.authored')

    def _formatWasChanged(self, index: int) -> None:
        entry = self._entries[index] if 0 <= index < len(self._entries) else None
        if entry is None:
            self._refusal.setText('')
            self._refusal.setVisible(False)
            return
        reason = '' if entry.get('available') else (entry.get('reason') or '')
        self._refusal.setText(reason)
        self._refusal.setVisible(bool(reason))
        # R125. An OpenFOAM case is a folder that gets created; every other
        # format on this list is one file with an extension. The destination
        # preview said "folder" for both until it was told.
        self._pathWidget.setDestination(entry.get('destination') or 'directory',
                                        entry.get('suffix') or '')
        self._offerOverwrite(entry)
        self._showDimensionality(entry)
        self._offerDestination()
        self._pathWasChanged(self._pathWidget.projectPath())
        self.formatChanged.emit(entry['entry_id'])

    # -- dimensionality ---------------------------------------------------- #

    def _showDimensionality(self, entry) -> None:
        """Offer the extrusion only where there is something to extrude into.

        Plan 33: conditional stays conditional. An SU2 or CGNS destination is
        one file written by a writer that has no extrusion in it, so the
        choice is absent there rather than present and refused.
        """
        offered = entry.get('entry_id') == EXTRUDABLE
        self._dimension.setVisible(offered)
        self._dimensionLabel.setVisible(offered)
        if not offered and self._dimension.currentIndex() != 0:
            self._dimension.setCurrentIndex(0)

    def _offerOverwrite(self, entry) -> None:
        """Offer overwriting where the export can replace what is there.

        DP-680. An OpenFOAM case folder and an SU2 file are replaced; any
        other format is written under a free name only, so the box is
        disabled there with the reason on it rather than taken away.
        """
        offered = entry.get('entry_id') in OVERWRITABLE
        if not offered and self._overwrite.isChecked():
            self._overwrite.setChecked(False)
        self._overwrite.setEnabled(offered)
        self._overwrite.setToolTip(
            self.tr('Write into the named destination even if it exists. '
                    'You are asked first, and only that folder or file is '
                    'replaced.') if offered else
            self.tr('Only an OpenFOAM case folder or an SU2 file can be '
                    'overwritten; this format is written under a new name.'))

    def overwrite(self) -> bool:
        """Whether the reader asked to replace an existing destination."""
        return self._overwrite.isEnabled() and self._overwrite.isChecked()

    def overwriteKind(self) -> str:
        """``'su2'`` for a file destination, ``'folder'`` for a case."""
        entry = self.selectedEntry() or {}
        return 'su2' if entry.get('destination') == 'file' else 'folder'

    def dimensionality(self) -> str:
        """`''`, `'plane'` or `'wedge'`."""
        if not self._dimension.isVisibleTo(self):
            return THREE_D
        return str(self._dimension.currentData() or THREE_D)

    def twoDimensionalInputsVisible(self) -> bool:
        return self._twoD.isVisibleTo(self)

    def _dimensionChanged(self, *_args) -> None:
        mode = str(self._dimension.currentData() or THREE_D)
        self._twoD.setVisible(bool(mode))
        self._planeBox.setVisible(mode == PLANE)
        self._wedgeBox.setVisible(mode == WEDGE)

    def _selectBoundary(self, widget) -> None:
        self._selector = SelectorDialog(self, self.tr('Select boundary'),
                                        self.tr('Select boundary'),
                                        self._boundaries)
        self._selector.accepted.connect(
            lambda: widget.setText(self._selector.selectedText()))
        self._selector.open()

    # -- destination ------------------------------------------------------- #

    def projectPath(self):
        """The destination, carrying the chosen format's suffix.

        A directory destination (the OpenFOAM case) takes no suffix; a file
        one does, and appending it here is what keeps ``duct`` from being
        written as an extension-less file the solver will not recognise.
        """
        path = self._pathWidget.projectPath()
        entry = self.selectedEntry()
        if path is None or entry is None:
            return path
        suffix = entry.get('suffix') or ''
        if entry.get('destination') == 'file' and suffix and path.suffix != suffix:
            return path.with_name(path.name + suffix)
        return path

    def fillDestination(self, location, name: str) -> None:
        """Put a folder and a name into the destination rows.

        The two fields are what a person types; this is the same act for a
        caller that already knows both, and it validates exactly as typing
        does rather than writing past the validation.
        """
        field = self._pathWidget.findChild(QLineEdit, 'projectLocation')
        if field is not None:
            field.setText(str(location))
        field = self._pathWidget.findChild(QLineEdit, 'projectName')
        if field is not None:
            field.setText(str(name))

    def _pathWasChanged(self, path) -> None:
        self.pathChanged.emit(path)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._offerDestination()

    def _destinationWasTyped(self, *_args) -> None:
        if not self._offering:
            self._destinationOwned = True

    def _offerDestination(self) -> None:
        """Offer ``<case>_<format>`` in the case's folder until the reader
        says otherwise.

        DP-536. Audit 2026-09-24: the Export step opened with no name and the
        home folder as its location. `NewProjectWidget` falls back to both
        when it is given nothing, and this form never gave it anything, so
        every export began with the reader typing out what the case already
        knew. A name or folder the reader has typed is theirs; a new format
        only moves an offered name to the new format's suffix.
        """
        project = getattr(app, 'project', None)
        case = getattr(project, 'path', None) if project is not None else None
        if not case:
            return
        case = Path(case)
        if case != self._offeredFor:
            self._offeredFor = case
            self._destinationOwned = False
        if self._destinationOwned:
            return
        stem = suggested_name(case) if is_scratch_case(case) else case.name
        stem = _UNSAFE_NAME.sub('_', stem).strip() or 'case'
        self._offering = True
        try:
            location = self._pathWidget.findChild(QLineEdit, 'projectLocation')
            if not is_scratch_case(case):
                # A scratch case lives in a folder the application throws
                # away; an export put there would go with it.
                if location is not None and location.text() != str(case):
                    location.setText(str(case))
            name = self._unusedName(
                Path(location.text()) if location is not None else case,
                f'{stem}_{self.selectedFormat()}')
            field = self._pathWidget.findChild(QLineEdit, 'projectName')
            if field is not None and field.text() != name:
                field.setText(name)
        finally:
            self._offering = False

    def _unusedName(self, location: Path, name: str) -> str:
        """*name*, or *name* numbered, whichever is not on disk yet (DP-562).

        An offered name that is already there is offered only to be refused
        in red beneath it -- which is what the page did right after its own
        export wrote the folder it had offered.
        """
        entry = self.selectedEntry() or {}
        suffix = (entry.get('suffix') or ''
                  if entry.get('destination') == 'file' else '')
        candidate, number = name, 1
        try:
            while (location / f'{candidate}{suffix}').exists():
                number += 1
                candidate = f'{name}_{number}'
        except OSError:
            return name
        return candidate

    def destinationWasWritten(self) -> None:
        """The export this form described has been written (DP-562).

        MEASURED on the 0924 rerun: right after a successful export the
        destination rows turned red with "... already exists", because the
        folder the form named now did. The page reports what was written
        (the export record); the form moves on to a name that is free, so it
        describes the next export rather than refusing the last one.
        """
        location = self._pathWidget.findChild(QLineEdit, 'projectLocation')
        field = self._pathWidget.findChild(QLineEdit, 'projectName')
        if self.overwrite():
            # DP-680. The reader chose to write here and will again; the
            # name they are overwriting is kept.
            field = None
        if location is not None and field is not None and field.text():
            # The folder stays where the reader put it; only the name moves,
            # to the next free number of the same name.
            base = re.sub(r'_\d+$', '', field.text()) or field.text()
            free = self._unusedName(Path(location.text()), base)
            if free != field.text():
                self._offering = True
                try:
                    field.setText(free)
                finally:
                    self._offering = False
        self._pathWidget.revalidate()

    def validationMessage(self) -> str:
        return self._pathWidget.validationMessage()

    def refusal(self) -> str:
        """Why this form cannot be run yet, in one sentence, or ``''``."""
        entry = self.selectedEntry()
        if entry is not None and not entry.get('available'):
            return str(entry.get('reason')
                       or self.tr('This format cannot be written from this '
                                  'case.'))
        if self._pathWidget.projectPath() is None:
            return (self.validationMessage()
                    or self.tr('Give the export a name and a folder that '
                               'exists.'))
        mode = self.dimensionality()
        if mode == PLANE:
            for region in self._planeRegions:
                if not region.boundary():
                    return self.tr('Select boundary — {0}').format(
                        region.rname())
            try:
                self._thickness.validate(self.tr('Thickness'))
            except ValueError as error:
                return str(error)
        elif mode == WEDGE:
            for region in self._wedgeRegions:
                if not region.p1():
                    return self.tr('Select P1 — {0}').format(region.rname())
                if not region.p2():
                    return self.tr('Select P2 — {0}').format(region.rname())
            try:
                self._angle.validate(self.tr('Angle'), low=0, high=90,
                                     lowInclusive=False, highInclusive=False)
                for axis, edit in zip('XYZ', self._origin):
                    edit.validate(self.tr('Origin {0}').format(axis))
                for axis, edit in zip('XYZ', self._direction):
                    edit.validate(self.tr('Direction {0}').format(axis))
            except ValueError as error:
                return str(error)
            if not any(float(edit.text()) for edit in self._direction):
                return self.tr('Direction cannot be a zero vector.')
        return ''

    def extrudeOptions(self):
        """The regions and the extrusion the 2D routes take, as the dialogs
        built them: one shape of payload, read off one form."""
        mode = self.dimensionality()
        if mode == PLANE:
            return ([(region.rname(), region.boundary(), region.boundary())
                     for region in self._planeRegions],
                    ExtrudeOptions(ExtrudeModel.PLANE,
                                   thickness=self._thickness.text()))
        return ([(region.rname(), region.p1(), region.p2())
                 for region in self._wedgeRegions],
                ExtrudeOptions(ExtrudeModel.WEDGE,
                               point=[edit.text() for edit in self._origin],
                               axis=[edit.text() for edit in self._direction],
                               angle=self._angle.text()))
