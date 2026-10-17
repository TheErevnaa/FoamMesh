#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QBoxLayout, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox, QSizePolicy,
)


# --------------------------------------------------------------------------- #
# Plan 44 MF26 -- the dialog language (mockup `.snap`, M:333-345)
#
# A sheet dialog is a ``QDialog[foammeshRole="sheetDialog"]``: the raised
# surface, radius 6, padding 10 12 12 with an 8 px gap, a caps section title,
# a 78 px label column and its buttons on the right, secondary then primary.
# The colours, the radius and the button order are the stylesheet's
# (``qss/dialogs.qss.tmpl``); what Qt only takes from code -- the layout
# margins, the caps face and the label column -- is set here, once, for every
# box this module builds and for the dialogs that ask for the look.
# --------------------------------------------------------------------------- #

SHEET_ROLE = 'sheetDialog'
#: The caps title of a box (its window title, restated inside the sheet).
SHEET_TITLE = 'sheetDialogTitle'
#: A caps section title inside a dialog's body.
SHEET_SECTION = 'sheetSectionTitle'
#: The label column of the mockup's `.row` grid.
SHEET_LABEL_WIDTH = 78
#: `.snap` padding 10 12 12, as (left, top, right, bottom).
SHEET_MARGINS = (12, 10, 12, 12)
#: `.snap` gap.
SHEET_GAP = 8


def captionFont():
    """The caps face of a section title (``fonts.caps_font``)."""
    from foammesh.view.theming import fonts, metrics
    return fonts.caps_font(metrics.TYPE_CAPS)


def sectionTitle(text: str, parent=None, objectName: str = SHEET_SECTION) -> QLabel:
    """A caps section title (mockup `.snap .st`)."""
    label = QLabel(text, parent)
    label.setObjectName(objectName)
    label.setFont(captionFont())
    label.setAccessibleName(text)
    return label


def orderButtons(dialog) -> None:
    """Lay every button box of *dialog* out again, secondary then primary.

    ``QDialogButtonBox`` reads its order (the stylesheet's ``button-layout``)
    once, when it is built, and caches the rule it read. A box built before
    its dialog became a sheet -- every ``QMessageBox``, and every ``.ui`` form
    -- would keep the platform order, OK before Cancel, so its rule is read
    again and the buttons laid out again.
    """
    for buttons in dialog.findChildren(QDialogButtonBox):
        style = buttons.style()
        style.unpolish(buttons)
        style.polish(buttons)
        orientation = buttons.orientation()
        # The layout is only rebuilt when the orientation changes.
        buttons.setOrientation(Qt.Orientation.Vertical
                               if orientation == Qt.Orientation.Horizontal
                               else Qt.Orientation.Horizontal)
        buttons.setOrientation(orientation)


def labelGrid(form: QFormLayout) -> None:
    """The 78 px label column of the mockup's `.row`, labels to the left."""
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setHorizontalSpacing(SHEET_GAP)
    form.setVerticalSpacing(SHEET_GAP)
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        label = None if item is None else item.widget()
        if isinstance(label, QLabel):
            # At least the column; never less than the words, or an explicit
            # minimum would let the form squeeze a long label below its text.
            label.setMinimumWidth(max(SHEET_LABEL_WIDTH, label.sizeHint().width()))
            label.setProperty('foammeshSheetLabel', True)


def makeSheet(dialog: QDialog, title: str | None = None) -> QLabel | None:
    """Give *dialog* the sheet look; with *title*, put a caps title on top.

    Returns the title label, or None. A ``QMessageBox`` keeps the grid Qt
    builds for it; :class:`SheetMessageBox` places its own title.
    """
    dialog.setProperty('foammeshRole', SHEET_ROLE)
    layout = dialog.layout()
    label = None
    if layout is not None and not isinstance(dialog, QMessageBox):
        layout.setContentsMargins(*SHEET_MARGINS)
        layout.setSpacing(SHEET_GAP)
        if title is not None and isinstance(layout, QBoxLayout):
            label = sectionTitle(title, dialog, SHEET_TITLE)
            layout.insertWidget(0, label)
    style = dialog.style()
    style.unpolish(dialog)
    style.polish(dialog)
    orderButtons(dialog)
    return label


#: D1 (DP-2662). The narrowest a sheet message box is drawn.
SHEET_BOX_MINIMUM = 460


class SheetMessageBox(QMessageBox):
    """A ``QMessageBox`` in the sheet look, with its title restated in caps.

    Qt rebuilds a message box's grid whenever its texts change, so the caps
    title is put in the grid when the box is shown, after every text is set.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sheetTitle = None
        # D1 (DP-2662). A box as wide as its sentence put "Show Details..." and OK
        # in the left half of a 218 px sheet; the sheet is never narrower than
        # this, so its buttons stand on the right.
        makeSheet(self)

    def sheetTitle(self) -> QLabel:
        if self._sheetTitle is None:
            self._sheetTitle = sectionTitle('', self, SHEET_TITLE)
        return self._sheetTitle

    def placeSheetTitle(self) -> None:
        grid = self.layout()
        title = self.sheetTitle()
        title.setText(self.windowTitle())
        title.setAccessibleName(self.windowTitle())
        title.setVisible(bool(self.windowTitle()))
        if grid is None or grid.indexOf(title) >= 0:
            return
        grid.setContentsMargins(*SHEET_MARGINS)
        # Every cell moves one row down, and the title takes the top row.
        cells = []
        while grid.count():
            index = grid.count() - 1
            cells.append((grid.getItemPosition(index), grid.takeAt(index)))
        for (row, column, rows, columns), item in cells:
            grid.addItem(item, row + 1, column, rows, columns, item.alignment())
        grid.addWidget(title, 0, 0, 1, max(1, grid.columnCount()))
        orderButtons(self)
        # D1 (DP-2662). The button row is not centred in the sheet: it hangs
        # from the right edge.
        for row in self.findChildren(QDialogButtonBox):
            grid.setAlignment(row, Qt.AlignmentFlag.AlignRight)
            for button in row.buttons():
                button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            inner = row.layout()
            if inner is not None and not getattr(row, '_sheetStretch', False):
                inner.insertStretch(0, 1)
                row._sheetStretch = True
        self._markPrimary()

    def _markPrimary(self) -> None:
        """The accept (or Yes) button is the primary one."""
        for button in self.buttons():
            role = self.buttonRole(button)
            if role in (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole):
                button.setProperty('foammeshRole', 'primary')
                button.style().unpolish(button)
                button.style().polish(button)

    def showEvent(self, event):
        self.placeSheetTitle()
        super().showEvent(event)
        # QMessageBox fixes its own size in its showEvent, ignoring
        # setMinimumWidth, so a narrow sheet is widened after it.
        self._holdMinimumWidth()

    def _holdMinimumWidth(self) -> None:
        if self.width() < SHEET_BOX_MINIMUM:
            self.setFixedSize(SHEET_BOX_MINIMUM, self.height())

    def event(self, event):
        result = super().event(event)
        # CHL (DP-2662). The widening above did not hold live: Qt's own
        # ``QMessageBox::event`` re-fixes the size to the sentence on the next
        # LayoutRequest (the stylesheet and the real face arrive after show),
        # back to ~200 px with the buttons in the left half. Win it back after
        # every layout pass.
        if event.type() == QEvent.Type.LayoutRequest and self.isVisible():
            self._holdMinimumWidth()
        return result


class AsyncMessageBox:
    _messageBoxes = set()  # To keep "AsyncQMessageBox" objects created at the same line with "await"

    def __init__(self):
        self._future = None
        self._mbox = None

    def critical(self, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                 defaultButton=QMessageBox.StandardButton.NoButton):
        return self._showMessageBox(parent, QMessageBox.Icon.Critical, title, text,
                                    buttons=buttons, defaultButton=defaultButton)

    def information(self, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                    defaultButton=QMessageBox.StandardButton.NoButton):
        return self._showMessageBox(parent, QMessageBox.Icon.Information, title, text,
                                    buttons=buttons, defaultButton=defaultButton)

    def question(self, parent, title, text, buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                 defaultButton=QMessageBox.StandardButton.NoButton):
        return self._showMessageBox(parent, QMessageBox.Icon.Question, title, text,
                                    buttons=buttons, defaultButton=defaultButton)

    def warning(self, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                defaultButton=QMessageBox.StandardButton.NoButton, *,
                informativeText='', detailedText=''):
        # DP-506. A failure names its log beneath the sentence and keeps the
        # long diagnostic behind Details, rather than in the sentence.
        return self._showMessageBox(parent, QMessageBox.Icon.Warning, title, text,
                                    buttons=buttons, defaultButton=defaultButton,
                                    informativeText=informativeText,
                                    detailedText=detailedText)

    async def confirm(self, parent, title, text, defaultButton=QMessageBox.StandardButton.NoButton):
        return await self.question(parent, title, text, defaultButton=defaultButton) == QMessageBox.StandardButton.Yes

    def _showMessageBox(self, parent, icon, title, text, buttons, defaultButton,
                        informativeText='', detailedText=''):
        self._messageBoxes.add(self)

        loop = asyncio.get_running_loop()
        self._future = loop.create_future()

        self._mbox = SheetMessageBox(icon, title, text, buttons=buttons, parent=parent)
        # Plan 40 UXC7: only a decision takes the window; a notice (OK only,
        # not a question) does not.
        decision = icon == QMessageBox.Icon.Question or not isNotice(buttons)
        self._mbox.setWindowModality(Qt.WindowModality.ApplicationModal if decision
                                     else Qt.WindowModality.NonModal)
        self._mbox.finished.connect(self._finished)
        if informativeText:
            self._mbox.setInformativeText(informativeText)
        if detailedText:
            self._mbox.setDetailedText(detailedText)

        if defaultButton != QMessageBox.StandardButton.NoButton:
            self._mbox.setDefaultButton(defaultButton)

        self._mbox.show()

        return self._future

    def _finished(self, result: int):
        if not self._future.cancelled():
            self._future.set_result(result)

        self._messageBoxes.discard(self)


# --------------------------------------------------------------------------- #
# Plan 40 UXC7 -- the one message helper (F6b)
#
# Every message box of the application is made here.  A *notice* (information,
# warning, critical, with only an OK button) is shown without taking the window:
# it is not application-modal, it does not run a nested event loop, and the
# caller carries on.  A *decision* (a question, or any box offering more than OK)
# blocks, because the answer is read straight away.  Call sites use these
# functions through ``from widgets import async_message_box as messages`` and
# never construct or call ``QMessageBox`` themselves (the census gate in
# ``test_plan40_ux0_gui_first_class_gates_open.py`` fails on a direct call).
# --------------------------------------------------------------------------- #

_OK = QMessageBox.StandardButton.Ok
_NO_BUTTON = QMessageBox.StandardButton.NoButton
_noticeBoxes = set()    # a shown notice is kept alive until the user closes it


def isNotice(buttons) -> bool:
    """True for a box that only says something: just an OK button."""
    return buttons == _OK


def box(*args, **kwargs) -> QMessageBox:
    """A ``QMessageBox`` for the few boxes that build their own buttons.

    Plan 44 MF26: it is a :class:`SheetMessageBox`, in the sheet look.
    """
    return SheetMessageBox(*args, **kwargs)


_KIND = {QMessageBox.Icon.Information: 'information', QMessageBox.Icon.Warning: 'warning',
         QMessageBox.Icon.Critical: 'critical', QMessageBox.Icon.Question: 'question'}
#: The class's own static calls, as Qt ships them.
_STATIC = {kind: getattr(QMessageBox, kind) for kind in _KIND.values()}


def decide(icon, parent, title, text, buttons, defaultButton=_NO_BUTTON):
    """A decision in the sheet look: it blocks until the user answers.

    Plan 44 MF26. What ``QMessageBox.question`` and its siblings did, built
    here so the box is a sheet: the same buttons, the same default (the given
    one, else the first accept button), application-modal (UXC7: a decision
    takes the window), and the button pressed returned as a
    ``StandardButton`` (Cancel when the box closes without one).

    The class's static call stays the seam a decision is answered through:
    when ``QMessageBox.question`` (or a sibling) is not the one Qt ships --
    the unit tests answer the box that way -- it is asked instead.
    """
    kind = _KIND.get(icon)
    answer = getattr(QMessageBox, kind) if kind else None
    if kind and answer is not _STATIC[kind]:
        return answer(parent, title, text, buttons, defaultButton)
    mbox = SheetMessageBox(icon, title, text, _NO_BUTTON, parent)
    mbox.setWindowModality(Qt.WindowModality.ApplicationModal)
    wanted = QMessageBox.StandardButton(buttons).value
    default = QMessageBox.StandardButton(defaultButton)
    mask = QMessageBox.StandardButton.FirstButton.value
    while mask <= QMessageBox.StandardButton.LastButton.value:
        standard = wanted & mask
        mask <<= 1
        if not standard:
            continue
        standard = QMessageBox.StandardButton(standard)
        button = mbox.addButton(standard)
        if mbox.defaultButton() is not None:
            continue
        if ((default == _NO_BUTTON
             and mbox.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole)
                or (default != _NO_BUTTON and standard == default)):
            mbox.setDefaultButton(button)
    if mbox.exec() == -1:
        return QMessageBox.StandardButton.Cancel
    clicked = mbox.clickedButton()
    return (QMessageBox.StandardButton.Cancel if clicked is None
            else mbox.standardButton(clicked))


def showNotice(icon, parent, title, text, *, informativeText='', detailedText=''):
    """Show a notice without blocking: not modal, no nested event loop."""
    mbox = SheetMessageBox(icon, title, text, _OK, parent)
    mbox.setObjectName('noticeBox')
    mbox.setWindowModality(Qt.WindowModality.NonModal)
    mbox.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if informativeText:
        mbox.setInformativeText(informativeText)
    if detailedText:
        mbox.setDetailedText(detailedText)
    _noticeBoxes.add(mbox)
    mbox.finished.connect(lambda *_: _noticeBoxes.discard(mbox))
    mbox.show()
    return _OK


def information(parent, title, text, buttons=_OK, defaultButton=_NO_BUTTON):
    if isNotice(buttons):
        return showNotice(QMessageBox.Icon.Information, parent, title, text)
    return decide(QMessageBox.Icon.Information, parent, title, text, buttons, defaultButton)


def warning(parent, title, text, buttons=_OK, defaultButton=_NO_BUTTON):
    if isNotice(buttons):
        return showNotice(QMessageBox.Icon.Warning, parent, title, text)
    return decide(QMessageBox.Icon.Warning, parent, title, text, buttons, defaultButton)


def critical(parent, title, text, buttons=_OK, defaultButton=_NO_BUTTON):
    if isNotice(buttons):
        return showNotice(QMessageBox.Icon.Critical, parent, title, text)
    return decide(QMessageBox.Icon.Critical, parent, title, text, buttons, defaultButton)


def question(parent, title, text,
             buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
             defaultButton=_NO_BUTTON):
    """A decision: it blocks until the user answers."""
    return decide(QMessageBox.Icon.Question, parent, title, text, buttons, defaultButton)


def reasonOf(error, what: str):
    """``(sentence, details)`` for a caught *error* (DP-1376).

    An ``OSError`` carries the operating system's own wording ("Access is
    denied" and a path); the box says *what* in a sentence and keeps that
    wording behind Details. Any other error the product raised itself, and its
    message is already a sentence for the reader.
    """
    if isinstance(error, OSError):
        return what, str(error)
    return str(error), ''


def error(parent, what, *, why='', todo='', exception=None, title=None):
    """Say what happened, why, and what to do; ``str(exception)`` sits behind Details.

    The sentence is ``what``; ``why`` and ``todo`` follow it beneath, and the raw
    text of the exception is only ever offered in the Details expander.
    """
    informative = '\n'.join(part for part in (why, todo) if part)
    return showNotice(QMessageBox.Icon.Warning, parent, title or 'Something went wrong',
                      what, informativeText=informative,
                      detailedText='' if exception is None else str(exception))
