"""Workspace page shown when a case mesh is outside the authored wizard."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from foammesh.core.case import ExternalMeshSummary


class ExternalMeshPage(QWidget):
    """Give external meshes a useful home without pretending they are authored."""

    showMeshRequested = Signal()
    meshInfoRequested = Signal()
    startWorkflowRequested = Signal()
    returnExternalRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._summary = None

        layout = QVBoxLayout(self)
        title = QLabel(self.tr('External mesh'))
        title.setObjectName('externalMeshTitle')
        layout.addWidget(title)

        description = QLabel(self.tr(
            'This mesh was opened outside the FoamMesh generation workflow. '
            'The authored Geometry through Export steps are suspended to preserve its provenance.'))
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QFormLayout()
        self._casePath = self._valueLabel()
        self._meshPath = self._valueLabel()
        self._origin = self._valueLabel()
        self._state = self._valueLabel()
        self._reason = self._valueLabel()
        form.addRow(self.tr('Case'), self._casePath)
        form.addRow(self.tr('polyMesh'), self._meshPath)
        form.addRow(self.tr('Origin'), self._origin)
        form.addRow(self.tr('Artifact state'), self._state)
        form.addRow(self.tr('Why this mode'), self._reason)
        layout.addLayout(form)

        self._showMesh = QPushButton(self.tr('Display mesh'))
        self._meshInfo = QPushButton(self.tr('Mesh info'))
        self._copyCasePath = QPushButton(self.tr('Copy case path'))
        self._startWorkflow = QPushButton(self.tr('Start meshing workflow'))
        self._returnExternal = QPushButton(self.tr('Return to external mesh'))
        self._showMesh.clicked.connect(self.showMeshRequested)
        self._meshInfo.clicked.connect(self.meshInfoRequested)
        self._copyCasePath.clicked.connect(self._copyPath)
        self._startWorkflow.clicked.connect(self.startWorkflowRequested)
        self._returnExternal.clicked.connect(self.returnExternalRequested)
        layout.addWidget(self._showMesh)
        layout.addWidget(self._meshInfo)
        layout.addWidget(self._copyCasePath)
        layout.addWidget(self._startWorkflow)
        layout.addWidget(self._returnExternal)
        layout.addStretch()

    def _valueLabel(self):
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(label.textInteractionFlags())
        return label

    def setSummary(self, summary: ExternalMeshSummary, *, can_return=False):
        self._summary = summary
        self._casePath.setText(summary.case_path)
        self._meshPath.setText(summary.poly_mesh_path or self.tr('No complete polyMesh found'))
        self._origin.setText(summary.origin)
        self._state.setText(summary.artifact_state)
        self._reason.setText(summary.reason)
        self._showMesh.setEnabled(summary.has_mesh)
        self._meshInfo.setEnabled(summary.has_mesh)
        self._startWorkflow.setEnabled(summary.has_mesh)
        self._returnExternal.setVisible(can_return)

    def _copyPath(self):
        if self._summary is not None:
            QGuiApplication.clipboard().setText(self._summary.case_path)
