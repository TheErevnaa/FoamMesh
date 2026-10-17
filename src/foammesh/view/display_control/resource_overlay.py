#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CPU and RAM on the viewport (Plan 39 VI13, F7).

A Qt panel over the render widget, refreshed once a second from ``psutil``.
It is not a VTK actor on purpose: an actor would force a scene render every
second, and the readout exists so a user can watch the machine while the view
is busy. The panel turns amber when the free RAM is below what the next load's
admission check would refuse (``resource_budget.quick_snapshot``).

The timer runs only while the panel is shown.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QToolButton, QToolTip, QVBoxLayout, QWidget)
from foammesh.view.theming.metrics import FORM_MARGIN, MARGIN_TIGHT
from foammesh.view.theming.status_colors import set_text_colour, style_resource_overlay

logger = logging.getLogger(__name__)

REFRESH_MS = 1000
#: Colour of the panel's text when RAM is short.
AMBER = '#d98c00'

INFO_TEXT = (
    'CPU: the machine-wide processor load.\n'
    'App: memory held by FoamMesh itself.\n'
    'Free: RAM the machine can still give; amber when it is below what the '
    'next load needs plus the 1 GiB kept for the window.\n'
    'GPU: video memory of the adapter in use, when the driver reports it.\n'
    'WSL: memory available inside the meshing distro, as last probed.')


def _gib(value: int) -> str:
    return '{0:.1f} GiB'.format(max(0, int(value)) / (1024 ** 3))


def sample() -> dict:
    """One reading. Cheap: no process walk, nothing blocking."""
    import psutil

    from foammesh.support import resource_budget

    out = {'cpu': float(psutil.cpu_percent(interval=None)),
           'process': int(psutil.Process().memory_info().rss)}
    snap = resource_budget.quick_snapshot()
    out['free'] = int(snap.available)
    out['total'] = int(snap.total)
    out['reserve'] = int(snap.reserve + snap.wsl_vm)
    out['short'] = snap.available < snap.reserve + snap.wsl_vm
    return out


def wsl_available() -> int | None:
    """Free memory in the meshing distro as last probed, else ``None``."""
    try:
        from foammesh.core.execution.resources import meshing_host
        host = meshing_host()
        if str(host.source).startswith('wsl') and host.memory_available_bytes:
            return int(host.memory_available_bytes)
    except Exception:                                       # noqa: BLE001
        pass
    return None


class ResourceOverlay(QFrame):
    """The panel; ``sampler`` and ``wsl`` are seams for tests."""

    def __init__(self, parent: QWidget | None = None, *, sampler=None,
                 wsl=None, gpu=None):
        super().__init__(parent)
        self.setObjectName('resourceOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        style_resource_overlay(self)
        self._sampler = sampler or sample
        self._wsl = wsl or wsl_available
        self._gpu = gpu
        self._amber = False
        self._lines: list[str] = []
        self.refreshes = 0
        self._hint = None

        font = QFont('Consolas')
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self._label = QLabel()
        self._label.setObjectName('resourceOverlayText')
        self._label.setFont(font)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._info = QToolButton()
        self._info.setObjectName('resourceOverlayInfo')
        self._info.setText('ⓘ')
        self._info.setToolTip('What these figures are')
        self._info.setAutoRaise(True)
        self._info.clicked.connect(self._showInfo)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._label, 1)
        top.addWidget(self._info, 0, Qt.AlignmentFlag.AlignTop)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(FORM_MARGIN, MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT)
        layout.addLayout(top)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self.hide()

    # -- state -------------------------------------------------------------- #

    def isShown(self) -> bool:
        return not self.isHidden()

    def setShown(self, on: bool) -> None:
        on = bool(on)
        if on:
            self.refresh()
            self.show()
            self.raise_()
            self._timer.start()
        else:
            self._timer.stop()
            self.hide()

    def timerActive(self) -> bool:
        return self._timer.isActive()

    def isAmber(self) -> bool:
        return self._amber

    def text(self) -> str:
        return self._label.text()

    # -- refresh ------------------------------------------------------------ #

    def refresh(self) -> None:
        """Read the machine and set the text. Qt only: nothing here renders."""
        self.refreshes += 1
        try:
            reading = self._sampler()
        except Exception:                                   # noqa: BLE001
            logger.debug('Resource reading failed', exc_info=True)
            return
        lines = ['CPU {0:5.1f} %'.format(reading['cpu']),
                 'App {0:>9}'.format(_gib(reading['process'])),
                 'Free {0:>8}'.format(_gib(reading['free']))]
        gpu = self._gpu() if self._gpu is not None else None
        if gpu:
            lines.append('GPU {0:>9}'.format(_gib(gpu)))
        wsl = self._wsl()
        if wsl:
            lines.append('WSL {0:>9}'.format(_gib(wsl)))
        amber = bool(reading.get('short'))
        text = '\n'.join(lines)
        if text != self._label.text():
            self._label.setText(text)
        if amber != self._amber:
            self._amber = amber
            colour = AMBER if amber else '#f0f0f0'
            set_text_colour(self._label, colour)
            self.setToolTip(
                'Free RAM is below what the next load needs.' if amber
                else '')
        hint = self.sizeHint()
        if hint != self._hint:
            self._hint = hint
            arrange = getattr(self.parentWidget(), 'arrangeOverlays', None)
            if arrange is not None:
                arrange()

    def _showInfo(self) -> None:
        QToolTip.showText(self._info.mapToGlobal(self._info.rect().bottomLeft()),
                          INFO_TEXT, self._info)
