"""Visibility, display, clipping, manipulator and image export (Plan 41 GN17).

Gmsh's *Visibility* window, *Mesh > Options > Visibility* and *General >
Clipping / Manipulator* in one panel, working on the mesh in the viewport:

* **Visibility** -- filter entities by dimension, tag ranges (``1-5, 8``) and
  physical group; Show, Hide, Show only, Show all; the *Visibility affects
  meshing* switch is ``Mesh.MeshOnlyVisible``.  The calls are the Generate
  actions ``visibility`` and ``only_visible`` on the resident session.
* **Elements and labels, colours** -- moved (Plan 42 SG9) to the *Elements*
  group in the viewport's Display popover, one for both engines
  (:mod:`foammesh.view.display_control.element_display_panel`).
* **Clip** -- moved (Plan 42 SG12) to the Section tool, one for both
  engines: six planes, the ``a x + b y + c z + d`` row of its Advanced pane,
  *Clip by whole cells* for *keep whole elements*.
* **Camera** -- the manipulator (three rotations, translation, scale). The
  rotation centre is the shared toolbar's Rotation centre control, which takes
  a typed X/Y/Z behind its arrow (Plan 42 SG23); the projection (perspective
  or orthographic) is the shared toggle of the same toolbar (Plan 42 SG20).
* **Parts list** -- the entities are also rows of the "Gmsh entities" group of
  the shared Parts list while this panel holds a model (Plan 42 SG9).
* Export of the viewport image is the shared "Save image as..." in the view menu (Plan 42 SG10).

The drawing is :class:`foammesh.rendering.gmsh_mesh_view.GmshMeshView`, a
producer that feeds the shared viewport and never hides the standard mesh; the
rules are :mod:`foammesh.core.gmsh.session_display`.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from foammesh.core import display_unit
from foammesh.view.theming.metrics import CompactDoubleSpinBox, place_unit
from foammesh.view.workflow_controls.length_box import ModelLengthBox
from foammesh.core.gmsh import session_display as sd
from foammesh.core.gmsh.session_entities import EntityRecord


class _Bridge(QObject):
    done = Signal(object, object)
    failed = Signal(str)


def _spin(parent, name, low, high, value, decimals=3, step=1.0):
    box = CompactDoubleSpinBox(parent)
    box.setObjectName(name)
    box.setDecimals(decimals)
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setValue(value)
    box.setAccessibleName(name)
    return box


class GmshDisplayPanel(QWidget):
    """The Gmsh display options, visibility window and camera."""

    displayChanged = Signal()
    #: LY13 D11. The panel became visible (the Display popover opened).
    opened = Signal()

    def __init__(self, controller_provider=lambda: None, view_provider=None,
                 elements_provider=None, parts_provider=None,
                 centre_provider=None, parent=None):
        super().__init__(parent)
        self._provider = controller_provider
        self._view_provider = view_provider
        #: Plan 42 SG9. The shared Elements group, which owns the producer.
        self._elements_provider = elements_provider
        #: Plan 42 SG9. ``() -> GmshEntityGroup``: the "Gmsh entities" group of
        #: the shared Parts list.
        self._parts_provider = parts_provider
        #: Plan 42 SG23. ``() -> tool``: the shared viewport toolbar, whose
        #: Rotation centre control owns the typed centre (DP-1426).
        self._centre_provider = centre_provider
        self._partsWired = None
        self._controller = None
        self._records: list = []
        self._group_names: dict = {}
        self._visibility = sd.VisibilityModel()
        self._worker = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._loadFailed)
        self.setObjectName('gmshDisplayPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget(self)
        self._tabs.setObjectName('gmshDisplayTabs')
        layout.addWidget(self._tabs)
        self._buildVisibility()
        self._buildCamera()
        self._status = QLabel('', self)
        self._status.setObjectName('gmshDisplayStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    # -- helpers ------------------------------------------------------------ #

    def _tab(self, title: str, name: str):
        page = QWidget(self)
        page.setObjectName(name)
        box = QVBoxLayout(page)
        self._tabs.addTab(page, title)
        return box

    def _check(self, parent_layout, text, name, checked, handler=None):
        box = QCheckBox(text, self)
        box.setObjectName(name)
        box.setChecked(checked)
        if handler is not None:
            box.toggled.connect(handler)
        parent_layout.addWidget(box)
        return box

    def _loadFailed(self, text: str) -> None:
        group = self._partsWired
        if group is not None:
            try:
                group.setLoading(False)
            except RuntimeError:
                pass
        self._say(text)

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def status(self) -> str:
        return self._status.text()

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    # -- Visibility ---------------------------------------------------------- #

    def _buildVisibility(self) -> None:
        box = self._tab(self.tr('Visibility'), 'gmshDisplayVisibilityTab')
        row = QHBoxLayout()
        self._dimBoxes = {}
        for dim, text in ((0, self.tr('Points')), (1, self.tr('Curves')),
                          (2, self.tr('Surfaces')), (3, self.tr('Volumes'))):
            check = QCheckBox(text, self)
            check.setObjectName('gmshVisDim%d' % dim)
            check.setChecked(True)
            self._dimBoxes[dim] = check
            row.addWidget(check)
        box.addLayout(row)
        self._tagRanges = QLineEdit(self)
        self._tagRanges.setObjectName('gmshVisTags')
        self._tagRanges.setPlaceholderText(self.tr('Tags, e.g. 1-5, 8 (empty: all)'))
        self._tagRanges.setAccessibleName(self.tr('Entity tag ranges'))
        box.addWidget(self._tagRanges)
        self._physical = QLineEdit(self)
        self._physical.setObjectName('gmshVisPhysical')
        self._physical.setPlaceholderText(self.tr('Physical group name or tag'))
        self._physical.setAccessibleName(self.tr('Physical group'))
        box.addWidget(self._physical)
        row = QHBoxLayout()
        for text, mode in ((self.tr('Show'), 'show'), (self.tr('Hide'), 'hide'),
                           (self.tr('Show only'), 'only'),
                           (self.tr('Show all'), 'all')):
            button = QPushButton(text, self)
            button.setObjectName('gmshVis_' + mode)
            button.clicked.connect(
                lambda _c=False, m=mode: self.applyVisibility(m))
            row.addWidget(button)
        box.addLayout(row)
        self._affects = self._check(
            box, self.tr('Visibility affects meshing'), 'gmshVisAffectsMeshing',
            False, self._onAffects)
        self._affects.setToolTip(self.tr(
            'Mesh.MeshOnlyVisible: the next Generate meshes only the visible '
            'entities'))
        self._visLabel = QLabel('', self)
        self._visLabel.setObjectName('gmshVisCount')
        box.addWidget(self._visLabel)
        box.addStretch(1)

    def setModel(self, rows, groups=()) -> None:
        """Install the entity table (``session.entities`` rows) and groups."""
        self._records = [EntityRecord(
            dim=int(r['dim']), tag=int(r['tag']), name=r.get('name') or '',
            physical=tuple(r.get('physical') or ())) for r in rows]
        self._group_names = {int(g.tag): g.name for g in groups}
        self._visibility.reset(self._records)
        self._showCount()
        self._syncParts()

    def visibilityModel(self) -> 'sd.VisibilityModel':
        return self._visibility

    def filterValue(self) -> 'sd.VisibilityFilter':
        return sd.VisibilityFilter(
            dims=tuple(d for d, b in self._dimBoxes.items() if b.isChecked()),
            tags=self._tagRanges.text(), physical=self._physical.text())

    def _showCount(self) -> None:
        total = len(self._records)
        shown = len(self._visibility.visible)
        self._visLabel.setText(
            self.tr('%d of %d entities visible.') % (shown, total))

    def applyVisibility(self, mode: str) -> list:
        """Show, hide, show only or show all of the filtered entities."""
        if not self._records:
            self._say(self.tr('Show the model first; there are no entities '
                              'to filter.'))
            return []
        try:
            selected = sd.matching_entities(self._records, self.filterValue(),
                                            self._group_names)
            steps = sd.visibility_steps(self._records, selected, mode)
        except ValueError as error:
            self._say(str(error))
            return []
        if mode != 'all' and not selected:
            self._say(self.tr('No entity matches the filter.'))
            return []
        self._applySteps(steps)
        self._say(self.tr('%d entities selected.') % len(selected))
        return steps

    def _applySteps(self, steps) -> None:
        self._visibility.apply(steps)
        self._showCount()
        self._redraw()
        controller = self._controllerOrNone()
        if controller is not None and steps:
            self._start('visibility', lambda: [
                controller.run('visibility', step) for step in steps])

    # -- the shared Parts list (Plan 42 SG9) ----------------------------------- #

    def partsGroup(self):
        """The "Gmsh entities" group of the shared Parts list, or ``None``."""
        group = self._parts_provider() if self._parts_provider else None
        if group is not None and group is not self._partsWired:
            group.entityToggled.connect(self.setEntityVisible)
            group.entitySolo.connect(self.soloEntity)
            self._partsWired = group
            self._followEngine()

            # FIX-K (SG9): the rows belong to the panel that listed them last;
            # an old panel going away after a newer one listed must not wipe.
            token = object()
            group._rowOwner = token
            self._rowToken = token

            def leave(*_args, _group=group, _token=token):
                try:
                    if getattr(_group, '_rowOwner', _token) is not _token:
                        return
                    _group.setEntities([])
                except RuntimeError:
                    pass                      # the group is already gone

            self.destroyed.connect(leave)
        return group

    def _followEngine(self) -> None:
        """Plan 42 SG23 (DP-1425). The rows belong to the Gmsh engine: they
        leave the Parts list when another engine is chosen and come back when
        Gmsh is chosen again."""
        try:
            from foammesh.view import engine_context as contexts

            contexts.engine_context().engineChanged.connect(self._engineMoved)
        except Exception:                                 # noqa: BLE001
            pass

    def _engineMoved(self, engine: str) -> None:
        from foammesh.view.engine_context import GMSH

        try:
            if engine == GMSH:
                self._syncParts()
            else:
                self.clearParts()
        except RuntimeError:
            pass                              # the group or the panel is gone

    def _entityName(self, record) -> str:
        kind = (self.tr('Point'), self.tr('Curve'), self.tr('Surface'),
                self.tr('Volume'))[record.dim]
        label = '%s %d' % (kind, record.tag)
        names = [self._group_names.get(int(g)) for g in record.physical]
        names = [n for n in names if n]
        extra = record.name or ', '.join(names)
        return '%s (%s)' % (label, extra) if extra else label

    def _syncParts(self) -> None:
        """List the model's entities in the Parts list; none hides the frame."""
        group = self.partsGroup()
        if group is None:
            return
        group.setEntities(
            [(r.dim, r.tag, self._entityName(r)) for r in self._records],
            self._visibility.hidden)

    def clearParts(self) -> None:
        """Gmsh is no longer the active engine: the group leaves the list."""
        group = self.partsGroup()
        if group is not None:
            group.setEntities([])

    def setEntityVisible(self, dim: int, tag: int, visible: bool) -> list:
        """An eye in the Parts list: the same step the Visibility window takes."""
        key = (int(dim), int(tag))
        if key not in {r.key for r in self._records}:
            return []
        if self._visibility.is_visible(*key) == bool(visible):
            return []
        steps = sd.visibility_steps(self._records, [key],
                                    'show' if visible else 'hide')
        self._applySteps(steps)
        return steps

    def soloEntity(self, dim: int, tag: int) -> list:
        """The solo button: show only this entity."""
        key = (int(dim), int(tag))
        if key not in {r.key for r in self._records}:
            return []
        steps = sd.visibility_steps(self._records, [key], 'only')
        self._applySteps(steps)
        return steps

    def _onAffects(self, on: bool) -> None:
        self._visibility.only_visible = bool(on)
        controller = self._controllerOrNone()
        if controller is not None:
            self._start('only', lambda: controller.run(
                'only_visible', {'visible': bool(on)}))

    def _controllerOrNone(self):
        try:
            self._controller = self._controller or self._provider()
        except Exception:                                 # noqa: BLE001
            return None
        return self._controller

    # -- Camera ------------------------------------------------------------------ #

    def _buildCamera(self) -> None:
        box = self._tab(self.tr('Camera'), 'gmshDisplayCameraTab')
        grid = QGridLayout()
        self._pose = {}
        for index, (name, text, value) in enumerate((
                ('rx', self.tr('Rotation X'), 0.0),
                ('ry', self.tr('Rotation Y'), 0.0),
                ('rz', self.tr('Rotation Z'), 0.0),
                ('tx', self.tr('Translation X'), 0.0),
                ('ty', self.tr('Translation Y'), 0.0),
                ('tz', self.tr('Translation Z'), 0.0),
                ('scale', self.tr('Scale'), 1.0))):
            if name in ('tx', 'ty', 'tz'):
                # UXB3c: a translation is a length in model units, typed in
                # the project display unit.
                spin = ModelLengthBox(self, minimum=-1e9, maximum=1e9,
                                      step=0.1)
                spin.setObjectName('gmshPose_' + name)
                spin.setAccessibleName(text)
                spin.setModelValue(value)
            else:
                spin = _spin(self, 'gmshPose_' + name, -1e9, 1e9, value, 4,
                             1.0 if name.startswith('r') else 0.1)
            if name == 'scale':
                spin.setRange(0.0001, 1e6)
            self._pose[name] = spin
            grid.addWidget(QLabel(text, self), index, 0)
            grid.addWidget(spin, index, 1)
        box.addLayout(grid)
        for name in ('rx', 'ry', 'rz'):
            # DP-198. The unit stands beside the number, not in the label.
            place_unit(self._pose[name], 'deg')
        for name in ('tx', 'ty', 'tz'):
            self._pose[name].unit_label = place_unit(
                self._pose[name], display_unit.current())
        row = QHBoxLayout()
        apply_ = QPushButton(self.tr('Apply'), self)
        apply_.setObjectName('gmshPoseApply')
        apply_.clicked.connect(self.applyPose)
        read = QPushButton(self.tr('Read from view'), self)
        read.setObjectName('gmshPoseRead')
        read.clicked.connect(self.readPose)
        row.addWidget(apply_)
        row.addWidget(read)
        box.addLayout(row)
        # Plan 42 SG23 (DP-1426). The rotation centre is the shared
        # toolbar's Rotation centre control (typed X/Y/Z behind its arrow);
        # this tab no longer has a second copy of it.
        note = QLabel(self.tr(
            'The rotation centre is the viewport toolbar\'s Rotation centre '
            'control. With none set, the pose turns about the centre of the '
            'model.'), self)
        note.setObjectName('gmshRotCentreNote')
        note.setWordWrap(True)
        box.addWidget(note)
        box.addStretch(1)

    def _camera(self):
        host = self._view_provider() if self._view_provider else None
        renderer = getattr(host, 'renderer', None)
        if not callable(renderer):
            return None, None
        renderer = renderer()
        return renderer, renderer.GetActiveCamera()

    def _modelCentre(self):
        view = self.meshView()
        if view is not None and view.loaded:
            points = view.arrays.points
            return (points.min(axis=0) + points.max(axis=0)) / 2.0, float(
                (points.max(axis=0) - points.min(axis=0)).max())
        renderer, _camera = self._camera()
        bounds = renderer.ComputeVisiblePropBounds() if renderer else None
        if not bounds or bounds[0] > bounds[1]:
            return None, 1.0
        import numpy as np

        low, high = np.array(bounds[0::2]), np.array(bounds[1::2])
        return (low + high) / 2.0, float((high - low).max()) or 1.0

    def rotationCentre(self):
        """The shared toolbar's rotation centre, else the model's centre."""
        import numpy as np

        tool = self._centre_provider() if self._centre_provider else None
        reader = getattr(tool, 'rotationCentre', None)
        centre = reader() if callable(reader) else None
        if centre is not None:
            return np.array(centre, dtype=float)
        return self._modelCentre()[0]

    def poseValue(self) -> 'sd.CameraPose':
        return sd.CameraPose(**{
            n: (s.modelValue() if isinstance(s, ModelLengthBox) else s.value())
            for n, s in self._pose.items()})

    def applyPose(self) -> bool:
        renderer, camera = self._camera()
        centre, size = self._modelCentre()
        if camera is None or centre is None:
            self._say(self.tr('There is nothing in the viewport to turn.'))
            return False
        try:
            vectors = sd.camera_vectors(self.poseValue(), self.rotationCentre(),
                                        size * 2.5)
        except ValueError as error:
            self._say(str(error))
            return False
        camera.SetPosition(*vectors['position'])
        camera.SetFocalPoint(*vectors['focal'])
        camera.SetViewUp(*vectors['up'])
        if camera.GetParallelProjection():
            camera.SetParallelScale(size * 2.5 / 2.0 / self.poseValue().scale)
        renderer.ResetCameraClippingRange()
        self._refresh()
        return True

    def readPose(self) -> 'sd.CameraPose | None':
        renderer, camera = self._camera()
        centre, size = self._modelCentre()
        if camera is None or centre is None:
            return None
        pose = sd.pose_from_vectors(camera.GetPosition(), camera.GetFocalPoint(),
                                    camera.GetViewUp(), self.rotationCentre(),
                                    size * 2.5)
        for name, spin in self._pose.items():
            spin.blockSignals(True)
            if isinstance(spin, ModelLengthBox):
                spin.setModelValue(getattr(pose, name))
            else:
                spin.setValue(getattr(pose, name))
            spin.blockSignals(False)
        return pose

    def _refresh(self) -> None:
        host = self._view_provider() if self._view_provider else None
        if host is not None and hasattr(host, 'refresh'):
            host.refresh()

    # -- drawing -------------------------------------------------------------- #

    def elements(self):
        """The shared Elements group (Plan 42 SG9), or ``None``."""
        return self._elements_provider() if self._elements_provider else None

    def meshView(self):
        """The producer the shared group drew with, once it has drawn."""
        elements = self.elements()
        return elements.meshView() if elements is not None else None

    def _redraw(self) -> None:
        """The entity mask changed: the producer draws again without them."""
        group = self.partsGroup()
        if group is not None:
            group.setHidden(self._visibility.hidden)
        elements = self.elements()
        if elements is not None:
            report = elements.setEntityMask(self._visibility.hidden)
            if report:
                self._say(self.tr('%(cells)d elements drawn.') % report)

    def showEvent(self, event):
        super().showEvent(event)
        self.opened.emit()

    # -- the session --------------------------------------------------------- #

    def loadModel(self) -> None:
        """Read the entity table from the session (worker thread)."""
        if self.busy():
            return
        controller = self._controllerOrNone()
        if controller is None:
            self._say(self.tr('Open a project first.'))
            return
        directory = tempfile.mkdtemp(prefix='foammesh-display-')

        def work():
            rows, _vtp = controller.entity_model(directory)
            return rows, controller.physical_editor().groups()

        self._start('model', work)

    def restoreRows(self) -> bool:
        """FIX-K (SG9). The Gmsh pages were rebuilt (an engine switch) while
        the session lived on: read its entities again, with the Parts list
        saying so, instead of waiting for the Display tier to be opened.
        A session that is not up is not started here."""
        from foammesh.view import engine_context as contexts

        if self._records or self.busy():
            return False
        try:
            if contexts.engine_context().engine != contexts.GMSH:
                return False
            controller = self._controllerOrNone()
            session = getattr(controller, 'session', None)
            if session is None or not session.alive:
                return False
        except RuntimeError:
            return False
        group = self.partsGroup()
        if group is not None:
            group.setLoading(True)
        self.loadModel()
        return True

    def _start(self, token: str, work) -> None:
        def run():
            try:
                self._bridge.done.emit(token, work())
            except Exception as error:                 # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=run, name='gmsh-display',
                                        daemon=True)
        self._worker.start()

    def _onDone(self, token, result) -> None:
        self._worker = None
        if token == 'model':
            rows, groups = result
            self.setModel(rows, groups)
            self._say(self.tr('%d entities.') % len(rows))
        elif token == 'visibility':
            failed = [s for s in result if not getattr(s, 'ok', True)]
            if failed:
                self._say(getattr(failed[0], 'error', '') or
                          self.tr('Gmsh refused the visibility change.'))
