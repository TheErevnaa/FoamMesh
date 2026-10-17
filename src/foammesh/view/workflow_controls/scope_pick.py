"""Plan 41 GN18: pick an entity-list field's value from the viewport.

A collection row refers to the geometry it acts on (a surface group, a curve
group, a region) through a combo box of the prepared scopes. The viewport
already keeps one selection, and a click on the model puts the entity in it, so
the field offers the other half of the pick: a button that takes what is
selected, and a chip that names what the field holds. Nothing else is written
by the button, and a selection the field cannot use is said, not guessed at.

The row is an annotation of the editor (``ChildControlPanel.addAnnotation``),
so the row editor lays it out under the field and shows it with the field.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QWidget,
)

from foammesh.app import app


class ScopePickRow(QWidget):
    """``[Use selected in viewport]  chip`` for one scope or choice field."""

    def __init__(self, panel, key: str, parent=None):
        super().__init__(parent)
        self._panel = panel
        self._key = key
        self.setObjectName('childPanelScopePickRow')
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._button = QPushButton(self.tr('Use selected in viewport'), self)
        self._button.setObjectName('childPanelScopePick')
        self._button.setToolTip(self.tr(
            'Click the surface, curve or region in the viewport, then press '
            'this to set the field to it'))
        self._button.clicked.connect(self.useSelection)
        row.addWidget(self._button)
        self._chip = QLabel(self)
        self._chip.setObjectName('childPanelScopeChip')
        self._chip.setProperty('foammeshStatus', 'info')
        self._chip.setWordWrap(True)
        row.addWidget(self._chip, 1)
        self.showChip()

    @property
    def _editor(self):
        """The field's live editor, which the panel rebuilds when the
        prepared scopes change."""
        return self._panel.editor(self._key)

    # -- what the field can take ---------------------------------------------- #

    def options(self) -> dict:
        """``{value: label}`` of the choices the field offers and accepts."""
        found = {}
        if self._editor is None:
            return found
        widget = self._editor.editor
        count = getattr(widget, 'count', None)
        if not callable(count):
            return found
        for index in range(count()):
            value = widget.itemData(index)
            if value in (None, ''):
                continue
            found[str(value)] = widget.itemText(index)
        return found

    def useSelection(self) -> str:
        """Set the field to the first selected entity it offers; say which."""
        selected = tuple(app.selectionService.snapshot().selected_ids)
        options = self.options()
        by_label = {label: value for value, label in options.items()}
        usable = []
        for item in selected:
            if item in options:
                usable.append(item)
                continue
            # A field of numbered choices (a surface of the prepared
            # geometry) offers the entity under the name the boundary has.
            entity = app.selectionService.entity(item)
            label = str(getattr(entity, 'label', '') or '')
            if label in by_label:
                usable.append(by_label[label])
        if not usable:
            self._say(self.tr(
                'Nothing is selected in the viewport that this field offers. '
                'Click an entity first.' if not selected else
                'The selected entity is not one this field offers.'))
            return ''
        chosen = usable[0]
        editor = self._editor
        widget = editor.editor
        raw = next((widget.itemData(index) for index in range(widget.count())
                    if str(widget.itemData(index)) == chosen), chosen)
        editor.set_value(raw)
        editor.valueChanged.emit(editor.field_id, editor.value())
        extra = len(usable) - 1
        self.showChip(more=extra)
        return chosen

    # -- the chip -------------------------------------------------------------- #

    def showChip(self, more: int = 0) -> None:
        editor = self._editor
        held = (str(editor.value()) if editor is not None
                and editor.value() not in (None, '') else '')
        if editor is not None and getattr(self, '_watched', None) is not editor:
            editor.valueChanged.connect(lambda *_args: self.showChip())
            self._watched = editor
        label = self.options().get(held, '')
        if not held or not label:
            self._chip.setText('')
            return
        text = label
        if more > 0:
            text = self.tr('%s (%d more selected; one field takes one)') % (
                label, more)
        self._chip.setText(text)

    def chipText(self) -> str:
        return self._chip.text()

    def _say(self, text: str) -> None:
        self._chip.setText(text)


def addScopePickers(panel) -> None:
    """Give every scope field of *panel* its pick row (inline panels only)."""
    for key in tuple(panel._editors):
        if not (panel._is_scope_field(key) or key in panel._choices):
            continue
        if key in panel._annotations:
            continue
        panel._annotations[key] = ScopePickRow(panel, key, panel.editorHost())
