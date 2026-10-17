#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The small things drawn over the picture (Plan 39 VI15).

* ``ProgressStrip`` -- a load, an export or a quality measurement along the top
  of the view: job name, stage, elapsed time and Cancel. The view keeps
  turning (I1). It shows the progress dialogs that asked for the viewport;
  the status bar mirrors the same list.
* ``CameraPresetBar`` -- Measure and a ``...`` menu holding Copy view and Save
  view (I5, I3, I8). The preset buttons are gone (Plan 43 LY6): the toolbar's
  Standard views and the cube say the same.
* ``HoverTip`` -- the cell or face under the cursor (I2).

None of them places itself: ``RenderingWidget.registerOverlay`` puts them in
the overlay layout's slots.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QWidget)

from foammesh.view.theming.metrics import (
    BAR_INSET_H, BAR_INSET_V, GAP_TIGHT, MARGIN_TIGHT)
from foammesh.view.theming.status_colors import style_floating_panel, style_progress_strip

STRIP_HEIGHT = 30
#: Plan 39 VI14d. How long after a task starts or ends the strip is redrawn.
DEFER_STRIP_MS = 4
HOVER_OFFSET = QPoint(16, 18)


class ProgressStrip(QFrame):
    """A thin strip along the top of the view for the jobs that asked for it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from widgets.progress_dialog import localTasks

        self.setObjectName('viewportProgressStrip')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(STRIP_HEIGHT)
        style_progress_strip(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(MARGIN_TIGHT * 2, 0, MARGIN_TIGHT * 2, 0)
        layout.setSpacing(GAP_TIGHT * 2)
        self._label = QLabel()
        self._label.setObjectName('viewportProgressLabel')
        self._elapsed = QLabel()
        self._elapsed.setObjectName('viewportProgressElapsed')
        self._cancel = QPushButton(self.tr('Cancel'))
        self._cancel.setObjectName('viewportProgressCancel')
        self._cancel.setToolTip(self.tr('Stop this job'))
        layout.addWidget(self._label, 1)
        layout.addWidget(self._elapsed)
        layout.addWidget(self._cancel)
        self._cancel.clicked.connect(self._cancelCurrent)
        self._tasks = localTasks()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        # Plan 39 VI14d. Answered a few milliseconds after the task list
        # changes, not inside the emit: the strip, the status bar and the
        # task list each cost 10 to 15 ms and ran as one 40 ms hold.
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(DEFER_STRIP_MS)
        self._later.timeout.connect(self.refresh)
        self._tasks.changed.connect(self._later.start)
        #: Plan 44 MF3 (U7). The main window carries the run and progress
        #: strip in its status strip, and asks this copy to stay off the view.
        self._suppressed = False
        self._tasks.setStrip(self)
        self.destroyed.connect(lambda *_a, t=self._tasks: t.clearStrip())
        self.refresh()

    def dialog(self):
        return self._tasks.current(onViewport=True)

    def labelText(self) -> str:
        return self._label.text()

    def cancelButton(self) -> QPushButton:
        return self._cancel

    def _cancelCurrent(self):
        dialog = self.dialog()
        if dialog is not None:
            dialog.requestCancel()

    def setSuppressed(self, suppressed: bool) -> None:
        """Keep the strip off the view (the status strip shows the task and
        its Cancel instead); the progress dialogs themselves are unchanged."""
        self._suppressed = bool(suppressed)
        self.refresh()

    def isSuppressed(self) -> bool:
        return self._suppressed

    def refresh(self):
        dialog = self.dialog()
        if dialog is None or self._suppressed:
            self._timer.stop()
            self.hide()
            return
        text = dialog.labelText()
        title = dialog.windowTitle()
        self._label.setText('{0} — {1}'.format(title, text) if text
                            else title)
        seconds = max(0, int(time.monotonic() - dialog.openedAt()))
        self._elapsed.setText('{0:d}:{1:02d}'.format(*divmod(seconds, 60)))
        self._cancel.setVisible(dialog.isCancelable())
        self._cancel.setEnabled(dialog.isCancelable() and not dialog.isCanceled())
        if not self._timer.isActive():
            self._timer.start()
        self.show()
        self.raise_()


class CameraPresetBar(QFrame):
    """Measure, and a menu for the view capture, on the overlay.

    Plan 43 LY6. The +X ... Isometric buttons that stood here were a second
    copy of the toolbar's Standard views (Alt+1..7) and the corner cube, and
    are gone (register VP-160, merged). Measure and the "..." menu are the
    top bar's Inspect and Output controls: ``takeTools`` hands them over.
    Until then (no top bar, as in a bare viewport) they stand here.
    """

    copyViewRequested = Signal()
    saveViewRequested = Signal()
    #: Plan 39 VI17 (I8). The Measure tool went on or off.
    measureToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('cameraPresetBar')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        style_floating_panel(self, radius=6)
        self.setAccessibleName(self.tr('Viewport tools'))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT)
        layout.setSpacing(0)
        self._layout = layout
        self._measure = QToolButton()
        self._measure.setObjectName('viewportMeasure')
        self._measure.setText(self.tr('Measure'))
        self._measure.setCheckable(True)
        self._measure.setAutoRaise(True)
        self._measure.setToolTip(self.tr(
            'Click two points for their distance; the cell or face under '
            'each click is measured too'))
        self._measure.setAccessibleName(self.tr('Measure'))
        self._measure.toggled.connect(self.measureToggled)
        layout.addWidget(self._measure)
        self._more = QToolButton()
        self._more.setObjectName('cameraPresetMore')
        self._more.setText('⋯')
        self._more.setAutoRaise(True)
        self._more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more.setToolTip(self.tr('Copy or save the view'))
        self._more.setAccessibleName(self.tr('View capture'))
        menu = QMenu(self._more)
        self._copyAction = menu.addAction(self.tr('Copy view'))
        self._copyAction.triggered.connect(
            lambda _c=False: self.copyViewRequested.emit())
        self._saveAction = menu.addAction(self.tr('Save view…'))
        self._saveAction.triggered.connect(
            lambda _c=False: self.saveViewRequested.emit())
        self._more.setMenu(menu)
        layout.addWidget(self._more)

    def measureButton(self) -> QToolButton:
        return self._measure

    def moreButton(self) -> QToolButton:
        return self._more

    def takeTools(self) -> tuple:
        """Hand Measure and the "..." menu to the top bar (LY6). The same two
        buttons, same names and signals; the empty bar is hidden."""
        for button in (self._measure, self._more):
            self._layout.removeWidget(button)
        self.hide()
        return self._measure, self._more

    def menuActions(self) -> list:
        return [self._copyAction, self._saveAction]


class HoverTip(QFrame):
    """The cell or face under the cursor, a few lines, near the cursor."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName('hoverReadout')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        style_floating_panel(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(BAR_INSET_H, BAR_INSET_V, BAR_INSET_H, BAR_INSET_V)
        self._label = QLabel()
        self._label.setObjectName('hoverReadoutText')
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._label)
        self.hide()

    def text(self) -> str:
        return self._label.text()

    def show_at(self, text: str, x: int, y: int) -> None:
        """Show *text* near (x, y), inside the parent."""
        if not text:
            self.hide()
            self._label.clear()
            return
        self._label.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        room_w = parent.width() if parent is not None else 10 ** 6
        room_h = parent.height() if parent is not None else 10 ** 6
        left = min(x + HOVER_OFFSET.x(), max(0, room_w - self.width() - 4))
        top = min(y + HOVER_OFFSET.y(), max(0, room_h - self.height() - 4))
        self.move(max(0, left), max(0, top))
        self.show()
        self.raise_()


class MeasureReadout(QFrame):
    """The Measure tool's answer, a few lines and Clear, in the bottom right."""

    clearRequested = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName('measureReadout')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        style_floating_panel(self)
        self.setAccessibleName(self.tr('Measure readout'))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(BAR_INSET_H, BAR_INSET_V, BAR_INSET_H, BAR_INSET_V)
        self._label = QLabel()
        self._label.setObjectName('measureReadoutText')
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._clear = QPushButton(self.tr('Clear'))
        self._clear.setObjectName('measureReadoutClear')
        self._clear.clicked.connect(self.clearRequested)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._clear)
        self.hide()

    def text(self) -> str:
        return self._label.text()

    def show_lines(self, lines) -> None:
        lines = list(lines or ())
        if not lines:
            self.hide()
            self._label.clear()
            return
        self._label.setText(chr(10).join(lines))
        self.adjustSize()
        self.show()
        self.raise_()
