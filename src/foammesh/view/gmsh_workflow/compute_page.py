"""Gmsh workflow page: gmsh.compute."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QPushButton

from .base import GmshTaskPage
from .session_tiers import SessionTiers



def _same_case(controller, case_path) -> bool:
    try:
        return Path(controller.case_path) == Path(case_path)
    except (TypeError, ValueError):
        return False


def residentGenerateController(client):
    """The session controller of the case that is open, if one was made.

    Never makes one and never starts a session; ``None`` when no case is open
    or the controller held belongs to a case opened before (WALK-2).
    """
    controller = getattr(client, '_gmshGenerateController', None)
    if controller is None:
        return None
    try:
        case_path = client.case_path
    except Exception:                        # noqa: BLE001
        return None
    return controller if _same_case(controller, case_path) else None


def sharedGenerateController(client):
    """The project's session controller, made when the first action runs.

    Plan 41 GN6. One per project client, so the Size fields page previews on
    the same resident session the Compute page generates on.
    """
    from foammesh.core.gmsh.generate_actions import ActionRefusal
    from foammesh.core.gmsh.option_catalogue import OVERRIDES_FIELD
    from foammesh.core.gmsh.session_generate import (
        GenerateController, geometry_paths_from_prepared)

    try:
        case_path = client.case_path
    except Exception:                        # noqa: BLE001
        raise ActionRefusal('open a project first') from None
    controller = getattr(client, '_gmshGenerateController', None)
    if controller is not None:
        if _same_case(controller, case_path):
            return controller
        # WALK-2. The desktop keeps one client for its life: a controller
        # made on the case opened before this one would import, hand over
        # and write into that case. Its session ends with it.
        try:
            controller.close()
        except Exception:                    # noqa: BLE001
            pass
        client._gmshGenerateController = None

    def overrides():
        try:
            return client.field_values((OVERRIDES_FIELD,)).get(
                OVERRIDES_FIELD) or ''
        except Exception:                    # noqa: BLE001
            return ''

    def fields():
        # Plan 41 GN6: the rows of the Size fields page, checked against
        # the algorithms chosen on Global sizing.
        from foammesh.core.gmsh import field_catalogue as catalogue
        ids = (catalogue.FIELD_ID, 'gmsh.algorithms.surface',
               'gmsh.algorithms.volume')
        try:
            values = client.field_values(ids)
        except Exception:                    # noqa: BLE001
            return []
        raw = {key: getattr(value, 'value', value)
               for key, value in values.items()}
        rows = catalogue.rows_from_text(raw.get(catalogue.FIELD_ID))
        return catalogue.validate(
            rows, surface_algorithm=str(
                raw.get('gmsh.algorithms.surface') or ''),
            volume_algorithm=str(
                raw.get('gmsh.algorithms.volume') or '')).to_list()

    def point_sizes():
        # Plan 41 GN7: the sizes at the points picked on the Size fields page.
        from foammesh.core.gmsh import point_sizes as points
        try:
            raw = client.field_values((points.FIELD_ID,)).get(points.FIELD_ID)
        except Exception:                    # noqa: BLE001
            return []
        raw = getattr(raw, 'value', raw)
        return points.validate(points.rows_from_text(raw)).to_list()

    def entities():
        # Plan 41 GN9: the embedded entities and per-entity constraints set
        # on the Volume controls step.
        from foammesh.core.gmsh import entity_controls as entity
        try:
            raw = client.field_values((entity.FIELD_ID,)).get(entity.FIELD_ID)
        except Exception:                    # noqa: BLE001
            return []
        raw = getattr(raw, 'value', raw)
        return entity.validate(entity.rows_from_text(raw)).to_list()

    def automatic():
        # Plan 41 GN25f: the saved automatic size field as the background
        # field, when the project asks for it.
        from foammesh.core.sizing import background
        from foammesh.view.facade_client import query
        try:
            on = client.field_values((background.CLIENT_FIELD,)).get(
                background.CLIENT_FIELD)
        except Exception:                    # noqa: BLE001
            return {}
        if not bool(getattr(on, 'value', on)):
            return {}
        try:
            status = query(client, 'sizing.field.status', {})
            state = str((getattr(status, 'payload', None) or {}).get(
                'state') or 'none')
        except Exception:                    # noqa: BLE001
            state = 'none'
        return background.block(case_path, True, state)

    controller = GenerateController(
        case_path,
        geometry_paths=lambda: geometry_paths_from_prepared(case_path),
        overrides=overrides, fields=fields, point_sizes=point_sizes,
        entities=entities, automatic=automatic)
    try:
        client._gmshGenerateController = controller
    except Exception:                        # noqa: BLE001
        pass
    return controller


class GmshComputePage(SessionTiers, GmshTaskPage):
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

    #: Plan 40 UXD9 (#54). The gate judges the mesh after it is made, so its
    #: limits are edited on Quality, which is where the verdict is read. The
    #: task still binds them (the run reads them); this page draws none.
    GATE_EDITED_ON_QUALITY = (
        'gmsh.compute.quality_type', 'gmsh.compute.min_quality',
        'gmsh.compute.allowed_fraction', 'gmsh.compute.allowed_count',
        'gmsh.compute.hard_floor', 'gmsh.compute.extra_limits',
    )

    def renders_field(self, field_id: str) -> bool:
        if field_id in self.GATE_EDITED_ON_QUALITY:
            return False
        return super().renders_field(field_id)

    def build_sections(self, layout) -> None:
        self._gateNote = QLabel(self)
        self._gateNote.setObjectName('gmshLayerGateNote')
        self._gateNote.setWordWrap(True)
        self._gateNote.setProperty('foammeshStatus', 'warning')
        self._gateNote.setVisible(False)
        layout.addWidget(self._gateNote)
        self.mountGenerateTier(layout)
        self.mountPluginsTier(layout)
        self.mountDisplayTier(layout)

    # -- Plan 41 GN12: the Generate actions on the resident session ---------- #

    def mountGenerateTier(self, layout) -> None:
        """A closed tier: 1D/2D/3D, optimise, order, refine, partition, ...

        Closed by default so the guided Run is as short as it was; the batch
        run above stays until every step is ported (Plan 41 Q1).
        """
        from .generate_panel import GmshGeneratePanel

        self._generateHeader = QPushButton(self.tr('Generate step by step'),
                                           self)
        self._generateHeader.setObjectName('gmshGenerateHeader')
        self._generateHeader.setCheckable(True)
        self._generateHeader.setFlat(True)
        self._generateHeader.setAccessibleDescription(self.tr(
            "Run the actions of Gmsh's Mesh module one at a time on the open "
            'model, each from its own button and with its own options.'))
        self._generatePanel = GmshGeneratePanel(self.generateController, self)
        self._generatePanel.setVisible(False)
        self._generateHeader.toggled.connect(self._generatePanel.setVisible)
        self._generatePanel.stageFinished.connect(self.showSessionMesh)
        layout.addWidget(self._generateHeader)
        layout.addWidget(self._generatePanel)
        # Plan 41 GN18 (I-05). Gmsh's own 1, 2 and 3 while this page is up.
        from .command_entries import install_shortcuts
        self._shortcuts = install_shortcuts(self, self.runGenerateAction)

    def chooseGenerateAction(self, action_id: str) -> bool:
        """Open the Generate tier on *action_id*, focus on Run (Ctrl+K)."""
        if not self._generatePanel.chooseAction(action_id):
            return False
        self._generateHeader.setChecked(True)
        self._generatePanel.focusRun()
        return True

    def runGenerateAction(self, action_id: str) -> bool:
        """Gmsh's digit keys: choose the action and run it.

        A result-locked page is read-only (Plan 44 MF18): a page-level
        QShortcut is not stopped by the read-only guard, so the keys are
        refused here (DP-2553).
        """
        is_read_only = getattr(self, 'isReadOnly', None)
        if callable(is_read_only) and is_read_only():
            return False
        if self._generatePanel.busy():
            return False
        if not self.chooseGenerateAction(action_id):
            return False
        self._generatePanel.runSelected()
        return True

    def mountPluginsTier(self, layout) -> None:
        """Plan 41 GN20. A closed tier: Gmsh's plugins on the session mesh."""
        from .plugins_panel import GmshPluginsPanel

        self._pluginsHeader = QPushButton(self.tr('Plugins'), self)
        self._pluginsHeader.setObjectName('gmshPluginsHeader')
        self._pluginsHeader.setCheckable(True)
        self._pluginsHeader.setFlat(True)
        self._pluginsHeader.setAccessibleDescription(self.tr(
            "Run Gmsh's plugins (Skin, mesh volume, distance, cut plane and "
            'the others) with their options; the views they make are drawn '
            'in the viewport and a size field can read them.'))
        self._plugins = GmshPluginsPanel(
            lambda: getattr(self, '_generateController', None),
            self.entityViewport, self, client=self._client)
        self._plugins.meshEdited.connect(self.refreshStatistics)
        self._plugins.setVisible(False)
        self._pluginsHeader.toggled.connect(self._plugins.setVisible)
        layout.addWidget(self._pluginsHeader)
        layout.addWidget(self._plugins)

    def mountDisplayTier(self, layout) -> None:
        """Gmsh's Visibility window and display options.

        Plan 43 LY13 D11 (Plan 42 SG9): the panel lives in the side rail's
        Display popover, on the shared surface, while the engine is Gmsh; every
        control is the panel's own, none dropped. A page with no main window
        around it (no Display popover to hang from) keeps the closed tier it
        always had here.
        """
        from .display_panel import GmshDisplayPanel

        self._displayPanel = GmshDisplayPanel(
            self.generateController, self.entityViewport,
            elements_provider=self.sharedElements,
            parts_provider=self.sharedParts,
            centre_provider=self.sharedRotationTool, parent=self)
        self.offerGmshHandover()
        # FIX-K (SG9): a rebuilt page (engine switch) lists the live
        # session's entities in the Parts list without the tier being opened.
        panel = self._displayPanel
        QTimer.singleShot(0, lambda: self._restoreRows(panel))
        panel.opened.connect(lambda: self._loadDisplayModel(True))
        tool = self.sharedRotationTool()
        mount = getattr(tool, 'mountGmshDisplay', None)
        if callable(mount) and mount(panel):
            self._displayHeader = None
            self.destroyed.connect(
                lambda *_a, t=tool, p=panel: t.unmountGmshDisplay(p))
            return
        self._displayHeader = QPushButton(
            self.tr('Visibility and display'), self)
        self._displayHeader.setObjectName('gmshDisplayHeader')
        self._displayHeader.setCheckable(True)
        self._displayHeader.setFlat(True)
        self._displayHeader.setAccessibleDescription(self.tr(
            'Show or hide entities (and mesh only what is visible), choose '
            'element kinds, labels, colours, clip planes and the camera, and '
            'export the view as a picture in PNG or another format Gmsh '
            'writes.'))
        self._displayPanel.setVisible(False)
        self._displayHeader.toggled.connect(self._displayPanel.setVisible)
        layout.addWidget(self._displayHeader)
        layout.addWidget(self._displayPanel)

    def _restoreRows(self, panel) -> None:
        try:
            if getattr(self, '_displayPanel', None) is panel:
                panel.restoreRows()
        except RuntimeError:
            pass                                   # the page has gone

    def _loadDisplayModel(self, shown: bool) -> None:
        panel = getattr(self, '_displayPanel', None)
        if shown and panel is not None and not panel.visibilityModel().visible:
            panel.loadModel()

    def sharedParts(self):
        """Plan 42 SG9. The "Gmsh entities" group of the shared Parts list."""
        from foammesh.app import app

        overlay = getattr(getattr(app, 'window', None), 'viewportOverlay', None)
        getter = getattr(overlay, 'gmshEntities', None)
        return getter() if callable(getter) else None

    def sharedRotationTool(self):
        """Plan 42 SG23. The viewport toolbar, which owns the rotation centre."""
        from foammesh.app import app

        return getattr(getattr(app, 'window', None), '_renderingTool', None)

    def sharedElements(self):
        """Plan 42 SG9. The viewport's Elements group, shared by both engines."""
        from foammesh.app import app

        display = getattr(getattr(app, 'window', None), 'displayControl', None)
        getter = getattr(display, 'elementDisplay', None)
        return getter() if callable(getter) else None

    def offerGmshHandover(self) -> None:
        """Give the shared Elements group a hand-over with curve elements.

        The standard load of a session mesh holds the highest dimensions only;
        the group asks the session for 1D, 2D and 3D when the mesh on screen is
        its own. It runs on a worker thread.
        """
        elements = self.sharedElements()
        if elements is None:
            return

        def handover():
            controller = self.generateController()
            if controller is None:
                return ''
            directory = (Path(controller.case_path) / 'foammesh' / 'gmsh'
                         / 'display')
            written = controller.handover(directory, dims=[1, 2, 3],
                                          surfaces=False, geometry=False)
            return (written.get('mesh') or {}).get('host_path') or ''

        elements.setGmshHandover(handover)

    def refreshStatistics(self, *_args) -> None:
        """Plan 42 SG20. The statistics are the Mesh quality tab's; ask it."""
        from foammesh.app import app

        display = getattr(getattr(app, 'window', None), 'displayControl', None)
        info = getattr(display, 'meshQualityInfo', None)
        info = info() if callable(info) else None
        sync = getattr(info, 'syncStatistics', None)
        if callable(sync):
            sync()

    def showSessionMesh(self, stage) -> None:
        """Plan 41 GN13. A stage that left a mesh puts it in the viewport."""
        import asyncio

        from foammesh.app import app

        if not getattr(stage, 'ok', False):
            return
        manager = getattr(getattr(app, 'window', None), 'meshManager', None)
        controller = getattr(self, '_generateController', None)
        if manager is None or controller is None:
            return
        if not (stage.after or {}).get('nodes'):
            # The session holds no mesh now: the picture of it comes down.
            if manager.artifactId() == 'gmsh-session':
                manager.clear()
            self.refreshStatistics()
            plugins = getattr(self, '_plugins', None)
            if plugins is not None:
                plugins.clear()
            return
        try:
            self._sessionShow = asyncio.ensure_future(
                manager.loadSessionMesh(controller))
            self._sessionShow.add_done_callback(self.refreshStatistics)
        except RuntimeError:                      # no running event loop
            return

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
            'Set a Share of cells allowed below it or a Number of cells '
            'allowed below it on Quality that you can defend, or turn layers '
            'off on Layers, before running.')

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
