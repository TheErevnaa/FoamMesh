"""Typed editors generated from the AF2 field registry.

Plan 17 §5.18 requires every published Tier 1 field to expose its unit, valid
range, default, and documentation, and release gate 10 forbids a field that no
validation, derivation, or runner consumes.  Building the editors from the
registry descriptor - rather than hand-placing widgets - keeps the desktop in
step with the schema and makes an unbacked field impossible to render.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QSpinBox, QWidget,
)


#: Widened bounds used when the schema leaves a side unconstrained. Qt spin
#: boxes require finite limits, so an unbounded field still needs a range.
_UNBOUNDED = 1e12
_UNBOUNDED_INT = 2 ** 31 - 1


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
        accessible = f'{name} ({descriptor.unit})' if descriptor.unit else name
        self._editor.setAccessibleName(accessible)
        tooltip = _tooltip(descriptor)
        if tooltip:
            self._editor.setToolTip(tooltip)
            self.label.setToolTip(tooltip)
        self.unit_label = QLabel(descriptor.unit or '', self)
        _connect(self._editor, self._emit)
        # CP-09 item 4. Set from ``applies_when`` by whichever page owns this
        # editor. Until it is, every field applies -- which is what the
        # desktop did before the metadata was read at all.
        self._applies = True
        self._inactive_reason = ''
        self._tooltip = tooltip

    # -- conditional editors ----------------------------------------------- #

    def setApplicability(self, applies: bool, reason: str = '') -> None:
        """Grey an inactive setting and say why, without hiding it.

        CP-09 item 4 asks for the setting to stay visible: the value is real,
        it is what the case will use if the condition changes back, and a
        control that vanishes reads as a control that was never there. So it
        is disabled, labelled, and carries the sentence explaining the
        condition -- and :meth:`applies` lets the page keep it out of the
        patch, so an inactive field cannot stale the mesh from off screen.
        """
        self._applies = bool(applies)
        self._inactive_reason = '' if applies else str(reason or '')
        # Readable without hovering: a greyed row and nothing else reads as a
        # bug, and the tooltip is only found by someone who already suspects
        # one (CP-09 item 7).
        self.label.setText(self._name if self._applies
                           else f'{self._name} (inactive)')
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
    if descriptor.minimum is not None or descriptor.maximum is not None:
        low = '-inf' if descriptor.minimum is None else f'{descriptor.minimum:g}'
        high = 'inf' if descriptor.maximum is None else f'{descriptor.maximum:g}'
        parts.append(f'Range: {low} to {high}')
    if descriptor.default is not None:
        parts.append(f'Default: {descriptor.default}')
    if descriptor.invalidates:
        parts.append('Changing this stales: ' + ', '.join(descriptor.invalidates))
    return '\n'.join(parts)


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
    if value_type == 'integer':
        widget = QSpinBox(parent)
        widget.setRange(
            int(descriptor.minimum) if descriptor.minimum is not None else -_UNBOUNDED_INT,
            int(descriptor.maximum) if descriptor.maximum is not None else _UNBOUNDED_INT)
        return widget
    if value_type == 'number':
        widget = QDoubleSpinBox(parent)
        widget.setDecimals(9)
        widget.setRange(
            float(descriptor.minimum) if descriptor.minimum is not None else -_UNBOUNDED,
            float(descriptor.maximum) if descriptor.maximum is not None else _UNBOUNDED)
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
    if isinstance(widget, QSpinBox):
        # Persisted integers can arrive as scientific-notation strings
        # (e.g. maximum_cells default '1e7'); int() alone rejects those.
        widget.setValue(int(float(value)) if value not in (None, '') else 0)
        return
    if isinstance(widget, QDoubleSpinBox):
        widget.setValue(float(value if value is not None else 0.0))
        return
    widget.setText('' if value is None else str(value))


def _humanise(option: str) -> str:
    return str(option).replace('_', ' ').strip().capitalize()
