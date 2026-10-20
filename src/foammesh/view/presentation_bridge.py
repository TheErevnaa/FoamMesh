"""AF4 presentation bridge: facade presentation ops <-> the desktop renderer.

Presentation commands are declarative facade operations (§7). This bridge lets
a GUI submit them through the facade client and applies the resulting
declarative state to a renderer that implements :class:`PresentationRenderer`.
The concrete VTK renderer lives in the view layer; this module is Qt/VTK-free
so the mapping is unit-testable headlessly.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Protocol

logger = logging.getLogger(__name__)


class PresentationRenderer(Protocol):
    """Minimal renderer surface the bridge drives from facade state."""

    def set_camera_axis(self, axis: str) -> None: ...
    def set_perspective(self, enabled: bool) -> None: ...
    def set_roll(self, degrees: float) -> None: ...
    def set_rotation_center(self, point) -> None: ...
    def fit(self) -> None: ...
    def set_overlay(self, name: str, visible: bool) -> None: ...
    def set_selection(self, entity_ids) -> None: ...
    def set_visibility(self, entity_id: str, visible: bool) -> None: ...


class PresentationBridge:
    """Submit presentation ops via the client and reflect state to a renderer."""

    def __init__(self, client, renderer: PresentationRenderer | None = None):
        self._client = client
        self._renderer = renderer

    def set_renderer(self, renderer: PresentationRenderer) -> None:
        self._renderer = renderer

    async def submit(self, operation: str, parameters: dict | None = None) -> dict:
        result = await self._client.presentation(operation, parameters or {})
        state = result.payload.get('state', {})
        if self._renderer is not None:
            self.reflect(state)
        return state

    def reflect(self, state: dict) -> None:
        """Apply a full declarative presentation state to the renderer."""
        renderer = self._renderer
        if renderer is None:
            return
        renderer.set_camera_axis(state.get('camera_axis', 'iso'))
        renderer.set_perspective(bool(state.get('perspective', True)))
        renderer.set_roll(float(state.get('roll_degrees', 0.0)))
        renderer.set_rotation_center(state.get('rotation_center'))
        for name, visible in state.get('overlays', {}).items():
            renderer.set_overlay(name, bool(visible))
        renderer.set_selection(state.get('selection', []))
        for entity_id, visible in state.get('visibility', {}).items():
            renderer.set_visibility(entity_id, bool(visible))


#: The facade's camera axes, as the viewport's presets name them.
VIEW_PRESETS = {'x': '+x', 'y': '+y', 'z': '+z', '-x': '-x', '-y': '-y',
                '-z': '-z', 'iso': 'isometric'}


class DesktopPresentationRenderer:
    """DP-2745. A presentation op over the local API moves the open window.

    Called by the facade, on the Qt thread the local API is served from, with
    the op and what it changed. Each op goes through the control a click
    would use -- the toolbar button, the Parts list row -- so the toolbar,
    the Parts list and the view agree afterwards. A part the window does not
    hold is refused before anything changes, with the parts it does hold.
    """

    def __init__(self, *, view, tool, display, ui, window=None, application=None):
        self._view = view
        self._tool = tool
        self._display = display
        self._ui = ui
        self._window = window
        self._app = application
        self._parameters = {}
        self._lastSnapshot = None
        self._console = ConsoleTap()
        self._status = StatusTap(getattr(ui, 'statusbar', None))
        #: DP-2741. Snapshot files still being written off the GUI thread.
        self._writing = []

    async def settle(self) -> None:
        """DP-2741. Wait, without holding the event loop, for the files the
        last op is still writing on a worker thread."""
        import asyncio

        writing, self._writing = getattr(self, '_writing', []), []
        for thread in writing:
            await asyncio.to_thread(thread.join)

    def window(self):
        """The main window: given, or the app's."""
        if self._window is not None:
            return self._window
        from foammesh.app import app
        return getattr(app, 'window', None)

    def refresh(self, state) -> None:
        """Read the toolbar's toggles into ``state``; they may have been clicked."""
        state.overlays['cube_axis'] = bool(self._ui.cubeAxis.isChecked())
        state.overlays['ruler'] = bool(self._ui.ruler.isChecked())
        state.perspective = not self._ui.perspective.isChecked()
        self._refreshMore(state)

    def __call__(self, operation: str, changed: dict, parameters: dict):
        handler = getattr(self, '_' + operation.removeprefix(
            'presentation.').replace('.', '_'), None)
        if handler is None:
            from foammesh.core.facade.errors import PresentationUnavailableError
            raise PresentationUnavailableError(
                f'the window has no control for {operation}')
        self._parameters = parameters or {}
        return handler(changed)

    # -- camera and overlays ------------------------------------------------ #

    def _view_fit(self, _changed):
        self._tool._fitCamera()

    def _view_axis(self, changed):
        self._view.setViewPreset(VIEW_PRESETS[changed['camera_axis']])

    def _view_align_axis(self, changed):
        self._view.setViewPreset(VIEW_PRESETS[changed['aligned_axis']])

    def _view_cube_axis(self, changed):
        self._ui.cubeAxis.setChecked(bool(changed['cube_axis']))

    def _view_ruler(self, changed):
        self._ui.ruler.setChecked(bool(changed['ruler']))

    def _view_perspective(self, changed):
        # The toolbar's button is the parallel projection: checked = parallel.
        self._ui.perspective.setChecked(not changed['perspective'])

    def _view_roll(self, changed):
        # The step, not the running total the state keeps.
        self._view.rollCameraBy(float(self._parameters.get('degrees', 90.0)))

    def _view_rotation_center(self, changed):
        point = changed.get('rotation_center')
        if point is None:
            self._tool.setRotationCentreToModel()
        else:
            self._tool.setRotationCentre(*point)

    # -- parts -------------------------------------------------------------- #

    def _known(self, entity_ids) -> None:
        held = self._display.partIds()
        missing = [value for value in entity_ids if value not in held]
        if missing:
            from foammesh.core.facade.errors import ValidationFailedError
            raise ValidationFailedError('the window holds no such part', details={
                'unknown': missing, 'parts': held})

    def _select(self, changed):
        ids = list(changed['selection'])
        self._known(ids)
        self._display.selectParts(ids)

    def _visibility(self, changed):
        self._known([changed['entity_id']])
        self._display.setVisibilities({changed['entity_id']: changed['visible']})

    def _color(self, changed):
        from PySide6.QtGui import QColor
        if not QColor(changed['color']).isValid():
            from foammesh.core.facade.errors import ValidationFailedError
            raise ValidationFailedError('not a colour', details={
                'color': changed['color'],
                'example': '#ff8800 or a name such as red'})
        self._known([changed['entity_id']])
        self._display.setPartColour(changed['entity_id'], QColor(changed['color']))

    def _opacity(self, changed):
        self._known([changed['entity_id']])
        self._display.setPartOpacity(changed['entity_id'], changed['opacity'])

    def _measurement(self, changed):
        measured = changed.get('measurement')
        if measured is None:
            self._view.clearMeasure()
        else:
            self._view.measureBetween(measured['start'], measured['end'])

    # -- DP-2750..2761: the rest of the viewport ------------------------------ #
    #
    # Each op presses the control a person would: the toolbar button, the
    # combo, the panel's own field. What the window then shows is read back,
    # and the op says what the window did -- a press the window refused is
    # an error with the window's own reason, never a quiet success.

    #: The facade's inspection names -> (the toolbar's name, its button).
    INSPECTION_BUTTONS = {
        'deviation': ('fidelity', '_fidelityButton'),
        'feature_edges': ('features', '_featuresButton'),
        'worst_cells': ('poorCells', '_poorCellsButton'),
        'failed_cells': ('failedCells', '_failedCellsButton'),
        'section': ('section', '_sectionButton'),
    }
    #: The Elements group's numbers -> its spin boxes.
    ELEMENT_SPINS = {'node_size': '_nodeSize', 'line_width': '_lineWidth',
                     'shrink': '_shrink', 'label_sampling': '_sampling',
                     'normals': '_normals', 'tangents': '_tangents',
                     'sub_edges': '_subEdges'}
    #: DP-2766. The Elements group's choices -> its combos.
    ELEMENT_COMBOS = {'colour_by': '_colour', 'label_type': '_labelType',
                      'node_style': '_nodeStyle'}
    #: A part's right-click Display mode -> the slot of its menu entry.
    DISPLAY_MODE_SLOTS = {'wireframe': '_displayWireframe',
                          'surface': '_displaySurface',
                          'surface_edge': '_displayWireSurfaceWithEdges'}

    @staticmethod
    def _refused(message, **details):
        from foammesh.core.facade.errors import PreconditionFailedError
        raise PreconditionFailedError(str(message), details=details)

    @staticmethod
    def _invalid(message, **details):
        from foammesh.core.facade.errors import ValidationFailedError
        raise ValidationFailedError(str(message), details=details)

    def _statusLine(self) -> str:
        window = self.window()
        try:
            return str(window.statusBar().currentMessage())
        except (AttributeError, RuntimeError, TypeError):
            return ''

    def _answer(self, **values) -> dict:
        values['status_line'] = self._statusLine()
        return values

    def _refreshMore(self, state) -> None:
        """Read what the rest of the viewport shows into ``state``."""
        tool = self._tool
        for read in (self._readViewMode, self._readInspection,
                     self._readCameraHistory, self._readExplode,
                     self._readColours, self._readBackground,
                     self._readSection, self._readElements,
                     self._readNamedViews):
            try:
                read(state, tool)
            except (AttributeError, RuntimeError, TypeError, KeyError,
                    ValueError, IndexError):
                logger.debug('presentation read-back skipped', exc_info=True)
        state.last_snapshot = self._lastSnapshot

    def _readNamedViews(self, state, tool):
        names = tool.namedViews()
        if isinstance(names, list):
            state.named_views = tuple(str(name) for name in names)

    def _readViewMode(self, state, tool):
        mode = tool.currentViewMode()
        if isinstance(mode, str):
            state.view_mode = mode

    def _readInspection(self, state, tool):
        for name, (key, _button) in self.INSPECTION_BUTTONS.items():
            active = tool.isInspectionActive(key)
            if isinstance(active, bool):
                state.inspection[name] = active

    def _readCameraHistory(self, state, _tool):
        back, forward = self._view.canGoBack(), self._view.canGoForward()
        if isinstance(back, bool) and isinstance(forward, bool):
            state.camera_history = {'can_go_back': back,
                                    'can_go_forward': forward}

    def _readExplode(self, state, _tool):
        overlay = getattr(self.window(), '_viewportOverlay', None)
        if overlay is not None:
            state.explode = overlay.explodeSlider().value() / 100.0

    def _readColours(self, state, tool):
        choice = tool.colourMeshChoice()
        if isinstance(choice, str):
            state.colour_mesh_by = choice
        from foammesh.support import colour_standard as cs
        state.colour_scale = cs.current().to_record()
        from foammesh.rendering.actor_info import meshLineStyle
        style = meshLineStyle()
        state.mesh_lines = {'opacity': style.opacity, 'color': style.color,
                            'width': style.width}

    def _readBackground(self, state, _tool):
        from PySide6.QtGui import QColor
        state.background = {
            'top': QColor.fromRgbF(*self._view.background2()).name(),
            'bottom': QColor.fromRgbF(*self._view.background1()).name()}

    def _readSection(self, state, _tool):
        cut = self._display.cutTool()
        panel = cut.panel()
        if not cut.isSectionActive():
            state.section = {'enabled': False}
            return
        plane = panel.activePlane()
        state.section = {'enabled': True, 'mode': panel.mode().value,
                         'origin': [float(v) for v in plane.origin],
                         'normal': [float(v) for v in plane.normal]}

    def _readElements(self, state, _tool):
        panel = self._display.elementDisplay()
        if panel is not None:
            state.elements['visible'] = bool(panel.isShown())
            state.elements.update(self._elementChoices(panel))

    # -- view mode and camera history --------------------------------------- #

    def _view_mode(self, changed):
        """The view-mode combo, as a pick from its list."""
        mode = changed['view_mode']
        combo = self._tool._modeCombo
        index = combo.findData(mode)
        if index < 0:
            self._invalid('the window has no such view mode', mode=mode)
        if combo.currentIndex() != index:
            combo.setCurrentIndex(index)
        shown = self._tool.currentViewMode()
        if shown != mode:
            self._refused(self._statusLine() or 'the window did not enter the mode',
                          mode=mode, shown=shown)
        return self._answer(view_mode=shown)

    def _camera_step(self, direction):
        can = (self._view.canGoBack() if direction == 'back'
               else self._view.canGoForward())
        if not can:
            self._refused(f'there is no camera position to go {direction} to',
                          direction=direction)
        (self._tool._goBack if direction == 'back' else self._tool._goForward)()
        return self._answer(camera_step=direction)

    def _view_back(self, _changed):
        return self._camera_step('back')

    def _view_forward(self, _changed):
        return self._camera_step('forward')

    def _view_fit_selection(self, _changed):
        """The Fit menu's *Fit the selection*."""
        if not self._display.selectedActorIds():
            self._refused('Select a part first, then fit the view to it.')
        self._tool.zoomSelectionRequested.emit()
        return self._answer(fit_selection=True)

    # -- inspection overlays ------------------------------------------------- #

    def _press(self, name, wanted):
        key, attribute = self.INSPECTION_BUTTONS[name]
        tool = self._tool
        if bool(tool.isInspectionActive(key)) == bool(wanted):
            return self._answer(inspection=name, visible=bool(wanted),
                                pressed=False)
        button = getattr(tool, attribute)
        if not button.isEnabled():
            self._refused(tool.actionReason(key) or button.toolTip()
                          or f'{name} is not available', inspection=name)
        button.click()
        now = bool(tool.isInspectionActive(key))
        # Worst cells may still be measuring: lit only once it paints.
        return self._answer(inspection=name, visible=now, pressed=True,
                            pending=bool(wanted) and not now)

    def _deviation(self, changed):
        return self._press('deviation', changed['visible'])

    def _feature_edges(self, changed):
        return self._press('feature_edges', changed['visible'])

    def _worst_cells(self, changed):
        return self._press('worst_cells', changed['visible'])

    def _failed_cells(self, changed):
        name = changed.get('set')
        if name is None:
            if not changed['visible'] and self._tool.isInspectionActive('failedCells'):
                # The menu's *Off*: the button steps to the next set.
                self._tool._chooseFailedCellSet('')
                return self._answer(inspection='failed_cells', visible=False,
                                    pressed=True)
            return self._press('failed_cells', changed['visible'])
        names = list(self.window().failedCellSetNames() or ())
        if name not in names:
            self._invalid('the last mesh check wrote no such set',
                          set=name, sets=names)
        button = self._tool._failedCellsButton
        if not button.isEnabled():
            self._refused(self._tool.actionReason('failedCells')
                          or 'failed cells are not available')
        self._tool._chooseFailedCellSet(name)
        return self._answer(inspection='failed_cells', set=name,
                            visible=bool(self._tool.isInspectionActive('failedCells')))

    def _reset_inspection(self, _changed):
        self._tool._resetButton.click()
        return self._answer(reset=True)

    def _deviation_settings(self, changed):
        """The deviation panel's tolerance, scale and Surfaces ticks."""
        panel = getattr(self.window(), '_deviationPanel', None)
        if panel is None:
            self._refused('the window has no deviation panel')
        asked = changed['deviation']
        surfaces = asked.get('surfaces') or {}
        if surfaces:
            held = list(panel.surfaceNames())
            missing = [name for name in surfaces if name not in held]
            if missing:
                self._invalid('the deviation on screen has no such surface',
                              unknown=missing, surfaces=held,
                              note='turn the deviation colouring on first'
                              if not held else '')
            shut = [name for name in surfaces
                    if not panel.surfaceCheck(name).isEnabled()]
            if shut:
                self._refused('the stored run has no field for these surfaces',
                              surfaces=shut)
        if 'tolerance_mm' in asked:
            panel.setTolerance(asked['tolerance_mm'])
        if 'scale_mm' in asked:
            if asked['scale_mm'] == 0.0:
                panel._scaleReset()
            else:
                panel.scaleBox().setValue(asked['scale_mm'])
                panel._scaleEdited()
        for name, ticked in surfaces.items():
            panel.surfaceCheck(name).setChecked(ticked)
        return self._answer(deviation={
            'tolerance_mm': panel.tolerance(),
            'surfaces': {name: panel.surfaceCheck(name).isChecked()
                         for name in panel.surfaceNames()}})

    # -- colour ---------------------------------------------------------------- #

    def _colour_scale(self, changed):
        """What the colour scale panel writes: the session's standard, or one
        of its three buttons."""
        from foammesh.support import colour_standard as cs
        action = changed.get('action')
        if action == 'make_standard':
            cs.make_my_standard()
        elif action == 'back_to_standard':
            cs.go_back_to_standard()
        elif action == 'app_standard':
            cs.use_app_standard()
        else:
            cs.set_current(None, **changed['colour_scale'])
        return self._answer(colour_scale=cs.current().to_record(),
                            is_standard=cs.is_standard())

    def _colour_mesh_by(self, changed):
        """The view menu's *Colour mesh by* entry."""
        key = changed['colour_mesh_by']
        actions = self._tool._colourMeshActions
        if key not in actions:
            self._invalid('no such Colour mesh by choice', key=key,
                          allowed=list(actions))
        action = actions[key]
        if not action.isEnabled():
            self._refused(action.toolTip() or f'{key} is not available', key=key)
        if self._tool.colourMeshChoice() != key:
            action.trigger()
        shown = self._tool.colourMeshChoice()
        if shown != key:
            self._refused(self._statusLine() or 'the window kept another colouring',
                          key=key, shown=shown)
        return self._answer(colour_mesh_by=shown)

    # -- section and clip -------------------------------------------------------- #

    def _section(self, changed):
        """The toolbar's Section, and the section panel's mode and plane."""
        from foammesh.core.section.modes import SectionMode
        asked = changed['section']
        cut = self._display.cutTool()
        panel = cut.panel()
        if not asked['enabled']:
            if cut.isSectionActive():
                panel._clearClicked()          # the panel's Clear
            self._tool.setInspectionActive('section', cut.isSectionActive())
            return self._answer(section={'enabled': cut.isSectionActive()})
        if not cut.isSectionActive():
            window = self.window()
            raise_ = getattr(window, '_quickSection', None)
            if callable(raise_):
                raise_(toggle=False)           # the Section button's press
            else:
                self._tool._sectionButton.click()
            if not cut.isSectionActive():
                self._refused(self._statusLine() or 'Nothing is loaded to cut yet.')
        if 'mode' in asked:
            panel.setMode(SectionMode(asked['mode']))
        if 'origin' in asked or 'normal' in asked:
            index = panel.activeIndex()
            plane = panel.planes()[index]
            panel.setPlaneGeometry(index, asked.get('origin', list(plane.origin)),
                                   asked.get('normal', list(plane.normal)))
            panel._emitChanged()
            sync = getattr(cut, '_syncGizmos', None)
            if callable(sync):
                sync()
        plane = panel.activePlane()
        return self._answer(section={
            'enabled': cut.isSectionActive(), 'mode': panel.mode().value,
            'origin': [float(v) for v in plane.origin],
            'normal': [float(v) for v in plane.normal]})

    def _explode(self, changed):
        """The Parts card's Explode slider."""
        overlay = getattr(self.window(), '_viewportOverlay', None)
        if overlay is None:
            self._refused('the window has no Explode slider')
        overlay.explodeSlider().setValue(round(changed['explode'] * 100))
        return self._answer(explode=overlay.explodeSlider().value() / 100.0)

    # -- named views (DP-2765) -------------------------------------------------- #

    def _view_named(self, changed):
        """The camera button's Saved views: list them, save the view on screen
        under a name (*Save current view…* with the name typed), apply one, or
        forget one (*Forget*)."""
        asked = changed['named_view']
        action, name = asked['action'], asked.get('name')
        tool = self._tool
        names = list(tool.namedViews())
        if action == 'save':
            window = self.window()
            if window is None or not window.saveNamedView(name):
                self._refused(self._statusLine() or 'the view was not saved',
                              name=name)
        elif action in ('apply', 'delete'):
            if name not in names:
                self._invalid('no saved view has that name', name=name,
                              named_views=names)
            done = (tool.applyNamedView(name) if action == 'apply'
                    else tool.deleteNamedView(name))
            if not done:
                self._refused(self._statusLine()
                              or f'the view {name!r} was not {action}d',
                              name=name)
        now = list(tool.namedViews())
        if action == 'save' and name not in now:
            self._refused('the saved view is not in the Saved views menu',
                          name=name, named_views=now)
        if action == 'delete' and name in now:
            self._refused('the view is still in the Saved views menu',
                          name=name, named_views=now)
        answer = {'action': action, 'named_views': now}
        if name is not None:
            answer['name'] = name
        return self._answer(named_view=answer)

    # -- snapshot ------------------------------------------------------------- #

    def _snapshot(self, changed):
        """The Snapshot popover's Save: the case's captures folder, with the
        sidecar. Nothing is written anywhere else over the API."""
        from foammesh.core import snapshot as snapshot_core
        asked = changed['snapshot']
        window = self.window()
        writer = getattr(window, '_captureManager', None)
        if writer is None:
            self._refused('the window has no snapshot writer')
        includes = snapshot_core.default_includes()
        includes.update(asked['includes'])
        options = snapshot_core.SnapshotOptions(
            name=asked['name'], directory=None, format=asked['format'],
            scale=asked['scale'], includes=includes)
        from foammesh.rendering import label_pass

        # DP-2741: the PNG is encoded and written on a worker thread, as the
        # Save button's is; the facade awaits it (`settle`) before it
        # answers, so the file named in the answer is on disk.
        with label_pass.deferred_writes() as pending:
            path = writer.snapshot(options)
        thread = pending.start()
        if thread is not None:
            self.__dict__.setdefault('_writing', []).append(thread)
        message = str(getattr(writer, 'snapshotMessage', '') or '')
        if path is None:
            self._refused(message or 'no snapshot was written')
        try:
            window.statusBar().showMessage(message, 10000)
        except (AttributeError, RuntimeError):
            pass
        self._lastSnapshot = {'path': str(path), 'format': asked['format'],
                              'message': message, 'at': time.time()}
        return self._answer(path=str(path), message=message)

    # -- how the model is drawn ------------------------------------------------- #

    def _mesh_lines(self, changed):
        """The Mesh lines panel: opacity, colour and width of the grid."""
        asked = dict(changed['mesh_lines'])
        if 'color' in asked and asked['color'] != 'auto':
            if _colour_of(asked['color']) is None:
                self._invalid('not a colour', color=asked['color'],
                              example="#1c2530, a name such as red, or 'auto'")
            asked['color'] = _colour_of(asked['color']).name()
        self._tool._meshLinesButton.panel()._apply(**asked)
        from foammesh.rendering.actor_info import meshLineStyle
        style = meshLineStyle()
        return self._answer(mesh_lines={'opacity': style.opacity,
                                        'color': style.color,
                                        'width': style.width})

    def _background(self, changed):
        """The Background button's Top colour, Bottom colour and Reset."""
        from PySide6.QtGui import QColor
        asked = changed['background']
        tool = self._tool
        if asked.get('reset'):
            tool._resetBackground()
        else:
            for end, value in asked.items():
                if _colour_of(value) is None:
                    self._invalid('not a colour', **{end: value})
            if 'top' in asked:
                tool._setBackground2(_colour_of(asked['top']))
            if 'bottom' in asked:
                tool._setBackground1(_colour_of(asked['bottom']))
        return self._answer(background={
            'top': QColor.fromRgbF(*self._view.background2()).name(),
            'bottom': QColor.fromRgbF(*self._view.background1()).name()})

    def _elements(self, changed):
        """The Display popover's Elements group."""
        panel = self._display.elementDisplay()
        if panel is None:
            self._refused('the window has no Elements group')
        asked = changed['elements']
        options = dict(asked.get('options') or {})
        # The combos and cell types first: a bad choice changes nothing.
        picks = {key: self._elementPick(panel, key, options.pop(key))
                 for key in self.ELEMENT_COMBOS if key in options}
        kinds = options.pop('cell_types', None)
        if kinds is not None:
            held = list(panel._kindBoxes)
            unknown = [kind for kind in kinds if kind not in held]
            if unknown:
                self._invalid('the mesh on screen has no such cell type',
                              unknown=unknown, allowed=held)
        for combo, index in picks.values():
            combo.setCurrentIndex(index)
        for kind, shown in (kinds or {}).items():
            panel._kindBoxes[kind].setChecked(shown)
        for key, value in options.items():
            if key in self.ELEMENT_SPINS:
                getattr(panel, self.ELEMENT_SPINS[key]).setValue(value)
            else:
                panel._flag[key].setChecked(value)
        if 'visible' in asked and panel.isShown() != asked['visible']:
            panel._show.setChecked(asked['visible'])
        notice = panel.notice()
        if asked.get('visible') and notice:
            self._refused(notice)
        return self._answer(elements={'visible': panel.isShown(),
                                      'notice': notice,
                                      **self._elementChoices(panel)})

    def _elementPick(self, panel, key, value):
        """The combo entry ``value`` names, matched as the combo shows it."""
        combo = getattr(panel, self.ELEMENT_COMBOS[key])
        offered = [combo.itemText(index) for index in range(combo.count())]
        if not offered:
            self._refused('Show a mesh in the viewport first: the Elements '
                          'group offers its choices for the mesh on screen.',
                          option=key)
        wanted = str(value).strip().lower()
        for index, text in enumerate(offered):
            if text.strip().lower() == wanted:
                return combo, index
        what = key.replace('_', ' ')
        self._invalid(f'the Elements group offers no such {what}',
                      option=key, value=value, allowed=offered)

    def _elementChoices(self, panel) -> dict:
        """What the combos and cell-type boxes show now."""
        shown = {key: getattr(panel, attribute).currentText()
                 for key, attribute in self.ELEMENT_COMBOS.items()}
        shown['cell_types'] = {kind: box.isChecked()
                               for kind, box in panel._kindBoxes.items()}
        return shown

    def _display_mode(self, changed):
        """Select the parts, then the right-click menu's Display mode."""
        ids = list(changed['entity_ids'])
        self._known(ids)
        self._display.selectParts(ids)
        self._display._selectedItemsInfo()
        getattr(self._display, self.DISPLAY_MODE_SLOTS[changed['mode']])()
        return self._answer(entity_ids=ids, mode=changed['mode'])

    def _isolate(self, changed):
        """Isolate on the toolbar: the selection, or these parts."""
        ids = self._parameters.get('entity_ids')
        if ids is not None:
            ids = [str(item) for item in ids]
            self._known(ids)
            self._display.selectParts(ids)
        if not self._display.selectedActorIds():
            self._refused('Select a part first, then show only it.')
        self._tool._isolateButton.click()
        return self._answer(isolated=list(self._display.selectedActorIds()))

    def _show_all(self, _changed):
        self._tool._showAllButton.click()
        return self._answer(show_all=True)

    # -- DP-2764: close the window's case ------------------------------------ #

    async def close_case(self, *, save=None) -> dict:
        """The window's own close (File > Close): what it asks, it asks.

        ``save`` answers the questions as their buttons would: True is
        *Save changed*'s Ok, False its Discard (and Discard on *Keep this
        untitled case?*). Left out, the person is asked, and the reply waits
        for them. A running job, or an untitled case to keep somewhere, is
        the person's to settle in the window, so a pre-answered close is
        refused then.
        """
        window = self.window()
        closer = getattr(window, '_closeProject', None)
        if not callable(closer):
            self._refused('the window cannot close a case')
        application = self._application()
        project = getattr(application, 'project', None)
        if project is None:
            self._refused('no case is open in the window')
        dirty = bool(project.isDirty)
        if save is not None:
            if tuple(getattr(application.jobManager, 'active_job_ids', ()) or ()):
                self._refused('A case operation is running. Cancel it first, '
                              'or close without save to let the person choose.')
            if save and application.isScratchCase():
                self._refused('This case has never been saved anywhere '
                              'permanent; choosing where to keep it is done '
                              'in the window.')
        closed = await closer(False, save)
        if not closed:
            self._refused(self._statusLine() or 'the case was kept open',
                          was_dirty=dirty)
        return {'closed': True, 'was_dirty': dirty, 'save': save,
                'asked': dirty and save is None}

    def _application(self):
        if self._app is not None:
            return self._app
        from foammesh.app import app
        return app

    # -- DP-2762: what the window said ------------------------------------------ #

    def console(self, *, after_line: int = 0, limit: int = 500,
                min_level: str = 'info') -> dict:
        window = self.window()
        console = getattr(window, 'consoleView', None)
        if console is None:
            return {'attached': False, 'lines': [], 'next_line': after_line,
                    'reason': 'the window has no console'}
        self._console.attach(console)
        return {'attached': True, **self._console.read(
            after_line=after_line, limit=limit, min_level=min_level)}

    def status(self, *, after: int = 0) -> dict:
        window = self.window()
        notices, run = [], None
        try:
            getter = getattr(window.renderingView, 'noticeStrip', None)
            strip = getter() if callable(getter) else None
            for widget in (strip.occupants() if strip is not None else ()):
                if widget.isVisible():
                    text = _widget_text(widget)
                    if text:
                        notices.append(text)
        except (AttributeError, RuntimeError):
            logger.debug('notice strip unreadable', exc_info=True)
        strip = getattr(window, '_runStatusStrip', None)
        if strip is not None:
            try:
                run = {'state': strip.state, 'text': strip.text()}
            except (AttributeError, RuntimeError):
                run = None
        return {'attached': True, 'status_line': self._statusLine(),
                **self._status.read(after=after), 'notices': notices,
                'run_status': run}


#: DP-2768. Every name the renderer reaches on the window's own controls,
#: by the class that must have it. Many are private -- the click handlers
#: are where the behaviour lives -- so a rename there would break the API
#: quietly; ``test_dp2768_the_bridge_names_real_controls`` loads these
#: classes and fails the suite instead. A method is checked on the class;
#: a widget the class builds in ``__init__`` by its ``self.<name> =``.
WINDOW_CONTROLS = {
    'foammesh.view.main_window.rendering_tool:RenderingTool': (
        'currentViewMode', 'isInspectionActive', 'setInspectionActive',
        'actionReason', 'colourMeshChoice', 'zoomSelectionRequested',
        'setRotationCentre', 'setRotationCentreToModel',
        '_fitCamera', '_goBack', '_goForward', '_chooseFailedCellSet',
        '_setBackground1', '_setBackground2', '_resetBackground',
        '_modeCombo', '_colourMeshActions', '_meshLinesButton',
        '_fidelityButton', '_featuresButton', '_poorCellsButton',
        '_failedCellsButton', '_sectionButton', '_resetButton',
        '_isolateButton', '_showAllButton',
        'namedViews', 'saveNamedView', 'applyNamedView', 'deleteNamedView'),
    'foammesh.view.main_window.mesh_lines_control:MeshLinesButton': ('panel',),
    'foammesh.view.main_window.mesh_lines_control:MeshLinesPanel': ('_apply',),
    'foammesh.view.main_window.main_window:MainWindow': (
        '_closeProject', 'statusBar', 'failedCellSetNames', '_quickSection',
        'consoleView', 'renderingView', '_deviationPanel', '_captureManager',
        '_viewportOverlay', '_runStatusStrip', 'saveNamedView'),
    'foammesh.view.main_window.capture_manager:CaptureManager': (
        'snapshot', 'snapshotMessage'),
    'foammesh.view.main_window.console_view:Console': ('flush', '_view'),
    'foammesh.view.display_control.deviation_panel:DeviationPanel': (
        'setTolerance', 'tolerance', 'scaleBox', 'surfaceNames', 'surfaceCheck',
        '_scaleEdited', '_scaleReset'),
    'foammesh.view.display_control.viewport_overlay:ViewportOverlay': (
        'explodeSlider',),
    'foammesh.view.display_control.display_control:DisplayControl': (
        'cutTool', 'elementDisplay', 'partIds', 'selectParts',
        'selectedActorIds', 'setVisibilities', 'setPartColour',
        'setPartOpacity', '_selectedItemsInfo', '_displayWireframe',
        '_displaySurface', '_displayWireSurfaceWithEdges'),
    'foammesh.view.display_control.cut_tool:CutTool': (
        'isSectionActive', 'panel', '_syncGizmos'),
    'foammesh.view.display_control.section_panel:SectionPanel': (
        'mode', 'setMode', 'activeIndex', 'planes', 'activePlane',
        'setPlaneGeometry', '_emitChanged', '_clearClicked'),
    'foammesh.view.display_control.element_display_panel:ElementDisplayPanel': (
        'isShown', 'notice', '_show', '_flag', '_nodeSize', '_lineWidth',
        '_shrink', '_sampling', '_normals', '_tangents', '_subEdges',
        '_colour', '_labelType', '_nodeStyle', '_kindBoxes'),
    'widgets.rendering.rendering_widget:RenderingWidget': (
        'setViewPreset', 'rollCameraBy', 'canGoBack', 'canGoForward',
        'background1', 'background2', 'measureBetween', 'clearMeasure',
        'noticeStrip'),
}

def _colour_of(value):
    """The colour a text names, or None when it names none."""
    from PySide6.QtGui import QColor
    return QColor(value) if QColor(value).isValid() else None


def _widget_text(widget) -> str:
    """The words a notice shows: its labels, in order."""
    from PySide6.QtWidgets import QLabel
    labels = [widget] if isinstance(widget, QLabel) else []
    labels += widget.findChildren(QLabel)
    words = [label.text().strip() for label in labels
             if label.isVisibleTo(widget) or label is widget]
    return ' '.join(word for word in words if word)


#: The console's levels by name, as its level box offers them.
CONSOLE_LEVELS = {'info': 0, 'warning': 1, 'error': 2}
CONSOLE_LEVEL_NAMES = {value: key for key, value in CONSOLE_LEVELS.items()}


class ConsoleTap:
    """DP-2762. The Console panel's lines, numbered from the first one read.

    The panel keeps the newest 20 000 lines and drops the oldest in a block,
    so a line's place in the panel is not a cursor. The tap counts the lines
    dropped off the top (``blockCountChanged``) and numbers each line by
    ``dropped + place``: a client that read up to line N asks for the lines
    after N and gets exactly the new ones, or is told how many it missed.
    """

    def __init__(self):
        self._console = None
        self._document = None
        self._dropped = 0
        self._count = 0

    def attach(self, console) -> None:
        if console is self._console:
            return
        document = console._view.document()
        self._console = console
        self._document = document
        self._dropped = 0
        self._count = document.blockCount()
        document.blockCountChanged.connect(self._counted)

    def _counted(self, count: int) -> None:
        if count < self._count:
            # Trimmed off the top, or cleared (one empty block is left).
            self._dropped += (self._count if count <= 1 else self._count - count)
        self._count = count

    def read(self, *, after_line: int = 0, limit: int = 500,
             min_level: str = 'info') -> dict:
        from foammesh.view.main_window import console_lines
        console = self._console
        try:
            console.flush()
        except (AttributeError, RuntimeError):
            pass
        floor = CONSOLE_LEVELS[min_level]
        document = self._document
        held = document.blockCount()
        first = self._dropped
        start = max(int(after_line) - first, 0)
        lines = []
        block = document.findBlockByNumber(start)
        number = first + start
        while block.isValid() and len(lines) < limit:
            text = block.text()
            if number >= after_line and (text or number + 1 < first + held):
                level = console_lines.line_level(text)
                if level >= floor:
                    lines.append({'line': number + 1, 'text': text,
                                  'level': CONSOLE_LEVEL_NAMES.get(level, 'info')})
            number += 1
            block = block.next()
        last = first + held
        return {'lines': lines, 'next_line': min(number, last),
                'first_held_line': first + 1, 'last_line': last,
                'missed': max(first - int(after_line), 0),
                'more': block.isValid()}


class StatusTap:
    """DP-2762. Every message the status bar showed, numbered.

    The status bar holds one message, which a timeout then clears; the tap
    keeps the last 200 so a message shown for five seconds can still be read.
    """

    KEEP = 200

    def __init__(self, statusbar=None):
        self._messages = deque(maxlen=self.KEEP)
        self._sequence = 0
        signal = getattr(statusbar, 'messageChanged', None)
        if signal is not None and hasattr(signal, 'connect'):
            signal.connect(self.record)

    def record(self, text) -> None:
        text = str(text or '')
        if not text:
            return
        self._sequence += 1
        self._messages.append({'sequence': self._sequence, 'text': text,
                               'at': time.time()})

    def read(self, *, after: int = 0) -> dict:
        return {'messages': [m for m in self._messages if m['sequence'] > after],
                'next_sequence': self._sequence}

