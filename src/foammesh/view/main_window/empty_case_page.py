"""Designed main-window state shown before a case is opened.

The welcome panel (Plan 40 UXA8, #27). It says where the File menu keeps case
lifecycle and also offers the four ways in as buttons -- New case, Open case,
Open recent and Open example -- because a first-time user looks at the window,
not at a menu. The buttons are the same actions the menu runs: this page only
raises a signal and the window does the work, so there is still one backend
for each.
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QMenu, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from foammesh.view.theming.metrics import apply_prose_measure


class EmptyCasePage(QWidget):
    newRequested = Signal()
    openRequested = Signal()
    recentSelected = Signal(str)
    exampleSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recentsProvider = lambda: []
        self._examplesProvider = None
        # DP-216. The actions are held so the sentence can be written
        # again whenever one of them changes. The window wires this page
        # fifty lines before it installs its shortcuts, so a key read at
        # wiring time is always empty; holding the actions is what makes
        # that order stop mattering.
        self._actions = (None, None, None)
        layout = QVBoxLayout(self)
        # R3. This layout carried `setAlignment(AlignCenter)`, which makes a
        # QVBoxLayout take only its own size hint and sit centred in the
        # widget. A word-wrapped QLabel answers `heightForWidth`, and an
        # aligned layout never asks: the message was given the height of the
        # hint it had at its *unwrapped* width, so the last line was cut off
        # and the first thing a new user read ended mid-sentence at
        # "...and ask where to keep the case once you have". Stretches centre
        # the block without taking height-for-width away from the label.
        layout.addStretch(1)

        # DP-220. The heading and the guidance are ranged left against one
        # edge and held to one measure. Centred, the guidance started every
        # line at a different place and ran the full width of the pane, so
        # on a wide window the first thing a new user read was a single
        # line more than a hundred characters long with a ragged left side.
        ranged = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        title = QLabel(self.tr('No case open'))
        title.setObjectName('emptyCaseTitle')
        title.setAlignment(ranged)

        self._message = QLabel()
        self._message.setObjectName('emptyCaseMessage')
        self._message.setAlignment(ranged)
        self._message.setWordWrap(True)
        apply_prose_measure(self._message)
        # Vertical `Minimum` means "never shorter than the wrapped text";
        # without it the stretches above and below can squeeze it again.
        self._message.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.setGuidance()

        layout.addWidget(title)
        layout.addWidget(self._message)
        layout.addSpacing(12)
        self._buildButtons(layout)
        layout.addStretch(1)

    def _buildButtons(self, layout):
        left = Qt.AlignmentFlag.AlignLeft

        def push(name, text, tip, signal):
            button = QPushButton(text, self)
            button.setObjectName(name)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False: signal.emit())
            layout.addWidget(button, 0, left)
            return button

        def menu(name, text, tip, fill):
            button = QToolButton(self)
            button.setObjectName(name)
            button.setText(text)
            button.setToolTip(tip)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            popup = QMenu(button)
            popup.setToolTipsVisible(True)
            popup.aboutToShow.connect(lambda: fill(popup))
            button.setMenu(popup)
            layout.addWidget(button, 0, left)
            return button

        self._newButton = push(
            'welcomeNewButton', self.tr('New case…'),
            self.tr('Make a FoamMesh case in a folder you choose'),
            self.newRequested)
        self._newButton.setProperty('foammeshRole', 'primary')
        self._openButton = push(
            'welcomeOpenButton', self.tr('Open case…'),
            self.tr('Open an existing FoamMesh or OpenFOAM case'),
            self.openRequested)
        self._recentButton = menu(
            'welcomeRecentButton', self.tr('Open recent'),
            self.tr('Reopen a case you worked on lately'),
            self._fillRecents)
        self._exampleButton = menu(
            'welcomeExampleButton', self.tr('Open example'),
            self.tr('Start from a model that ships with FoamMesh'),
            self._fillExamples)

    def setRecentsProvider(self, provider):
        """`provider()` answers the recent cases, newest first, as records
        with a `path` (and optionally a `display_name`) or as bare paths."""
        self._recentsProvider = provider

    def setExamplesProvider(self, provider):
        """`provider()` answers objects with `name`, `title` and `summary`."""
        self._examplesProvider = provider

    def _fillRecents(self, popup):
        popup.clear()
        records = [r if isinstance(r, dict) else {'path': str(r)}
                   for r in (self._recentsProvider() or [])]
        if not records:
            popup.addAction(self.tr('No recent cases')).setEnabled(False)
            return
        for record in records:
            path = str(record['path'])
            action = popup.addAction(
                str(record.get('display_name') or Path(path).name or path))
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self.recentSelected.emit(p))

    def _fillExamples(self, popup):
        popup.clear()
        provider = self._examplesProvider
        if provider is None:
            from foammesh.core.examples import list_examples as provider
        for example in provider():
            action = popup.addAction(example.title)
            action.setToolTip(example.summary)
            action.triggered.connect(
                lambda _checked=False, n=example.name:
                self.exampleSelected.emit(n))

    def setGuidance(self, newAction=None, openAction=None,
                    importAction=None):
        """Point at the File menu, quoting the actions' own shortcuts.

        The shortcuts are read from the real QActions rather than restated, so
        this text cannot drift from the menu contract.

        DP-216. Reading them once was not enough to keep that promise.
        The window wires this page fifty lines before it installs its
        shortcuts, so every key was read while the action still carried
        none, and the sentence went out with no keys in it at all. The
        actions are followed instead of read once.

        The import line matters most and is deliberately last, where the eye
        lands: "I have a CAD file" is how most people arrive, and until it
        worked without a case the answer was to go and make an empty folder
        first -- the one folder that does not contain their model.
        """
        for action in set(a for a in self._actions if a is not None):
            action.changed.disconnect(self._refreshGuidance)
        self._actions = (newAction, openAction, importAction)
        for action in set(a for a in self._actions if a is not None):
            action.changed.connect(self._refreshGuidance)
        self._refreshGuidance()

    def _refreshGuidance(self):
        """Write the sentence from what the actions carry right now."""
        newAction, openAction, importAction = self._actions

        def hint(action, fallback):
            if action is None:
                return fallback
            shortcut = action.shortcut().toString()
            return f'{fallback} ({shortcut})' if shortcut else fallback

        self._message.setText(self.tr(
            'Use {0} to create a FoamMesh case, or {1} to open an existing '
            'OpenFOAM case.\n\n'
            'Or just {2} — FoamMesh will start in a temporary folder and ask '
            'where to keep the case once you have something worth saving.'
        ).format(
            hint(newAction, self.tr('File → New case in folder')),
            hint(openAction, self.tr('File → Open')),
            hint(importAction, self.tr('File → Import → Geometry'))))
