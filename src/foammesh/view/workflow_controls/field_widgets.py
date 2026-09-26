"""Typed editors generated from the AF2 field registry.

Plan 17 §5.18 requires every published Tier 1 field to expose its unit, valid
range, default, and documentation, and release gate 10 forbids a field that no
validation, derivation, or runner consumes.  Building the editors from the
registry descriptor - rather than hand-placing widgets - keeps the desktop in
step with the schema and makes an unbacked field impossible to render.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QSpinBox, QWidget,
)

from foammesh.view.theming.metrics import (
    CompactDoubleSpinBox, CompactSpinBox, UnitLabel,
)
from foammesh.view.workflow_controls.labels import humanise_option


#: Widened bounds used when the schema leaves a side unconstrained. Qt spin
#: boxes require finite limits, so an unbounded field still needs a range.
_UNBOUNDED = 1e12
_UNBOUNDED_INT = 2 ** 31 - 1

#: How much precision a registry number field keeps. Kept deliberately wide:
#: a feature angle is written to a degree and a first-layer thickness to a
#: micron, and one box serves both.
_NUMBER_PRECISION = 9

#: DP-214. A memory cap is measured against the RAM the machine reports, so
#: the box has to be able to say a machine-sized figure. Counted in bytes it
#: cannot: a Qt spin box carries a C++ int, and 2**31-1 bytes is 1.999 GiB.
#: So the box for a byte-valued field counts gibibytes and stores the bytes.
_BYTES_PER_GIB = 1024 ** 3
_GIB_LABEL = 'GiB'
_GIB_PRECISION = 3
_GIB_STEP = 0.5
_UNBOUNDED_GIB = 1024 * 1024

#: What the bottom of a byte box means: no cap at all, not no memory.
_NO_LIMIT_TEXT = 'No limit'

#: DP-517. What an optional number with no default shows while it is unset:
#: the key is not written and the tool's own default applies.
_UNSET_TEXT = 'Auto'

#: The dynamic property that marks a number box whose floor means "unset".
_UNSET_PROPERTY = 'foammeshUnsetFloor'


def _can_be_unset(descriptor) -> bool:
    """An optional number with no default: `None` is its real value.

    DP-517, MEASURED on Domain & regions > Advanced (audit 2026-09-23):
    `snappyGeometry/tolerance`, `minQuality`, `scale` and `maxTreeDepth` are
    `setOptional().setDefault(None)`, and `CaseBuilder._add_optional` writes
    one only when it is set. The box had no way to say so: it drew `None` as
    0, clamped to the minimum, so the page showed a scale of 0 and an octree
    depth of 1 that the run never used. It needs a floor to stand the word
    on.

    DP-598 (field audit 0924 snappy-back D5). "A field with no lower bound
    keeps the plain box" left the eleven optional relaxed quality thresholds
    showing 0.0 while nothing was written, and once one was typed there was
    no way back to "not written". Such a field stands the word on the box's
    own floor instead: a number that far down is not one anyone types.
    """
    if getattr(descriptor, 'required', True):
        return False
    return getattr(descriptor, 'default', None) is None


def _offer_unset(widget, descriptor) -> None:
    """Stand `Auto` one step under the range, or on an excluded minimum."""
    if not _can_be_unset(descriptor):
        _exclude_minimum(widget, descriptor)
        return
    low = descriptor.minimum
    if low is None:
        # DP-598: no lower bound, so the floor of the box itself means unset.
        low = widget.minimum()
    elif not getattr(descriptor, 'exclusive_minimum', False):
        # The next figure the box can show below the range, so there is no
        # band of typeable values between `Auto` and the first valid one.
        low = low - (1 if isinstance(widget, QSpinBox)
                     else 10 ** -widget.decimals())
    widget.setMinimum(int(low) if isinstance(widget, QSpinBox)
                      else float(low))
    widget.setSpecialValueText(_UNSET_TEXT)
    widget.setProperty(_UNSET_PROPERTY, True)


def _exclude_minimum(widget, descriptor) -> None:
    """Start the box one step above a minimum the schema excludes.

    DP-583 (field audit 0924 snappy-front D10). Target cell size, Scale and
    Grading X/Y/Z are declared greater than 0, and their boxes started at 0,
    so the box's own floor was a value the patch then refused ("did not
    satisfy field validation"). The first value the box can show above the
    limit is the floor instead. A field that can be unset keeps its `Auto`
    on the excluded minimum (`_offer_unset`).
    """
    low = getattr(descriptor, 'minimum', None)
    if low is None or not getattr(descriptor, 'exclusive_minimum', False):
        return
    step = (1 if isinstance(widget, QSpinBox)
            else 10 ** -widget.decimals())
    floor = low + step
    widget.setMinimum(int(floor) if isinstance(widget, QSpinBox)
                      else float(floor))


def _is_unset(widget) -> bool:
    return (bool(widget.property(_UNSET_PROPERTY))
            and widget.value() == widget.minimum())


def _counts_in_gibibytes(descriptor) -> bool:
    """True for a byte field, whose box counts in a unit a reader can type."""
    if getattr(descriptor, 'unit', None) != 'bytes':
        return False
    declared = getattr(descriptor, 'value_type', None)
    return getattr(declared, 'value', declared) in ('integer', 'number')


def _display_unit(descriptor) -> str:
    """The unit the reader is typing in, which is not always the stored one."""
    if _counts_in_gibibytes(descriptor):
        return _GIB_LABEL
    return getattr(descriptor, 'unit', '') or ''


def _bytes_text(value, descriptor) -> str:
    """A byte count as the reader meets it: gibibytes, or the no-cap word."""
    number = float(value)
    if number == 0 and float(getattr(descriptor, 'minimum', 0) or 0) == 0:
        return _NO_LIMIT_TEXT
    return f'{number / _BYTES_PER_GIB:g} {_GIB_LABEL}'


# DP-164. `CompactDoubleSpinBox` and `CompactSpinBox` moved to
# `view/theming/metrics.py`, beside `UnitLabel`, so the geometry pages and
# the refinement dialogs can build the same number box these forms do
# without importing the task-page module. They are re-exported here
# because this is where the registry factory below and DP-130's tests
# have always found them.


#: DP-602 (field audit 0924 snappy-back D10). Thresholds whose sensible
#: values sit far outside what nine decimals and a +/-1e12 range can show.
#: ``minTetQuality`` defaults to 1e-15, which the fixed box printed as ``0``,
#: and ``minVol`` to -1e30, which it clamped to -1e12 -- and a clamped
#: default is written back as a different number on the next edit.
_SCIENTIFIC_FIELDS = frozenset({
    'quality.thresholds.min_vol',
    'quality.thresholds.min_tet_quality',
    'quality.thresholds.relaxed.min_vol',
    'quality.thresholds.relaxed.min_tet_quality',
})
_SCIENTIFIC_UNBOUNDED = 1e300
_SCIENTIFIC_DECIMALS = 323  # QDoubleSpinBox's own ceiling
_SCIENTIFIC_ACCEPTABLE = re.compile(
    r'[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?')
_SCIENTIFIC_PARTIAL = re.compile(r'[-+]?\d*\.?\d*([eE][-+]?\d*)?')


def _is_scientific(descriptor) -> bool:
    """DP-602: a number shown and typed in exponent form."""
    if getattr(descriptor, 'id', None) in _SCIENTIFIC_FIELDS:
        return True
    try:
        default = abs(float(getattr(descriptor, 'default', None)))
    except (TypeError, ValueError):
        return False
    return default != 0 and not (
        10 ** -(_NUMBER_PRECISION - 3) <= default < _UNBOUNDED)


class ScientificDoubleSpinBox(CompactDoubleSpinBox):
    """A number box that shows and takes ``1e-15`` and ``-1e30``.

    DP-602. It keeps every digit a double has and prints nine significant
    figures, so a tiny tolerance is not shown as ``0`` and a huge sentinel
    is not clamped to the range of a length.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDecimals(_SCIENTIFIC_DECIMALS)

    def textFromValue(self, value: float) -> str:
        return f'{value:.9g}'.replace('e+', 'e')

    def valueFromText(self, text: str) -> float:
        try:
            return float(text.strip())
        except ValueError:
            return self.value()

    def validate(self, text: str, position: int):
        stripped = text.strip()
        special = self.specialValueText()
        if special and stripped == special:
            return QValidator.State.Acceptable, text, position
        if _SCIENTIFIC_ACCEPTABLE.fullmatch(stripped):
            number = float(stripped)
            if self.minimum() <= number <= self.maximum():
                return QValidator.State.Acceptable, text, position
            return QValidator.State.Intermediate, text, position
        if _SCIENTIFIC_PARTIAL.fullmatch(stripped) or (
                special and special.startswith(stripped)):
            return QValidator.State.Intermediate, text, position
        return QValidator.State.Invalid, text, position


class FieldEditor(QWidget):
    """One registry-backed field: label, editor, unit suffix, documentation."""

    valueChanged = Signal(str, object)

    def __init__(self, descriptor, parent=None, *, choices=None):
        super().__init__(parent)
        self.descriptor = descriptor
        self.field_id = descriptor.id
        self._editor = _build_editor(descriptor, self, choices=choices)
        self._editor.setEnabled(not descriptor.read_only)
        name = descriptor.title or descriptor.id.rsplit('.', 1)[-1]
        self._name = name
        self.label = QLabel(name, self)
        self.label.setBuddy(self._editor)
        unit = _display_unit(descriptor)
        accessible = f'{name} ({unit})' if unit else name
        self._editor.setAccessibleName(accessible)
        tooltip = _tooltip(descriptor)
        if tooltip:
            self._editor.setToolTip(tooltip)
            self.label.setToolTip(tooltip)
        # DP-156. A label that knows its column: it names itself so the
        # alignment can find it after the page reparents it into a row, and
        # it takes the column's width as a preference rather than a minimum
        # so a cramped row narrows instead of wrapping.
        self.unit_label = UnitLabel(unit, self)
        _connect(self._editor, self._emit)
        # CP-09 item 4. Set from ``applies_when`` by whichever page owns this
        # editor. Until it is, every field applies -- which is what the
        # desktop did before the metadata was read at all.
        self._applies = True
        self._inactive_reason = ''
        self._tooltip = tooltip
        #: The form this editor's row was added to, and the widget holding
        #: the editor and its unit, set by whichever page built the row. Both
        #: are needed to take the row's height away when it does not apply.
        self._form = None
        self._row_widget = None

    # -- conditional editors ----------------------------------------------- #

    def setRow(self, form, row_widget=None) -> None:
        """Say which form row this editor was placed in.

        Plan 33 FORM-01: an inapplicable field is absent, and a row is only
        absent if the layout stops giving it a line. `QFormLayout` keeps the
        line for a row whose widgets are merely hidden, so the page hands the
        layout over and `setApplicability` uses `setRowVisible`.
        """
        self._form = form
        self._row_widget = row_widget

    def setApplicability(self, applies: bool, reason: str = '') -> None:
        """Take an inactive setting off the form, and say why on the page.

        CP-09 item 4 kept the control on screen and greyed it, on the
        reasoning that the value is real and a control that vanishes reads as
        a control that was never there. Plan 33 FORM-01 measures the cost:
        twelve greyed editors across the twenty-nine task pages of the two
        engines, each holding a full row of the one narrow column the user
        edits in, none of them editable and none of them read by the run. A
        disabled control is a question the reader has to answer -- is this
        broken? -- before they can get back to the settings that do apply.

        So the row goes: label, editor and unit all leave the layout, and the
        row takes no height. Nothing is lost -- the value is still held, the
        editor is still registered in the page's `_editors`, :meth:`applies`
        still keeps it out of the patch, and :meth:`inactiveReason` still
        carries the sentence saying which setting would bring it back.
        """
        self._applies = bool(applies)
        self._inactive_reason = '' if applies else str(reason or '')
        # The plain name: " (inactive)" was there to explain a greyed row,
        # and there is no greyed row left to explain.
        self.label.setText(self._name)
        self._editor.setEnabled(
            self._applies and not self.descriptor.read_only)
        self.label.setEnabled(self._applies)
        self.unit_label.setEnabled(self._applies)
        self.setProperty('foammeshInactive', not self._applies)
        tip = self._tooltip
        if self._inactive_reason:
            tip = '\n\n'.join(part for part in
                              (self._inactive_reason, self._tooltip) if part)
        self._editor.setToolTip(tip)
        self.label.setToolTip(tip)
        self._editor.setAccessibleDescription(self._inactive_reason)
        self._setRowShown(self._applies)

    def _setRowShown(self, shown: bool) -> None:
        """Show or take away the whole row: label, editor and unit."""
        for widget in (self.label, self._editor, self.unit_label,
                       self._row_widget):
            if widget is not None:
                widget.setVisible(bool(shown))
        form = self._form
        if form is None:
            return
        anchor = self.label if self.label is not None else self._editor
        try:
            form.setRowVisible(anchor, bool(shown))
        except (AttributeError, RuntimeError, TypeError):
            # An older Qt, or a row this editor is no longer part of. The
            # per-widget visibility above is still in force.
            pass

    def applies(self) -> bool:
        return self._applies

    def inactiveReason(self) -> str:
        return self._inactive_reason

    # -- value access ------------------------------------------------------ #

    def value(self):
        return _read(self._editor, self.descriptor)

    def set_value(self, value) -> None:
        blocked = self._editor.blockSignals(True)
        try:
            _write(self._editor, self.descriptor, value)
        finally:
            self._editor.blockSignals(blocked)

    @property
    def editor(self) -> QWidget:
        return self._editor

    def _emit(self, *_args) -> None:
        self.valueChanged.emit(self.field_id, self.value())


def _tooltip(descriptor) -> str:
    parts = []
    if descriptor.documentation:
        parts.append(descriptor.documentation)
    scaled = _counts_in_gibibytes(descriptor)

    def bound(value):
        # A bound is a figure, not a meaning: the bottom of a memory box is
        # zero gibibytes, and only the value sitting on it reads as no cap.
        if scaled:
            return f'{float(value) / _BYTES_PER_GIB:g} {_GIB_LABEL}'
        return f'{value:g}'

    if descriptor.minimum is not None or descriptor.maximum is not None:
        low = '-inf' if descriptor.minimum is None else bound(descriptor.minimum)
        high = 'inf' if descriptor.maximum is None else bound(descriptor.maximum)
        parts.append(f'Range: {low} to {high}')
    if descriptor.default is not None:
        parts.append('Default: ' + (_bytes_text(descriptor.default, descriptor)
                                    if scaled else str(descriptor.default)))
    if descriptor.invalidates:
        parts.append('Changing this stales: ' + ', '.join(descriptor.invalidates))
    return '\n'.join(parts)


def _scale_to_gibibytes(widget, descriptor) -> None:
    """Range a byte field in the unit the box counts, bottom reading as none."""
    widget.setDecimals(_GIB_PRECISION)
    widget.setSingleStep(_GIB_STEP)
    low = (float(descriptor.minimum) / _BYTES_PER_GIB
           if descriptor.minimum is not None else 0.0)
    high = (float(descriptor.maximum) / _BYTES_PER_GIB
            if descriptor.maximum is not None else float(_UNBOUNDED_GIB))
    widget.setRange(low, high)
    if low == 0.0:
        widget.setSpecialValueText(_NO_LIMIT_TEXT)


def _build_editor(descriptor, parent, *, choices=None) -> QWidget:
    if choices is not None:
        widget = QComboBox(parent)
        for label, value, status_label, enabled in choices:
            widget.addItem(
                label if enabled else f'{label} — {status_label}', value)
            item = widget.model().item(widget.count() - 1)
            if item is not None:
                item.setEnabled(bool(enabled))
                item.setToolTip(
                    f'{status_label}; stable ID: {value}')
        return widget
    value_type = getattr(descriptor.value_type, 'value', descriptor.value_type)
    if value_type == 'enum':
        widget = QComboBox(parent)
        for option in descriptor.enum or ():
            widget.addItem(_humanise(option), option)
        return widget
    if value_type == 'boolean':
        return QCheckBox(parent)
    if value_type == 'integer' and not _counts_in_gibibytes(descriptor):
        widget = CompactSpinBox(parent)
        widget.setRange(
            int(descriptor.minimum) if descriptor.minimum is not None else -_UNBOUNDED_INT,
            int(descriptor.maximum) if descriptor.maximum is not None else _UNBOUNDED_INT)
        _offer_unset(widget, descriptor)
        return widget
    # DP-214. A byte field is declared a whole number, but the box that
    # edits it counts gibibytes, so it is this box and not the integer one.
    # One construction, two ranges: the fractional box is the only one that
    # can carry a machine-sized figure at all.
    if value_type == 'number' or _counts_in_gibibytes(descriptor):
        if (_is_scientific(descriptor)
                and not _counts_in_gibibytes(descriptor)):
            # DP-602: exponent form, over the range a double can hold.
            widget = ScientificDoubleSpinBox(parent)
            widget.setRange(
                float(descriptor.minimum) if descriptor.minimum is not None
                else -_SCIENTIFIC_UNBOUNDED,
                float(descriptor.maximum) if descriptor.maximum is not None
                else _SCIENTIFIC_UNBOUNDED)
            _offer_unset(widget, descriptor)
            return widget
        widget = CompactDoubleSpinBox(parent)
        if _counts_in_gibibytes(descriptor):
            _scale_to_gibibytes(widget, descriptor)
        else:
            widget.setDecimals(_NUMBER_PRECISION)
            widget.setRange(
                float(descriptor.minimum) if descriptor.minimum is not None else -_UNBOUNDED,
                float(descriptor.maximum) if descriptor.maximum is not None else _UNBOUNDED)
            _offer_unset(widget, descriptor)
        return widget
    return QLineEdit(parent)


def _connect(widget, slot) -> None:
    if isinstance(widget, QComboBox):
        widget.currentIndexChanged.connect(slot)
    elif isinstance(widget, QCheckBox):
        widget.toggled.connect(slot)
    elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        widget.valueChanged.connect(slot)
    else:
        widget.textEdited.connect(slot)


def _read(widget, descriptor):
    if isinstance(widget, QComboBox):
        return widget.currentData()
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        if _is_unset(widget):
            return None
        if isinstance(widget, QDoubleSpinBox) and _counts_in_gibibytes(descriptor):
            return int(round(widget.value() * _BYTES_PER_GIB))
        return widget.value()
    return widget.text()


def _write(widget, descriptor, value) -> None:
    if isinstance(widget, QComboBox):
        index = widget.findData(value)
        if index < 0:
            index = widget.findText(str(value))
        widget.setCurrentIndex(max(index, 0))
        return
    if isinstance(widget, QCheckBox):
        widget.setChecked(bool(value))
        return
    if (isinstance(widget, (QSpinBox, QDoubleSpinBox))
            and widget.property(_UNSET_PROPERTY)
            and value in (None, '')):
        widget.setValue(widget.minimum())
        return
    if isinstance(widget, QSpinBox):
        # Persisted integers can arrive as scientific-notation strings
        # (e.g. maximum_cells default '1e7'); int() alone rejects those.
        widget.setValue(int(float(value)) if value not in (None, '') else 0)
        return
    if isinstance(widget, QDoubleSpinBox):
        number = float(value if value not in (None, '') else 0.0)
        if _counts_in_gibibytes(descriptor):
            # A cap is a cap. A figure too small to show at this precision
            # still means one was set, so it takes the smallest step the box
            # has rather than rounding down onto the value that means none.
            gibibytes = number / _BYTES_PER_GIB
            floor = 10 ** -_GIB_PRECISION
            widget.setValue(max(gibibytes, floor) if number > 0 else gibibytes)
            return
        widget.setValue(number)
        return
    widget.setText('' if value is None else str(value))


def _humanise(option: str) -> str:
    """The label one stored enum value is offered under.

    DP-143. This used to be `replace('_', ' ').capitalize()`, which spells the
    two solvers the product writes meshes for `Openfoam` and `Su2`, and spells
    `asImported` `Asimported`. The rule lives in `labels` now so that the
    other place a value gets turned into words agrees with this one.
    """
    return humanise_option(option)
