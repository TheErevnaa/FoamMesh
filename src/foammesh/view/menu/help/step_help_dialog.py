"""What the current step is for, and what it calculated -- on demand.

Plan 33 FORM-03 and FORM-02. Every engine task page carried a help band at
the top -- an empty header row holding a single `?` -- and a four-column
`Calculated settings` table at the bottom of its form. MEASURED across the
twenty-nine task pages of the two engines before this: the band took a
control's height off every page whether or not the reader wanted it, and ten
of the pages spent the foot of the settings column on a read-only table of
field ids, classifications and native dictionary keys.

Both are still reachable, and from the one place a user looks for an
explanation they did not ask for: the Help menu. The page's own words are
the same words -- they are still authored on the page and still readable
from `_description` and `_prerequisites` -- and the table is the same table
object the page fills, mounted here rather than in the form.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from foammesh.view.theming.metrics import prose_measure


class StepHelpDialog(QDialog):
    """The step's purpose and what it needs settled first, in plain text.

    A plain dialog rather than a popup: this is opened deliberately, from a
    menu, and it should stay open while the reader looks at the page behind
    it rather than dismissing itself on the first click elsewhere.
    """

    def __init__(self, title: str, purpose: str, prerequisites: str = '',
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName('stepHelpDialog')
        self.setWindowTitle(title or self.tr('What this step is for'))
        self.setAccessibleName(self.tr('What this step is for'))
        layout = QVBoxLayout(self)
        self._purpose = QLabel(str(purpose or ''), self)
        self._purpose.setObjectName('taskHelpPurpose')
        self._purpose.setWordWrap(True)
        self._requires = QLabel(str(prerequisites or ''), self)
        self._requires.setObjectName('taskHelpRequires')
        self._requires.setWordWrap(True)
        for label in (self._purpose, self._requires):
            # Selectable, because the prerequisite line names tasks a reader
            # may want to copy, and keyboard-selectable so the label can take
            # focus at all -- `QLabel` only accepts focus when it does.
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            # DP-220's measure is stated in characters and resolved through
            # `prose_measure`, so the cap follows the shell's body font
            # instead of being a pixel number written here.
            label.setMaximumWidth(prose_measure(label.font()))
            label.setVisible(bool(label.text()))
            layout.addWidget(label)
        if not self._purpose.text() and not self._requires.text():
            self._purpose.setText(self.tr('This step has no description.'))
            self._purpose.setVisible(True)
        # DP-186/DP-187. What the dialog is announced as, and what it is
        # announced to say: the same words it paints, in the same order.
        self.setAccessibleDescription('\n\n'.join(
            label.text() for label in (self._purpose, self._requires)
            if label.text()))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                   parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def labels(self) -> tuple:
        """The labels that carry the words, in reading order."""
        return (self._purpose, self._requires)

    def firstLabel(self):
        """The label focus lands on when the dialog opens."""
        for label in (self._purpose, self._requires):
            if label.isVisibleTo(self) and label.text():
                return label
        return self._purpose

    def detail(self) -> tuple:
        """`(purpose, prerequisites)` as shown."""
        return (self._purpose.text(), self._requires.text())

    def showEvent(self, event):
        super().showEvent(event)
        # The words are the whole point of the window, so focus lands on
        # them rather than on Close: a screen reader announces what is being
        # read, and the keyboard can select and copy it.
        self.firstLabel().setFocus()


class StepDetailsDialog(QDialog):
    """The calculated settings behind the step, and what they come to.

    The page's own `CalculatedTable` is mounted here rather than copied, so
    there is one table, filled once, by the code that has always filled it.
    It is handed back to the page when the dialog closes.
    """

    #: Columns of the derived-quantities table: what it is, what it comes
    #: to, and in what.
    DERIVED_HEADINGS = ('Quantity', 'Value', 'Unit')

    def __init__(self, title: str, table=None, derived=(),
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName('stepDetailsDialog')
        self.setWindowTitle(title or self.tr('Calculated settings'))
        self.setAccessibleName(self.tr('Calculated settings for this step'))
        layout = QVBoxLayout(self)
        self._derived = None
        rows = [tuple(row) for row in derived or ()]
        if rows:
            self._derived = QTableWidget(len(rows), 3, self)
            self._derived.setObjectName('stepDerivedQuantities')
            self._derived.setHorizontalHeaderLabels(
                [self.tr(name) for name in self.DERIVED_HEADINGS])
            self._derived.setAccessibleName(
                self.tr('Values derived from the settings on this step'))
            self._derived.setEditTriggers(QTableWidget.NoEditTriggers)
            self._derived.verticalHeader().setVisible(False)
            for index, row in enumerate(rows):
                for column in range(3):
                    value = str(row[column]) if column < len(row) else ''
                    self._derived.setItem(index, column,
                                          QTableWidgetItem(value))
            self._derived.resizeColumnsToContents()
            layout.addWidget(self._derived)
        self._table = table
        self._owner = table.parentWidget() if table is not None else None
        if table is not None:
            table.setParent(self)
            table.setVisible(True)
            layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                   parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def table(self):
        """The calculated-settings table, mounted in this dialog."""
        return self._table

    def derivedTable(self):
        """The per-page derived quantities table, or ``None``.

        Read by `tests/unit/test_dp158_the_calculated_table_fits_the_panel_
        it_sits_in.py`, which holds this table to the same rule as the one
        below it. W-O2 found the accessor here with no caller at all, which
        is how the second table in this dialog came to be the one surface in
        the product that DP-158 did not reach.
        """
        return self._derived

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit()

    def _fit(self) -> None:
        """DP-158 again, one window on: the tables fit, they do not scroll."""
        table = self._table
        if table is not None:
            fit = getattr(table, 'fitContents', None)
            if fit is not None:
                fit()
        self._fitDerived()

    def _fitDerived(self) -> None:
        """The same rule for the second table, which has no class of its own.

        Plan 33 section 6 check 9, W-O2. `CalculatedTable` carries DP-158 for
        the table below; the derived-quantities table is a plain
        `QTableWidget` and kept the Qt defaults -- a viewport about 192 px
        tall and a scrollbar on each axis for whatever did not fit. Today
        one page publishes one derived row and one publishes three, so it
        rarely overflowed, and "rarely" is not a rule: a table that scrolls
        inside a dialog is the fault DP-158 closed.

        Stretching the first column rather than resizing all three to
        content is what makes the horizontal case impossible: the three
        widths always add up to the viewport, and the column that takes the
        slack is the one holding the words.
        """
        table = self._derived
        if table is None:
            return
        table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.resizeRowsToContents()
        rows = table.verticalHeader()
        height = table.horizontalHeader().height() + 2 * table.frameWidth()
        height += sum(rows.sectionSize(row) for row in range(table.rowCount()))
        table.setFixedHeight(height)
        header = table.horizontalHeader()
        for column in (1, 2):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    def done(self, result) -> None:
        # The table belongs to the page; the dialog only borrowed it.
        table, owner = self._table, self._owner
        self._table = None
        if table is not None and owner is not None:
            table.setParent(owner)
            table.setVisible(False)
        super().done(result)
