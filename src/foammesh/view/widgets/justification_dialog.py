"""Ask for a written reason and keep what was written legible.

R197. Two decisions in this app are recorded against the case and read later
by somebody else: `Use geometry as-is` on the Repair page, and `Accept anyway`
on a failing quality gate. Both asked for the reason through a stock
`QInputDialog` -- the Repair page through `getText`, whose editor is a single
line, and the quality gate through `getMultiLineText`, whose editor is six
lines tall and does not wrap. MEASURED on tee / gmsh: a paragraph typed into
the quality prompt rendered as one long line behind a horizontal scrollbar,
so the author of the justification could see its last few words and no more.

A box that hides what has been written into it discourages writing anything,
and these two prompts are the app's whole defence against an untraceable
override. So they share one dialog, it wraps, and it will not accept an empty
answer -- the check both callers were already making for themselves.
"""
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout)


class JustificationDialog(QDialog):
    """A titled prompt, a wrapping editor, and OK disabled until it has text."""

    #: Tall enough for a real paragraph rather than a sentence fragment.
    _VISIBLE_LINES = 8

    def __init__(self, parent, title: str, prompt: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName('justificationDialog')
        self.setModal(True)

        layout = QVBoxLayout(self)
        label = QLabel(prompt, self)
        label.setWordWrap(True)
        layout.addWidget(label)

        self._editor = QPlainTextEdit(self)
        self._editor.setObjectName('justificationText')
        self._editor.setAccessibleName(title)
        self._editor.setAccessibleDescription(prompt)
        # The point of the whole file.
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._editor.setMinimumHeight(
            self._VISIBLE_LINES * self._editor.fontMetrics().lineSpacing()
            + 2 * self._editor.frameWidth())
        self._editor.textChanged.connect(self._updateAcceptable)
        layout.addWidget(self._editor, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        # Both callers refused an empty reason after the fact, which meant the
        # dialog took the answer, closed, and threw the decision away without
        # saying so. Refusing it in the dialog says which answer is missing
        # while the user is still looking at the question.
        self._updateAcceptable()
        self.resize(560, 320)

    def _updateAcceptable(self) -> None:
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(
                bool(self._editor.toPlainText().strip()))

    def justification(self) -> str:
        return self._editor.toPlainText().strip()

    @classmethod
    def ask(cls, parent, title: str, prompt: str) -> str:
        """The written reason, or '' when the user backed out.

        Empty is never a reason, so the two cases collapse into one and every
        caller reads the same way: ask, and act only on a non-empty answer.
        """
        dialog = cls(parent, title, prompt)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ''
        return dialog.justification()
