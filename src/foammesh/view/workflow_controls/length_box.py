#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Length controls for panels that hold a length in model units (UXB3c).

Plan 40 UXB3 put every registry length in the project display unit; the Gmsh
panels, the shared toolbar's rotation centre and the 2D-extrusion form hold
lengths that never pass through the registry, so they stayed plain number
boxes reading raw model units. This module is the one length control they
use:

* :class:`ModelLengthBox` -- a :class:`LengthDoubleSpinBox` whose callers speak
  model units (metres) only. It shows and accepts the display unit; a value
  the reader did not retype is handed back bit for bit; a unit change keeps
  the stored value and re-words the box.
* :func:`length_cell` -- the box and its unit in a column of its own (DP-164:
  no suffix inside the box).
* :class:`LengthItemDelegate` -- the same for a table cell: the cell holds the
  model-unit text, the reader sees and types the display unit.
* :class:`LengthExpressionEdit` -- a text expression (``2*R + 1``) read in the
  display unit and handed over in model units.

Nothing here changes what a store, the facade or Gmsh receives: they get
model units, exactly as before.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLineEdit, QStyledItemDelegate, QWidget,
)

from foammesh.core import display_unit
from foammesh.view.theming.metrics import MARGIN_NONE, UnitLabel
from foammesh.view.workflow_controls.field_widgets import (
    LengthDoubleSpinBox, _NUMBER_PRECISION, fit_special_text,
)

#: A bound no length reaches; a Qt spin box needs finite limits.
UNBOUNDED_METRES = 1e30

#: The item role that marks a table cell as holding a length in model units.
LENGTH_ROLE = int(Qt.ItemDataRole.UserRole) + 41


def format_model_units(metres: float) -> str:
    """A model-unit length as the text a table cell or a field stores."""
    return format(float(metres), '.12g')


def model_text(display_text: str) -> str:
    """A number typed in the display unit, as the model-unit text a writer
    takes; an empty or unreadable text is handed back unchanged."""
    try:
        return format_model_units(display_unit.from_display(
            float(str(display_text).strip())))
    except ValueError:
        return display_text


class ModelLengthBox(LengthDoubleSpinBox):
    """A length box that speaks model units (metres) to its caller."""

    #: The new value in model units, or ``None`` while the box is unset.
    modelValueChanged = Signal(object)

    def __init__(self, parent=None, *, minimum=0.0, maximum=UNBOUNDED_METRES,
                 step=None):
        super().__init__(parent)
        self.setDecimals(_NUMBER_PRECISION)
        self.unit_label = None
        self._low = -UNBOUNDED_METRES if minimum is None else float(minimum)
        self._high = UNBOUNDED_METRES if maximum is None else float(maximum)
        self._step = step
        self._unset_text = ''
        self._unset = False
        self._range()
        self.valueChanged.connect(self._announce)
        display_unit.subscribe(self._on_display_unit)

    # -- range and step ----------------------------------------------------- #

    def setModelRange(self, low, high) -> None:
        self._low = -UNBOUNDED_METRES if low is None else float(low)
        self._high = UNBOUNDED_METRES if high is None else float(high)
        self._range()

    def setModelStep(self, step) -> None:
        self._step = None if step is None else float(step)
        self.setSingleStep(self._step_in_display())

    def _step_in_display(self) -> float:
        if self._step is None:
            return 1.0
        return self._step / self.per

    def _range(self) -> None:
        self.per = display_unit.metres_per_unit()
        low, high = self._low / self.per, self._high / self.per
        if self._unset_text:
            # The next figure the box can show below the range stands for
            # "not set"; there is no band of typeable values between.
            low = low - 10 ** -self.decimals()
        self.setRange(low, high)
        self.setSingleStep(self._step_in_display())

    # -- the value, in model units ------------------------------------------ #

    def setUnsetText(self, text: str) -> None:
        """Offer a word for "no value": the floor of the box shows it."""
        self._unset_text = str(text)
        self.setSpecialValueText(self._unset_text)
        fit_special_text(self)
        self._range()

    def setUnsetDefault(self, word: str, default=None) -> None:
        """The unset word with the value it stands for: ``Default (0.1 m)``.

        An unset box never shows a bare word (U3): the value the tool uses
        stands beside it, in the display unit, and follows a unit change.
        """
        self._unset_word = str(word)
        self._unset_default = None if default is None else float(default)
        self.setUnsetText(self._unset_label())

    def _unset_label(self) -> str:
        word = getattr(self, '_unset_word', '')
        default = getattr(self, '_unset_default', None)
        if default is None:
            return word
        return f'{word} ({display_unit.format_length(default)})'

    def isUnset(self) -> bool:
        return bool(self._unset_text) and self.value() == self.minimum()

    def setModelValue(self, metres) -> None:
        """Show *metres* (model units); ``None`` shows the unset word."""
        blocked = self.blockSignals(True)
        try:
            if metres is None:
                self.setValue(self.minimum())
                self.exact = None
                self.exact_shown = None
            else:
                number = float(metres)
                self.setValue(number / self.per)
                self.exact = number
                self.exact_shown = self.value()
        finally:
            self.blockSignals(blocked)

    def modelValue(self):
        """The value in model units; the metres last set when untouched."""
        if self.isUnset():
            return None
        if self.exact is not None and self.value() == self.exact_shown:
            return self.exact
        return self.value() * self.per

    def keyPressEvent(self, event) -> None:    # noqa: N802
        """Delete on a selected value hands the box back to "not set"."""
        edit = self.lineEdit()
        if (self._unset_text and not self.isUnset()
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and edit is not None and edit.text()
                and edit.selectedText() == edit.text()):
            self.setValue(self.minimum())
            self.exact = None
            self.exact_shown = None
            edit.selectAll()
            return
        super().keyPressEvent(event)

    def _announce(self, _value) -> None:
        self.modelValueChanged.emit(self.modelValue())

    # -- the unit ----------------------------------------------------------- #

    def _on_display_unit(self) -> None:
        """The project display unit changed: same metres, new words."""
        try:
            stored = self.modelValue()
            blocked = self.blockSignals(True)
            try:
                self._range()
            finally:
                self.blockSignals(blocked)
            if getattr(self, '_unset_word', ''):
                self.setSpecialValueText(self._unset_label())
                self._unset_text = self._unset_label()
                fit_special_text(self)
            self.setModelValue(stored)
            if self.unit_label is not None:
                self.unit_label.setText(display_unit.current())
            name = self.accessibleName()
            if name and '(' in name and name.endswith(')'):
                self.setAccessibleName(name[:name.rindex(' (')]
                                       + f' ({display_unit.current()})')
        except RuntimeError:
            # The widget was deleted with its panel.
            pass


def length_cell(box: ModelLengthBox, parent=None) -> QWidget:
    """The box with its unit label in a column of its own (DP-164)."""
    cell = QWidget(parent)
    row = QHBoxLayout(cell)
    row.setContentsMargins(MARGIN_NONE, MARGIN_NONE, MARGIN_NONE, MARGIN_NONE)
    row.addWidget(box, 1)
    label = UnitLabel(display_unit.current())
    box.unit_label = label
    row.addWidget(label)
    cell.setFocusProxy(box)
    return cell


# --------------------------------------------------------------------------- #
# Table cells
# --------------------------------------------------------------------------- #

def mark_length_item(item, *, unset_text: str = '') -> None:
    """Mark a table item as holding a length in model units."""
    item.setData(LENGTH_ROLE, unset_text or True)


class LengthItemDelegate(QStyledItemDelegate):
    """A table cell that holds model units and shows the display unit.

    The item's text stays the model-unit number (what the panel parses and
    stores). Only a cell marked with :func:`mark_length_item` is touched.
    """

    def __init__(self, parent=None, *, minimum=-UNBOUNDED_METRES):
        super().__init__(parent)
        self._minimum = minimum

    def _marked(self, index) -> bool:
        return bool(index.data(LENGTH_ROLE))

    def initStyleOption(self, option, index) -> None:    # noqa: N802
        super().initStyleOption(option, index)
        if not self._marked(index):
            return
        text = str(index.data(Qt.ItemDataRole.EditRole) or '').strip()
        if not text:
            return
        try:
            number = float(text)
        except ValueError:
            return
        option.text = display_unit.significant(display_unit.to_display(number))

    def createEditor(self, parent, option, index):       # noqa: N802
        if not self._marked(index):
            return super().createEditor(parent, option, index)
        box = ModelLengthBox(parent, minimum=self._minimum)
        flag = index.data(LENGTH_ROLE)
        if isinstance(flag, str) and flag:
            box.setUnsetText(flag)
        box.setFrame(False)
        return box

    def setEditorData(self, editor, index) -> None:      # noqa: N802
        if not isinstance(editor, ModelLengthBox):
            return super().setEditorData(editor, index)
        text = str(index.data(Qt.ItemDataRole.EditRole) or '').strip()
        try:
            editor.setModelValue(float(text) if text else None)
        except ValueError:
            editor.setModelValue(None if editor._unset_text else 0.0)

    def setModelData(self, editor, model, index) -> None:  # noqa: N802
        if not isinstance(editor, ModelLengthBox):
            return super().setModelData(editor, model, index)
        if editor.exact is not None and editor.value() == editor.exact_shown:
            return                      # untouched: the stored text stays
        metres = editor.modelValue()
        model.setData(index, '' if metres is None
                      else format_model_units(metres),
                      Qt.ItemDataRole.EditRole)


def length_header(title: str) -> str:
    """``Size (mm)``: a length column's heading in the display unit."""
    return f'{title} ({display_unit.current()})'


# --------------------------------------------------------------------------- #
# Expressions
# --------------------------------------------------------------------------- #

class LengthExpressionEdit(QLineEdit):
    """A typed number or expression read in the project display unit.

    ``text()`` is what the reader typed; :meth:`modelText` is the same in
    model units. With the display unit at ``m`` the two are identical, so
    nothing changes for a metre project.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.unit_label = None
        display_unit.subscribe(self._on_display_unit)

    def modelText(self) -> str:
        text = self.text().strip()
        per = display_unit.metres_per_unit()
        if not text or per == 1.0:
            return self.text()
        return f'({text})*{per!r}'

    def setModelText(self, text: str) -> None:
        """Show an expression stored in model units, in the display unit.

        A plain number is converted; any other expression is wrapped so that
        it still evaluates to the same model length.
        """
        text = str(text)
        per = display_unit.metres_per_unit()
        stripped = text.strip()
        if not stripped or per == 1.0:
            self.setText(text)
            return
        try:
            self.setText(display_unit.significant(
                display_unit.to_display(float(stripped))))
        except ValueError:
            self.setText(f'({stripped})/{per!r}')

    def _on_display_unit(self) -> None:
        try:
            if self.unit_label is not None:
                self.unit_label.setText(display_unit.current())
        except RuntimeError:
            pass


class LengthListEdit(QLineEdit):
    """Several lengths typed in one line (``xmin ymin zmin xmax ymax zmax``),
    in the display unit; :meth:`modelValues` hands them over in model units."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.unit_label = None
        display_unit.subscribe(self._on_display_unit)

    def modelValues(self) -> list:
        """The numbers in model units; ``ValueError`` when one is not a number."""
        return [display_unit.from_display(float(word))
                for word in self.text().replace(',', ' ').split()]

    def setModelValues(self, values) -> None:
        self.setText(' '.join(display_unit.significant(
            display_unit.to_display(v)) for v in values))

    def _on_display_unit(self) -> None:
        try:
            if self.unit_label is not None:
                self.unit_label.setText(display_unit.current())
        except RuntimeError:
            pass


def length_expression_cell(edit, parent=None) -> QWidget:
    cell = QWidget(parent)
    row = QHBoxLayout(cell)
    row.setContentsMargins(MARGIN_NONE, MARGIN_NONE, MARGIN_NONE, MARGIN_NONE)
    row.addWidget(edit, 1)
    label = UnitLabel(display_unit.current())
    edit.unit_label = label
    row.addWidget(label)
    cell.setFocusProxy(edit)
    return cell


__all__ = [
    'LENGTH_ROLE', 'LengthExpressionEdit', 'LengthItemDelegate',
    'LengthListEdit', 'UnitLabel',
    'ModelLengthBox', 'UNBOUNDED_METRES', 'format_model_units', 'length_cell',
    'length_expression_cell', 'length_header', 'mark_length_item',
    'model_text',
]
