"""What the Script, Statistics and Plugins panels do for the local API
(Plan 48 PA4).

One object per window, registered on the case session by the mount under
``ATTRIBUTE``; the routes in ``core/facade/plan48/pa4_routes.py`` call its
methods.  Every method finds the panel the window shows and drives that
panel's own methods, so a route and the button it stands for run the same
code and leave the window in the same state.  Methods run on the GUI thread
(``on_gui_thread``); one that starts a worker waits for the panel's own
``finished``/``failed`` signal.  A refusal is ``ValueError(sentence)``.
"""
from __future__ import annotations

import threading
import time

from foammesh.view.gmsh_tools.window_actions import on_gui_thread

ATTRIBUTE = 'gmsh_panels_api'

NO_PANEL = ('the %s is not in the window: open the case on the Gmsh route '
            'in the FoamMesh window')


def _plain(value):
    """*value* as JSON-safe plain data (numpy numbers and arrays included)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, 'tolist'):
        return _plain(value.tolist())
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return str(value)


class PanelsApi:
    def __init__(self, host):
        #: A widget whose children include the panels (the main window).
        self._host = host

    # -- finding the panels ----------------------------------------------- #

    def _panel(self, cls, what):
        found = self._host.findChildren(cls)
        if not found:
            raise ValueError(NO_PANEL % what)
        for panel in found:
            if panel.isVisibleTo(self._host):
                return panel
        return found[0]

    def _script(self):
        from foammesh.view.gmsh_workflow.script_panel import GmshScriptPanel
        return self._panel(GmshScriptPanel, 'Script panel')

    def _statistics(self):
        from foammesh.view.gmsh_workflow.statistics_panel import (
            GmshStatisticsPanel)
        return self._panel(GmshStatisticsPanel, 'Statistics panel')

    def _plugins(self):
        from foammesh.view.gmsh_workflow.plugins_panel import GmshPluginsPanel
        return self._panel(GmshPluginsPanel, 'Plugins panel')

    # -- waiting for a worker --------------------------------------------- #

    def _drive(self, panel, start, timeout=600.0) -> dict:
        """Call *start* on the GUI thread and wait for the panel's answer.

        ``{'finished': (what, outcome)}`` or ``{'failed': sentence}``.
        """
        from PySide6.QtCore import QCoreApplication, QThread

        done = threading.Event()
        box: dict = {}

        def on_finished(what, outcome):
            box['finished'] = (what, outcome)
            done.set()

        def on_failed(sentence):
            box['failed'] = sentence
            done.set()

        def go():
            if panel.isBusy():
                raise ValueError('a script action is still running; wait '
                                 'for it to finish')
            panel.finished.connect(on_finished)
            panel.failed.connect(on_failed)
            start()
            if not panel.isBusy():
                done.set()

        def release():
            for signal, slot in ((panel.finished, on_finished),
                                 (panel.failed, on_failed)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        on_gui_thread(go)
        try:
            application = QCoreApplication.instance()
            on_gui = (application is not None
                      and QThread.currentThread() is application.thread())
            deadline = time.monotonic() + timeout
            while not done.wait(0.01 if on_gui else 0.1):
                if on_gui:
                    application.processEvents()
                if time.monotonic() > deadline:
                    raise ValueError('the window did not answer in time')
        finally:
            on_gui_thread(release)
        return box

    # -- Script panel ----------------------------------------------------- #

    def _script_state(self, panel) -> dict:
        info = panel.scriptInfo()
        return {
            'language': panel.language(), 'text': panel.scriptText(),
            'read_only': panel._view.isReadOnly(),
            'calls': info['calls'], 'rebuildable': info['rebuildable'],
            'gaps': info['gaps'], 'note': panel.noteText(),
            'can_save': panel.canSave(), 'can_reload': panel.canReload(),
            'geo_path': panel.geoPath(),
            'parameters': _plain(panel.parameterRows()),
            'parameter_values': _plain(panel.parameterValues()),
            'rerun_on_change': panel.rerunOnChange(),
            'console_language': panel.consoleLanguage(),
            'confirming': panel.isConfirming(),
            'result': panel.resultText(), 'busy': panel.isBusy()}

    def script_read(self, language=None, refresh=False) -> dict:
        def run():
            panel = self._script()
            if language is not None:
                if panel._language.findData(language) < 0:
                    raise ValueError('the script format must be python or '
                                     'geo')
                panel.selectLanguage(language)
            if refresh:
                panel.refresh()
            return self._script_state(panel)
        return on_gui_thread(run)

    def script_save(self, path, language=None) -> dict:
        def run():
            panel = self._script()
            if language is not None:
                if panel._language.findData(language) < 0:
                    raise ValueError('the script format must be python or '
                                     'geo')
                panel.selectLanguage(language)
            if not panel.canSave():
                raise ValueError(panel.noteText() or 'there is no script '
                                 'to save')
            if not path:
                raise ValueError('a file to save the script to is required')
            saved = panel.saveScript(str(path))
            return {'path': saved, 'result': panel.resultText(),
                    'language': panel.language()}
        return on_gui_thread(run)

    def script_reload(self, text=None) -> dict:
        def prepare():
            panel = self._script()
            panel.selectLanguage('geo')
            if text is not None:
                panel.setScriptText(text)
            if not panel.canReload():
                raise ValueError(
                    'a script action is still running' if panel.isBusy()
                    else panel.noteText() or 'there is no script to reload')
            return panel
        panel = on_gui_thread(prepare)
        return self._finish_script(panel, panel.reloadFromView)

    def script_onelab(self, path=None, values=None, run_again=False,
                      rerun_on_change=None) -> dict:
        def prepare():
            panel = self._script()
            if rerun_on_change is not None:
                panel.setRerunOnChange(rerun_on_change)
            if values and not path:
                missing = [n for n in values
                           if n not in panel.parameterNames()]
                if missing:
                    raise ValueError(
                        'the opened script has no ONELAB parameter %s; it '
                        'has: %s' % (', '.join(missing),
                                     ', '.join(panel.parameterNames())
                                     or 'none'))
            return panel
        panel = on_gui_thread(prepare)
        if path:
            return self._finish_script(
                panel, lambda: panel.openGeo(str(path), dict(values or {})
                                             or None))
        if values:
            return self._finish_script(
                panel, lambda: panel.setParameters(dict(values)))
        if run_again:
            return self._finish_script(panel, panel.runAgain)
        return on_gui_thread(lambda: self._script_state(panel))

    def script_run(self, language, text, confirm=True, cancel=False) -> dict:
        def ask():
            panel = self._script()
            if cancel:
                panel.cancelRun()
                return panel, False
            if language not in ('python', 'geo'):
                raise ValueError('the script format must be python or geo')
            if panel.isBusy():
                raise ValueError('a script action is still running; wait '
                                 'for it to finish')
            panel.setConsole(language, str(text or ''))
            panel.askToRun()
            if not panel.isConfirming():
                raise ValueError(panel.resultText() or 'the script was not '
                                 'accepted')
            return panel, bool(confirm)
        panel, go = on_gui_thread(ask)
        if not go:
            return on_gui_thread(lambda: self._script_state(panel))
        return self._finish_script(panel, panel.confirmRun)

    def _finish_script(self, panel, start) -> dict:
        box = self._drive(panel, start)

        def state():
            payload = self._script_state(panel)
            if 'finished' in box:
                what, outcome = box['finished']
                payload['finished'] = what
                if isinstance(outcome, dict):
                    payload['outcome'] = _plain({
                        k: v for k, v in outcome.items()
                        if k in ('entities', 'parameters')})
            if 'failed' in box:
                payload['error'] = box['failed']
            return payload
        payload = on_gui_thread(state)
        if 'error' in payload and 'finished' not in payload:
            raise ValueError(payload['error'])
        return payload

    # -- Statistics panel ------------------------------------------------- #

    def statistics(self, refresh=True, plot=None, dim=None) -> dict:
        def run():
            panel = self._statistics()
            if refresh:
                panel.refresh()
            if plot:
                panel.plot(str(plot), None if dim is None else str(dim))
            return {'shown': _plain(panel.shown()),
                    'statistics': _plain(panel.statistics())}
        return on_gui_thread(run)

    def analyse(self) -> dict:
        def run():
            panel = self._statistics()
            panel.analyse()
            shown = panel.shown()
            if shown['message']:
                raise ValueError(shown['message'])
            return {'shown': _plain(shown),
                    'statistics': _plain(panel.statistics())}
        return on_gui_thread(run)

    # -- Plugins panel ---------------------------------------------------- #

    def _plugins_state(self, panel) -> dict:
        return {
            'plugin': panel.pluginName(),
            'input_views': _plain(panel.inputViews()),
            'input_view': _plain(panel.inputTag()),
            'results': _plain(panel.results()),
            'table': panel.tableRows(),
            'shown': _plain(panel.shownKeys()),
            'selected': panel.selectedIndex(),
            'note': panel.noteText(), 'log': panel.logText()}

    def plugins_results(self) -> dict:
        return on_gui_thread(lambda: self._plugins_state(self._plugins()))

    def _select(self, panel, tag, index) -> None:
        if tag is not None:
            at = panel.rowIndexOfTag(tag)
            if at < 0:
                raise ValueError('there is no result that is view %s' % tag)
        elif index is not None:
            at = int(index)
            if not 0 <= at < len(panel.results()):
                raise ValueError('there is no result row %s' % index)
        else:
            raise ValueError('give the tag or the row index of a result')
        panel.selectResult(at)

    def plugins_view(self, action, tag=None, index=None) -> dict:
        def run():
            panel = self._plugins()
            drawn = None
            if action == 'show_input':
                if tag is not None and not panel.chooseInputView(tag):
                    raise ValueError('view %s is not an input view of the '
                                     'session' % tag)
                drawn = panel.showInputView()
            elif action in ('show', 'hide'):
                self._select(panel, tag, index)
                drawn = (panel.showSelected() if action == 'show'
                         else panel.hideSelected())
            else:
                raise ValueError('action must be show, hide or show_input')
            state = self._plugins_state(panel)
            state['drawn'] = _plain(drawn)
            state['changed'] = bool(drawn)
            return state
        return on_gui_thread(run)

    def plugins_size_field(self, tag=None, index=None) -> dict:
        def run():
            panel = self._plugins()
            self._select(panel, tag, index)
            row = panel.useAsSizeField()
            if not row:
                raise ValueError(panel.noteText() or 'no size field was '
                                 'written')
            state = self._plugins_state(panel)
            state['field'] = _plain(row)
            return state
        return on_gui_thread(run)

    def plugins_run(self, name, options=None, view=None) -> dict:
        def run():
            panel = self._plugins()
            if not panel.choosePlugin(name):
                raise ValueError('%r is not a plugin of the panel' % name)
            panel.resetDefaults()
            for key, value in dict(options or {}).items():
                if key not in panel._editors:
                    raise ValueError(
                        'plugin %s has no option %s the panel shows' % (
                            name, key))
                panel.setValue(key, value)
            if view is not None and not panel.chooseInputView(view):
                raise ValueError('view %s is not an input view of the '
                                 'session' % view)
            result = panel.run()
            if not result and panel.noteText():
                raise ValueError(panel.noteText())
            state = self._plugins_state(panel)
            state['views'] = _plain(result.get('views') or [])
            return state
        return on_gui_thread(run)
