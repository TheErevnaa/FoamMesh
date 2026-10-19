"""Plan 46: the status-bar sign that a program on this PC can drive the case.

Shown only while the local API is serving. Its tooltip says where, and a
click turns the API off for the rest of this run.
"""
from __future__ import annotations

import asyncio

from PySide6.QtWidgets import QToolButton


class LocalApiIndicator(QToolButton):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.setObjectName('localApiIndicator')
        self.setAutoRaise(True)
        self.setText(self.tr('Local API on'))
        self._api = api
        self._unsubscribe = api.subscribe(self.refresh)
        self.clicked.connect(self.turnOff)
        self.destroyed.connect(lambda *_: self._unsubscribe())
        self.refresh()

    def refresh(self) -> None:
        active = self._api.active
        self.setVisible(active)
        if active:
            self.setToolTip(self.tr(
                'A program on this PC can work on the open case through {0}. '
                'Click to turn the local API off for this run.').format(
                    self._api.endpoint))

    def turnOff(self) -> None:
        asyncio.ensure_future(self._api.stop())


def install(window, api) -> LocalApiIndicator:
    indicator = LocalApiIndicator(api, window)
    window.statusBar().addPermanentWidget(indicator)
    return indicator
