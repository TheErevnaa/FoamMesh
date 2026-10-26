"""The sheet's form grid: label | editor | unit, one set of columns per page.

Plan 44 MF14 (mockup ``.row{grid-template-columns:44% 1fr 16px; gap:6px}``).

``QFormLayout`` sizes its label column from its own longest label, so two
forms stacked on one page put their editors at two x positions, and it wraps
a label above its editor in a narrow column -- the stacked label-over-editor
rows the mockup never has. :class:`SheetForm` is a ``QGridLayout`` that

* keeps the label in column 0, the editor in column 1 (the 1fr) and an
  optional unit in column 2 (16 px), always on one row;
* gives every form on a page the same label column width, which is the
  widest label capped at 44 % of the width the page is given
  (:func:`share_label_width`);
* answers the ``QFormLayout`` calls the rest of the application makes of a
  form (``addRow``, ``insertRow``, ``removeRow``, ``itemAt(row, role)``,
  ``getWidgetPosition``, ``labelForField``, ``setRowVisible`` ...), so a page
  that builds its rows with the old calls keeps working and a control keeps
  its object name, its row and its facade id.

A grid row whose widgets are all hidden takes neither height nor spacing, so
``setRowVisible(False)`` really does take the row away.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QFormLayout, QGridLayout, QLabel,
                               QGroupBox, QLayout, QStyle,
                               QStyleOptionComboBox, QStylePainter, QWidget)
from shiboken6 import isValid

#: Mockup ``.row`` columns.
LABEL_SHARE = 0.44
UNIT_COLUMN_WIDTH = 16
SHEET_INSET = 34   # page margins (2 x 8) + card padding (2 x 9)
COLUMN_GAP = 6
ROW_GAP = 6

#: ``QFormLayout.ItemRole`` values, so a caller may pass either enum.
_LABEL, _FIELD, _SPAN = 0, 1, 2
_ROLE_ENUM = {
    _LABEL: QFormLayout.ItemRole.LabelRole,
    _FIELD: QFormLayout.ItemRole.FieldRole,
    _SPAN: QFormLayout.ItemRole.SpanningRole,
}


def _role_number(role) -> int:
    value = getattr(role, 'value', role)
    return int(value)


class _Row:
    """One form row: the objects (widgets or layouts) in its three roles."""

    __slots__ = ('label', 'field', 'span', 'unit')

    def __init__(self, label=None, field=None, span=None, unit=None):
        self.label = label
        self.field = field
        self.span = span
        self.unit = unit

    def members(self):
        return [item for item in (self.label, self.field, self.span,
                                  self.unit) if item is not None]

    def get(self, role: int):
        return (self.label, self.field, self.span)[role]

    def put(self, role: int, value) -> None:
        if role == _LABEL:
            self.label = value
        elif role == _FIELD:
            self.field = value
        else:
            self.span = value


def _alive(obj) -> bool:
    try:
        return obj is not None and isValid(obj)
    except Exception:                                        # noqa: BLE001
        return False


def _widgets_of(obj):
    """Every widget a row member stands for (a layout holds several)."""
    if isinstance(obj, QWidget):
        return [obj]
    if isinstance(obj, QLayout):
        found = []
        for index in range(obj.count()):
            item = obj.itemAt(index)
            if item is None:
                continue
            if item.widget() is not None:
                found.append(item.widget())
            elif item.layout() is not None:
                found.extend(_widgets_of(item.layout()))
        return found
    return []


class SheetForm(QGridLayout):
    """A ``QGridLayout`` that behaves as the sheet's form (see module doc)."""

    #: `form.ItemRole.LabelRole` reads as it does on a `QFormLayout`.
    ItemRole = QFormLayout.ItemRole

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[_Row] = []
        self._labelWidth = 0
        super().setHorizontalSpacing(COLUMN_GAP)
        super().setVerticalSpacing(ROW_GAP)
        super().setColumnStretch(0, 0)
        super().setColumnStretch(1, 1)
        super().setColumnStretch(2, 0)
        self.setAlignment(Qt.AlignmentFlag.AlignTop)

    # -- QFormLayout settings that have no meaning on a grid ---------------- #

    def setLabelAlignment(self, alignment) -> None: ...
    def setFormAlignment(self, alignment) -> None: ...
    def setFieldGrowthPolicy(self, policy) -> None: ...
    def setRowWrapPolicy(self, policy) -> None: ...

    def rowWrapPolicy(self):
        return QFormLayout.RowWrapPolicy.DontWrapRows

    def fieldGrowthPolicy(self):
        return QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow

    def labelAlignment(self):
        return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

    # -- columns ------------------------------------------------------------ #

    def labelWidth(self) -> int:
        """The label column's shared minimum, 0 when it sizes itself."""
        return self._labelWidth

    def setLabelWidth(self, width: int) -> None:
        self._labelWidth = max(0, int(width))
        self.setColumnMinimumWidth(0, self._labelWidth)
        self.invalidate()

    def fieldWidth(self) -> int:
        """What the widest labelled row needs beside its label column.

        DP-5657. The field, the unit column when there is one, and the gaps
        between them: the label column may take only what that leaves.
        """
        def need(obj):
            if isinstance(obj, QWidget):
                if not _alive(obj) or obj.isHidden():
                    return 0
                return max(obj.minimumWidth(),
                           obj.minimumSizeHint().width())
            if isinstance(obj, QLayout):
                return obj.minimumSize().width()
            return 0
        widest = 0
        for row in self._rows:
            if row.span is not None or row.label is None:
                continue
            field = need(row.field)
            if not field:
                continue
            if row.unit is not None:
                field += COLUMN_GAP + max(need(row.unit),
                                          self.columnMinimumWidth(2))
            widest = max(widest, field)
        if not widest:
            return 0
        margins = self.contentsMargins()
        return widest + COLUMN_GAP + margins.left() + margins.right()

    def labelWidgets(self) -> list:
        return [row.label for row in self._rows
                if isinstance(row.label, QWidget) and _alive(row.label)]

    # -- building rows ------------------------------------------------------ #

    def _host(self):
        return self.parentWidget()

    def _as_label(self, label):
        if isinstance(label, str):
            text = QLabel(label, self._host())
            return text
        return label

    def _place(self, index: int, row: _Row) -> None:
        """Put one row on grid line ``index`` (the grid's own calls)."""
        def put(obj, column, span):
            if obj is None:
                return
            alignment = Qt.AlignmentFlag(0)
            if isinstance(obj, QWidget):
                QGridLayout.addWidget(self, obj, index, column, 1, span,
                                      alignment)
            elif isinstance(obj, QLayout):
                QGridLayout.addLayout(self, obj, index, column, 1, span,
                                      alignment)
        if row.span is not None:
            put(row.span, 0, 3)
            return
        put(row.label, 0, 1)
        if row.unit is None:
            put(row.field, 1, 2)
        else:
            put(row.field, 1, 1)
            put(row.unit, 2, 1)

    def _relayout(self) -> None:
        """Rebuild the grid lines after a row went in the middle or out."""
        while QGridLayout.count(self):
            QGridLayout.takeAt(self, 0)
        for index, row in enumerate(self._rows):
            self._place(index, row)
        self.setColumnMinimumWidth(
            2, UNIT_COLUMN_WIDTH if any(r.unit for r in self._rows) else 0)
        self.invalidate()

    def _adopt(self, label, field, span=None, unit=None) -> _Row:
        host = self._host()
        label = self._as_label(label)
        for obj in (label, field, span, unit):
            if isinstance(obj, QWidget) and host is not None \
                    and obj.parentWidget() is None:
                obj.setParent(host)
        if isinstance(label, QLabel) and isinstance(field, QWidget) \
                and label.buddy() is None:
            label.setBuddy(field)
        for obj in (field, span):
            if isinstance(obj, QWidget):
                elide_combos(obj)
        return _Row(label, field, span, unit)

    def insertRow(self, row: int, *args) -> None:
        """``insertRow(row, label, field)`` / ``insertRow(row, widget)``."""
        if len(args) == 1:
            entry = self._adopt(None, None, span=args[0])
        else:
            unit = args[2] if len(args) > 2 else None
            entry = self._adopt(args[0], args[1], unit=unit)
        row = len(self._rows) if row < 0 else min(row, len(self._rows))
        if row == len(self._rows):
            self._rows.append(entry)
            self._place(row, entry)
            if entry.unit is not None:
                self.setColumnMinimumWidth(2, UNIT_COLUMN_WIDTH)
        else:
            self._rows.insert(row, entry)
            self._relayout()

    def addRow(self, *args) -> None:
        """``addRow(label, field[, unit])`` or ``addRow(widget)`` (spanning)."""
        self.insertRow(len(self._rows), *args)

    def addUnitRow(self, label, field, unit) -> None:
        self.insertRow(len(self._rows), label, field, unit)

    def setWidget(self, row: int, role, widget) -> None:
        number = _role_number(role)
        entry = self._rows[row]
        old = entry.get(number)
        if old is not None:
            self._detach(old)
        entry.put(number, widget)
        if isinstance(widget, QWidget) and widget.parentWidget() is None:
            widget.setParent(self._host())
        self._relayout()

    # -- reading rows ------------------------------------------------------- #

    def rowCount(self) -> int:
        return len(self._rows)

    def itemAt(self, first, role=None):
        """``itemAt(index)`` for the grid, ``itemAt(row, role)`` for callers."""
        if role is None:
            return QGridLayout.itemAt(self, first)
        if not 0 <= first < len(self._rows):
            return None
        obj = self._rows[first].get(_role_number(role))
        if obj is None:
            return None
        return QGridLayout.itemAt(self, self.indexOf(obj))

    def _find(self, target):
        """``(row, role)`` of a widget or layout, ``(-1, 0)`` when absent."""
        for index, row in enumerate(self._rows):
            for role in (_LABEL, _FIELD, _SPAN):
                if row.get(role) is target:
                    return index, role
            if row.unit is target:
                return index, _FIELD
        return -1, _LABEL

    def getWidgetPosition(self, widget):
        row, role = self._find(widget)
        return row, _ROLE_ENUM[role]

    getLayoutPosition = getWidgetPosition

    def labelForField(self, field):
        row, _role = self._find(field)
        if row < 0:
            return None
        return self._rows[row].label

    def _row_index(self, target) -> int:
        if isinstance(target, int):
            return target
        return self._find(target)[0]

    def setRowVisible(self, target, visible: bool) -> None:
        row = self._row_index(target)
        if not 0 <= row < len(self._rows):
            return
        for member in self._rows[row].members():
            for widget in _widgets_of(member):
                widget.setVisible(bool(visible))
        self.invalidate()

    def isRowVisible(self, target) -> bool:
        row = self._row_index(target)
        if not 0 <= row < len(self._rows):
            return False
        return any(not widget.isHidden()
                   for member in self._rows[row].members()
                   for widget in _widgets_of(member))

    # -- taking rows away --------------------------------------------------- #

    def _detach(self, obj, delete: bool = False) -> None:
        if not _alive(obj):
            return
        if isinstance(obj, QWidget):
            QGridLayout.removeWidget(self, obj)
            if delete:
                obj.hide()
                obj.setParent(None)
                obj.deleteLater()
        elif isinstance(obj, QLayout):
            QGridLayout.removeItem(self, obj)
            if delete:
                for widget in _widgets_of(obj):
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
                obj.deleteLater()

    def removeRow(self, target) -> None:
        """Take a row away and delete what was in it (as ``QFormLayout``)."""
        row = self._row_index(target)
        if not 0 <= row < len(self._rows):
            return
        entry = self._rows.pop(row)
        for member in entry.members():
            self._detach(member, delete=True)
        self._relayout()

    def takeRow(self, target):
        row = self._row_index(target)
        if not 0 <= row < len(self._rows):
            return None
        entry = self._rows.pop(row)
        for member in entry.members():
            self._detach(member)
        self._relayout()
        return entry

    def takeAt(self, index: int):
        """Keep the row list true when the grid is emptied item by item."""
        item = QGridLayout.takeAt(self, index)
        if item is not None:
            target = item.widget() if item.widget() is not None \
                else item.layout()
            for entry in list(self._rows):
                for role in (_LABEL, _FIELD, _SPAN):
                    if entry.get(role) is target:
                        entry.put(role, None)
                if entry.unit is target:
                    entry.unit = None
                if not entry.members():
                    self._rows.remove(entry)
        return item

    def addWidget(self, widget, *position) -> None:
        """``addWidget(w)`` or a grid position past the last row: a new row.

        ``inline_error`` appends its banner to a grid layout with
        ``addWidget(w, rowCount(), 0, 1, columns)``; that lands as a spanning
        row here, so the row list stays the only truth.
        """
        if position and position[0] < len(self._rows):
            QGridLayout.addWidget(self, widget, *position)
            return
        self.addRow(widget)


# --------------------------------------------------------------------------- #
# Text that does not fit is elided, never clipped (MF14, audit B13)
# --------------------------------------------------------------------------- #

_FULL_TEXT = 'foammeshFullText'
_ELIDER = 'foammeshElider'


class _ComboElider(QObject):
    """Paint a combo box's text with an ellipsis when the box is too narrow.

    ``QComboBox`` clips the chosen text at the edge of the field: "snappy: 2
    ranks · Gmsh: 2 thread" and "pipe r1 (imported]" were cut mid-word. The
    paint is the one ``QComboBox::paintEvent`` does, with the text elided to
    the width the style gives the edit field; the whole text is added to the
    tooltip so nothing a reader needs is lost.
    """

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo
        combo.installEventFilter(self)
        combo.currentIndexChanged.connect(self._tooltip)
        self._tooltip()

    def _tooltip(self, *_args) -> None:
        combo = self._combo
        if not _alive(combo):
            return
        text = combo.currentText()
        base = combo.property(_FULL_TEXT + 'Base')
        if base is None:
            base = combo.toolTip()
            combo.setProperty(_FULL_TEXT + 'Base', base)
        elided = self.isElided()
        combo.setProperty(_FULL_TEXT, text if elided else '')
        if elided and text and text not in (base or ''):
            combo.setToolTip(text if not base else text + chr(10) * 2 + base)
        elif combo.toolTip() != base and not elided:
            combo.setToolTip(base)

    def _room(self, opt: QStyleOptionComboBox) -> int:
        combo = self._combo
        rect = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, combo)
        icon = 0 if opt.currentIcon.isNull() else opt.iconSize.width() + 4
        return max(0, rect.width() - icon - 4)

    def isElided(self) -> bool:
        combo = self._combo
        if not _alive(combo) or combo.isEditable():
            return False
        opt = QStyleOptionComboBox()
        combo.initStyleOption(opt)
        text = combo.currentText()
        return (combo.width() > 0 and bool(text)
                and combo.fontMetrics().horizontalAdvance(text)
                > self._room(opt))

    def eventFilter(self, watched, event):
        if watched is not self._combo:
            return False
        kind = event.type()
        if kind == QEvent.Type.Resize:
            self._tooltip()
            return False
        if kind != QEvent.Type.Paint or self._combo.isEditable():
            return False
        combo = self._combo
        opt = QStyleOptionComboBox()
        combo.initStyleOption(opt)
        full = opt.currentText
        room = self._room(opt)
        if not full or combo.fontMetrics().horizontalAdvance(full) <= room:
            return False
        opt.currentText = combo.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideRight, room)
        painter = QStylePainter(combo)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)
        painter.end()
        return True


def elide_combos(widget) -> int:
    """Make every combo box under ``widget`` elide instead of clip.

    Returns the number of combo boxes newly covered.
    """
    count = 0
    candidates = []
    if isinstance(widget, QComboBox):
        candidates.append(widget)
    if isinstance(widget, QWidget):
        candidates.extend(widget.findChildren(QComboBox))
    for combo in candidates:
        if combo.property(_ELIDER):
            continue
        combo.setProperty(_ELIDER, True)
        # A box that sizes itself to its longest entry cannot shrink below it,
        # so it would push the sheet wider instead of eliding.
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(4)
        _ComboElider(combo)
        count += 1
    # A spin box's minimum is its widest text, special value included
    # ("Auto (OpenFOAM default)"); the grid has no wrapped second line to
    # fall back on, so it may shrink and clip instead of widening the sheet.
    spins = []
    if isinstance(widget, QAbstractSpinBox):
        spins.append(widget)
    if isinstance(widget, QWidget):
        spins.extend(widget.findChildren(QAbstractSpinBox))
    for spin in spins:
        if spin.minimumWidth() == 0:
            spin.setMinimumWidth(72)
    return count


# --------------------------------------------------------------------------- #
# A card with nothing in it is not drawn (MF14)
# --------------------------------------------------------------------------- #

_EMPTY_HIDDEN = 'foammeshEmptyCardHidden'
_CARD_GUARD = 'foammeshCardGuard'


class CardGuard(QObject):
    """Hide a group box while every widget in it is hidden.

    A frame and a title around nothing is the "empty Grading group" of the
    audit. The box is hidden by this guard (and remembers that it was) when
    its last visible child goes, and shown again when a child comes back; a
    box some other code hid is never shown by it.
    """

    def __init__(self, box: QGroupBox) -> None:
        super().__init__(box)
        self._box = box
        self._pending = False
        box.setProperty(_CARD_GUARD, True)
        box.installEventFilter(self)
        for child in box.children():
            self._watch(child)
        self.schedule()

    def _watch(self, child) -> None:
        if isinstance(child, QWidget):
            child.installEventFilter(self)

    def schedule(self) -> None:
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self.evaluate)

    def evaluate(self) -> None:
        self._pending = False
        box = self._box
        if not _alive(box):
            return
        children = [child for child in box.children()
                    if isinstance(child, QWidget)]
        occupied = any(not child.isHidden() for child in children)
        if not occupied and not box.isHidden():
            box.setProperty(_EMPTY_HIDDEN, True)
            box.hide()
        elif occupied and box.property(_EMPTY_HIDDEN):
            box.setProperty(_EMPTY_HIDDEN, False)
            box.show()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self._box:
            if kind == QEvent.Type.ChildAdded:
                child = event.child()
                if isinstance(child, QWidget):
                    self._watch(child)
                self.schedule()
            elif kind == QEvent.Type.ChildRemoved:
                self.schedule()
        elif kind in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self.schedule()
        return False


def guard_cards(root: QWidget, skip=()) -> int:
    """Put a :class:`CardGuard` on every group box under ``root``."""
    count = 0
    for box in root.findChildren(QGroupBox):
        if box in skip or box.property(_CARD_GUARD):
            continue
        CardGuard(box)
        count += 1
    return count


# --------------------------------------------------------------------------- #
# One label column per page
# --------------------------------------------------------------------------- #

def _natural_width(label: QWidget) -> int:
    """The width the label's text asks for on one line."""
    if isinstance(label, QLabel) and label.text():
        metrics = label.fontMetrics()
        text = label.text()
        if label.textFormat() == Qt.TextFormat.RichText:
            return label.sizeHint().width()
        margin = 2 * label.margin() + 2 * label.frameWidth()
        cm = label.contentsMargins()
        margin += cm.left() + cm.right()
        return metrics.horizontalAdvance(text.replace('&', '')) + margin + 2
    return label.sizeHint().width()


_NATURAL = '_foammeshNaturalWidth'


def _natural_key(label: QWidget):
    """What a label's one-line width is measured from."""
    cm = label.contentsMargins()
    text = label.text() if hasattr(label, 'text') else None
    rich = (isinstance(label, QLabel)
            and label.textFormat() == Qt.TextFormat.RichText)
    return (type(label).__name__, text, label.font().key(), rich,
            label.margin() if isinstance(label, QLabel) else 0,
            label.frameWidth() if isinstance(label, QLabel) else 0,
            cm.left(), cm.right())


def _cached_natural_width(label: QWidget) -> int:
    """``_natural_width`` once per text, font and margins.

    A rich-text label (or any non-label) is measured from its size hint,
    which a wrap or a cap changes, so it is measured free of both -- the
    first time only.
    """
    key = _natural_key(label)
    held = getattr(label, _NATURAL, None)
    if held is not None and held[0] == key:
        return held[1]
    plain = (isinstance(label, QLabel) and label.text()
             and label.textFormat() != Qt.TextFormat.RichText)
    if not plain:
        if isinstance(label, QLabel):
            label.setWordWrap(False)
        label.setMinimumWidth(0)
        label.setMaximumWidth(16777215)
    width = _natural_width(label)
    try:
        setattr(label, _NATURAL, (key, width))
    except (AttributeError, TypeError):
        pass
    return width


def _labels_of(form) -> list:
    """The label widgets of a ``SheetForm`` or a legacy ``QFormLayout``."""
    if isinstance(form, SheetForm):
        return form.labelWidgets()
    labels = []
    if isinstance(form, QFormLayout):
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            widget = item.widget() if item is not None else None
            if widget is not None:
                labels.append(widget)
    return labels


def _longest_word(label) -> int:
    """The width a label cannot wrap below: its longest word, measured from
    the text (a label not yet wrapped reports its whole line as minimum).
    Qt's line breaker also breaks after a hyphen, so "Non-orthogonality"
    is two pieces."""
    if not isinstance(label, QLabel):
        return label.minimumSizeHint().width()
    words = [piece for word in label.text().split()
             for piece in re.split(r'(?<=-)', word) if piece]
    if not words:
        return 0
    m = label.contentsMargins()
    return (max(label.fontMetrics().horizontalAdvance(w) for w in words)
            + 2 * max(label.margin(), 0) + max(label.indent(), 0)
            + m.left() + m.right() + 1)


def share_label_width(forms, available: int = 0, room: int = 0) -> int:
    """Give several forms one label column, capped at 44 % of the page.

    ``available`` is the width the page offers its forms; 0 means "no cap
    known yet". ``room`` is what a form may really take, when the caller
    measured it (DP-5657); the fields beside the column must fit in it. Labels wider than the column wrap inside it, so no label is
    ever cut and no editor is pushed off the sheet.

    A form the page still builds with ``QFormLayout`` (a ``FieldGroupPage``
    panel) joins the same column: its labels get the shared minimum width and
    the left alignment the grid uses, so the page reads as one form.

    Returns the width applied (0 when no form has a label).
    """
    forms = [form for form in forms if form is not None]
    per_form = [(form, _labels_of(form)) for form in forms]
    labels = [label for _form, group in per_form for label in group]
    if not labels:
        for form, _group in per_form:
            if isinstance(form, SheetForm):
                form.setLabelWidth(0)
        return 0
    # Plan 45 PINQ. Each label is measured once per text and font, and a
    # label is only touched when what it ends with differs from what it has.
    # MEASURED live: un-wrapping, un-capping and re-capping every label on
    # every run laid each one out six times over, a pin's column relayout.
    naturals = [_cached_natural_width(label) for label in labels]
    bounds = [(label.minimumWidth(), label.maximumWidth()) for label in labels]
    natural = max(naturals)
    width = natural
    if available > 0:
        width = min(natural, int(available * LABEL_SHARE))
        # DP-5657. 44 % is the column's share, not a promise the fields
        # beside it fit: a number box and its unit took 149 px of the
        # Quality page's 223, so the 98 px column pushed the sheet 8 px
        # wider than its viewport. The column gives way to the widest field
        # row, and never below the longest word of a label (it wraps).
        fields = max((form.fieldWidth() for form, _group in per_form
                      if isinstance(form, SheetForm)), default=0)
        if fields:
            words = max(_longest_word(label) for label in labels)
            width = min(width, max((room or available) - fields, words))
    in_sheet_form = set()
    for form, group in per_form:
        if isinstance(form, SheetForm):
            in_sheet_form.update(id(label) for label in group)
    for label, own in zip(labels, naturals):
        if isinstance(label, QLabel):
            wrap = own > width
            if label.wordWrap() != wrap:
                label.setWordWrap(wrap)
        # A wrapped label's size hint is not its column; the column is
        # `width`, so the label may not ask for more.
        label.setMaximumWidth(max(width, 1))
        if id(label) in in_sheet_form:
            label.setMinimumWidth(0)
    for form, group in per_form:
        if isinstance(form, SheetForm):
            if getattr(form, '_labelWidth', None) != width:
                form.setLabelWidth(width)
        else:
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
            form.setHorizontalSpacing(COLUMN_GAP)
            fm = form.contentsMargins()
            form.setContentsMargins(0, fm.top(), 0, fm.bottom())
            # A panel's card wraps its form in a layout with a 4 px inset the
            # grid cards do not have; the columns only line up without it.
            holder = form.parent()
            if (holder is not None and hasattr(holder, 'contentsMargins')
                    and not isinstance(holder, QWidget)):
                m = holder.contentsMargins()
                holder.setContentsMargins(0, m.top(), 0, m.bottom())
            for label in group:
                label.setMinimumWidth(width)
    # DP-2818. QLabel keeps the size hint it worked out under the minimum
    # width it had then: MEASURED, a label capped from 139 px to 118 px went
    # on asking its layout for 139 ("Project name", 76 px of text), and every
    # label this column had narrowed read as cut. Re-setting the indent is
    # the cheapest call that makes a QLabel measure again; it is made only
    # for a label whose bounds this run changed.
    for label, before in zip(labels, bounds):
        if (isinstance(label, QLabel)
                and before != (label.minimumWidth(), label.maximumWidth())):
            label.setIndent(label.indent())
    return width


class _ButtonEliser(QObject):
    """Let a check box or fold header give its width back.

    A button's minimum is its whole text, so one long label made the sheet
    scroll sideways. The button may shrink (explicit minimum width), shows
    an ellipsis when it has, and keeps the whole text in ``fullText`` and the
    tooltip.
    """

    ROOM = 28   # indicator / chevron, icon gap and a little air
    MINIMUM = 60

    def __init__(self, button) -> None:
        super().__init__(button)
        self._button = button
        self._full = button.text()
        button.setProperty('foammeshFullText', self._full)
        button.setMinimumWidth(self.MINIMUM)
        button.installEventFilter(self)

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj is self._button and kind == QEvent.Type.Resize:
            self._elide()
        return False

    def _elide(self) -> None:
        button = self._button
        shown = button.text()
        # Text set from outside since the last elision is the new full text.
        if shown != self._elided and shown != self._full:
            self._full = shown
            button.setProperty('foammeshFullText', shown)
        room = max(0, button.width() - self.ROOM)
        text = button.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, room)
        self._elided = text
        if text != shown:
            button.setText(text)
        if text != self._full and not button.toolTip():
            button.setToolTip(self._full)

    _elided = ''


def shrink_button_text(buttons) -> None:
    """Make every check box / fold header in ``buttons`` shrinkable."""
    for button in buttons:
        if button.property('foammeshFullText') is None and button.text():
            _ButtonEliser(button)


class LabelWidthWatcher(QObject):
    """Re-share the label column when the page is resized.

    The cap is 44 % of the width the page is given, so a page that is
    narrowed re-measures. Installed once on the page's body.
    """

    def __init__(self, host: QWidget, forms) -> None:
        super().__init__(host)
        self._host = host
        self._forms = forms
        host.installEventFilter(self)
        self._busy = False
        self._viewport = None
        self._measured = None

    def _inputs(self):
        """Everything the shared column is measured from: the width offered,
        and each label's text and font (a theme or density change alters the
        font while the width stays put)."""
        labels = tuple((label.text() if hasattr(label, 'text') else '',
                        label.font().key())
                       for form in self._forms() if form is not None
                       for label in _labels_of(form))
        return (self._offered() if self._host.isVisible() else 0, labels)

    def run(self) -> int:
        if self._busy or not _alive(self._host):
            return 0
        self._busy = True
        try:
            self._measured = self._inputs()
            # A host that has not been shown has no real width yet; the
            # first Resize after it is shown measures again.
            from PySide6.QtWidgets import QCheckBox
            shrink_button_text(self._host.findChildren(QCheckBox))
            offered = self._offered() if self._host.isVisible() else 0
            available = room = 0
            if offered:
                # The page margins and the card padding take their share of
                # what is offered; the 44 % is of what is left for the form.
                available = max(1, offered - SHEET_INSET)
                room = self._room(offered)
            return share_label_width(self._forms(), available, room)
        finally:
            self._busy = False

    def _room(self, offered: int) -> int:
        """DP-5657. What the widest form may take of ``offered``: the host's
        own padding around its forms, measured, rather than SHEET_INSET's
        page margins, which a body inside a scroller does not have."""
        widest = max((form.minimumSize().width() for form in self._forms()
                      if isinstance(form, SheetForm) and form.parentWidget()
                      is not None and form.parentWidget().isVisible()),
                     default=0)
        if not widest:
            return 0
        around = self._host.minimumSizeHint().width() - widest
        return max(1, offered - max(around, 0)) if around >= 0 else 0

    def _offered(self) -> int:
        """The host's width, or the scroller viewport's when that is less.

        A body inside a scroll area is as wide as its own minimum, which the
        label column sets; the width the page is *given* is the viewport's.
        """
        from PySide6.QtWidgets import QScrollArea
        width = self._host.width()
        widget = self._host.parentWidget()
        while widget is not None:
            if isinstance(widget, QScrollArea):
                if widget.widget() is not None:
                    width = min(width, widget.viewport().width())
                    if self._viewport is not widget.viewport():
                        self._viewport = widget.viewport()
                        self._viewport.installEventFilter(self)
                break
            widget = widget.parentWidget()
        return width

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show) and (
                watched is self._host or watched is self._viewport):
            # Plan 45 PINQ. A pin or unpin changes the column's height only;
            # the column measured from the same width and fonts is the same
            # column. MEASURED live: re-measuring it was a sixth of a press.
            if (event.type() == QEvent.Type.Resize and not self._busy
                    and _alive(self._host)
                    and self._measured == self._inputs()):
                return False
            self.run()
        return False
