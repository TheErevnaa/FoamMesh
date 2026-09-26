"""What the last run did, said inline instead of in a modal (Plan 30 WP-08).

F-09/F-22. A finished mesh run raised ``QMessageBox.information`` -- "Run
gmsh-2026-09-05 completed." -- over the viewport that was at that moment
drawing the mesh it was talking about. The user had to dismiss the sentence to
see the thing the sentence was about, and the run id it named appears nowhere
else in the interface. A preview of a pending edit was announced the same way.

This is the replacement: one line under the step page, in the bottom bar,
carrying the same words without taking the window away. It also carries the
Cancel for a run that is still going, because the surface that says a run is
happening is the surface a user looks at to stop it.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from foammesh.view.theming.metrics import GAP, apply_bar_metrics

from .run_narration import log_label


class RunStatusStrip(QFrame):
    """Running / finished / failed, in one line, never modal."""

    #: The user asked to stop whatever is running.
    cancelRequested = Signal()
    #: The user took up the offer the strip was showing -- CP-02: a run that
    #: produced no mesh clears the viewport, and the previous accepted result
    #: is put back only when the user asks for it, by name.
    offerAccepted = Signal()
    #: The user asked to see the log of the run this line is about. Carries
    #: the path, because the strip does not know how this build opens files.
    logRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('runStatusStrip')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAccessibleName(self.tr('Run status'))
        self._state = 'idle'

        layout = QHBoxLayout(self)
        # DP-191. The shell's bars share one inset, measured from the
        # bar's own edge, so their content starts at one x.
        apply_bar_metrics(self, layout)
        layout.setSpacing(GAP)
        self._text = QLabel(self)
        self._text.setObjectName('runStatusText')
        self._text.setWordWrap(True)
        self._text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._cancel = QPushButton(self.tr('Cancel'), self)
        self._cancel.setObjectName('runStatusCancel')
        # DP-186. The name a screen reader announces has to contain the
        # words painted on the button, or voice control cannot reach it:
        # `click Cancel` matched nothing while the name read `Stop the
        # running job`. The button's own text is its name; what it does
        # is a description, which is where the rest of the app puts it.
        self._cancel.setAccessibleDescription(
            self.tr('Stop the running job.'))
        self._cancel.clicked.connect(self._onCancel)
        self._offer = QPushButton(self)
        self._offer.setObjectName('runStatusOffer')
        self._offer.clicked.connect(self._onOffer)
        # Plan 31 CP-07 item 6. The log path was in the run payload and on no
        # surface: a user whose parallel run failed was left to find it under
        # the case directory, which is where the processor* directories are --
        # the internals this work package exists to stop making them read.
        self._log = QPushButton(self.tr('Show log'), self)
        self._log.setObjectName('runStatusLog')
        self._log.setAccessibleDescription(
            self.tr('Open the log of this run.'))
        self._log.setFlat(True)
        self._log.clicked.connect(self._onLog)
        self._logPath = ''
        layout.addWidget(self._text, 1)
        layout.addWidget(self._offer)
        layout.addWidget(self._log)
        layout.addWidget(self._cancel)
        self.clear()

    # -- state ------------------------------------------------------------- #

    @property
    def state(self) -> str:
        return self._state

    def text(self) -> str:
        return self._text.text()

    def cancelVisible(self) -> bool:
        return self._cancel.isVisible()

    def offerVisible(self) -> bool:
        return self._offer.isVisible()

    def offerText(self) -> str:
        return self._offer.text()

    def logVisible(self) -> bool:
        return self._log.isVisible()

    def logPath(self) -> str:
        return self._logPath

    def clear(self) -> None:
        self._state = 'idle'
        self._render('', '')
        self._cancel.setVisible(False)
        self._offer.setVisible(False)
        self._setLog('')
        self.hide()

    def showRunning(self, message: str, *, cancelable: bool = True,
                    log: str = '') -> None:
        """A run is under way. Cancel belongs here while it is."""
        self._state = 'running'
        self._render(message, '')
        self._cancel.setEnabled(True)
        self._cancel.setText(self.tr('Cancel'))
        self._cancel.setVisible(bool(cancelable))
        self._offer.setVisible(False)
        self._setLog(log)
        self.show()

    def showResult(self, message: str, *, failed: bool = False,
                   log: str = '') -> None:
        """A run finished. The verdict is text; the mesh is already drawn."""
        self._state = 'failed' if failed else 'finished'
        self._render(message, 'error' if failed else '')
        self._cancel.setVisible(False)
        self._offer.setVisible(False)
        self._setLog(log)
        self.setVisible(bool(message))

    def showOffer(self, message: str, action: str, *,
                  failed: bool = True, log: str = '') -> None:
        """Say what happened, and put one named alternative next to it.

        CP-02. A run that produced no mesh clears the viewport -- leaving the
        previous mesh up under this run's verdict is the F-37 defect. But an
        empty viewport is not the whole truth either when the case still holds
        an earlier accepted mesh, so that mesh is offered here by name, and
        drawn only if the user picks it. Never silently, and never as the
        failed run's own result.
        """
        self._state = 'offer'
        self._render(message, 'error' if failed else '')
        self._cancel.setVisible(False)
        self._offer.setText(action)
        self._offer.setAccessibleName(action)
        self._offer.setVisible(bool(action))
        # DP-133. An offer is not always a failure now -- a run that finished
        # can offer its surface pass -- and a finished run's log has to
        # survive the offer replacing the result line. Defaults to '', which
        # is what the CP-02 caller passed by hand.
        self._setLog(log)
        self.setVisible(bool(message))

    def markCancelling(self) -> None:
        self._state = 'cancelling'
        self._cancel.setEnabled(False)
        self._cancel.setText(self.tr('Cancelling…'))

    # -- internals --------------------------------------------------------- #

    def _onCancel(self) -> None:
        self.markCancelling()
        self.cancelRequested.emit()

    def _onOffer(self) -> None:
        self._offer.setVisible(False)
        self.offerAccepted.emit()

    def _onLog(self) -> None:
        if self._logPath:
            self.logRequested.emit(self._logPath)

    def _setLog(self, log: str) -> None:
        """Offer the log only when there is one, and name the file on it.

        A permanently visible `Show log` that does nothing half the time is
        worse than no button: it teaches the user the product is broken.
        """
        self._logPath = str(log or '')
        self._log.setText(self.tr(log_label(self._logPath)))
        self._log.setVisible(bool(self._logPath))

    def _render(self, message: str, role: str) -> None:
        self._text.setText(message)
        self._text.setAccessibleName(message)
        # Colour is reinforcement only: the sentence carries the meaning, and
        # `foammeshStatus` keeps the shade inside the theme tokens.
        if self._text.property('foammeshStatus') != role:
            self._text.setProperty('foammeshStatus', role)
            style = self._text.style()
            if style is not None:
                style.unpolish(self._text)
                style.polish(self._text)
