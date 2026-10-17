"""Plan 40 UXA1: the Mesh size control on Mesh setup."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QWidget)

from foammesh.view.facade_client import submit

from foammesh.view.workflow_controls.field_group_page import FieldGroupPage


class MeshSizePage(FieldGroupPage):
    """Coarse / Medium / Fine / Custom and a one-line readout of what it means.

    One choice sizes both meshers (``core/sizing/derive.py``). It is written
    the moment it is chosen, like the target solver above it: nothing else on
    this page needs it applied first, and a reader who chose Fine and pressed
    the footer must get Fine.
    """

    token = 'mesh.size'
    heading = 'Mesh size'
    field_ids = ('mesh.size_preset',)
    purpose = (
        'Coarse, Medium or Fine sizes every value you leave on Auto, for '
        'either mesher. A size you type on a later page wins and turns this '
        'to Custom.')

    #: The sizes whose being typed makes the choice Custom.
    TYPED_FIELDS = ('meshing.base_grid.target_cell_size',
                    'gmsh.global_sizing.target_size')

    def build(self) -> None:
        super().build()
        self._readout = QLabel(self)
        self._readout.setObjectName('meshSizeReadout')
        self._readout.setWordWrap(True)
        self._readout.setProperty('foammeshTone', 'muted')
        self._form.layout().addRow(self._readout)
        self._refreshReadout()
        self._buildPresets()

    # -- physics presets (Plan 40 UXD8) -------------------------------------- #

    def _buildPresets(self) -> None:
        from foammesh.core.presets import preset_titles

        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self._presetBox = QComboBox(row)
        self._presetBox.setObjectName('physicsPresetBox')
        self._presetBox.setAccessibleName(self.tr('Physics preset'))
        self._presetBox.addItem(self.tr('None'), '')
        for key, title in preset_titles().items():
            self._presetBox.addItem(title, key)
        self._presetApply = QPushButton(self.tr('Apply preset'), row)
        self._presetApply.setObjectName('physicsPresetApply')
        self._presetApply.setToolTip(self.tr(
            'Fills only the settings you have not changed, and lists them'))
        # DP-385: `clicked` hands over its flag, which is not a preset key.
        self._presetApply.clicked.connect(lambda: self.applyPreset())
        layout.addWidget(self._presetBox, 1)
        layout.addWidget(self._presetApply)
        self._presetResult = QLabel(self)
        self._presetResult.setObjectName('physicsPresetResult')
        self._presetResult.setWordWrap(True)
        self._presetResult.setProperty('foammeshTone', 'muted')
        # Plan 44 F1. An empty line under the preset row reserved 22 px of
        # the 700 px column for a sentence that only Apply writes; it shows
        # when there is something to say.
        self._presetResult.setVisible(False)
        # The row's label names the box it sits beside, so a screen reader
        # hears the label the screen shows (DP-218).
        label = QLabel(self)
        label.setText(self.tr('Physics preset'))
        label.setBuddy(self._presetBox)
        self._form.layout().addRow(label, row)
        self._form.layout().addRow(self._presetResult)
        # Plan 44 F4 (MF14). The preset names size the box to the longest
        # one; it elides in the sheet column, the full name in its tooltip.
        from foammesh.view.theming.sheet_form import elide_combos
        elide_combos(self._form)

    def presetPlan(self, key: str):
        """What ``key`` would write now: untouched fields only."""
        from foammesh.core.presets import PRESETS, plan_preset

        ids = tuple(field_id for field_id, _ in PRESETS[key].values)
        current = self._client.field_values(ids)
        defaults = {field_id: self._client.descriptor(field_id).default
                    for field_id in ids}
        return plan_preset(key, current, defaults)

    def applyPreset(self, key=None):
        """Write the chosen preset's untouched fields and list them."""
        if not isinstance(key, str):
            key = self._presetBox.currentData()
        if not key:
            return None
        plan = self.presetPlan(key)
        self._presetResult.setText(plan.summary())
        self._presetResult.setVisible(bool(self._presetResult.text()))
        if not plan.fill:
            return None
        return submit(self._client, 'configuration.patch',
                      {'patch': dict(plan.fill)})

    # -- what the control reads --------------------------------------------- #

    def typedSizes(self) -> dict:
        try:
            values = self._client.field_values(self.TYPED_FIELDS)
        except Exception:                                   # noqa: BLE001
            return {}
        return {key: value for key, value in values.items()
                if value not in (None, '')}

    def proposal(self):
        from foammesh.core.sizing import derive_sizes, effective_preset

        stored = (self._pending.get('mesh.size_preset')
                  or self._storedPreset())
        typed = self.typedSizes()
        base = typed.get('meshing.base_grid.target_cell_size')
        target = typed.get('gmsh.global_sizing.target_size')
        bounds = self.modelBounds()
        return (derive_sizes(
            stored, bounds,
            typed={'background_cell': base, 'gmsh_target_size': target})
            if bounds is not None else None), effective_preset(
                stored, tuple(typed.values()))

    def _storedPreset(self):
        try:
            return self._client.field_values(
                ('mesh.size_preset',)).get('mesh.size_preset')
        except Exception:                                   # noqa: BLE001
            return None

    def modelBounds(self):
        """The model's extent as ``(xmin, xmax, ymin, ymax, zmin, zmax)``."""
        try:
            from foammesh.app import app
            manager = getattr(getattr(app, 'window', None),
                              'geometryManager', None)
            surfaces = getattr(manager, 'getSurfaceBounds', None)
            extent = surfaces() if surfaces is not None else None
            return None if extent is None else tuple(extent.toTuple())
        except Exception:                                   # noqa: BLE001
            return None

    def readoutText(self) -> str:
        return self._readout.text()

    def _refreshReadout(self) -> None:
        from foammesh.core.sizing import CUSTOM, readout_text

        readout = getattr(self, '_readout', None)
        if readout is None:
            return
        proposal, effective = self.proposal()
        text = readout_text(proposal)
        if effective == CUSTOM:
            text += ('  Custom: a size you typed is kept; the rest follow '
                     'Medium.')
        readout.setText(text)

    def reload(self, *, discard_pending: bool = True) -> None:
        super().reload(discard_pending=discard_pending)
        from foammesh.core.sizing import CUSTOM, effective_preset

        editor = self._editors.get('mesh.size_preset')
        if editor is not None and not self._pending:
            # Custom is shown as soon as a derived size was typed.
            shown = effective_preset(
                self._storedPreset(), tuple(self.typedSizes().values()))
            if shown == CUSTOM:
                editor.set_value(CUSTOM)
        self._refreshReadout()

    def _on_changed(self, field_id: str, value) -> None:
        super()._on_changed(field_id, value)
        self._refreshReadout()
        self.apply()
