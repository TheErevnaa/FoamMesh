"""More of the window's own controls for the local API (Plan 48 AG).

The console's level, fold and search; the notification centre; the Mesh
quality tab's saved element selections; the fold headers of the page the
sheet shows; *Show expert settings*; the Preparation choice; the Mesh size
read-out and physics preset; the page filter box; and the help strip. Each
method presses or sets the widget's own control, so its own handler runs, and
answers what the widgets show afterwards. :class:`WindowApi` takes these in.
"""
from __future__ import annotations

#: The console level box's levels, by name (``console_lines``).
CONSOLE_LEVELS = {'info': 0, 'warning': 1, 'error': 2}

#: The Preparation step's three choices, in the order they are added.
PREPARATION_CHOICES = ('repair', 'wrap', 'use_as_is')


class WindowControlsMixin:
    """The controls; ``self.window`` and ``self._find`` are WindowApi's."""

    # -- the console ------------------------------------------------------ #

    def _console(self):
        from foammesh.view.main_window.console_view import Console
        console = getattr(self.window, 'consoleView', None)
        if callable(console):
            console = console()
        if not isinstance(console, Console):
            console = self._find(Console, 'the console')
        return console

    def console(self, level=None, folded=None, search_open=None, find=None,
                step=None) -> dict:
        console = self._console()
        if level is not None:
            if level not in CONSOLE_LEVELS:
                raise ValueError('level is info, warning or error')
            console.chooseLevel(CONSOLE_LEVELS[level])
        if folded is not None:
            console.pressFold(bool(folded))
        if search_open is True or find is not None:
            if not console.readout()['search_open']:
                console.pressFind()
        if find is not None:
            console.typeSearch(find)
        if step is not None:
            if step not in ('next', 'previous'):
                raise ValueError('step is next or previous')
            if not console.readout()['search_open']:
                raise ValueError('the search bar is closed; open it first')
            console.pressSearchButton(step)
        if search_open is False and console.readout()['search_open']:
            console.pressSearchButton('close')
        state = console.readout()
        state['level'] = next((name for name, value in CONSOLE_LEVELS.items()
                               if value == state['level']), state['level'])
        return state

    # -- the notification centre ------------------------------------------ #

    def _status_bar(self):
        from foammesh.view.main_window.notification_centre import (
            NotifyStatusBar)
        bar = self.window.statusBar() if hasattr(
            self.window, 'statusBar') else None
        if not isinstance(bar, NotifyStatusBar):
            bar = self._find(NotifyStatusBar, 'the notification centre')
        return bar

    def notifications(self, clear=None, open=None,              # noqa: A002
                      go_to=None) -> dict:
        bar = self._status_bar()
        panel = bar.panel
        if clear:
            if not panel.clearButton.isEnabled():
                raise ValueError('there are no notifications to clear')
            panel.clearButton.click()
        if open is True:
            bar.bell.click()
        elif open is False:
            panel.hide()
        if go_to is not None:
            panel.goTo(int(go_to))
        entries = bar.log.entries()
        return {
            'entries': [{'id': e.id, 'text': e.text, 'severity': e.severity,
                         'when': e.when, 'count': e.count, 'seen': e.seen,
                         'has_target': e.target is not None,
                         'shown': panel.rowText(e)}
                        for e in reversed(entries)],
            'empty_text': '' if entries else panel.emptyText(),
            'unseen': bar.log.unseen(),
            'bell': bar.bell.text(),
            'open': not panel.isHidden(),
            'clear_enabled': panel.clearButton.isEnabled(),
        }

    # -- the Mesh quality tab's saved selections -------------------------- #

    def _quality_tab(self):
        tab = getattr(self.window, '_meshQualityTab', None)
        if tab is None:
            from foammesh.view.main_window.mesh_quality_tab import (
                MeshQualityTab)
            tab = self._find(MeshQualityTab, 'the Mesh quality tab')
        return tab

    def _last_sentence(self):
        """``(id, count, text)`` of the newest notification, or None."""
        try:
            entries = self._status_bar().log.entries()
        except LookupError:
            return None
        last = entries[-1] if entries else None
        return None if last is None else (last.id, last.count, last.text)

    def quality_selections(self, action=None, name=None) -> dict:
        tab = self._quality_tab()
        message = ''
        if action is not None:
            if action not in ('save', 'show', 'delete'):
                raise ValueError('action is save, show or delete')
            if not str(name or '').strip():
                raise ValueError('a selection needs a name')
            before = self._last_sentence()
            tab.press_selection(action, str(name))
            after = self._last_sentence()
            if after is not None and after != before:
                message = after[2]
            if action == 'save':
                saved = {row['name'] for row in
                         tab.selection_state()['selections']}
                if str(name).strip() not in saved:
                    raise ValueError(message or f'"{name}" was not saved')
        state = tab.selection_state()
        state['message'] = message
        return state

    # -- the page the sheet shows ----------------------------------------- #

    def _sheet_page(self):
        sheet = getattr(self.window, '_settingsSheet', None)
        page = sheet.page() if sheet is not None else self.window
        if page is None:
            raise LookupError('the sheet shows no page')
        return page

    def _fold_headers(self) -> list:
        from PySide6.QtWidgets import QAbstractButton, QPushButton
        from foammesh.view.widgets.folder_header import FolderHeader
        page = self._sheet_page()
        found = []
        for button in page.findChildren(QAbstractButton):
            if not button.isCheckable() or not button.isVisibleTo(page):
                continue
            name = button.objectName()
            if isinstance(button, FolderHeader) or (
                    isinstance(button, QPushButton)
                    and name.endswith('Header')):
                found.append(button)
        return found

    def fold(self, name=None, open=None, index=0) -> dict:      # noqa: A002
        headers = self._fold_headers()
        if name is not None:
            named = [h for h in headers if h.objectName() == str(name)]
            if not named:
                raise ValueError(
                    f'{name} is not a fold on the page shown; folds are: '
                    + (', '.join(sorted({h.objectName() for h in headers}))
                       or 'none'))
            if not 0 <= int(index) < len(named):
                raise ValueError(f'there are {len(named)} folds named {name}')
            header = named[int(index)]
            if open is not None and header.isChecked() != bool(open):
                if not header.isEnabled():
                    raise ValueError(f'{header.text()} is not available now')
                header.setChecked(bool(open))
        rows, seen = [], {}
        for header in self._fold_headers():
            key = header.objectName()
            rows.append({'name': key, 'index': seen.get(key, 0),
                         'text': header.text(), 'open': header.isChecked(),
                         'enabled': header.isEnabled()})
            seen[key] = seen.get(key, 0) + 1
        return {'folds': rows}

    # -- Show expert settings --------------------------------------------- #

    def expert_settings(self, shown=None) -> dict:
        from foammesh.view.workflow_controls.tiers import expert_visibility
        visibility = expert_visibility()
        if shown is not None:
            visibility.setShown(bool(shown))
        from PySide6.QtWidgets import QCheckBox
        boxes = [box for box in self.window.findChildren(QCheckBox)
                 if box.objectName() == 'showExpertSettings']
        return {'shown': visibility.shown(),
                'boxes': [box.isChecked() for box in boxes]}

    # -- the Preparation choice ------------------------------------------- #

    def preparation_choice(self, choice=None) -> dict:
        from foammesh.view.geometry.repair_page import PreparationChoices
        choices = self._find(PreparationChoices, 'the Preparation step')
        if choice is not None:
            if choice not in PREPARATION_CHOICES:
                raise ValueError('choice is repair, wrap or use_as_is')
            index = PREPARATION_CHOICES.index(choice)
            if index >= choices.count():
                raise ValueError(f'{choice} is not offered')
            button = choices.choiceButtons()[index]
            if not button.isEnabled():
                raise ValueError(f'{button.text()} is not available now')
            choices.setCurrentIndex(index)
        current = choices.currentIndex()
        return {'choice': (PREPARATION_CHOICES[current]
                           if 0 <= current < len(PREPARATION_CHOICES)
                           else None),
                'choices': {PREPARATION_CHOICES[i]: choices.choiceText(i)
                            for i in range(min(choices.count(),
                                               len(PREPARATION_CHOICES)))}}

    # -- Mesh size --------------------------------------------------------- #

    def mesh_size(self, preset=None, apply=None) -> dict:
        from foammesh.view.meshing_method.size_page import MeshSizePage
        page = self._find(MeshSizePage, 'the Mesh size control')
        if preset is not None:
            page.choosePreset(preset)
        if apply:
            if not page.presetState()['preset']:
                raise ValueError('choose a physics preset first')
            page.pressApplyPreset()
        state = page.presetState()
        state['readout'] = page.readoutText()
        return state

    # -- the page filter --------------------------------------------------- #

    def _filtered_page(self):
        from foammesh.view.workflow_controls.field_group_page import (
            FieldGroupPage)
        from foammesh.view.workflow_controls.task_page import EngineTaskPage
        page = self._sheet_page()
        for kind in (EngineTaskPage, FieldGroupPage):
            if isinstance(page, kind):
                return page
            found = [w for w in page.findChildren(kind)
                     if w.isVisibleTo(page) and w.filterBox().isVisibleTo(w)]
            if found:
                return found[0]
        raise LookupError('the page shown has no filter box')

    def page_filter(self, text=None) -> dict:
        page = self._filtered_page()
        if text is not None:
            page.setFilterText(str(text))
        return {'text': page.filterText(),
                'visible_field_ids': list(page.visibleFieldIds())}

    # -- the help strip ----------------------------------------------------- #

    def help(self, open=None) -> dict:                          # noqa: A002
        pane = getattr(getattr(self.window, '_ui', None), 'regionBHelpPane',
                       None)
        if pane is None:
            from foammesh.view.main_window.help_pane import HelpPane
            pane = self._find(HelpPane, 'the help strip')
        if open is not None:
            pane.setOpen(bool(open))
        return {'open': pane.is_open(),
                'name': pane.name_text(),
                'explanation': pane.explanation_text(),
                'keyword': pane.keyword_text(),
                'link': pane.link_text()}
