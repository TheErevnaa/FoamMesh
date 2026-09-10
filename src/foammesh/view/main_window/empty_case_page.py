"""Designed main-window state shown before a case is opened.

The page is guidance only. Case lifecycle lives in the File menu, which already
offers New Case, Open Project and Open Recent with their shortcuts; duplicating
two of those three as buttons here gave a second, narrower entry point to the
same actions, so the page points at the menu instead of restating it.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class EmptyCasePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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

        title = QLabel(self.tr('No Case Open'))
        title.setObjectName('emptyCaseTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._message = QLabel()
        self._message.setObjectName('emptyCaseMessage')
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
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

        The import line matters most and is deliberately last, where the eye
        lands: "I have a CAD file" is how most people arrive, and until it
        worked without a case the answer was to go and make an empty folder
        first -- the one folder that does not contain their model.
        """
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
            hint(newAction, self.tr('File → New Case in Folder')),
            hint(openAction, self.tr('File → Open')),
            hint(importAction, self.tr('File → Load Model → Geometry'))))
