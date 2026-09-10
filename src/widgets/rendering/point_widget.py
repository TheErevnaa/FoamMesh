#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import QObject, Signal
from vtkmodules.vtkCommonCore import vtkCommand
from vtkmodules.vtkInteractionWidgets import vtkPointWidget
from foammesh.view.theming.vtk_theme import rgb


class PointWidget(QObject):
    pointMoved = Signal(tuple)

    def __init__(self, view):
        super().__init__()

        self._view = view
        self._bounds = None

        self._widget = vtkPointWidget()
        self._widget.SetInteractor(view.interactor())
        self._widget.GetSelectedProperty().SetLineWidth(2)
        self._widget.GetProperty().SetLineWidth(2)
        self._widget.GetProperty().SetColor(*rgb('#2e9d60'))

        self._widget.AddObserver(vtkCommand.InteractionEvent, self._pointMoved)

    def applyTheme(self, tokens):
        self._widget.GetProperty().SetColor(*rgb(tokens.value('status.success')))
        self._widget.GetSelectedProperty().SetColor(*rgb(tokens.value('accent.default')))
        self._view.refresh()

    def setBounds(self, bounds):
        center = bounds.center
        position = center() if callable(center) else center
        limits = tuple(
            getattr(bounds, legacy)
            if hasattr(bounds, legacy)
            else getattr(bounds, modern)
            for legacy, modern in (
                ('xMin', 'xmin'), ('xMax', 'xmax'),
                ('yMin', 'ymin'), ('yMax', 'ymax'),
                ('zMin', 'zmin'), ('zMax', 'zmax'),
            )
        )

        self._bounds = bounds
        self._widget.SetPosition(*position)
        self._widget.PlaceWidget(*limits)

        return position

    def setPosition(self, x, y, z):
        def limit(legacy, modern):
            return (getattr(self._bounds, legacy)
                    if hasattr(self._bounds, legacy)
                    else getattr(self._bounds, modern))

        x = max(x, limit('xMin', 'xmin'))
        x = min(x, limit('xMax', 'xmax'))
        y = max(y, limit('yMin', 'ymin'))
        y = min(y, limit('yMax', 'ymax'))
        z = max(z, limit('zMin', 'zmin'))
        z = min(z, limit('zMax', 'zmax'))
        self._widget.SetPosition(x, y, z)
        self._view.refresh()

        return x, y, z

    def bounds(self):
        return self._bounds

    def on(self):
        self._widget.On()
        self._view.refresh()

    def off(self):
        self._widget.Off()
        self._view.refresh()

    def close(self):
        self._widget.RemoveAllObservers()
        self._widget.Off()
        self._widget = None

    def outlineOff(self):
        self._widget.OutlineOff()
        self._widget.XShadowsOff()
        self._widget.YShadowsOff()
        self._widget.ZShadowsOff()

    def _pointMoved(self, obj, evnent):
        self.pointMoved.emit(obj.GetPosition())
