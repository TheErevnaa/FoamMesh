"""What the main window lets the local API do (Plan 48 PA5).

A plain remote control for controls a person has in the window: opening a
panel, the viewport rail, the run panel's read-outs, the sheet header, the
recovery and runtime bars, the export page's read-outs, File > Save and Save
as, and a few panels of the snappy and geometry pages. The window registers
one :class:`WindowApi` on the case session (``pa5_window``); the routes in
``core/facade/plan48/pa5_routes.py`` call it, and answer ``precondition_failed``
when there is no window. Every method runs the code the widget runs, so the
route and the control cannot differ.
"""
from __future__ import annotations

ATTRIBUTE = 'pa5_window'

#: Link buttons of the Gmsh tools tab that ``window.navigate`` may press.
BUTTON_PREFIX = 'gmshTools_'


def _visible_first(widgets):
    widgets = list(widgets)
    for widget in widgets:
        if widget.isVisible():
            return widget
    return widgets[0] if widgets else None


class WindowApi:
    """The window, as the local API drives it."""

    def __init__(self, window):
        self.window = window

    # -- finding what the window holds ----------------------------------- #

    def _find(self, kind, what: str):
        found = list(self.window.findChildren(kind))
        if isinstance(self.window, kind):
            found.insert(0, self.window)
        widget = _visible_first(found)
        if widget is None:
            raise LookupError(f'{what} is not open in the window')
        return widget

    def _rail(self):
        rail = getattr(self.window, '_viewportRail', None)
        if rail is None:
            raise LookupError('the window has no tool rail')
        return rail

    # -- navigation ------------------------------------------------------- #

    def navigate(self, kind: str, target) -> dict:
        """Open a step, a branch, a task, a rail tool, an output tab, a
        menu action or a Gmsh tools link, as pressing it does."""
        from foammesh.db.configurations_schema import Step
        window = self.window
        kind = str(kind)
        if kind == 'step':
            step = target
            if isinstance(target, str) and not target.isdigit():
                try:
                    step = Step[target.upper()]
                except KeyError:
                    raise ValueError(f'{target} is not a step') from None
            else:
                try:
                    step = Step(int(target))
                except ValueError:
                    raise ValueError(f'{target} is not a step') from None
            if not window._navigationView.openStep(step):
                raise ValueError(f'the step {step.name} is not open yet')
        elif kind == 'branch':
            tokens = window._navigationView.routeTokens()
            if str(target) not in tokens:
                raise ValueError(f'{target} is not a route; routes are: '
                                 + ', '.join(sorted(tokens)))
            window._navigationView.requestBranch(str(target))
        elif kind == 'task':
            window.openLikelyStep(str(target))
        elif kind == 'rail':
            self._open_tool(str(target))
        elif kind == 'output_tab':
            tabs = window._outputTabs
            if not tabs.contains(str(target)):
                raise ValueError(f'{target} is not an open output tab')
            tabs.show(str(target))
        elif kind == 'action':
            from PySide6.QtGui import QAction
            action = next((a for a in window.findChildren(QAction)
                           if a.objectName() == str(target)), None)
            if action is None:
                raise ValueError(f'{target} is not a menu action')
            if not action.isEnabled():
                raise ValueError(f'{action.text() or target} is not '
                                 'available now')
            action.trigger()
        elif kind == 'button':
            from PySide6.QtWidgets import QAbstractButton
            name = str(target)
            if not name.startswith(BUTTON_PREFIX):
                raise ValueError(
                    f'only the {BUTTON_PREFIX}* link buttons are pressed')
            button = next((b for b in window.findChildren(QAbstractButton)
                           if b.objectName() == name), None)
            if button is None:
                raise ValueError(f'{name} is not a button of the window')
            if not button.isEnabled():
                raise ValueError(f'{name} is not available now')
            button.click()
        else:
            raise ValueError(
                'kind is step, branch, task, rail, output_tab, action or '
                'button')
        return self.where()

    def where(self) -> dict:
        window = self.window
        step = window._navigationView.currentStep()
        tabs = window._outputTabs
        host = window._ui.regionCTabHost
        current = host.currentIndex() if hasattr(host, 'currentIndex') else -1
        return {'step': getattr(step, 'name', None),
                'step_number': int(step) if step is not None else None,
                'routes': list(window._navigationView.routeTokens()),
                'output_tab': (host.tabText(current) if current >= 0
                               else None),
                'output_tabs': [host.tabText(i)
                                for i in range(host.count())],
                'rail_tools': self._rail().reachable()
                if getattr(window, '_viewportRail', None) else []}

    # -- the viewport rail ----------------------------------------------- #

    def _open_tool(self, key: str) -> None:
        """Open a tool's popover as its button does, or say why it will not."""
        rail = self._rail()
        tools = {t['key']: t for t in rail.railState()['tools']}
        tool = tools.get(key)
        if tool is None or not tool['visible']:
            raise ValueError(f'{key} is not a tool of the rail')
        if not tool['enabled']:
            raise ValueError(f"{tool['title']} is not available now"
                             + (f": {tool['reason']}" if tool['reason']
                                else ''))
        if not tool['popover']:
            raise ValueError(f"{tool['title']} has no popover; it is a "
                             'switch of the rail')
        rail.open(key)

    def rail(self, **values) -> dict:
        """Read the rail; apply open/close/pin/size/customise first."""
        rail = self._rail()
        key = values.get('tool')
        if 'preferences' in values and values['preferences'] is not None:
            merged = rail.preferences()
            merged.update(values['preferences'])
            rail.setPreferences(merged, emit=True)
        if 'rail_pinned' in values and values['rail_pinned'] is not None:
            rail.setRailPinned(values['rail_pinned'])
        if values.get('overflow'):
            rail.pressOverflow(values['overflow'])
        if key is not None:
            if key not in {t['key'] for t in rail.railState()['tools']}:
                raise ValueError(f'{key} is not a tool of the rail')
            if values.get('open') is True:
                self._open_tool(key)
            elif values.get('open') is False:
                rail.close(key)
            if values.get('pinned') is not None:
                rail.setPopoverPinned(key, values['pinned'])
            if values.get('width') is not None or values.get(
                    'reset_size'):
                rail.setPopoverSize(
                    key, values.get('width'), values.get('height'))
        return rail.railState()

    # -- the run panel ----------------------------------------------------- #

    def run_panel(self) -> dict:
        panel = getattr(self.window, '_runPanel', None)
        if panel is None:
            raise LookupError('the window has no run panel')
        return panel.readout()

    # -- the sheet --------------------------------------------------------- #

    def sheet(self, **values) -> dict:
        sheet = self.window._settingsSheet
        if values.get('mode') is not None:
            sheet.set_mode(values['mode'])
        if values.get('side') is not None:
            sheet.set_side(values['side'])
        if values.get('minimised') is not None:
            sheet.set_minimised(values['minimised'])
        if values.get('reset'):
            sheet.reset()
        state = sheet.header.readout()
        state.update(mode=sheet.mode, side=sheet.side,
                     minimised=sheet.is_minimised)
        return state

    # -- the bars ---------------------------------------------------------- #

    def _bars(self):
        return getattr(self.window, '_autosaveBars', None)

    def recovery(self, action: str | None = None) -> dict:
        bars = self._bars()
        offer = bars.offer if bars is not None else None
        result = {}
        if action in ('restore', 'discard'):
            if offer is None or offer.isHidden():
                raise ValueError('there are no unsaved changes on offer')
            (offer._restore if action == 'restore' else offer._discard)()
            result['done'] = action
        elif action not in (None, 'show'):
            raise ValueError('action is restore, discard or show')
        if action == 'show':
            if offer is None:
                raise ValueError('there are no unsaved changes on offer')
            offer.showChanges()
        result['offer'] = offer.readout() if offer is not None and \
            not offer.isHidden() else {'offered': False}
        result['protection'] = (bars.protection.readout()
                                if bars is not None else {'shown': False})
        return result

    def _wsl_bar(self):
        bar = getattr(self.window, '_wslHealthBar', None)
        if bar is None:
            raise LookupError('the window has no WSL health bar (no WSL '
                              'runtime is configured)')
        return bar

    def runtime(self) -> dict:
        bar = self._wsl_bar()
        return bar.readout()

    async def runtime_act(self, action: str, confirmed: bool) -> dict:
        bar = self._wsl_bar()
        if action in ('retry', 'restart') and bar.isHidden():
            raise ValueError('the runtime is answering; there is nothing '
                             'to retry or restart')
        if action == 'retry':
            outcome = await bar.retryConnection()
        elif action == 'restart':
            if not confirmed:
                raise ValueError(bar.restartQuestion())
            outcome = await bar.restartRuntime(True)
        elif action == 'shutdown':
            from foammesh.view.main_window.wsl_health_bar import (
                shutdown_question)
            if not confirmed:
                raise ValueError(shutdown_question())
            monitor = bar._monitor()
            if monitor is None:
                raise LookupError('no WSL runtime is watched')
            outcome = await monitor.shutdown_all()
        else:
            raise ValueError('action is retry, restart or shutdown')
        return {'action': action, 'outcome': outcome, 'bar': bar.readout()}

    # -- the export page --------------------------------------------------- #

    def _export_page(self):
        from foammesh.view.export_task_page import ExportTaskPage
        return self._find(ExportTaskPage, 'the Export page')

    def export(self, action: str | None = None, **values) -> dict:
        page = self._export_page()
        if action == 'change_target':
            page.changeTarget()
        elif action == 'script':
            page.setScriptShown(bool(values.get('shown')))
        elif action is not None:
            raise ValueError('action is change_target or script')
        result = page.readouts()
        result['script_panel'] = page.scriptState()
        return result

    # -- File > Save, Save as ---------------------------------------------- #

    async def save(self) -> dict:
        from foammesh.core.case import CaseConflictError
        try:
            outcome = await self.window.saveCase(ask=False)
        except CaseConflictError as error:
            raise ValueError(
                f'{error} (reload the case, or save a copy)') from error
        return {'result': outcome}

    async def save_as(self, path: str, open_copy: bool) -> dict:
        return await self.window.saveStateAs(path, open_copy=open_copy)

    def save_as_cancel(self) -> dict:
        return {'cancelled': self.window.cancelSaveAs()}

    # -- panels of the pages ---------------------------------------------- #

    def interface_pair_check(self, values: dict) -> dict:
        from foammesh.view.geometry.interface_pair_panel import (
            InterfacePairPanel)
        panel = self._find(InterfacePairPanel, 'the interface-pair table')
        return panel.contactCheck(dict(values))

    def periodic_pair_check(self, values: dict) -> dict:
        """Plan 49 SR8. The transform check the Gmsh Periodic page's pair
        editor makes on OK (``GmshPeriodicPairPanel.mapCheck``)."""
        from foammesh.view.gmsh_workflow.periodic_pair_panel import (
            GmshPeriodicPairPanel)
        panel = self._find(GmshPeriodicPairPanel,
                           'the Gmsh periodic pair table')
        return panel.mapCheck(dict(values))

    def exclude_findings(self) -> dict:
        from foammesh.view.snappy_workflow.domain_regions_page import (
            ExcludePointPanel)
        return self._find(ExcludePointPanel,
                          'the exclusion points table').findingsReadout()

    def seed(self, action: str | None = None, **values) -> dict:
        from foammesh.view.snappy_workflow.domain_regions_page import (
            RegionSeedPanel)
        panel = self._find(RegionSeedPanel, 'the region seed table')
        done = None
        if action == 'move':
            panel.movePlacement(values['point'])
        elif action == 'nudge':
            panel.nudgeSeed(int(values['axis']), int(values['steps']))
        elif action == 'undo':
            done = panel.undoPlacement()
        elif action == 'redo':
            done = panel.redoPlacement()
        elif action == 'merge':
            done = panel.mergeSelected()
        elif action is not None:
            raise ValueError('action is move, nudge, undo, redo or merge')
        state = panel.placementState()
        if done is not None:
            state['done'] = bool(done)
        return state

    def first_layer(self, **values) -> dict:
        from foammesh.view.snappy_workflow.layers_page import LayerGroupPanel
        panel = self._find(LayerGroupPanel, 'the layer group table')
        return panel.estimateFirstLayer(
            values['target_yplus'], values['velocity'], values['density'],
            values['viscosity'], values['length'],
            apply=bool(values.get('apply')))

    async def block_grading(self, entity_id, axis, fine=None, ratio=None,
                            text=None, vertices=None, apply=True) -> dict:
        """One direction's grading on a background-mesh block.

        The widget's rule (``BlockPanel.gradingFields``): a side with its
        ratio regenerates the text; Custom profile keeps the typed text. The
        fields are written through ``base_grid.blocks.patch``, as the panel's
        OK writes them. ``vertices`` also returns the Start/End sentences.
        """
        from foammesh.app import app
        from foammesh.view.snappy_workflow.base_grid_page import BlockPanel
        fields = BlockPanel.gradingFields(axis, fine, ratio, text)
        result = {'entity_id': str(entity_id), 'fields': fields,
                  'applied': False}
        if vertices is not None:
            result['directions'] = BlockPanel.directionDescriptions(vertices)
        if apply:
            await app.facadeClient.run(
                'base_grid.blocks.patch',
                {'entity_id': str(entity_id), 'fields': fields})
            result['applied'] = True
        return result


# -- registering on the case session --------------------------------------- #

def register_on(session, window) -> WindowApi:
    api = WindowApi(window)
    setattr(session, ATTRIBUTE, api)
    return api


def unregister_from(session) -> None:
    if getattr(session, ATTRIBUTE, None) is not None:
        setattr(session, ATTRIBUTE, None)


def _case_session():
    from foammesh.app import app
    client = getattr(app, 'facadeClient', None)
    try:
        return client.session()
    except Exception:                                   # noqa: BLE001
        return None


def project_opened(window) -> None:
    session = _case_session()
    if session is not None:
        register_on(session, window)


def project_closed(window) -> None:
    session = _case_session()
    if session is not None:
        api = getattr(session, ATTRIBUTE, None)
        if api is not None and api.window is window:
            unregister_from(session)
