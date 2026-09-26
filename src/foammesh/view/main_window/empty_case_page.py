"""Designed main-window state shown before a case is opened.

The page is guidance only. Case lifecycle lives in the File menu, which already
offers New case, Open Project and Open recent with their shortcuts; duplicating
two of those three as buttons here gave a second, narrower entry point to the
same actions, so the page points at the menu instead of restating it.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from foammesh.view.theming.metrics import apply_prose_measure


class EmptyCasePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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
        layout.addStretch(1)

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
