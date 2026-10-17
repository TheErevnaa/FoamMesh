#!/usr/bin/env python
# -*- coding: utf-8 -*-


import time

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QWidget
from PySide6.QtCore import QObject, QTimer, Qt, Signal

from foammesh.view.theming.metrics import BAR_INSET_H

from .progress_dialog_ui import Ui_ProgressDialog


#: Plan 39 VI14d. How long after a task starts or ends the status bar follows.
DEFER_STATUS_MS = 14


class _LocalTasks(QObject):
    """The progress dialogs that are up, for the status bar to mirror (F6b).

    A dialog that blocks the window also blocks the status bar's Cancel, so
    the dialogs that do not block register here and the status bar shows the
    newest one with its own Cancel, whichever window the dialog belongs to.
    """

    changed = Signal()

    def __init__(self):
        super().__init__()
        self._dialogs: list = []
        self._strip = None

    def add(self, dialog):
        if dialog not in self._dialogs:
            self._dialogs.append(dialog)
            self.changed.emit()

    def remove(self, dialog):
        if dialog in self._dialogs:
            self._dialogs.remove(dialog)
            self.changed.emit()

    def current(self, onViewport: bool = False):
        """The newest dialog; with ``onViewport`` the newest one the strip shows."""
        for dialog in reversed(self._dialogs):
            if not onViewport or dialog.isOnViewport():
                return dialog
        return None

    # Plan 39 VI15 (I1). The progress strip along the top of the viewport
    # shows the dialogs that asked to be there; with one present they are not
    # opened as windows at all.
    def setStrip(self, strip):
        self._strip = strip

    def clearStrip(self):
        self._strip = None

    def stripPresent(self) -> bool:
        return self._strip is not None

    def count(self) -> int:
        return len(self._dialogs)


_tasks = None


def localTasks() -> _LocalTasks:
    global _tasks
    if _tasks is None:
        _tasks = _LocalTasks()
    return _tasks


class LocalTaskStatus(QWidget):
    """Status-bar mirror of the newest non-modal progress dialog.

    Name and stage, elapsed time and the dialog's own Cancel. It never does the
    work of cancelling: it asks the dialog, which asks its owner.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('localTaskStatus')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(BAR_INSET_H, 0, BAR_INSET_H, 0)
        self._label = QLabel()
        self._elapsed = QLabel()
        self._cancel = QPushButton(self.tr('Cancel'))
        for widget in (self._label, self._elapsed, self._cancel):
            layout.addWidget(widget)
        self._cancel.clicked.connect(self._cancelCurrent)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        # Plan 39 VI14d. Redrawn a few milliseconds after the viewport strip,
        # not in the same emit (see ProgressStrip).
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(DEFER_STATUS_MS)
        self._later.timeout.connect(self.refresh)
        localTasks().changed.connect(self._later.start)
        self.refresh()

    def _cancelCurrent(self):
        dialog = localTasks().current()
        if dialog is not None:
            dialog.requestCancel()

    def refresh(self):
        dialog = localTasks().current()
        if dialog is None:
            self._timer.stop()
            self.hide()
            return
        title = dialog.windowTitle()
        text = dialog.labelText()
        self._label.setText('{0} — {1}'.format(title, text) if text else title)
        seconds = max(0, int(time.monotonic() - dialog.openedAt()))
        self._elapsed.setText('{0:d}:{1:02d}'.format(*divmod(seconds, 60)))
        self._cancel.setVisible(dialog.isCancelable())
        self._cancel.setEnabled(dialog.isCancelable() and not dialog.isCanceled())
        if not self._timer.isActive():
            self._timer.start()
        self.show()


def _installMirror(parent):
    """Put the status-bar mirror on the window the dialog belongs to, once."""
    try:
        window = parent.window() if parent is not None else None
        statusBar = getattr(window, 'statusBar', None)
        if not callable(statusBar) or getattr(window, '_localTaskStatus', None):
            return
        mirror = LocalTaskStatus(window)
        statusBar().addPermanentWidget(mirror)
        window._localTaskStatus = mirror
    except (AttributeError, RuntimeError):
        return


class ProgressDialog(QDialog):
    """A working-on-it window.

    Plan 39 VI1 (F6b). It does not block the application: the view keeps
    turning behind it, and the status bar mirrors it with its own Cancel, so
    neither is dead while a load or an export runs. ``modal=True`` keeps the
    window-modal behaviour for the run dialogs that must hold the page.
    """

    cancelClicked = Signal()

    def __init__(self, parent, title: str, cancelable: bool = False, autoCloseOnCancel: bool = True, openDelay: int = 0,
                 modal: bool = False, onViewport: bool = False):
        super().__init__(parent)
        self._ui = Ui_ProgressDialog()
        self._ui.setupUi(self)

        self._autoCloseOnCancel = autoCloseOnCancel
        self._openDelay = openDelay
        self._modal = bool(modal)
        self._onViewport = bool(onViewport) and not modal
        self._cancelable = bool(cancelable)
        self._openedAt = time.monotonic()
        self._registered = False

        self._canceled = False
        self._isOpen = False

        self._timer: QTimer = None

        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)

        self._ui.button.setVisible(cancelable)

        # F6. This dialog's bar is the sweeping busy kind, which needs a
        # square chunk to read as a bar at all; the rule lives with the rest
        # of the theme, under `ProgressDialog QProgressBar::chunk`.

        self._ui.button.clicked.connect(self._cancelClicked)

    def isCanceled(self):
        return self._canceled

    def isCancelable(self) -> bool:
        return self._cancelable

    def isModalDialog(self) -> bool:
        return self._modal

    def isOnViewport(self) -> bool:
        """Whether the progress strip on the viewport shows this job."""
        return self._onViewport

    def isInStrip(self) -> bool:
        """True while the strip is showing this job and no window is."""
        return (self._onViewport and localTasks().stripPresent()
                and self._registered and not self.isVisible())

    def openedAt(self) -> float:
        return self._openedAt

    def labelText(self) -> str:
        return self._ui.label.text()

    def setLabelText(self, text: str):
        self._ui.label.setText(text)
        if self._registered:
            localTasks().changed.emit()

    def requestCancel(self):
        """What the status bar's Cancel presses: the dialog's own Cancel."""
        if self._cancelable and self._ui.button.isEnabled() and not self._canceled:
            self._cancelClicked()

    def showCancelButton(self, text='Cancel'):
        self._cancelable = True
        self._ui.button.setText(text)
        self._ui.button.setVisible(True)

    def hideCancelButton(self):
        self._cancelable = False
        self._ui.button.setVisible(False)

    def _show(self):
        """Show, without blocking the rest of the application unless asked."""
        if self._modal:
            super().open()
        elif self._onViewport and localTasks().stripPresent():
            # The strip on the viewport shows it; no window to cover the view.
            self._register()
        else:
            self.setWindowModality(Qt.WindowModality.NonModal)
            self.show()
            self._register()

    def _register(self):
        if not self._registered:
            self._registered = True
            self._openedAt = time.monotonic()
            _installMirror(self.parentWidget())
            localTasks().add(self)

    def _unregister(self):
        if self._registered:
            self._registered = False
            localTasks().remove(self)

    def abort(self, text: str):
        if not self._isOpen:
            self._show()

        self.finish(text)

    def finish(self, text: str):
        self._clearTimer()
        self._unregister()
        if not self.isVisible():
            # A job that ran in the strip ends with something to say (an
            # error, a refusal): that is a window, not a flicker of the strip.
            self.setWindowModality(Qt.WindowModality.NonModal)
            self.show()

        self._ui.label.setText(text)
        self._ui.button.setText(self.tr('Close'))
        self._ui.progressBar.hide()
        self._ui.button.setEnabled(True)
        self._ui.button.show()

        self._ui.button.clicked.connect(self.close)

    def open(self):
        if self._openDelay > 0:
            if self._timer is None:
                self._timer = QTimer()
                self._timer.setInterval(self._openDelay)
                self._timer.setSingleShot(True)
                self._timer.timeout.connect(self._timeout)
                self._timer.start()
        else:
            self._show()
            self._isOpen = True

    def _timeout(self):
        self._show()
        self._isOpen = True

    def close(self):
        self._clearTimer()
        self._unregister()
        super().close()
        self._isOpen = False

    def cancel(self):
        self.close()

    def _cancelClicked(self):
        self._ui.button.setEnabled(False)
        self._canceled = True
        self.cancelClicked.emit()
        if self._registered:
            localTasks().changed.emit()
        if self._autoCloseOnCancel:
            self.close()

    def _clearTimer(self):
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


def closeProgressWindowsOf(window) -> int:
    """Close every progress window that belongs to ``window``; answer how many.

    MEASURED in the guided walks: a finished export window ("Export
    completed", Close) that nobody dismissed was still on screen after the
    main window had closed (``lastWindowClosed fired; still visible:
    ['ProgressDialog']``). The main window calls this as it closes, so no
    progress window outlives the window it belongs to.
    """
    closed = 0
    if window is None:
        return closed
    for dialog in window.findChildren(ProgressDialog):
        try:
            if dialog.isVisible() or dialog._registered:
                dialog.close()
                closed += 1
        except RuntimeError:
            # Deleted by Qt between the search and the close.
            continue
    return closed
