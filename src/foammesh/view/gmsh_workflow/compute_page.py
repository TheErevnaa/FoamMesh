"""Gmsh workflow page: gmsh.compute."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from .base import GmshTaskPage


class GmshComputePage(GmshTaskPage):
    """The run, and the quality gate it will be judged by.

    R113. The shipped gate -- ``sicn``, minimum 0.1, allowed fraction 0,
    allowed count 0 -- fails a layered mesh every time, because ``sicn`` is a
    scaled inverse condition number and a thin prism is exactly what it
    penalises. MEASURED on venturi.stl: first height 0.0003 / ratio 1.125 / 4
    layers put 13,479 of 65,958 elements (20.44%) below 0.1, and the obvious
    remedy -- a gentler stack, first height 0.00015 / ratio 1.2 / 3 layers --
    made it **worse**, 24,450 of 58,699 (41.65%). The same case with layers
    disabled passed the same gate. The gate is not silently loosened here:
    the numbers are the user's to set, so the page states the conflict and
    names the controls that resolve it.
    """

    task_id_default = 'gmsh.compute'

    #: Plan 30 WP-07 (F-36). Element order is a solver contract, and the page
    #: that offers it has to say so before the run rather than after it: a
    #: second-order mesh on the OpenFOAM route used to be accepted here, meshed,
    #: and then refused at publication with a message about CAD solids.
    ELEMENT_ORDER_FIELD = 'gmsh.compute.element_order'

    GATE_FIELDS = (
        'gmsh.compute.quality_type', 'gmsh.compute.allowed_fraction',
        'gmsh.compute.allowed_count', 'gmsh.compute.min_quality',
        'gmsh.boundary_layers.enabled',
    )

    def build_sections(self, layout) -> None:
        self._gateNote = QLabel(self)
        self._gateNote.setObjectName('gmshLayerGateNote')
        self._gateNote.setWordWrap(True)
        self._gateNote.setProperty('foammeshStatus', 'warning')
        self._gateNote.setVisible(False)
        layout.addWidget(self._gateNote)

    def refresh(self) -> None:
        super().refresh()
        self.updateElementOrderCapability()
        if not hasattr(self, '_gateNote'):
            return
        for field_id in self.GATE_FIELDS:
            editor = self._editors.get(field_id)
            if editor is not None:
                editor.valueChanged.connect(self._onGateFieldChanged)
        self.updateGateNote()

    def _onGateFieldChanged(self, *_args) -> None:
        self.updateGateNote()

    # -- element order, per target solver ---------------------------------- #

    def targetSolver(self) -> str:
        """Which solver this project is meshing for, as a plain token."""
        try:
            values = self._client.field_values(('mesh.target_solver',))
        except Exception:                        # noqa: BLE001 - advisory only
            return ''
        value = values.get('mesh.target_solver')
        return str(getattr(value, 'value', value) or '')

    def elementOrderOptions(self):
        """``(order, supported, reason)`` from the one capability table."""
        from foammesh.core.gmsh.plan_derivation import element_order_options

        return element_order_options(self.targetSolver())

    def updateElementOrderCapability(self) -> None:
        """Grey the orders this route cannot write, and say why.

        The capability table in ``plan_derivation`` is the single answer; this
        page renders it. Refusing here is what stops a user meshing for twenty
        minutes at an order the target cannot read.
        """
        options = self.elementOrderOptions()
        supported = [order for order, ok, _reason in options if ok]
        refused = [(order, reason) for order, ok, reason in options if not ok]
        self._orderRefusals = []
        editor = self._editors.get(self.ELEMENT_ORDER_FIELD)
        if editor is not None and supported:
            widget = editor.editor
            if not hasattr(self, '_orderTooltip'):
                self._orderTooltip = widget.toolTip()
            # The schema bounds stay as the schema wrote them -- they are what
            # the field can hold, and every numeric editor is checked against
            # them. What changes with the target is whether the control may be
            # used at all: when only one order is on offer there is nothing to
            # choose, so the control is greyed and pinned to it.
            try:
                current = int(editor.value())
            except (TypeError, ValueError):
                current = max(supported)
            if current > max(supported):
                # The page is taking the user's number away, so it says so in
                # the note: a greyed control with a tooltip is not an answer to
                # "where did my order 2 go".
                editor.set_value(max(supported))
                self._orderRefusals = [
                    self.tr('Element order %d is not offered: %s')
                    % (order, reason)
                    for order, reason in refused if order == current]
            widget.setEnabled(
                not editor.descriptor.read_only and len(supported) > 1)
            widget.setToolTip(refused[0][1] if refused else self._orderTooltip)
        if hasattr(self, '_gateNote'):
            self.updateGateNote()

    def gateValues(self) -> dict:
        """The gate as it stands on screen, editors first, then storage."""
        try:
            stored = self._client.field_values(self.GATE_FIELDS)
        except Exception:                        # noqa: BLE001 - advisory note
            stored = {}
        values = {}
        for field_id in self.GATE_FIELDS:
            editor = self._editors.get(field_id)
            value = editor.value() if editor is not None else None
            values[field_id] = stored.get(field_id) if value is None else value
        return values

    def gateCondemnsLayers(self) -> bool:
        """True when layers are on and the gate allows no layer elements."""
        values = self.gateValues()
        if not bool(values.get('gmsh.boundary_layers.enabled')):
            return False
        measure = str(getattr(values.get('gmsh.compute.quality_type'), 'value',
                              values.get('gmsh.compute.quality_type') or '')
                      ).split('.')[-1].lower()
        if measure != 'sicn':
            return False
        try:
            fraction = float(values.get('gmsh.compute.allowed_fraction') or 0.0)
            count = int(values.get('gmsh.compute.allowed_count') or 0)
        except (TypeError, ValueError):
            return False
        return fraction <= 0.0 and count <= 0

    def noteLines(self) -> list:
        """Everything this page has to say before a run, in one note.

        The element-order refusals and the layer-gate conflict are both
        pre-run warnings about the same button, so they share one block rather
        than each claiming a label of their own.
        """
        lines = list(getattr(self, '_orderRefusals', ()))
        # DP-115. This is the button the refused run is launched from, so the
        # pre-flight belongs here as well as on Boundary Layers -- the page
        # that holds the controls is not necessarily the page the user is
        # standing on when they start the run.
        refusal = self.assemblyRefusal()
        if refusal:
            lines.append(refusal)
        if self.gateCondemnsLayers():
            lines.append(self.gateNoteText())
        return lines

    def assemblyRefusal(self) -> str:
        """The run-start pre-flight, read with this page's layer switch.

        DP-115. The patch selection is read from storage -- it is authored on
        Boundary Layers -- while the switch is taken from the editor here,
        which is the one term of the condition this page can change.
        """
        return self.assemblyLayerRefusal(enabled=bool(
            self.gateValues().get('gmsh.boundary_layers.enabled')))

    def runAllRefusal(self) -> str:
        """DP-123. Graded with this page's switch, not only with storage."""
        return self.assemblyRefusal()

    def gateNoteText(self) -> str:
        return self.tr(
            'Boundary layers are enabled and this gate rejects every element '
            'below the sicn limit. A thin prism scores low on sicn by '
            'construction: measured on venturi.stl, a four-layer stack put '
            '20.4% of elements below the default 0.1 and a gentler stack made '
            'it worse (41.7%), while the same case without layers passed. '
            'Set an Allowed fraction or Allowed count you can defend, or turn '
            'layers off on Boundary layers, before running.')

    def updateGateNote(self) -> None:
        # DP-115. A refusal is not a warning: when one is in the note the
        # note says so, and the run button carries the same sentence.
        refusal = self.assemblyRefusal()
        lines = self.noteLines()
        self._gateNote.setText('\n'.join(lines))
        self._gateNote.setVisible(bool(lines))
        status = 'error' if refusal else 'warning'
        if self._gateNote.property('foammeshStatus') != status:
            self._gateNote.setProperty('foammeshStatus', status)
            style = self._gateNote.style()
            style.unpolish(self._gateNote)
            style.polish(self._gateNote)
        self.setRunStageAvailable(not refusal, refusal)
        # DP-123. `_runStage` is hidden on every Gmsh page -- none of them
        # declares `run_stage` -- so the line above shuts a button no user can
        # see. This is the one that reaches "Run to end".
        self.runAllRefusalChanged.emit(refusal)
