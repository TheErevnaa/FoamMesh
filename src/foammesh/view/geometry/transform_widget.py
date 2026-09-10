#!/usr/bin/env python
# -*- coding: utf-8 -*-
import qasync
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from vtkmodules.vtkCommonTransforms import vtkTransform
from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter

from foammesh.support.pfloat import PFloat
from widgets.async_message_box import AsyncMessageBox

from .transform_widget_ui import Ui_TransformWidget


def axisAngleToEulerXYZ(angleDegrees, axis):
    """XYZ Euler angles (degrees) for a rotation of *angleDegrees* about *axis*.

    The artifact store composes ``Rz @ Ry @ Rx``; this reads those three
    angles back out of the axis-angle matrix. A zero axis has no rotation.
    """
    import math

    ax, ay, az = (float(v) for v in axis)
    norm = math.sqrt(ax * ax + ay * ay + az * az)
    if norm == 0.0:
        return None
    ax, ay, az = ax / norm, ay / norm, az / norm
    theta = math.radians(float(angleDegrees))
    c, s, t = math.cos(theta), math.sin(theta), 1.0 - math.cos(theta)
    r = (
        (t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay),
        (t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax),
        (t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c),
    )
    pitch = math.asin(max(-1.0, min(1.0, -r[2][0])))
    if abs(math.cos(pitch)) > 1e-9:
        roll = math.atan2(r[2][1], r[2][2])
        yaw = math.atan2(r[1][0], r[0][0])
    else:
        roll = math.atan2(-r[1][2], r[1][1])
        yaw = 0.0
    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


class TransformWidget(QWidget):
    transformed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_TransformWidget()
        self._ui.setupUi(self)

        self._meshes = None
        #: What was applied, in order, as the artifact store's transform
        #: operations, so the same transform can reach the mesher's copy.
        self._operations = []

        self._connectSignalsSlots()

    def setMeshes(self, sources):
        self._meshes = sources
        self._operations = []

    def meshes(self):
        return self._meshes

    def operations(self):
        return [dict(op) for op in self._operations]

    def showEvent(self, ev):
        if not ev.spontaneous():
            self._ui.tabWidget.setCurrentIndex(0)

        return super().showEvent(ev)

    def _connectSignalsSlots(self):
        self._ui.scale.clicked.connect(self._scale)
        self._ui.rotate.clicked.connect(self._rotate)
        self._ui.translate.clicked.connect(self._translate)

    @qasync.asyncSlot()
    async def _scale(self):
        self._ui.scale.setEnabled(False)

        try:
            x = float(PFloat(self._ui.scaleX.text(), self.tr('Scale Factor')))
            y = float(PFloat(self._ui.scaleY.text(), self.tr('Scale Factor')))
            z = float(PFloat(self._ui.scaleZ.text(), self.tr('Scale Factor')))
        except ValueError as e:
            await AsyncMessageBox().information(self, self.tr('Input Error'), str(e))
            self._ui.scale.setEnabled(True)
            return

        transform = vtkTransform()
        transform.Scale(x, y, z)
        self._operations.append({'kind': 'scale', 'values': [x, y, z]})

        for gId, source in self._meshes.items():
            transformFilter = vtkTransformPolyDataFilter()
            transformFilter.SetInputData(source)
            transformFilter.SetTransform(transform)
            transformFilter.Update()
            self._meshes[gId] = transformFilter.GetOutput()

        self.transformed.emit()

        self._ui.scale.setEnabled(True)

    @qasync.asyncSlot()
    async def _rotate(self):
        self._ui.rotate.setEnabled(False)

        try:
            angle = float(PFloat(self._ui.rotationAngle.text(), self.tr('Rotation Angle')))
            originX = float(PFloat(self._ui.originX.text(), self.tr('Rotation Origin')))
            originY = float(PFloat(self._ui.originY.text(), self.tr('Rotation Origin')))
            originZ = float(PFloat(self._ui.originZ.text(), self.tr('Rotation Origin')))
            axisX = float(PFloat(self._ui.axisX.text(), self.tr('Rotation Axis')))
            axisY = float(PFloat(self._ui.axisY.text(), self.tr('Rotation Axis')))
            axisZ = float(PFloat(self._ui.axisZ.text(), self.tr('Rotation Axis')))
        except ValueError as e:
            await AsyncMessageBox().information(self, self.tr('Input Error'), str(e))
            self._ui.rotate.setEnabled(True)
            return

        transform = vtkTransform()
        transform.PostMultiply()
        transform.Translate(-originX, -originY, -originZ)
        transform.RotateWXYZ(angle, axisX, axisY, axisZ)
        transform.Translate(originX, originY, originZ)
        # The store rotates about the origin by XYZ Euler angles; the same
        # rotation about another point is a translate on either side of it.
        euler = axisAngleToEulerXYZ(angle, (axisX, axisY, axisZ))
        if euler is not None:
            if any((originX, originY, originZ)):
                self._operations.append(
                    {'kind': 'translate', 'values': [-originX, -originY, -originZ]})
            self._operations.append({'kind': 'rotate', 'values': list(euler)})
            if any((originX, originY, originZ)):
                self._operations.append(
                    {'kind': 'translate', 'values': [originX, originY, originZ]})

        for gId, source in self._meshes.items():
            transformFilter = vtkTransformPolyDataFilter()
            transformFilter.SetInputData(source)
            transformFilter.SetTransform(transform)
            transformFilter.Update()
            self._meshes[gId] = transformFilter.GetOutput()

        self.transformed.emit()

        self._ui.rotate.setEnabled(True)

    @qasync.asyncSlot()
    async def _translate(self):
        self._ui.translate.setEnabled(False)

        try:
            x = float(PFloat(self._ui.translateX.text(), self.tr('Translate Offset')))
            y = float(PFloat(self._ui.translateY.text(), self.tr('Translate Offset')))
            z = float(PFloat(self._ui.translateZ.text(), self.tr('Translate Offset')))
        except ValueError as e:
            await AsyncMessageBox().information(self, self.tr('Input Error'), str(e))
            self._ui.translate.setEnabled(True)
            return

        transform = vtkTransform()
        transform.Translate(x, y, z)
        self._operations.append({'kind': 'translate', 'values': [x, y, z]})

        for gId, source in self._meshes.items():
            transformFilter = vtkTransformPolyDataFilter()
            transformFilter.SetInputData(source)
            transformFilter.SetTransform(transform)
            transformFilter.Update()
            self._meshes[gId] = transformFilter.GetOutput()

        self.transformed.emit()

        self._ui.translate.setEnabled(True)
