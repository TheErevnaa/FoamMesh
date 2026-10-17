"""Plan 44 FX2. A run read-out that stands on one line until it is opened.

MEASURED in the MF27 live pass at 1280x800 (sheet 300 px) on two meshed
cases: what a finished run says about itself -- the result strip's counts,
the castellation estimate, the layer shortfall, the "built stage by stage"
advisory, the Gmsh thread note and the run's own warnings -- took three to
six wrapped lines each, and on Castellation, Boundary layers and Gmsh Sizing
they pushed the settings past the sheet, so the sheet scrolled. The headless
fit gate never saw them: it measured pages that had not run.

The read-out is organised, not cut. A ``FoldedNote`` is the same ``QLabel``
(same object name, text, colour property and accessible name, so nothing that
finds it or reads it changes), drawn as one line: its summary, elided, with a
``▸`` when something is not on screen. The whole text is its tooltip, and one
click, Return or Space opens it in full, wrapped, as the label it always was;
the same again folds it. ``text()`` is always the whole text.
"""
from __future__ import annotations

from PySide6 import QtGui
from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QAbstractTextDocumentLayout, QFontMetrics,
                           QPainter, QPalette, QTextDocument, QTextOption)
from PySide6.QtWidgets import (QLabel, QSizePolicy, QToolTip, QVBoxLayout,
                               QWidget)

from foammesh.view.theming.metrics import GAP_TIGHT

#: Drawn after the visible part when the note holds more than it shows.
MORE_MARK = ' ▸'


def one_line(text: str) -> str:
    """*text* with its line breaks folded into one line."""
    lines = [line.strip() for line in str(text or '').splitlines()]
    return ' '.join(line for line in lines if line)


class FoldedNote(QLabel):
    """A word-wrapped read-out label drawn on one line while it is folded."""

    #: The note was opened (``True``) or folded again (``False``).
    foldChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._open = False
        self._summary = ''
        self._resting_focus = self.focusPolicy()
        self.setWordWrap(True)
        self._fit_policy()

    # -- state ---------------------------------------------------------- #

    def isOpen(self) -> bool:
        return self._open

    def setOpen(self, opened: bool) -> None:
        opened = bool(opened)
        if opened == self._open:
            return
        self._open = opened
        self._fit_policy()
        self.updateGeometry()
        self.update()
        self._refresh_affordance()
        self.foldChanged.emit(opened)

    def _fit_policy(self) -> None:
        # Folded, the note is its one line and takes no spare height.
        policy = self.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Policy.Preferred if self._open
                                 else QSizePolicy.Policy.Fixed)
        self.setSizePolicy(policy)

    def toggle(self) -> None:
        self.setOpen(not self._open)

    def setSummary(self, summary: str) -> None:
        """The one line the folded note shows; empty = the text on one line."""
        self._summary = str(summary or '')
        self.updateGeometry()
        self.update()
        self._refresh_affordance()

    def summary(self) -> str:
        return self._summary or self._one_line_text()

    def setText(self, text: str) -> None:                  # noqa: N802
        super().setText(text)
        self._refresh_affordance()

    def setTextFormat(self, text_format) -> None:          # noqa: N802
        super().setTextFormat(text_format)
        self._refresh_affordance()

    def holdsMore(self) -> bool:
        """Whether the folded line leaves some of the note off screen."""
        if not self.text():
            return False
        if self._summary and one_line(self._plain(self._summary)) != \
                one_line(self._plain(self.text())):
            return True
        return self._summary_width() > self._room()

    # -- what it is made of --------------------------------------------- #

    def _rich(self, text: str) -> bool:
        form = self.textFormat()
        if form == Qt.TextFormat.RichText:
            return True
        if form == Qt.TextFormat.PlainText:
            return False
        # QtGui's `Qt` namespace carries it (QtCore's does not).
        return QtGui.Qt.mightBeRichText(text)

    def _plain(self, text: str) -> str:
        if not self._rich(text):
            return text
        document = QTextDocument()
        document.setHtml(text)
        return document.toPlainText()

    def _one_line_text(self) -> str:
        text = self.text()
        if self._rich(text):
            return text.replace('<br>', ' ').replace('<br/>', ' ')
        return one_line(text)

    def _document(self, text: str) -> QTextDocument:
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.NoWrap)
        document.setDefaultTextOption(option)
        if self._rich(text):
            document.setHtml(text)
        else:
            document.setPlainText(text)
        return document

    def _summary_width(self) -> float:
        return self._document(self.summary()).idealWidth()

    def _line_height(self) -> int:
        text = self.summary()
        if self._rich(text):
            height = self._document(text).size().height()
        else:
            height = QFontMetrics(self.font()).height()
        return int(round(height))

    def _frame(self) -> int:
        margins = self.contentsMargins()
        return margins.top() + margins.bottom() + 2 * self.margin()

    def _room(self) -> int:
        rect = self.contentsRect()
        return max(rect.width() - 2 * self.margin(), 0)

    def _folded_height(self) -> int:
        return self._line_height() + self._frame() if self.text() else 0

    # -- size ------------------------------------------------------------- #

    def hasHeightForWidth(self) -> bool:                    # noqa: N802
        return True if not self._open else super().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:            # noqa: N802
        if self._open:
            return super().heightForWidth(width)
        return self._folded_height()

    def sizeHint(self) -> QSize:                            # noqa: N802
        hint = super().sizeHint()
        if self._open:
            return hint
        return QSize(hint.width(), self._folded_height())

    def minimumSizeHint(self) -> QSize:                     # noqa: N802
        hint = super().minimumSizeHint()
        if self._open:
            return hint
        return QSize(hint.width(), self._folded_height())

    # -- drawing ---------------------------------------------------------- #

    def paintEvent(self, event) -> None:                    # noqa: N802
        if self._open or not self.text():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        rect = self.contentsRect().adjusted(
            self.margin(), self.margin(), -self.margin(), -self.margin())
        colour = self.palette().color(self.foregroundRole())
        if not self.isEnabled():
            colour = self.palette().color(
                QPalette.ColorGroup.Disabled, self.foregroundRole())
        painter.setPen(colour)
        painter.setFont(self.font())
        metrics = QFontMetrics(self.font())
        more = self.holdsMore()
        mark = MORE_MARK if more else ''
        mark_width = metrics.horizontalAdvance(mark)
        room = max(rect.width() - mark_width, 0)
        text = self.summary()
        if not self._rich(text):
            shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, room)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft
                                       | Qt.AlignmentFlag.AlignVCenter),
                             shown + mark)
            return
        document = self._document(text)
        cut = document.idealWidth() > room
        ellipsis = '…' if cut else ''
        ellipsis_width = metrics.horizontalAdvance(ellipsis)
        visible = max(room - ellipsis_width, 0)
        painter.save()
        painter.translate(rect.left(), rect.top()
                          + (rect.height() - document.size().height()) / 2)
        context = QAbstractTextDocumentLayout.PaintContext()
        palette = QPalette(self.palette())
        palette.setColor(QPalette.ColorRole.Text, colour)
        context.palette = palette
        context.clip = QRectF(0, 0, visible, document.size().height())
        painter.setClipRect(context.clip)
        document.documentLayout().draw(painter, context)
        painter.restore()
        tail = ellipsis + mark
        if tail:
            used = min(document.idealWidth(), visible)
            tail_rect = rect.adjusted(int(used), 0, 0, 0)
            painter.drawText(tail_rect, int(Qt.AlignmentFlag.AlignLeft
                                            | Qt.AlignmentFlag.AlignVCenter),
                             tail)

    # -- opening ---------------------------------------------------------- #

    def resizeEvent(self, event) -> None:                   # noqa: N802
        super().resizeEvent(event)
        self._refresh_affordance()

    def _refresh_affordance(self) -> None:
        foldable = self._open or self.holdsMore()
        self.setCursor(Qt.CursorShape.PointingHandCursor if foldable
                       else Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if foldable
                            else self._resting_focus)

    def mousePressEvent(self, event) -> None:               # noqa: N802
        if self._open:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton and self.holdsMore():
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:             # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._open:
            super().mouseReleaseEvent(event)
            if not self.hasSelectedText():
                self.setOpen(False)
            return
        if self.holdsMore():
            event.accept()
            self.setOpen(True)
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:                 # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                           Qt.Key.Key_Space) and (self._open or self.holdsMore()):
            self.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event) -> bool:
        if (event.type() == QEvent.Type.ToolTip and not self._open
                and self.holdsMore()):
            whole = self._plain(self.text())
            own = self.toolTip()
            tip = whole if not own or own == whole else f'{whole}\n\n{own}'
            QToolTip.showText(event.globalPos(),
                              tip + '\n\n' + self.tr('Click to read it all.'),
                              self)
            return True
        return super().event(event)



class FoldedNotes(QWidget):
    """Several read-out notes of one result under a single folded line.

    Plan 44 FX2. The Boundary layers page says two things about the layers
    stage it just ran -- which layers fell short, and that the mesh was built
    stage by stage. Two folded lines still took Boundary layers 22 px past
    the 300 px sheet on the meshed case (measured in the real branch), so
    folded they share one line: each note's summary, in the warning colour
    when either note warns. Opened, the line gives way to the notes
    themselves, whole and in their own colours; folding either of them folds
    the group again. The notes keep their object names and their text.
    """

    def __init__(self, parent=None, name: str = '') -> None:
        super().__init__(parent)
        if name:
            self.setObjectName(name)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(GAP_TIGHT)
        self.line = FoldedNote(self)
        self.line.setObjectName(f'{name}Line' if name else 'foldedNotesLine')
        self.line.foldChanged.connect(self._lineFolded)
        box.addWidget(self.line)
        self._notes: list[FoldedNote] = []
        self._open = False
        self.setVisible(False)

    def add(self, note: 'FoldedNote') -> 'FoldedNote':
        note.setParent(self)
        self.layout().addWidget(note)
        note.foldChanged.connect(self._noteFolded)
        self._notes.append(note)
        return note

    def notes(self) -> list:
        return list(self._notes)

    def isOpen(self) -> bool:
        return self._open

    def setOpen(self, opened: bool) -> None:
        self._open = bool(opened)
        self.sync()

    def sync(self) -> None:
        """Re-read the notes: call it after their text changes."""
        shown = [note for note in self._notes if note.text()]
        self.setVisible(bool(shown))
        warns = any(note.property('foammeshStatus') == 'warning'
                    for note in shown)
        if self.line.property('foammeshStatus') != ('warning' if warns
                                                     else None):
            self.line.setProperty('foammeshStatus',
                                  'warning' if warns else None)
            style = self.line.style()
            style.unpolish(self.line)
            style.polish(self.line)
        self.line.blockSignals(True)
        self.line.setOpen(False)
        self.line.blockSignals(False)
        self.line.setText('\n\n'.join(note._plain(note.text())
                                      for note in shown))
        self.line.setSummary(' · '.join(note.summary() for note in shown))
        self.line.setVisible(bool(shown) and not self._open)
        for note in self._notes:
            note.blockSignals(True)
            note.setOpen(self._open)
            note.blockSignals(False)
            note.setVisible(self._open and bool(note.text()))

    def _lineFolded(self, opened: bool) -> None:
        if opened:
            self.setOpen(True)

    def _noteFolded(self, opened: bool) -> None:
        if not opened:
            self.setOpen(False)
