"""A segmented control: two to four mutually exclusive choices in one row.

Plan 44 MF14 (mockup ``.seg``). A radio button with an indicator circle takes
a line per answer and says "this is a form"; a segmented control says "pick
one" in one row. The control is a framed row of checkable buttons; the chosen
one is filled with ``selection.background`` (``foammeshRole="segment"`` in
``forms.qss.tmpl``).

Q8 (decided 2026-10-14): **no radio-id remap.** The choices this control
replaces were ``QRadioButton`` widgets with object names the facade, the
tests and the tool register know. Each option therefore keeps its radio as a
*hidden alias*: the same object, the same object name, the same ``toggled``
signal. Choosing a segment checks the alias; checking the alias (a test, a
facade call, ``setChecked``) moves the segment. Nothing that addressed the
radio notices the swap.

Arrow keys move the selection (Left/Up to the previous option, Right/Down to
the next), as they do in a radio group.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QPushButton,
                               QRadioButton, QSizePolicy, QWidget)

from foammesh.view.theming.metrics import FRAME_WIDTH

SEGMENT_ROLE = 'segment'
GROUP_ROLE = 'segmentGroup'


class _Segment(QPushButton):
    """A segment that gives its width back rather than widen the sheet.

    A push button's minimum is its whole text; a long answer in a 300 px
    column would make the page scroll sideways. The segment shrinks and
    elides its text, keeping the whole text in the tooltip.
    """

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self._full = text

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), 48))
        return hint

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        room = max(0, self.width() - 16)
        text = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, room)
        if text != self.text():
            self.setText(text)
        if text != self._full and not self.toolTip():
            self.setToolTip(self._full)


class SegmentedControl(QWidget):
    """A framed row of exclusive choices (see module doc)."""

    #: The index of the chosen option (-1 when none is).
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty('foammeshRole', GROUP_ROLE)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self._row = QHBoxLayout(self)
        # The frame is 1 px; the segments sit inside it with no gap so the
        # separators are the segments' own left borders.
        self._row.setContentsMargins(
            FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH)
        self._row.setSpacing(0)
        self._segments: list[QPushButton] = []
        self._aliases: list[QAbstractButton | None] = []
        self._keys: list[object] = []
        self._guard = False

    # -- building ----------------------------------------------------------- #

    def addOption(self, text: str, key=None, alias: QAbstractButton | None =
                  None, tooltip: str = '', description: str = '',
                  object_name: str = '') -> QPushButton:
        """Add one choice; ``alias`` is the radio it stands in for.

        The alias keeps its object name and becomes a hidden child of this
        control, so ``findChild(QRadioButton, name)`` still finds it.
        """
        index = len(self._segments)
        segment = _Segment(text, self)
        segment.setCheckable(True)
        segment.setAutoExclusive(False)
        segment.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        segment.setProperty('foammeshRole', SEGMENT_ROLE)
        segment.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
        name = object_name or (alias.objectName() + 'Segment'
                               if alias is not None and alias.objectName()
                               else '')
        if name:
            segment.setObjectName(name)
        if tooltip or (alias is not None and alias.toolTip()):
            segment.setToolTip(tooltip or alias.toolTip())
        description = description or (
            alias.accessibleDescription() if alias is not None else '')
        if description:
            segment.setAccessibleDescription(description)
        segment.setAccessibleName(text)
        self._row.addWidget(segment, 1)
        self._segments.append(segment)
        self._aliases.append(alias)
        self._keys.append(key if key is not None else index)
        segment.toggled.connect(
            lambda checked, i=index: self._segment_toggled(i, checked))
        if alias is not None:
            alias.setParent(self)
            alias.hide()
            if not isinstance(alias, QRadioButton):
                alias.setCheckable(True)
            alias.toggled.connect(
                lambda checked, i=index: self._alias_toggled(i, checked))
            if alias.isChecked():
                self._set_checked(index)
        self._refresh_positions()
        return segment

    def _refresh_positions(self) -> None:
        last = len(self._segments) - 1
        for index, segment in enumerate(self._segments):
            position = ('only' if last == 0 else 'first' if index == 0
                        else 'last' if index == last else 'middle')
            segment.setProperty('foammeshSegment', position)
            style = segment.style()
            style.unpolish(segment)
            style.polish(segment)

    # -- state -------------------------------------------------------------- #

    def count(self) -> int:
        return len(self._segments)

    def segments(self) -> list:
        return list(self._segments)

    def aliases(self) -> list:
        return [alias for alias in self._aliases if alias is not None]

    def currentIndex(self) -> int:
        for index, segment in enumerate(self._segments):
            if segment.isChecked():
                return index
        return -1

    def currentKey(self):
        index = self.currentIndex()
        return self._keys[index] if index >= 0 else None

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < len(self._segments):
            self._set_checked(index)
            alias = self._aliases[index]
            if alias is not None and not alias.isChecked():
                alias.setChecked(True)

    def setCurrentKey(self, key) -> None:
        if key in self._keys:
            self.setCurrentIndex(self._keys.index(key))

    def _set_checked(self, index: int) -> None:
        self._guard = True
        try:
            for i, segment in enumerate(self._segments):
                segment.setChecked(i == index)
        finally:
            self._guard = False

    def _segment_toggled(self, index: int, checked: bool) -> None:
        if self._guard:
            return
        if not checked:
            # An exclusive row never ends with none chosen: pressing the
            # chosen one again keeps it.
            if self.currentIndex() < 0:
                self._set_checked(index)
            return
        self.setCurrentIndex(index)
        self.currentIndexChanged.emit(index)

    def _alias_toggled(self, index: int, checked: bool) -> None:
        if self._guard or not checked:
            return
        if not self._segments[index].isChecked():
            self._set_checked(index)
            self.currentIndexChanged.emit(index)

    # -- keyboard ----------------------------------------------------------- #

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1,
                Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1}.get(event.key())
        if step is None or not self._segments:
            super().keyPressEvent(event)
            return
        current = self.currentIndex()
        target = max(0, min(len(self._segments) - 1,
                            (current if current >= 0 else 0) + step))
        if target != current:
            self.setCurrentIndex(target)
            self.currentIndexChanged.emit(target)
        event.accept()


def segmented_from_radios(parent, radios, *, object_name: str = ''
                          ) -> SegmentedControl:
    """Build a control that stands in for existing radios, in order."""
    control = SegmentedControl(parent)
    if object_name:
        control.setObjectName(object_name)
    for radio in radios:
        control.addOption(radio.text(), alias=radio)
    return control


__all__ = ['SegmentedControl', 'segmented_from_radios', 'SEGMENT_ROLE',
           'GROUP_ROLE']
