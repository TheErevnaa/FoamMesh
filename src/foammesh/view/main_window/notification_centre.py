"""The notification centre: a bell in the status bar (Plan 40 UXC5, F6d).

A sentence put on the status bar used to run the full width of the window and
vanish after a few seconds, so a warning that appeared while the user looked
elsewhere was simply lost. Now every timed status sentence is also kept as an
entry. The bell in the status bar counts the ones not yet seen; its popup lists
them newest first, each with a *Go to* that stands on the step the sentence was
about, and the sentence on the bar itself is cut to a readable length.

Modal boxes are untouched: they stay for decisions (overwrite, discard, quit).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QStatusBar, QToolButton, QVBoxLayout, QWidget)
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT
from foammesh.view.theming.status_colors import set_bold_text

#: Longest sentence the bar itself shows, as a share of the window width.
BAR_SHARE = 0.55
#: Entries kept; the oldest go first.
MAX_ENTRIES = 200
#: Plan 44 MF3. The status strip is 24 px; the run bar in it is at most 160 px
#: wide and 4 px tall; the Notifications chip is no taller than 18 px.
STATUS_HEIGHT = 24
RUN_BAR_WIDTH = 160
RUN_BAR_HEIGHT = 4
CHIP_HEIGHT = 18

_WARNING_WORDS = (
    'warning', 'cannot', "can't", 'could not', "couldn't", 'failed', 'fail',
    'refused', 'not ', 'no ', 'unavailable', 'error', 'missing', 'skipped',
    'stopped', 'cancelled', 'canceled', 'lost', 'unable')


def classify(text: str) -> str:
    """``'warning'`` for a sentence that reports a problem, else ``'info'``."""
    low = text.lower()
    return 'warning' if any(word in low for word in _WARNING_WORDS) else 'info'


@dataclass
class Notification:
    text: str
    severity: str
    when: float
    #: What *Go to* stands on: a workflow step, and a branch route token.
    target: Optional[tuple] = None
    count: int = 1
    seen: bool = False
    id: int = field(default=0)


class NotificationLog(QObject):
    """The entries, newest last. Pure model: no widgets."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[Notification] = []
        self._next = 1

    def entries(self) -> list[Notification]:
        return list(self._entries)

    def unseen(self) -> int:
        return sum(1 for entry in self._entries if not entry.seen)

    def post(self, text: str, target=None) -> Notification | None:
        text = ' '.join(str(text).split())
        if not text:
            return None
        last = self._entries[-1] if self._entries else None
        if last is not None and last.text == text and last.target == target:
            last.count += 1
            last.when = time.time()
            last.seen = False
            entry = last
        else:
            entry = Notification(text, classify(text), time.time(), target,
                                 id=self._next)
            self._next += 1
            self._entries.append(entry)
            del self._entries[:-MAX_ENTRIES]
        self.changed.emit()
        return entry

    def markSeen(self) -> None:
        if self.unseen():
            for entry in self._entries:
                entry.seen = True
            self.changed.emit()

    def clear(self) -> None:
        if self._entries:
            self._entries.clear()
            self.changed.emit()


class NotificationPanel(QFrame):
    """The popup under the bell."""

    def __init__(self, log: NotificationLog, goTo: Callable, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('notificationPanel')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._log = log
        self._goTo = goTo
        outer = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel(self.tr('Notifications'))
        set_bold_text(title)
        head.addWidget(title, 1)
        self.clearButton = QPushButton(self.tr('Clear all'))
        self.clearButton.setObjectName('notificationClear')
        self.clearButton.clicked.connect(log.clear)
        head.addWidget(self.clearButton)
        outer.addLayout(head)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(GAP_TIGHT)
        holder = QWidget()
        holder.setLayout(self._rows)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        self.setMinimumSize(460, 260)
        log.changed.connect(self.rebuild)
        self.rebuild()

    def rebuild(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        entries = self._log.entries()
        if not entries:
            empty = QLabel(self.tr('Nothing to report.'))
            empty.setObjectName('notificationEmpty')
            self._rows.addWidget(empty)
        for entry in reversed(entries):
            self._rows.addWidget(self._row(entry))
        self._rows.addStretch(1)
        self.clearButton.setEnabled(bool(entries))

    def _row(self, entry: Notification) -> QWidget:
        row = QWidget()
        row.setObjectName('notificationRow')
        row.setProperty('severity', entry.severity)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT)
        stamp = time.strftime('%H:%M:%S', time.localtime(entry.when))
        text = entry.text + (f'  (x{entry.count})' if entry.count > 1 else '')
        label = QLabel(f'{stamp}  {text}')
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        if entry.severity == 'warning':
            set_bold_text(label)
        layout.addWidget(label, 1)
        button = QPushButton(self.tr('Go to'))
        button.setObjectName('notificationGoTo')
        button.setEnabled(entry.target is not None)
        button.clicked.connect(lambda _=False, e=entry: self._go(e))
        layout.addWidget(button)
        return row

    def _go(self, entry: Notification) -> None:
        self.hide()
        self._goTo(entry)


class StatusRunBar(QProgressBar):
    """Plan 44 MF3 (U7). The compact bar of the run host in the status strip.

    The run and progress strip used to stand between the menu and the view;
    the bar, the stage text and the Cancel now live in the status strip, to
    the right of its message. The stage text and the Cancel are the widgets
    they always were (``JobProgressWidget`` for a mesher run,
    ``LocalTaskStatus`` for a load, an export or a measurement); this bar
    stands in front of them and shows exactly while any of them does. A job
    reports no fraction, so it is the busy kind."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('statusRunBar')
        self.setRange(0, 0)
        self.setTextVisible(False)
        self.setFixedHeight(RUN_BAR_HEIGHT)
        self.setMaximumWidth(RUN_BAR_WIDTH)
        self.setMinimumWidth(RUN_BAR_WIDTH // 2)
        self.setAccessibleName(self.tr('Run progress'))
        self._watched: list = []
        self.hide()

    def watch(self, widget: QWidget) -> None:
        """Show this bar while *widget* (a run host) is shown."""
        if widget in self._watched:
            return
        self._watched.append(widget)
        widget.installEventFilter(self)
        self.sync()

    def sync(self) -> None:
        busy = any(not w.isHidden() for w in self._watched)
        if busy != (not self.isHidden()):
            self.setVisible(busy)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide,
                            QEvent.Type.ShowToParent,
                            QEvent.Type.HideToParent):
            self.sync()
        return False


class NotifyStatusBar(QStatusBar):
    """A status bar that keeps what it is told, and shows it short."""

    def __init__(self, window, targetProvider: Callable, goTo: Callable):
        super().__init__(window)
        self._window = window
        self._permanent: list = []
        self._targetProvider = targetProvider
        self.log = NotificationLog(self)
        self.panel = NotificationPanel(self.log, goTo, window)
        self.panel.hide()
        # Plan 44 MF3: a 24 px strip (a top line is drawn by the style sheet).
        self.setFixedHeight(STATUS_HEIGHT)
        self.runBar = StatusRunBar()
        self.addPermanentWidget(self.runBar)
        self.bell = QToolButton()
        self.bell.setObjectName('notificationBell')
        self.bell.setAutoRaise(True)
        # The chip is drawn by the style sheet (1 px border, 10 px of side
        # padding, 3 px radius); it is never taller than 18 px.
        self.bell.setMaximumHeight(CHIP_HEIGHT)
        self.bell.setToolTip(self.tr('Notifications'))
        self.bell.setAccessibleName(self.tr('Notifications'))
        self.bell.clicked.connect(self.showPanel)
        self.addPermanentWidget(self.bell)
        self.log.changed.connect(self._refreshBell)
        self._refreshBell()

    def addPermanentWidget(self, widget, stretch: int = 0) -> None:  # noqa: N802
        """Run hosts (the job progress, a load's mirror) stand left of the
        Notifications chip, which stays the last item; each one also drives
        the run bar."""
        bell = getattr(self, 'bell', None)
        if bell is None or widget is bell:
            self._permanent.append(widget)
            super().addPermanentWidget(widget, stretch)
            return
        index = len(self._permanent) - 1
        self._permanent.insert(index, widget)
        self.insertPermanentWidget(index, widget, stretch)
        self.runBar.watch(widget)

    def _refreshBell(self) -> None:
        unseen = self.log.unseen()
        self.bell.setText(
            self.tr('Notifications') if not unseen
            else self.tr('Notifications ({0})').format(unseen))
        self.bell.setProperty('unseen', unseen)

    def showPanel(self) -> None:
        panel = self.panel
        anchor = self.bell.mapToGlobal(self.bell.rect().topRight())
        panel.adjustSize()
        panel.move(anchor.x() - panel.width(), anchor.y() - panel.height())
        panel.show()
        self.log.markSeen()

    def showMessage(self, text, timeout: int = 0) -> None:  # noqa: N802
        text = str(text)
        if timeout and timeout > 0:
            try:
                target = self._targetProvider()
            except Exception:
                target = None
            self.log.post(text, target)
        super().showMessage(self.shortened(text), timeout)

    def shortened(self, text: str) -> str:
        width = int(max(self._window.width(), 600) * BAR_SHARE)
        flat = ' '.join(text.split())
        return QFontMetrics(self.font()).elidedText(
            flat, Qt.TextElideMode.ElideRight, width)


def install_notification_centre(window, targetProvider, goTo) -> NotifyStatusBar:
    """Swap the designed status bar for the one with a bell.

    ``targetProvider()`` answers where the user is standing (whatever
    ``goTo(entry)`` can return to); both are the window's, not this module's.
    """
    bar = NotifyStatusBar(window, targetProvider, goTo)
    window.setStatusBar(bar)
    window._ui.statusbar = bar
    return bar
