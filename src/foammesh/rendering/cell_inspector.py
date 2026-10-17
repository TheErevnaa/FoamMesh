"""Click an element of the Gmsh session mesh in the viewport and read it.

Plan 41 GN13 (A-13, I-02). While active, a left click picks the cell under the
cursor with a ``vtkCellPicker`` and hands the cell back through a callback; the
camera is not moved by that click. The picked cell is a cell of whatever the
mapper draws -- the volume's skin, a section, a clipped part -- and every one
of those keeps Gmsh's cell arrays, so the element is identified by its tag and
described from the array row it was drawn from.

It touches the scene through the viewport widget only (``renderer``,
``interactor``), like the cell focus and the entity view.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PRIORITY = 1.5


class CellInspector:
    """Pick one cell under the cursor while switched on."""

    def __init__(self, view, onPicked):
        self._view = view
        self._onPicked = onPicked
        self._active = False
        self._observers: list = []

    # -- state -------------------------------------------------------------- #

    def isActive(self) -> bool:
        return self._active

    def setActive(self, on: bool) -> None:
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        if on:
            self._attach()
        else:
            self._detach()

    # -- picking ------------------------------------------------------------ #

    def _renderer(self):
        reader = getattr(self._view, 'renderer', None)
        return reader() if callable(reader) else None

    def pickAt(self, x: float, y: float):
        """``(dataset, cell)`` under a display point, ``(None, None)`` if none."""
        from vtkmodules.vtkRenderingCore import vtkCellPicker

        renderer = self._renderer()
        if renderer is None:
            return None, None
        picker = vtkCellPicker()
        picker.SetTolerance(0.0005)
        if not picker.Pick(float(x), float(y), 0.0, renderer):
            return None, None
        cell = picker.GetCellId()
        data = picker.GetDataSet()
        if cell < 0 or data is None:
            return None, None
        return data, int(cell)

    # -- the interactor ----------------------------------------------------- #

    def _interactor(self):
        reader = getattr(self._view, 'interactor', None)
        return reader() if callable(reader) else None

    def _attach(self) -> None:
        interactor = self._interactor()
        if interactor is None or not hasattr(interactor, 'AddObserver'):
            return
        self._observers.append((interactor, interactor.AddObserver(
            'LeftButtonPressEvent', self._pressed, _PRIORITY)))

    def _detach(self) -> None:
        for subject, tag in self._observers:
            try:
                subject.RemoveObserver(tag)
            except Exception:                                      # noqa: BLE001
                pass
        self._observers = []

    def _pressed(self, interactor, _event) -> None:
        if not self._active:
            return
        x, y = interactor.GetEventPosition()
        data, cell = self.pickAt(x, y)
        if data is None:
            return
        try:
            taken = self._onPicked(data, cell)
        except Exception:                                          # noqa: BLE001
            logger.debug('element inspection failed', exc_info=True)
            taken = False
        if taken:
            for subject, tag in self._observers:
                if subject is interactor:
                    command = interactor.GetCommand(tag)
                    if command is not None:
                        command.SetAbortFlag(1)

    def dispose(self) -> None:
        self.setActive(False)
