"""Plan 43 LY13 (D12). View > Camera > Camera pose... for both engines.

The Gmsh Display panel's Camera tab has a pose editor (three rotations, three
translations, a scale; Apply and Read from view).  The decision of 2026-10-13
puts the same editor in the one View menu, on the one shared viewport, so the
snappy and the Gmsh views set and read their camera the same way.  The maths
is Gmsh's Manipulator (``session_display.camera_vectors`` and its inverse);
nothing here is engine-specific.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout)

from foammesh.core import display_unit
from foammesh.core.gmsh import session_display as sd
from foammesh.view.theming.metrics import CompactDoubleSpinBox, place_unit
from foammesh.view.workflow_controls.length_box import ModelLengthBox

FIELDS = (('rx', 'Rotation X'), ('ry', 'Rotation Y'), ('rz', 'Rotation Z'),
          ('tx', 'Translation X'), ('ty', 'Translation Y'),
          ('tz', 'Translation Z'), ('scale', 'Scale'))


class CameraPoseDialog(QDialog):
    """Set the viewport camera from a pose, or read the pose back."""

    def __init__(self, view_provider, centre_provider=None, parent=None):
        super().__init__(parent)
        self._view_provider = view_provider
        self._centre_provider = centre_provider
        self.setObjectName('cameraPoseDialog')
        self.setWindowTitle(self.tr('Camera pose'))
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        self._pose = {}
        for index, (name, text) in enumerate(FIELDS):
            text = self.tr(text)
            if name in ('tx', 'ty', 'tz'):
                spin = ModelLengthBox(self, minimum=-1e9, maximum=1e9, step=0.1)
                spin.setModelValue(0.0)
            else:
                spin = CompactDoubleSpinBox(self)
                spin.setDecimals(4)
                spin.setRange(0.0001 if name == 'scale' else -1e9,
                              1e6 if name == 'scale' else 1e9)
                spin.setSingleStep(1.0 if name.startswith('r') else 0.1)
                spin.setValue(1.0 if name == 'scale' else 0.0)
            spin.setObjectName('cameraPose_' + name)
            spin.setAccessibleName(text)
            self._pose[name] = spin
            grid.addWidget(QLabel(text, self), index, 0)
            grid.addWidget(spin, index, 1)
        layout.addLayout(grid)
        for name in ('rx', 'ry', 'rz'):
            place_unit(self._pose[name], 'deg')
        for name in ('tx', 'ty', 'tz'):
            self._pose[name].unit_label = place_unit(
                self._pose[name], display_unit.current())
        row = QHBoxLayout()
        apply_ = QPushButton(self.tr('Apply'), self)
        apply_.setObjectName('cameraPoseApply')
        apply_.clicked.connect(lambda _c=False: self.applyPose())
        read = QPushButton(self.tr('Read from view'), self)
        read.setObjectName('cameraPoseRead')
        read.clicked.connect(lambda _c=False: self.readPose())
        row.addWidget(apply_)
        row.addWidget(read)
        layout.addLayout(row)
        self._status = QLabel('', self)
        self._status.setObjectName('cameraPoseStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def _camera(self):
        host = self._view_provider() if self._view_provider else None
        renderer = getattr(host, 'renderer', None)
        if not callable(renderer):
            return None, None
        renderer = renderer()
        if renderer is None:
            return None, None
        return renderer, renderer.GetActiveCamera()

    def _modelCentre(self):
        import numpy as np

        renderer, _camera = self._camera()
        bounds = renderer.ComputeVisiblePropBounds() if renderer else None
        if not bounds or bounds[0] > bounds[1]:
            return None, 1.0
        low, high = np.array(bounds[0::2]), np.array(bounds[1::2])
        return (low + high) / 2.0, float((high - low).max()) or 1.0

    def rotationCentre(self):
        """The toolbar's rotation centre, else the middle of the model."""
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
        host = self._view_provider()
        refresh = getattr(host, 'refresh', None)
        if callable(refresh):
            refresh()
        self._say('')
        return True

    def readPose(self) -> 'sd.CameraPose | None':
        renderer, camera = self._camera()
        centre, size = self._modelCentre()
        if camera is None or centre is None:
            self._say(self.tr('There is nothing in the viewport to read.'))
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
        self._say('')
        return pose


def show_camera_pose(window) -> CameraPoseDialog:
    """Open (or raise) the window's one Camera pose dialog."""
    dialog = getattr(window, '_cameraPoseDialog', None)
    if dialog is None:
        dialog = CameraPoseDialog(
            lambda: window._ui.renderingView,
            lambda: getattr(window, '_renderingTool', None), window)
        window._cameraPoseDialog = dialog
    dialog.readPose()
    dialog.show()
    dialog.raise_()
    return dialog
