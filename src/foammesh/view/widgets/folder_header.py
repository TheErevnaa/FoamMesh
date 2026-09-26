#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import QEvent, QSize
from PySide6.QtWidgets import QCheckBox

from foammesh.view.theming.icons import load_themed_icon

#: DP-150.  The disclosure chevron used to come from the stylesheet, as
#: `image: url(:/icons/chevron-forward.svg)`.  The ionicons carry
#: `stroke:#000`, and a stylesheet `image:` is the one icon path in the
#: application that skips `load_themed_icon`'s recolouring, so on the dark
#: canvas the closed header drew black on `#1e1f22` -- MEASURED as an
#: indicator area with no ink in it at all.  Painting the chevron as the
#: button own icon puts it back on the themed path, where it follows the
#: palette in both themes and in every icon mode.
_CHEVRON_CLOSED = ':/icons/chevron-forward.svg'
_CHEVRON_OPEN = ':/icons/chevron-down.svg'


class FolderHeader(QCheckBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._contents = None

        self._pos = None
        self._size = None

        # Plan 33 section 6 check 2, W-O2. A fold opens open.
        #
        # FORM-01 put that rule in `EngineTaskPage` as one `setChecked(True)`,
        # and every page that inherits from it got the rule for nothing. The
        # Preparation page does not inherit from it. It builds two folds of
        # its own: the wrap section, which had to write the same line out by
        # hand, and the Gmsh import-and-healing section, which did not --
        # MEASURED, twenty-four editors behind a press. A rule that lives in
        # one base class is a rule the next page outside that class misses,
        # so it lives here instead, where every fold in the tree reaches it.
        #
        # A fold that must open closed now says so at its own site, which is
        # where the reason for it belongs. Section 1.1 sanctions two of
        # those -- CAD tessellation and interface pairs on Geometry -- and
        # the folds that hold reports rather than settings say it too.
        self.setChecked(True)
        self.setIconSize(QSize(12, 12))
        self._refreshChevron()
        self.toggled.connect(self._refreshChevron)

    def _refreshChevron(self, *_):
        self.setIcon(load_themed_icon(
            _CHEVRON_OPEN if self.isChecked() else _CHEVRON_CLOSED,
            QSize(64, 64)))

    def changeEvent(self, event):
        super().changeEvent(event)
        # A theme swap replaces the palette and empties the icon cache; the
        # chevron has to be asked for again or it keeps the old theme ink.
        if event.type() == QEvent.Type.PaletteChange:
            self._refreshChevron()

    def setContents(self, widget):
        self._contents = widget
        self._contents.setVisible(self.isChecked())
        self.toggled.connect(self._toggled)

    #
    # def showEvent(self, ev):
    #     if self._size is None:
    #         self._pos = self._contents.pos()
    #         self._size = self._contents.size()
    #         self._contents.setVisible(self.isChecked())
    #
    #         self.toggled.connect(self._toggled)

    def _toggled(self, checked):
        if checked:
            # animation = QPropertyAnimation(self._contents, b'height')
            # animation.setDuration(1000)
            # animation.setEasingCurve(QEasingCurve.Type.Linear)
            # animation.setStartValue(self._contents.geometry())
            # animation.setEndValue(QRect(self._pos.x(), self._pos.y(), self._size.width(), self._size.height()))
            # print(animation.startValue())
            # print(animation.endValue())
            # animation.start(QPropertyAnimation.DeleteWhenStopped)
            self._contents.show()
        else:
            # animation = QPropertyAnimation(self._contents, b'geometry')
            # animation.setDuration(10000)
            # animation.setEasingCurve(QEasingCurve.Type.Linear)
            # animation.setStartValue(self._contents.geometry())
            # animation.setEndValue(QRect(self._contents.x(), self._contents.y(), self._contents.width(), 0))
            # animation.start(QPropertyAnimation.DeleteWhenStopped)
            # print(animation.startValue())
            # print(animation.endValue())
            self._contents.hide()
