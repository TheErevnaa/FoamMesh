"""Typed editors generated from the AF2 field registry.

Plan 17 §5.18 requires every published Tier 1 field to expose its unit, valid
range, default, and documentation, and release gate 10 forbids a field that no
validation, derivation, or runner consumes.  Building the editors from the
registry descriptor - rather than hand-placing widgets - keeps the desktop in
step with the schema and makes an unbacked field impossible to render.
"""
from __future__ import annotations

import math
import re

from PySide6.QtCore import (
    QCoreApplication, QEvent, QObject, QPoint, Qt, Signal,
)
from PySide6.QtGui import QColor, QPainter, QPalette, QValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QSpinBox, QToolButton, QWidget,
)

from shiboken6 import isValid

from foammesh.core import display_unit, quantity_expr
from foammesh.core.facade.field_tiers import tier_of
from foammesh.view.theming.metrics import (
    NUMBER_FIELD_MAX_WIDTH, CompactDoubleSpinBox, CompactSpinBox, UnitLabel,
)
from foammesh.view.combo_items import set_item_enabled
from foammesh.view import inline_error
from foammesh.view.workflow_controls import effective_values
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

#: Plan 40 UXB2 (#38). An *Auto* is never shown bare: it says what it came to.
#: Until a page resolves the value (``FieldEditor.set_effective``) it says
#: whose default applies, which is true of every unset field.
_UNSET_PHRASES = {
    'gmsh.global_sizing.target_size': 'from the geometry',
    'gmsh.global_sizing.minimum_size': 'a tenth of the target size',
    'meshing.base_grid.target_cell_size': 'from the geometry',
}


def unset_phrase(field_id: str) -> str:
    """The words that stand in the brackets of *Auto* before a value is known."""
    if field_id in _UNSET_PHRASES:
        return _UNSET_PHRASES[field_id]
    if '/{id}/target_size' in field_id:
        return 'the global target size'
    if field_id.startswith('gmsh.'):
        return 'Gmsh default'
    return 'OpenFOAM default'


#: The narrowest a spin box with a special-value text may become: the sheet
#: grid's floor for every spin box (``sheet_form.elide_combos``).
SPECIAL_TEXT_FLOOR = 72
_ELIDER_PROPERTY = 'foammeshSpecialTextElider'
#: DP-2874 (user, 2026-10-21): a field that was rewritten carries a small
#: anticlockwise arrow beside it; one click puts it back.
#: DP-2873. Editors that draw a frame share the compact field width.
FRAMED_EDITORS = (QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit)
EDITOR_STRETCH = 100
RESET_ICON = ':/graphicsIcons/resetField.svg'
#: Plan 47 SZ11 (user, 2026-10-21): a number the size field proposed carries
#: only this mark; the tooltip names who proposed it.
PROPOSED_MARK = '*'
PROPOSED_TIP = 'Proposed by FoamMesh sizing'
RESET_BUTTON_SIZE = 16
RESET_ICON_SIZE = 12


def add_editor_row(row, editor) -> None:
    """Put *editor*, its unit, its reset arrow and its ``*`` into *row*.

    DP-2873. The editor takes the compact field width and the rest of the row
    is empty. Every framed editor (number, choice, text) gets the same width,
    so a column still ends its boxes at one x (DP-156). The editor's stretch
    far outweighs the trailing one, so it fills to the cap before the space
    goes to the empty end, and on a narrow panel it narrows like the others.
    """
    widget = editor.editor
    if isinstance(widget, FRAMED_EDITORS):
        widget.setMaximumWidth(NUMBER_FIELD_MAX_WIDTH)
    row.addWidget(widget, EDITOR_STRETCH)
    row.addWidget(editor.unit_label)
    # The arrow and the ``*`` sit together with no gap: on a sheet column
    # every pixel of a row's minimum counts (Plan 45 V11, 257 px).
    marks = QHBoxLayout()
    marks.setContentsMargins(0, 0, 0, 0)
    marks.setSpacing(0)
    marks.addWidget(editor.reset_button)
    marks.addWidget(editor.badge_label)
    row.addLayout(marks)
    row.addStretch(1)


class _SpecialTextElider(QObject):
    """Paint a spin box's special-value text with an ellipsis when it is cut.

    Plan 45 V11. DP-2701 kept ``Auto (OpenFOAM default)`` whole by making it
    the box's minimum width (193 px), and a sheet column of 257 px cannot
    hold that beside its label: the whole page grew sideways inside a
    scroller with no sideways bar, so the layers table and the orange notes
    under it were cut on the right. The box now shrinks with the column as
    every other field does, and when the column is too narrow for the words
    it shows them elided -- ``Auto (OpenFOAM de...`` -- the same rule the
    sheet's combo boxes follow, with the whole text in the tooltip. A box
    with room shows the text whole, as DP-2701 asked; it is never clipped
    on either side.
    """

    def __init__(self, box) -> None:
        # A child of the editor it watches, so it goes with the editor and
        # is never asked about a box half way through its destructor.
        super().__init__(box.lineEdit())
        self._box = box
        self._edit = self.parent()
        self._tip = None
        self._edit.installEventFilter(self)

    def _room(self):
        edit = self._edit
        rect = edit.contentsRect()
        margins = edit.textMargins()
        # QLineEdit's own horizontal text margin (``horizontalMargin`` = 2).
        return rect.adjusted(margins.left() + 2, margins.top(),
                             -(margins.right() + 2), -margins.bottom())

    def _showing_special(self) -> str:
        box = self._box
        text = box.specialValueText()
        edit = self._edit
        if not text or edit.text() != text or edit.hasFocus():
            return ''
        return text

    def isElided(self) -> bool:
        text = self._showing_special()
        if not text:
            return False
        edit = self._edit
        return (edit.width() > 0 and edit.fontMetrics().horizontalAdvance(text)
                > self._room().width())

    def _tooltip(self) -> None:
        edit = self._edit
        if self.isElided():
            text = self._box.specialValueText()
            base = self._box.toolTip()
            wanted = text if not base else text + chr(10) * 2 + base
            if edit.toolTip() != wanted:
                edit.setToolTip(wanted)
                self._tip = wanted
        elif self._tip is not None and edit.toolTip() == self._tip:
            edit.setToolTip('')
            self._tip = None

    def eventFilter(self, watched, event):
        kind = event.type()
        if (kind not in (QEvent.Type.Resize, QEvent.Type.Paint)
                or watched is not self._edit):
            return False
        if not (isValid(self._box) and isValid(self._edit)):
            return False
        # DP-2873 (live). The text can arrive after the box took its width
        # (Threads gets "Mesh setup (15 threads)" from its page), so the
        # tooltip is kept in step at paint as well as at resize.
        self._tooltip()
        if kind == QEvent.Type.Resize:
            return False
        if not self.isElided():
            return False
        box, edit = self._box, self._edit
        room = self._room()
        metrics = edit.fontMetrics()
        shown = metrics.elidedText(box.specialValueText(),
                                   Qt.TextElideMode.ElideRight, room.width())
        group = (QPalette.ColorGroup.Active if edit.isEnabled()
                 else QPalette.ColorGroup.Disabled)
        painter = _open_painter(edit)
        painter.setFont(edit.font())
        painter.setPen(edit.palette().color(group, QPalette.ColorRole.Text))
        horizontal = edit.alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask
        painter.drawText(room, int(horizontal | Qt.AlignmentFlag.AlignVCenter),
                         shown)
        painter.end()
        return True


def _open_painter(widget) -> QPainter:
    """A painter on ``widget``, for a paint event an event filter takes."""
    return QPainter(widget)


def special_text_is_elided(widget) -> bool:
    """True while ``widget`` shows its special-value text with an ellipsis."""
    elider = getattr(widget, '_foammeshSpecialElider', None)
    return bool(elider is not None and elider.isElided())


def fit_special_text(widget) -> None:
    """Let a spin box with a special-value text fit its column, never clip it.

    DP-2701. ``Auto (7.71 mm)`` and ``Auto (Gmsh default)`` were clipped on
    both sides ("uto (7.71 mm)"). Plan 45 V11: raising the box's minimum to
    the text's width fixed that by pushing the whole sheet page wider than
    its column. The box keeps the sheet's floor instead, asks for the text's
    width as its preferred size (``_value_sized_hint``), and a
    :class:`_SpecialTextElider` draws the text whole when it has the room and
    elided, with the whole text in the tooltip, when it does not.
    """
    text = widget.specialValueText() if hasattr(widget, 'specialValueText')         else ''
    base = widget.property('foammeshBaseMinWidth')
    if base is None:
        base = widget.minimumWidth()
        widget.setProperty('foammeshBaseMinWidth', base)
    if not text:
        widget.setMinimumWidth(base)
        return
    # The minimum width is set, never left at 0: a 0 lets the layout fall
    # back on the minimum size hint, which counts the special text whole.
    _fit_number(widget)
    if (getattr(widget, '_foammeshSpecialElider', None) is None
            and hasattr(widget, 'lineEdit')):
        setattr(widget, '_foammeshSpecialElider', _SpecialTextElider(widget))
        widget.setProperty(_ELIDER_PROPERTY, True)
        if hasattr(widget, 'valueChanged'):
            widget.valueChanged.connect(lambda *_a, w=widget: _fit_number(w))
    widget.updateGeometry()


def number_width(widget) -> int:
    """The width a spin box needs to show its value whole, as Qt sizes it.

    DP-2879 (live, 1280x800 and 1600x1000): a box with a special text kept
    only the sheet's floor as its minimum, so on a narrow column the number
    lost its leading digits (``0.0007706`` showed as ``007706``). The number
    is what the box is for; only the special text may be elided.
    """
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QStyle, QStyleOptionSpinBox
    if not hasattr(widget, 'textFromValue'):
        return 0
    if widget.specialValueText() and widget.value() == widget.minimum():
        return 0
    metrics = widget.fontMetrics()
    # QAbstractSpinBox::sizeHint: the text, a space, and 2 px for the cursor.
    text = (widget.prefix() + widget.textFromValue(widget.value())
            + widget.suffix() + ' ')
    contents = QSize(metrics.horizontalAdvance(text) + 2,
                     widget.lineEdit().sizeHint().height())
    option = QStyleOptionSpinBox()
    widget.initStyleOption(option)
    return widget.style().sizeFromContents(
        QStyle.ContentsType.CT_SpinBox, option, contents, widget).width()


def _fit_number(widget) -> None:
    if not isValid(widget):
        return
    base = widget.property('foammeshBaseMinWidth')
    wanted = max(int(base or 0), SPECIAL_TEXT_FLOOR)
    if widget.specialValueText():
        wanted = max(wanted, number_width(widget))
    wanted = min(wanted, widget.maximumWidth())
    if widget.minimumWidth() != wanted:
        widget.setMinimumWidth(wanted)
        widget.updateGeometry()


def auto_text(field_id: str, effective: str | None = None) -> str:
    """``Auto (15.4 mm)``, or ``Auto (OpenFOAM default)``."""
    return f'{_UNSET_TEXT} ({effective or unset_phrase(field_id)})'


#: Plan 40 UXB2 (#48). A stored number that means "no such limit" is shown as
#: the word, and typing the word stores the number. ``(value, word)``.
_SENTINELS = {
    'quality.thresholds.min_area': (-1.0, 'Off'),
    'quality.thresholds.min_vol_collapse_ratio': (-1.0, 'Off'),
    'quality.thresholds.min_tet_quality': (1e-15, 'Off'),
    'quality.thresholds.min_vol': (-1e30, 'No limit'),
}
_SENTINEL_WORDS = ('off', 'no limit', 'none')


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
    widget.setSpecialValueText(auto_text(descriptor.id))
    fit_special_text(widget)
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


def _is_length(descriptor) -> bool:
    """Plan 40 UXB3. A stored length (metres) that the box shows in the
    project display unit. Storage stays metres."""
    if getattr(descriptor, 'unit', None) != 'm':
        return False
    if _counts_in_gibibytes(descriptor) or descriptor.id in _SENTINELS:
        return False
    declared = getattr(descriptor, 'value_type', None)
    if getattr(declared, 'value', declared) != 'number':
        return False
    return not _is_scientific(descriptor)


class _ScaledBounds:
    """A descriptor whose bounds are in the display unit; the rest delegates."""

    def __init__(self, descriptor):
        self._descriptor = descriptor
        per = display_unit.metres_per_unit()
        low, high = descriptor.minimum, descriptor.maximum
        self.minimum = None if low is None else float(low) / per
        self.maximum = None if high is None else float(high) / per

    def __getattr__(self, name):
        return getattr(self._descriptor, name)


class LengthDoubleSpinBox(CompactDoubleSpinBox):
    """A length box counting the project display unit (UXB3).

    Four significant figures at rest, every digit while the reader is typing.
    The stored value stays metres: ``exact`` remembers the metres last set so
    a value the reader did not touch is handed back bit for bit.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.per = display_unit.metres_per_unit()
        self.exact = None
        self.exact_shown = None
        self.lineEdit().textEdited.connect(self._on_edited)

    def textFromValue(self, value: float) -> str:
        if self.hasFocus():
            return f'{value:.10g}'
        text = display_unit.significant(value)
        if 'e' in text and abs(value) < 1:
            # A length the box can hold is written out in full: a tolerance
            # of 1e-06 m reads 0.000001, never in the exponent form that
            # Plan 40 U3 keeps for a sentinel (UXB2).
            text = f'{float(text):.{self.decimals()}f}'.rstrip('0').rstrip('.')
        return text

    # -- Plan 40 UXD6: ``10 mm``, ``0.5*12`` and ``=diag/40`` ---------------- #

    def _display_value(self, text: str) -> float:
        """The display-unit number *text* stands for; raises QuantityError."""
        stripped = text.strip()
        names = [name for name in quantity_expr.NAMES if name in stripped.lower()]
        if names:
            effective_values.refresh_scene()
        return quantity_expr.evaluate(stripped, per=self.per) / self.per

    def valueFromText(self, text: str) -> float:
        try:
            return self._display_value(text)
        except quantity_expr.QuantityError:
            return self.value()

    def validate(self, text: str, position: int):
        """Acceptable when it evaluates inside the range; otherwise the reader
        may keep typing. Nothing here is ever refused character by character:
        the refusal is said in place by :meth:`_on_edited`."""
        stripped = text.strip()
        special = self.specialValueText()
        if special and stripped == special:
            return QValidator.State.Acceptable, text, position
        try:
            number = self._display_value(stripped)
        except quantity_expr.QuantityError:
            return QValidator.State.Intermediate, text, position
        if self.minimum() <= number <= self.maximum():
            return QValidator.State.Acceptable, text, position
        return QValidator.State.Intermediate, text, position

    def _on_edited(self, text: str) -> None:
        """The reader typed: red outline and a note, or the SI value as a hint."""
        stripped = text.strip()
        special = self.specialValueText()
        if not stripped or (special and stripped == special):
            inline_error.clear_invalid(self)
            return
        try:
            number = self._display_value(stripped)
        except quantity_expr.QuantityError as error:
            inline_error.set_invalid(self, f'{stripped}: {error}')
            return
        low, high = self.minimum(), self.maximum()
        if number < low or number > high:
            shown = display_unit.significant(number)
            unit = display_unit.current()
            bound = (f'at least {display_unit.significant(low)} {unit}'
                     if number < low else
                     f'at most {display_unit.significant(high)} {unit}')
            inline_error.set_invalid(
                self, f'{stripped} ({shown} {unit}) must be {bound}')
            return
        if quantity_expr.is_plain_number(stripped):
            inline_error.clear_invalid(self)
        else:
            inline_error.set_hint(
                self, '= ' + display_unit.format_length(number * self.per))

    def focusOutEvent(self, event) -> None:      # noqa: N802
        super().focusOutEvent(event)
        if not inline_error.is_invalid(self):
            inline_error.clear_invalid(self)


def _range_length(widget, descriptor) -> None:
    """(Re)range a length box in the display unit."""
    widget.per = display_unit.metres_per_unit()
    scaled = _ScaledBounds(descriptor)
    widget.setRange(
        scaled.minimum if scaled.minimum is not None else -_UNBOUNDED,
        scaled.maximum if scaled.maximum is not None else _UNBOUNDED)
    _offer_unset(widget, scaled)


def _length_editor(descriptor, parent) -> QWidget:
    widget = LengthDoubleSpinBox(parent)
    widget.setDecimals(_NUMBER_PRECISION)
    _range_length(widget, descriptor)
    return widget


def _display_unit(descriptor) -> str:
    """The unit the reader is typing in, which is not always the stored one."""
    if _counts_in_gibibytes(descriptor):
        return _GIB_LABEL
    if _is_length(descriptor):
        return display_unit.current()
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


class _SentinelWord:
    """Shows a sentinel number as its word and takes the word back.

    Plan 40 UXB2 (#48). ``-1e30`` is how the dictionary says "no limit"; the
    reader is told "No limit". Typing ``off``, ``none`` or the word stores the
    sentinel again, so the stored value never changes.
    """

    _sentinel = 0.0
    _word = 'Off'

    def _is_sentinel(self, value: float) -> bool:
        return math.isclose(value, self._sentinel, rel_tol=1e-9, abs_tol=0.0)

    def textFromValue(self, value: float) -> str:
        if self._is_sentinel(value):
            return self._word
        return super().textFromValue(value)

    def valueFromText(self, text: str) -> float:
        if text.strip().lower() in _SENTINEL_WORDS + (self._word.lower(),):
            return self._sentinel
        return super().valueFromText(text)

    def validate(self, text: str, position: int):
        low = text.strip().lower()
        words = _SENTINEL_WORDS + (self._word.lower(),)
        if low in words:
            return QValidator.State.Acceptable, text, position
        if low and any(word.startswith(low) for word in words):
            return QValidator.State.Intermediate, text, position
        return super().validate(text, position)


class SentinelDoubleSpinBox(_SentinelWord, CompactDoubleSpinBox):
    pass


class SentinelScientificSpinBox(_SentinelWord, ScientificDoubleSpinBox):
    pass


#: Plan 40 UXD4 (F3f). The room a label keeps on its right for the dot that
#: says its field is not at the default, and the dot's radius.
_MARKER_ROOM = 9
_MARKER_RADIUS = 3


class MarkedLabel(QLabel):
    """A field label that carries a dot when its field differs from default.

    The dot is painted, not typed into the text: pages reword labels, and a
    label that read ``Target size *`` would be a different string to every one
    of them. The room for it is reserved always, so a label does not change
    width when its field is edited and the label column stays one column.
    """

    def __init__(self, text: str = '', parent=None) -> None:
        super().__init__(text, parent)
        self._marked = False
        self.setContentsMargins(0, 0, _MARKER_ROOM, 0)

    def isMarked(self) -> bool:
        return self._marked

    def setMarked(self, marked: bool) -> None:
        marked = bool(marked)
        if marked == self._marked:
            return
        self._marked = marked
        self.setProperty('foammeshNotDefault', marked)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._marked:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().highlight().color()
                         if self.isEnabled() else QColor(128, 128, 128))
        centre = QPoint(self.width() - _MARKER_ROOM // 2 - 1,
                        self.height() // 2)
        painter.drawEllipse(centre, _MARKER_RADIUS, _MARKER_RADIUS)
        painter.end()


def same_as_default(value, default) -> bool:
    """Whether a field's value is its registry default."""
    value = None if value == '' else value
    default = None if default == '' else default
    if default is None or value is None:
        return default is None and value is None
    if isinstance(default, bool) or isinstance(value, bool):
        return bool(value) == bool(default)
    try:
        return math.isclose(float(value), float(default),
                            rel_tol=1e-9, abs_tol=1e-15)
    except (TypeError, ValueError):
        return str(value) == str(default)


class _BadgeElider(QObject):
    """Let the size field's "field: 20 mm" badge shrink before its row widens.

    Plan 45 V11. The badge sits on the editor's row, after the unit; a
    label asks for its whole text as its minimum, so on 9. Boundary layers
    ``field: 100 deg`` made the Feature angle row 184 px and the Guided card
    295 px in a 233 px column, and the whole page -- the layers table and
    the orange notes under it -- was cut on the right. The badge now asks
    for an ellipsis at least and, when its row gives it less than its words,
    draws them elided; the whole text stays its ``text()`` and its tooltip.
    """

    #: What the badge keeps at its narrowest: an ellipsis, so a 1280 x 800
    #: column (257 px) still holds label, editor, unit and badge.
    FLOOR_TEXT = '…'
    _REFIT = (QEvent.Type.Polish, QEvent.Type.StyleChange,
              QEvent.Type.FontChange, QEvent.Type.Show)

    def __init__(self, label) -> None:
        super().__init__(label)
        self._label = self.parent()
        self._label.installEventFilter(self)
        self.refit()

    def refit(self) -> None:
        label = self._label
        margins = label.contentsMargins()
        floor = (label.fontMetrics().horizontalAdvance(self.FLOOR_TEXT)
                 + margins.left() + margins.right() + 2 * label.margin()
                 + 2 * label.frameWidth())
        if label.minimumWidth() != floor:
            # Set, never left at 0: a 0 lets the layout fall back on the
            # minimum size hint, which counts the text whole.
            label.setMinimumWidth(floor)

    def isElided(self) -> bool:
        label = self._label
        text = label.text()
        return bool(text) and label.fontMetrics().horizontalAdvance(text) > (
            label.contentsRect().width() - 2 * label.margin())

    def eventFilter(self, watched, event):
        if watched is not self._label or not isValid(watched):
            return False
        kind = event.type()
        if kind in self._REFIT:
            self.refit()
            return False
        if kind != QEvent.Type.Paint or not self.isElided():
            return False
        label = self._label
        # The frame, as QLabel would draw it; the background was drawn
        # before this event. Then the words, elided.
        QFrame.paintEvent(label, event)
        painter = _open_painter(label)
        margin = label.margin()
        rect = label.contentsRect().adjusted(margin, margin, -margin, -margin)
        shown = label.fontMetrics().elidedText(
            label.text(), Qt.TextElideMode.ElideRight, rect.width())
        label.style().drawItemText(
            painter, rect, int(label.alignment()), label.palette(),
            label.isEnabled(), shown, QPalette.ColorRole.WindowText)
        painter.end()
        return True


def fit_badge(label) -> None:
    """Give a badge label :class:`_BadgeElider` (once)."""
    if getattr(label, '_foammeshBadgeElider', None) is None:
        setattr(label, '_foammeshBadgeElider', _BadgeElider(label))


def badge_is_elided(label) -> bool:
    """True while the badge ``label`` shows its text with an ellipsis."""
    elider = getattr(label, '_foammeshBadgeElider', None)
    return bool(elider is not None and elider.isElided())


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
        self.label = MarkedLabel(name, self)
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
        # Plan 41 GN25c. "field: 20 mm", set by the Automatic Sizing card's
        # proposals; hidden until the size field has proposed this control.
        self.badge_label = QLabel(self)
        self.badge_label.setObjectName('fieldSizingBadge')
        # Plan 45 V11: shrinks to an ellipsis before it widens the row.
        fit_badge(self.badge_label)
        self.badge_label.setProperty('foammeshStatus', 'info')
        self.badge_label.setVisible(False)
        self.reset_button = self._reset_button()
        _connect(self._editor, self._emit)
        if isinstance(self._editor, LengthDoubleSpinBox):
            display_unit.subscribe(self._on_display_unit)
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
        #: Plan 40 UXD4. The tier this field is in, and whether the page has
        #: taken its row away for it (Common folded, Expert not shown).
        self.tier = tier_of(self.field_id)
        self._tier_hidden = False
        self._filter_text = ''
        for widget in self._menu_hosts():
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda position, host=widget: self._show_menu(host, position))
        self._refresh_marker()

    # -- the size field's badge (Plan 41 GN25c) ------------------------------ #

    def setFieldBadge(self, text: str) -> None:
        """Say the size field proposed this control ('' takes the badge off).

        Plan 47 SZ11 (user, 2026-10-21): the number in the field is the whole
        statement; beside it is only a ``*`` whose tooltip says who proposed
        it. *text* (the rule, "field: 20 mm") is kept for ``fieldBadge``.
        """
        text = str(text or '').strip()
        self._badge_detail = text
        self.badge_label.setText(PROPOSED_MARK if text else '')
        self.badge_label.setToolTip(PROPOSED_TIP if text else '')
        self.badge_label.setAccessibleName(PROPOSED_TIP if text else '')
        self.badge_label.setVisible(bool(text) and self._applies
                                    and not self._tier_hidden)

    def fieldBadge(self) -> str:
        return getattr(self, '_badge_detail', '')

    # -- tiers, the default marker and reset (UXD4) ------------------------ #

    def _menu_hosts(self) -> list:
        hosts = [self.label, self._editor]
        line = getattr(self._editor, 'lineEdit', None)
        if callable(line) and line() is not None:
            hosts.append(line())
        return hosts

    def isAtDefault(self) -> bool:
        """Whether what the editor shows is the field's registry default.

        A field the registry gives no default, and whose box cannot say
        "unset", has nothing to differ from.
        """
        if self.descriptor.default is None and not _can_be_unset(
                self.descriptor):
            return True
        return same_as_default(self.value(), self.descriptor.default)

    def canReset(self) -> bool:
        if self.descriptor.read_only or not self._applies:
            return False
        return not self.isAtDefault()

    def resetToDefault(self) -> bool:
        """Put the field back to its default, as an edit the page can save."""
        if not self.canReset():
            return False
        self.set_value(self.descriptor.default)
        self._refresh_marker()
        self.valueChanged.emit(self.field_id, self.value())
        return True

    def _reset_button(self) -> QToolButton:
        """The small anticlockwise arrow shown while the field is rewritten."""
        from PySide6.QtCore import QSize
        from foammesh.view.theming.icons import load_themed_icon
        button = QToolButton(self)
        button.setObjectName('fieldResetButton')
        button.setIcon(load_themed_icon(RESET_ICON))
        button.setIconSize(QSize(RESET_ICON_SIZE, RESET_ICON_SIZE))
        button.setFixedSize(RESET_BUTTON_SIZE, RESET_BUTTON_SIZE)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        text = QCoreApplication.translate('FieldEditor', 'Reset to default')
        button.setToolTip(text)
        button.setAccessibleName(f'{text}: {self._name}')
        # Keeps its room while hidden, so a column's boxes keep one right
        # edge whether or not the arrow shows (DP-156).
        policy = button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        button.setSizePolicy(policy)
        button.clicked.connect(self.resetToDefault)
        button.setVisible(False)
        return button

    def _refresh_marker(self) -> None:
        try:
            self.label.setMarked(not self.isAtDefault())
            button = getattr(self, 'reset_button', None)
            if button is not None:
                button.setVisible(self.canReset() and not self._tier_hidden)
        except (RuntimeError, ValueError, TypeError, AttributeError):
            pass

    def _show_menu(self, host, position) -> None:
        menu = None
        if isinstance(host, QLineEdit):
            menu = host.createStandardContextMenu()
        if menu is None:
            menu = QMenu(host)
        if not menu.isEmpty():
            menu.addSeparator()
        action = menu.addAction(
            QCoreApplication.translate('FieldEditor', 'Reset to default'))
        action.setObjectName('fieldResetToDefault')
        action.setEnabled(self.canReset())
        action.triggered.connect(self.resetToDefault)
        menu.exec(host.mapToGlobal(position))
        menu.deleteLater()

    def setTierHidden(self, hidden: bool) -> None:
        """Take the row away (or give it back) for its tier."""
        hidden = bool(hidden)
        if hidden == self._tier_hidden:
            return
        self._tier_hidden = hidden
        self._setRowShown(self.isRowShown())

    def isTierHidden(self) -> bool:
        return self._tier_hidden

    def isRowShown(self) -> bool:
        """Whether the row is on the form: it applies and its tier is shown.

        Plan 40 UXD3. While the page filter has text, the filter decides
        instead of the tier: a match is shown even inside a folded fold or
        the Expert tier, and a non-match is not. An inapplicable field is
        still absent either way.
        """
        if not self._applies:
            return False
        if self._filter_text:
            return self.matchesFilter(self._filter_text)
        return not self._tier_hidden

    def filterHaystack(self) -> str:
        """The words a filter searches: the label and the field's own name."""
        tail = str(self.field_id).rsplit('.', 1)[-1].replace('_', ' ')
        return f'{self._name} {tail}'.lower()

    def matchesFilter(self, text: str) -> bool:
        """Whether every word of ``text`` occurs in the label or field name."""
        words = str(text or '').lower().split()
        haystack = self.filterHaystack()
        return all(word in haystack for word in words)

    def setFilterText(self, text: str) -> None:
        """Keep only the rows matching ``text`` ('' shows the tiers again)."""
        text = ' '.join(str(text or '').split())
        if text == self._filter_text:
            return
        self._filter_text = text
        self._setRowShown(self.isRowShown())

    def filterText(self) -> str:
        return self._filter_text

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
        self._setRowShown(self.isRowShown())

    def _setRowShown(self, shown: bool) -> None:
        """Show or take away the whole row: label, editor and unit."""
        for widget in (self.label, self._editor, self.unit_label,
                       self._row_widget):
            if widget is not None:
                widget.setVisible(bool(shown))
        self.badge_label.setVisible(bool(shown) and bool(
            self.badge_label.text()))
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
        if getattr(self._editor, '_foammeshSpecialElider', None) is not None:
            _fit_number(self._editor)       # DP-2879: signals were blocked
        inline_error.clear_invalid(self._editor)
        self._refresh_marker()

    @property
    def editor(self) -> QWidget:
        return self._editor

    def set_effective(self, text: str | None) -> None:
        """Say what an unset (*Auto*) field comes to: ``Auto (15.4 mm)``.

        Plan 40 UXB2 (#38). Only a box that can be unset has an *Auto* to
        complete; ``None`` goes back to naming whose default applies. The box's
        value is untouched, so a proposal never overwrites what was typed.
        """
        widget = self._editor
        if not isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return
        if not widget.property(_UNSET_PROPERTY):
            return
        wanted = auto_text(self.field_id, text)
        if widget.specialValueText() != wanted:
            widget.setSpecialValueText(wanted)
            fit_special_text(widget)
            widget.updateGeometry()

    def annotate_option(self, value, note: str) -> None:
        """Complete a combo option's words: ``Auto`` becomes ``Auto (15 ranks)``."""
        widget = self._editor
        if not isinstance(widget, QComboBox):
            return
        index = widget.findData(value)
        if index < 0:
            return
        base = _humanise(str(value))
        widget.setItemText(index, f'{base} ({note})' if note else base)

    def _on_display_unit(self) -> None:
        """The project display unit changed: same metres, new words (UXB3)."""
        widget = self._editor
        if not isinstance(widget, LengthDoubleSpinBox):
            return
        try:
            stored = self.value()
            blocked = widget.blockSignals(True)
            try:
                _range_length(widget, self.descriptor)
                widget.setSpecialValueText(widget.specialValueText())
                _write(widget, self.descriptor, stored)
            finally:
                widget.blockSignals(blocked)
            unit = _display_unit(self.descriptor)
            self.unit_label.setText(unit)
            self._editor.setAccessibleName(f'{self._name} ({unit})')
            tip = _tooltip(self.descriptor)
            self._tooltip = tip
            widget.setToolTip(tip)
            self.label.setToolTip(tip)
        except RuntimeError:
            # The widgets were deleted with their page.
            pass

    def _emit(self, *_args) -> None:
        self._refresh_marker()
        self.valueChanged.emit(self.field_id, self.value())


def _tooltip(descriptor) -> str:
    parts = []
    if descriptor.documentation:
        parts.append(descriptor.documentation)
    scaled = _counts_in_gibibytes(descriptor)
    length = _is_length(descriptor)

    def bound(value):
        if length:
            return display_unit.format_length(float(value))
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
        if length:
            try:
                shown = display_unit.format_length(float(descriptor.default))
            except (TypeError, ValueError):
                shown = str(descriptor.default)
        else:
            shown = (_bytes_text(descriptor.default, descriptor)
                     if scaled else str(descriptor.default))
        parts.append('Default: ' + shown)
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


def _sentinel_editor(descriptor, parent) -> QWidget:
    """The number box of a field whose stored sentinel reads as a word."""
    scientific = _is_scientific(descriptor)
    widget = (SentinelScientificSpinBox(parent) if scientific
              else SentinelDoubleSpinBox(parent))
    widget._sentinel, widget._word = _SENTINELS[descriptor.id]
    widget.setDecimals(_SCIENTIFIC_DECIMALS if scientific
                       else _NUMBER_PRECISION)
    unbounded = _SCIENTIFIC_UNBOUNDED if scientific else _UNBOUNDED
    widget.setRange(
        float(descriptor.minimum) if descriptor.minimum is not None
        else -unbounded,
        float(descriptor.maximum) if descriptor.maximum is not None
        else unbounded)
    _offer_unset(widget, descriptor)
    return widget


def _build_editor(descriptor, parent, *, choices=None) -> QWidget:
    if choices is not None:
        widget = QComboBox(parent)
        for label, value, status_label, enabled in choices:
            widget.addItem(
                label if enabled else f'{label} — {status_label}', value)
            last = widget.count() - 1
            set_item_enabled(widget, last, bool(enabled))
            widget.setItemData(
                last, f'{status_label}; stable ID: {value}',
                Qt.ItemDataRole.ToolTipRole)
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
    if descriptor.id in _SENTINELS:
        return _sentinel_editor(descriptor, parent)
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
        if _is_length(descriptor):
            return _length_editor(descriptor, parent)
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
        if isinstance(widget, LengthDoubleSpinBox):
            if widget.exact is not None and widget.value() == widget.exact_shown:
                return widget.exact
            return widget.value() * widget.per
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
    if (isinstance(widget, (QSpinBox, QDoubleSpinBox))
            and value in (None, '')
            and getattr(descriptor, 'default', None) is not None):
        # A field that cannot be unset and was never written runs on its
        # default, so the box shows the default. Drawn as 0 it clamped to
        # the box's floor: Plan 40 UX0 measured `1e-09` on Gap width, Total
        # thickness, the 2D thickness and the CAD import tolerance (whose
        # default is 1e-06) of a fresh case.
        value = descriptor.default
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
        if isinstance(widget, LengthDoubleSpinBox):
            widget.setValue(number / widget.per)
            widget.exact = number
            widget.exact_shown = widget.value()
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
