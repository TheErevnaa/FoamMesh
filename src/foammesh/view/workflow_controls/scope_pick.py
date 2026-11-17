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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from foammesh.app import app


#: Where a long name may wrap: after each of these a zero-width space is put.
_BREAK_AFTER = '_-./\\'


def breakable(text: str) -> str:
    """*text* with a wrap opportunity after every separator (DP-6119)."""
    return ''.join(ch + '\u200b' if ch in _BREAK_AFTER else ch
                   for ch in str(text))


class ScopePickRow(QWidget):
    """``[Use selected in viewport]`` over a chip, for one scope field.

    DP-6119. MEASURED live 2026-11-01 on the CHT tubes, Gmsh Structure ->
    Volume controls -> Add: the chip beside the button read
    `01_hot_fluid_r10mm_L500mm`, one word to Qt's word wrap, 176 px that would
    not wrap, so the page grew to 501 px in a 350 px column and the docked
    form's OK and Cancel were drawn past its right edge, out of sight. The
    chip sits under the button and wraps at the name's separators.
    """

    def __init__(self, panel, key: str, parent=None):
        super().__init__(parent)
        self._panel = panel
        self._key = key
        self.setObjectName('childPanelScopePickRow')
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._button = QPushButton(self.tr('Use selected in viewport'), self)
        self._button.setObjectName('childPanelScopePick')
        self._button.setToolTip(self.tr(
            'Click the surface, curve or region in the viewport, then press '
            'this to set the field to it'))
        self._button.clicked.connect(self.useSelection)
        # DP-6125. The button's text sets the row's minimum width, and a form
        # docked in the 205 px settings column was drawn wider than the column
        # (clipped sideways). It may be narrower than its text instead.
        self._button.setSizePolicy(QSizePolicy.Policy.Ignored,
                                   QSizePolicy.Policy.Fixed)
        self._button.setMinimumWidth(0)
        row.addWidget(self._button)
        self._chipPlain = ''
        self._chip = QLabel(self)
        self._chip.setObjectName('childPanelScopeChip')
        self._chip.setProperty('foammeshStatus', 'info')
        self._chip.setWordWrap(True)
        row.addWidget(self._chip)
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
        usable.extend(self._pickedInModel(options, by_label))
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

    def _pickedInModel(self, options: dict, by_label: dict) -> list:
        """DP-6162. The curves, surfaces and points picked in the Gmsh model
        view (the entity panel's live or accepted pick) that this field offers
        by name, so the button takes them as it takes a boundary."""
        try:
            from foammesh.view.gmsh_workflow.shared_pick import selection_service

            source = selection_service().pick_source
            picked = tuple(source.pickedEntities()) if source else ()
            picker = source.picker() if source is not None else None
        except Exception:                                   # noqa: BLE001
            return []
        found = []
        for entity in picked:
            names = [picker.map.name_of(entity)] if picker is not None else []
            record = (picker.map.records.get(tuple(entity))
                      if picker is not None else None)
            names.extend('physical %d' % t for t in (record.physical
                                                     if record else ()))
            names.append('%s%d' % ('PCSV'[int(entity[0])], int(entity[1])))
            for name in names:
                if name in options:
                    found.append(name)
                    break
                if name in by_label:
                    found.append(by_label[name])
                    break
        return found

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
            self._setChip('')
            return
        text = label
        if more > 0:
            text = self.tr('%s (%d more selected; one field takes one)') % (
                label, more)
        self._setChip(text)

    def chipText(self) -> str:
        return self._chipPlain

    def _say(self, text: str) -> None:
        self._setChip(text)

    def _setChip(self, text: str) -> None:
        self._chipPlain = str(text)
        self._chip.setText(breakable(text))
        self._chip.setToolTip(self._chipPlain)


def addScopePickers(panel) -> None:
    """Give every scope field of *panel* its pick row (inline panels only)."""
    for key in tuple(panel._editors):
        if not (panel._is_scope_field(key) or key in panel._choices):
            continue
        if key in panel._annotations:
            continue
        panel._annotations[key] = ScopePickRow(panel, key, panel.editorHost())
