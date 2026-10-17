"""Elements: one display group in the viewport's Display popover.

Plan 42 SG9 (D2, D3).  ``GmshMeshView`` used to be a second drawing of the
Gmsh mesh behind its own tier, switched in by hiding the standard mesh.  It is
a producer now: this group builds its arrays from the mesh on screen -- a Gmsh
hand-over or a snappyHexMesh scene -- and the producer puts its actors in the
one viewport beside the standard ones.  Nothing is hidden.

The group has one set of controls.  An item the mesh on screen cannot use is
hidden, and a frame with nothing left hides too: the Gmsh frame (light,
normals, tangents, Voronoi) is not shown on a snappy mesh, and Curve elements
is not shown when the mesh has no 1D cells.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QLabel,
    QVBoxLayout, QWidget,
)

from foammesh.view.theming.metrics import GAP, GAP_TIGHT, CompactDoubleSpinBox
from foammesh.core.gmsh import session_display as sd
from foammesh.core.mesh import element_display as ed

LEGEND_KEY = 'elementColours'


def _spin(name, low, high, value, decimals=2, step=1.0):
    box = CompactDoubleSpinBox()
    box.setObjectName(name)
    box.setAccessibleName(name)
    box.setDecimals(decimals)
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setValue(value)
    return box


class _Bridge(QObject):
    done = Signal(object)
    failed = Signal(str)


def sceneSource(manager) -> ed.ElementSource:
    """The arrays of the mesh the viewport shows, from the mesh manager.

    A Gmsh hand-over is read from its ``mesh.vtu``; any other scene is read
    from the datasets the viewport already holds (the internal mesh and the
    patches), so the producer sees what the user sees.
    """
    from foammesh.core.mesh import handover_scene
    from foammesh.rendering.actor_info import ActorType

    if manager is None:
        raise ValueError('There is no mesh in the viewport yet.')
    path = manager.nativePath()
    if path is not None and handover_scene.is_handover(path):
        return ed.source_from_gmsh(sd.read_arrays(path),
                                   sd.read_group_names(path))
    volume, patches = None, {}
    for key, info in manager._actorInfos.items():
        data = info.dataSet() if hasattr(info, 'dataSet') else None
        if data is None:
            continue
        if key == 'internalMesh' or (
                data.IsA('vtkUnstructuredGrid') and volume is None
                and info.type() is ActorType.MESH):
            volume = data
        elif info.type() is ActorType.BOUNDARY:
            patches[str(info.name())] = data
    if volume is None and not patches:
        raise ValueError('There is no mesh in the viewport yet.')
    return ed.source_from_foam(volume, patches)


class ElementDisplayPanel(QWidget):
    """Colour by, cell types, nodes, shrink, labels, for any mesh."""

    changed = Signal()

    def __init__(self, view=None, parent=None):
        super().__init__(parent)
        self.setObjectName('elementDisplayPanel')
        self._view = view
        self._provider = None             # () -> ElementSource
        self._gmshHandover = None         # () -> path of a [1,2,3] hand-over
        self._legendSink = None           # (add(key, bar), remove(key))
        self._source = None
        self._mesh_view = None
        self._legend = None
        self._options = sd.DisplayOptions()
        self._busy = False
        #: The Gmsh entity mask and clip planes (set by the Gmsh page).
        self._hidden = set()
        #: Plan 42 SG12. The Section tool's section, as the producer takes it:
        #: ``(clip, planes, whole, rule, curves)`` or ``None``.
        self._section = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onSource)
        self._bridge.failed.connect(self._failed)
        self._kindBoxes = {}
        self._building = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GAP)
        self._show = QCheckBox(self.tr('Show elements'))
        self._show.setObjectName('elementShow')
        self._show.setToolTip(self.tr(
            'Draw the elements of the mesh on screen beside the standard '
            'view: colour them by what they are, pick the cell types, show '
            'nodes and labels. Nothing is hidden; use Parts for that.'))
        self._show.toggled.connect(self.setShown)
        layout.addWidget(self._show)

        self._body = QWidget()
        self._body.setObjectName('elementBody')
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(GAP)
        layout.addWidget(self._body)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._colour = QComboBox()
        self._colour.setObjectName('elementColourBy')
        self._colour.setAccessibleName(self.tr('Colour elements by'))
        self._colour.currentIndexChanged.connect(self._changed)
        form.addRow(self.tr('Colour by'), self._colour)
        body.addLayout(form)

        # Cell types: one check per kind the mesh has.
        self._kindFrame = self._frame(body, 'elementKindFrame')
        self._kindLabel = QLabel(self.tr('Cell types'))
        self._kindGrid = QGridLayout()
        self._kindFrame.layout().addWidget(self._kindLabel)
        self._kindFrame.layout().addLayout(self._kindGrid)
        self._kindNote = QLabel('')
        self._kindNote.setObjectName('elementKindNote')
        self._kindNote.setWordWrap(True)
        self._kindFrame.layout().addWidget(self._kindNote)

        # What is drawn.
        self._drawFrame = self._frame(body, 'elementDrawFrame')
        self._flag = {}
        for name, text, default in (
                ('points', self.tr('Show nodes'), False),
                ('lines', self.tr('Curve elements'), True),
                ('surface_faces', self.tr('Surface faces'), True),
                ('surface_edges', self.tr('Surface edges'), True),
                ('volume_faces', self.tr('Volume faces'), True),
                ('volume_edges', self.tr('Volume edges'), True),
                ('draw_skin_only', self.tr('Skin only'), True)):
            self._flag[name] = self._check(self._drawFrame, text,
                                           'element_' + name, default)
        self._flag['draw_skin_only'].setToolTip(self.tr(
            'On: only the faces on the outside of the volume. Off: every '
            'face of every volume element.'))
        grid = QFormLayout()
        self._nodeSize = _spin('elementNodeSize', 0.5, 50.0, 4.0, 1)
        self._nodeStyle = QComboBox()
        self._nodeStyle.setObjectName('elementNodeStyle')
        self._nodeStyle.addItems([self.tr('Dots'), self.tr('Spheres')])
        self._lineWidth = _spin('elementLineWidth', 0.5, 20.0, 1.0, 1)
        self._shrink = _spin('elementShrink', 0.01, 1.0, 1.0, 2, 0.05)
        self._shrink.setToolTip(self.tr(
            'Shrink each element about its centre; 1 draws whole elements'))
        for text, widget in ((self.tr('Node size'), self._nodeSize),
                             (self.tr('Node style'), self._nodeStyle),
                             (self.tr('Line width'), self._lineWidth),
                             (self.tr('Shrink elements'), self._shrink)):
            grid.addRow(text, widget)
        for widget in (self._nodeSize, self._lineWidth, self._shrink):
            widget.valueChanged.connect(self._changed)
        self._nodeStyle.currentIndexChanged.connect(self._changed)
        self._drawFrame.layout().addLayout(grid)

        # Labels.
        self._labelFrame = self._frame(body, 'elementLabelFrame')
        self._labelType = QComboBox()
        self._labelType.setObjectName('elementLabelType')
        self._labelType.setAccessibleName(self.tr('Label type'))
        self._labelType.currentIndexChanged.connect(self._changed)
        form = QFormLayout()
        form.addRow(self.tr('Label type'), self._labelType)
        self._sampling = _spin('elementLabelSampling', 1, 100000, 1, 0)
        self._sampling.valueChanged.connect(self._changed)
        form.addRow(self.tr('One label in every'), self._sampling)
        self._labelFrame.layout().addLayout(form)
        self._flag['element_labels'] = self._check(
            self._labelFrame, self.tr('Element labels'),
            'element_element_labels', False)
        self._flag['node_labels'] = self._check(
            self._labelFrame, self.tr('Node labels'),
            'element_node_labels', False)

        # Gmsh items: shown while the mesh on screen is a Gmsh mesh.
        self._gmshFrame = self._frame(body, 'elementGmshFrame')
        for name, text, default in (
                ('light', self.tr('Light'), True),
                ('light_two_side', self.tr('Light both sides'), True),
                ('voronoi', self.tr('Voronoi'), False)):
            self._flag[name] = self._check(self._gmshFrame, text,
                                           'element_' + name, default)
        form = QFormLayout()
        self._normals = _spin('elementNormals', 0.0, 20.0, 0.0, 2, 0.25)
        self._tangents = _spin('elementTangents', 0.0, 20.0, 0.0, 2, 0.25)
        self._subEdges = _spin('elementSubEdges', 1, 64, 2, 0)
        self._subEdges.setToolTip(self.tr(
            'Pieces each edge of a second-order or higher element is drawn '
            'in; 1 draws it straight'))
        for text, widget in ((self.tr('Normals (length, 0 off)'),
                              self._normals),
                             (self.tr('Tangents (length, 0 off)'),
                              self._tangents),
                             (self.tr('Edge pieces, high order'),
                              self._subEdges)):
            form.addRow(text, widget)
            widget.valueChanged.connect(self._changed)
        self._gmshFrame.layout().addLayout(form)

        self._status = QLabel('')
        self._status.setObjectName('elementStatus')
        self._status.setWordWrap(True)
        body.addWidget(self._status)
        self._body.setVisible(False)
        self._applyEngineVisibility()

    # -- building blocks ------------------------------------------------------ #

    def _frame(self, layout, name):
        frame = QFrame()
        frame.setObjectName(name)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(GAP_TIGHT)
        layout.addWidget(frame)
        return frame

    def _check(self, frame, text, name, checked):
        box = QCheckBox(text)
        box.setObjectName(name)
        box.setChecked(checked)
        box.toggled.connect(self._changed)
        frame.layout().addWidget(box)
        return box

    def _say(self, text: str) -> None:
        self._status.setText(str(text))

    def status(self) -> str:
        return self._status.text()

    # -- wiring --------------------------------------------------------------- #

    def setSourceProvider(self, provider) -> None:
        """``provider() -> ElementSource`` for the mesh on screen."""
        self._provider = provider

    def setGmshHandover(self, provider) -> None:
        """``provider() -> path``: a hand-over with curve elements (the Gmsh
        page owns the session). Run off the GUI thread."""
        self._gmshHandover = provider

    def setLegendSink(self, add, remove) -> None:
        self._legendSink = (add, remove)

    def attachSection(self, cutTool) -> None:
        """Cut the producer's actors with the Section tool's planes."""
        self._cutTool = cutTool
        signal = getattr(cutTool, 'sectionApplied', None)
        if signal is not None:
            signal.connect(self._followSection)

    def setEntityMask(self, hidden) -> dict:
        """Entities the Gmsh Visibility window hides (``(dim, tag)`` pairs)."""
        self._hidden = {tuple(h) for h in hidden}
        return self._redrawNow()

    def _redrawNow(self) -> dict:
        if self._source is None or self._mesh_view is None:
            return {}
        return self._draw()

    def _followSection(self) -> None:
        """The Section tool's cut, on the producer's actors: the one clip of
        both engines (Plan 42 SG12), with Gmsh's whole-element rule."""
        view = self._mesh_view
        tool = self._cutTool
        option = tool.option()
        if option is None:
            self._section = None
        else:
            from .section_panel import CutType
            from foammesh.core.section.modes import SectionMode

            self._section = (
                option[0] == CutType.CLIP, list(option[1]),
                tool.sectionMode() is SectionMode.CLIP_WHOLE_CELLS,
                tool.cellRule().value, tool.clipCurves())
        if view is None or not view.loaded:
            return
        self._applySection(view)

    def _applySection(self, view, draw: bool = True) -> dict:
        if self._section is None:
            return view.applySection(None, [], draw=draw)
        clip, planes, whole, rule, curves = self._section
        return view.applySection(clip, planes, whole, rule, curves,
                                 draw=draw)

    def sceneChanged(self) -> None:
        """The viewport's parts changed: draw again from what is there now."""
        if self._show.isChecked():
            QTimer.singleShot(0, self.request)

    # -- source ----------------------------------------------------------------- #

    def meshView(self):
        return self._mesh_view

    def source(self):
        return self._source

    def isShown(self) -> bool:
        return self._show.isChecked()

    def setShown(self, on: bool) -> None:
        self._body.setVisible(bool(on))
        if not on:
            self.clear()
            self.changed.emit()
            return
        self.request()

    def request(self) -> None:
        """Read the mesh on screen and draw it."""
        if self._busy:
            return
        if self._gmshHandover is not None and self._onGmshSession():
            self._busy = True
            self._status.setText(self.tr('Reading the session mesh…'))

            def work():
                try:
                    path = self._gmshHandover()
                    if not path:
                        raise ValueError('The session holds no mesh to draw; '
                                         'generate one first.')
                    self._bridge.done.emit(ed.source_from_gmsh(
                        sd.read_arrays(path), sd.read_group_names(path)))
                except Exception as error:               # noqa: BLE001
                    self._bridge.failed.emit(str(error))

            threading.Thread(target=work, name='element-display',
                             daemon=True).start()
            return
        try:
            if self._provider is None:
                raise ValueError('There is no mesh in the viewport yet.')
            source = self._provider()
        except (ValueError, OSError) as error:
            self._refused(str(error))
            return
        self.showSource(source)

    def _onGmshSession(self) -> bool:
        try:
            from foammesh.app import app

            manager = getattr(getattr(app, 'window', None), 'meshManager',
                              None)
            return bool(manager is not None
                        and manager.artifactId() == 'gmsh-session')
        except Exception:                                 # noqa: BLE001
            return False

    def _refused(self, reason: str) -> None:
        self._say(reason)
        self._show.blockSignals(True)
        self._show.setChecked(False)
        self._show.blockSignals(False)
        self._body.setVisible(False)

    def _onSource(self, source) -> None:
        self._busy = False
        self.showSource(source)

    def _failed(self, reason: str) -> None:
        self._busy = False
        self._refused(reason)

    def showSource(self, source) -> dict:
        """Draw *source* and fit the controls to it."""
        from foammesh.rendering.gmsh_mesh_view import GmshMeshView

        self._source = source
        if self._mesh_view is None or self._mesh_view._view is not self._view:
            self._mesh_view = GmshMeshView(self._view)
        self._fitControls()
        if not self._show.isChecked():
            self._show.blockSignals(True)
            self._show.setChecked(True)
            self._show.blockSignals(False)
            self._body.setVisible(True)
        return self._draw()

    def clear(self) -> None:
        if self._mesh_view is not None:
            self._mesh_view.clear()
        self._source = None
        offer = getattr(getattr(self, '_cutTool', None),
                        'setCurvesOffered', None)
        if callable(offer):
            offer(False)
        self._dropLegend()
        self._status.setText('')

    # -- controls follow the mesh ------------------------------------------------ #

    def _fitControls(self) -> None:
        source = self._source
        self._building = True
        try:
            # Colour by: only what the arrays can distinguish.
            wanted = self._colour.currentData()
            self._colour.clear()
            for mode, label in ed.colour_modes(source):
                self._colour.addItem(self.tr(label), mode)
            index = self._colour.findData(
                wanted if wanted is not None else 1)
            self._colour.setCurrentIndex(max(index, 0))
            # Cell types.
            while self._kindGrid.count():
                item = self._kindGrid.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            kept = {kind: box.isChecked()
                    for kind, box in self._kindBoxes.items()}
            self._kindBoxes = {}
            for place, kind in enumerate(ed.kinds_on_screen(source)):
                box = QCheckBox(self.tr(kind))
                box.setObjectName('elementKind' + kind)
                box.setChecked(kept.get(kind, True))
                box.setToolTip(self.tr('%d in this mesh.')
                               % source.kind_counts[kind])
                box.toggled.connect(self._changed)
                self._kindBoxes[kind] = box
                self._kindGrid.addWidget(box, place // 3, place % 3)
            self._kindNote.setText(ed.polyhedra_note(source))
            self._kindNote.setVisible(bool(self._kindNote.text()))
            # Labels.
            wanted = self._labelType.currentIndex()
            self._labelType.clear()
            self._labelType.addItems(
                [self.tr(name) for name in source.label_names()])
            self._labelType.setCurrentIndex(max(wanted, 0))
            self._applyEngineVisibility()
        finally:
            self._building = False

    def _applyEngineVisibility(self) -> None:
        """Items the mesh on screen cannot use are hidden; so are the frames
        that are left with nothing."""
        source = self._source
        gmsh = source is not None and source.kind == ed.GMSH
        self._gmshFrame.setVisible(gmsh)
        has_lines = source is not None and bool(
            source.kind_counts.get('Lines'))
        self._flag['lines'].setVisible(has_lines)
        # Plan 42 SG12. The Section tool offers its curves-and-points switch
        # while a mesh with line elements is on screen.
        offer = getattr(getattr(self, '_cutTool', None),
                        'setCurvesOffered', None)
        if callable(offer):
            offer(has_lines)
        self._kindFrame.setVisible(source is not None)
        self._kindLabel.setVisible(source is not None)

    # -- options ------------------------------------------------------------------ #

    def options(self) -> sd.DisplayOptions:
        return self._options

    def collectOptions(self) -> sd.DisplayOptions:
        flags = {name: box.isChecked() for name, box in self._flag.items()}
        mode = self._colour.currentData()
        return sd.DisplayOptions(
            kinds=tuple(kind for kind, box in self._kindBoxes.items()
                        if box.isChecked()),
            explode=self._shrink.value(), point_size=self._nodeSize.value(),
            line_width=self._lineWidth.value(),
            point_type=self._nodeStyle.currentIndex(),
            colour_mode=int(mode if mode is not None else 1),
            label_type=max(self._labelType.currentIndex(), 0),
            label_sampling=int(self._sampling.value()),
            normals=self._normals.value(), tangents=self._tangents.value(),
            num_sub_edges=int(self._subEdges.value()),
            **flags)

    def _changed(self, *_args) -> None:
        if self._building or self._source is None:
            return
        self._draw()

    def _draw(self) -> dict:
        try:
            options = self.collectOptions()
            options.validate()
        except ValueError as error:
            self._say(str(error))
            return {}
        self._options = options
        view = self._mesh_view
        view.options = options
        gmsh = self._source.kind == ed.GMSH
        view.hidden = set(self._hidden) if gmsh else set()
        if gmsh:
            self._applySection(view, draw=False)
        else:
            view.applySection(None, [], draw=False)
        report = view.showSource(self._source)
        self._showLegend()
        self._say(self.tr('%(cells)d elements drawn.') % report)
        self.changed.emit()
        return report

    # -- the one categorical legend --------------------------------------------- #

    def legendRows(self) -> list:
        if self._source is None:
            return []
        return ed.legend_rows(self._source, self._options.colour_mode)

    def _showLegend(self) -> None:
        self._dropLegend()
        rows = self.legendRows()
        if not rows or self._legendSink is None:
            return
        from vtkmodules.vtkCommonCore import vtkLookupTable
        from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

        table = vtkLookupTable()
        table.IndexedLookupOn()
        table.SetNumberOfTableValues(len(rows))
        for index, (label, (red, green, blue)) in enumerate(rows):
            table.SetTableValue(index, red / 255.0, green / 255.0,
                                blue / 255.0, 1.0)
            table.SetAnnotation(index, label)
        bar = vtkScalarBarActor()
        bar.SetObjectName(LEGEND_KEY)
        bar.SetLookupTable(table)
        bar.SetTitle(self._colour.currentText())
        bar.SetNumberOfLabels(0)
        bar.DrawAnnotationsOn()
        bar.UnconstrainedFontSizeOn()
        bar.SetBarRatio(0.2)
        bar.SetTitleRatio(0.1)
        bar.SetWidth(0.2)
        bar.SetHeight(0.45)
        self._legend = bar
        self._legendSink[0](LEGEND_KEY, bar)

    def _dropLegend(self) -> None:
        if self._legend is not None and self._legendSink is not None:
            self._legendSink[1](LEGEND_KEY)
        self._legend = None
