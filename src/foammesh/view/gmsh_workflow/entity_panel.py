"""Pick entities in the viewport and group them (Plan 41 GN4).

The panel is Gmsh's Geometry > Physical groups and Coherence, with the
selection done on the model in the viewport instead of in a message line:

* a selection filter (Points, Curves, Surfaces, Volumes);
* **Pick**: hover highlights, a click picks, shift-click extends, Enter (or
  ``e``) accepts, Escape (or ``q``) aborts; the picked entities are chips;
* a table of the physical groups with add, set members, rename and remove;
* **Coherence** (remove duplicate entities) and the **Entity labels** toggle.

The session work (importing, reading the entities, the group calls) runs on a
worker thread; the picking itself is the viewport's and never waits on Gmsh.
"""
from __future__ import annotations

import tempfile
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView,
    QLabel, QLineEdit, QListWidget, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.gmsh import session_entities as entities
from foammesh.core.naming import humanise_option
from foammesh.core.selection.pick import pick_filters


class _Bridge(QObject):
    done = Signal(object, object)          # callback token, result
    failed = Signal(str)


class GmshEntityPanel(QWidget):
    """Filter, Pick, chips, the physical-group table, Coherence, labels."""

    groupsChanged = Signal()
    #: Plan 41 GN3. Emitted with the accepted ``((dim, tag), ...)`` pick.
    pickAccepted = Signal(object)

    def __init__(self, controller_provider, view_provider=None, parent=None,
                 store_provider=None):
        super().__init__(parent)
        self._storeProvider = store_provider
        self._provider = controller_provider
        self._view_provider = view_provider
        self._controller = None
        self._picker = None
        self._view = None
        self._worker = None
        self._groups: list = []
        #: Plan 42 SG13. Physical-group tag -> the patch_uuid it was seeded
        #: from (shared surface names); groups not listed are Gmsh's own.
        self._origins: dict = {}
        self._request = None
        self._queued = None
        self._service = None
        self._shared_signature = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self.setObjectName('gmshEntityPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._show = QPushButton(self.tr('Show model for picking'), self)
        self._show.setObjectName('gmshEntityShow')
        self._show.setAccessibleDescription(self.tr(
            'Open the geometry in the Gmsh session and draw its points, '
            'curves, surfaces and volumes in the viewport.'))
        self._show.clicked.connect(self.showModel)
        layout.addWidget(self._show)

        row = QHBoxLayout()
        self._filters = QButtonGroup(self)
        self._filters.setExclusive(True)
        self._filterButtons: dict = {}
        offered = pick_filters('gmsh')
        for name in entities.FILTERS:
            if name not in offered:
                continue
            button = QPushButton(self.tr(name), self)
            button.setCheckable(True)
            button.setObjectName('gmshEntityFilter' + name)
            button.setAccessibleName(self.tr('Select %s') % name.lower())
            self._filters.addButton(button)
            self._filterButtons[name] = button
            row.addWidget(button)
            button.clicked.connect(
                lambda _checked=False, n=name: self.setFilter(n))
        self._filterButtons['Surfaces'].setChecked(True)
        layout.addLayout(row)

        pick = QHBoxLayout()
        self._pick = QPushButton(self.tr('Pick'), self)
        self._pick.setObjectName('gmshEntityPick')
        self._pick.setToolTip(self.tr(
            'Click an entity in the viewport; shift-click adds more'))
        self._pick.clicked.connect(self.beginPick)
        self._accept = QPushButton(self.tr('Accept'), self)
        self._accept.setObjectName('gmshEntityAccept')
        self._accept.setToolTip(self.tr('Enter or e'))
        self._accept.clicked.connect(self.acceptPick)
        self._abort = QPushButton(self.tr('Cancel'), self)
        self._abort.setObjectName('gmshEntityAbort')
        self._abort.setToolTip(self.tr('Escape or q'))
        self._abort.clicked.connect(self.abortPick)
        for button in (self._pick, self._accept, self._abort):
            pick.addWidget(button)
        layout.addLayout(pick)

        self._chips = QListWidget(self)
        self._chips.setObjectName('gmshEntityChips')
        self._chips.setAccessibleName(self.tr('Picked entities'))
        self._chips.setMaximumHeight(72)
        self._chips.setToolTip(self.tr('Double-click a chip to drop it'))
        self._chips.itemDoubleClicked.connect(self._dropChip)
        layout.addWidget(self._chips)

        self._name = QLineEdit(self)
        self._name.setObjectName('gmshEntityGroupName')
        self._name.setPlaceholderText(self.tr('Physical group name'))
        self._name.setAccessibleName(self.tr('Physical group name'))
        layout.addWidget(self._name)
        buttons = QHBoxLayout()
        self._add = QPushButton(self.tr('Add group'), self)
        self._add.setObjectName('gmshEntityAddGroup')
        self._members = QPushButton(self.tr('Set members'), self)
        self._members.setObjectName('gmshEntitySetMembers')
        self._rename = QPushButton(self.tr('Rename'), self)
        self._rename.setObjectName('gmshEntityRename')
        self._remove = QPushButton(self.tr('Remove'), self)
        self._remove.setObjectName('gmshEntityRemove')
        for button, handler in ((self._add, self.addGroup),
                                (self._members, self.setMembers),
                                (self._rename, self.renameGroup),
                                (self._remove, self.removeGroup)):
            button.clicked.connect(handler)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self._table = QTableWidget(0, 5, self)
        self._table.setObjectName('gmshEntityGroups')
        self._table.setAccessibleName(self.tr('Physical groups'))
        self._table.setHorizontalHeaderLabels(
            [self.tr('Kind'), self.tr('Tag'), self.tr('Name'),
             self.tr('Entities'), self.tr('Origin')])
        self._table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self._table)

        tools = QHBoxLayout()
        self._coherence = QPushButton(self.tr('Coherence'), self)
        self._coherence.setObjectName('gmshEntityCoherence')
        self._coherence.setToolTip(self.tr(
            'Remove duplicate entities, as Geometry\u00a0→\u00a0Coherence '
            'does in Gmsh'))
        self._coherence.clicked.connect(self.runCoherence)
        self._labels = QCheckBox(self.tr('Entity labels'), self)
        self._labels.setObjectName('gmshEntityLabels')
        self._labels.toggled.connect(self.setLabels)
        tools.addWidget(self._coherence)
        tools.addWidget(self._labels)
        layout.addLayout(tools)
        # Plan 41 GN23 (FM-04): named selections of entities.
        keep = QHBoxLayout()
        self._selName = QLineEdit(self)
        self._selName.setObjectName('gmshEntitySelectionName')
        self._selName.setPlaceholderText(self.tr('Selection name'))
        self._selName.setAccessibleName(self.tr('Entity selection name'))
        self._selSave = QPushButton(self.tr('Save selection'), self)
        self._selSave.setObjectName('gmshEntitySelectionSave')
        self._selSave.setToolTip(self.tr(
            'Keep the picked entities under this name with the case'))
        self._selSave.clicked.connect(self.saveSelection)
        keep.addWidget(self._selName)
        keep.addWidget(self._selSave)
        layout.addLayout(keep)
        recall = QHBoxLayout()
        self._selCombo = QComboBox(self)
        self._selCombo.setObjectName('gmshEntitySelectionCombo')
        self._selCombo.setAccessibleName(self.tr('Saved entity selections'))
        self._selShow = QPushButton(self.tr('Show selection'), self)
        self._selShow.setObjectName('gmshEntitySelectionShow')
        self._selShow.clicked.connect(self.showSelection)
        self._selDelete = QPushButton(self.tr('Delete selection'), self)
        self._selDelete.setObjectName('gmshEntitySelectionDelete')
        self._selDelete.clicked.connect(self.deleteSelection)
        for widget in (self._selCombo, self._selShow, self._selDelete):
            recall.addWidget(widget)
        layout.addLayout(recall)
        self._status = QLabel('', self)
        self._status.setObjectName('gmshEntityStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._syncButtons()
        self.refreshSelections()

    # -- state -------------------------------------------------------------- #

    def picker(self):
        return self._picker

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def status(self) -> str:
        return self._status.text()

    def groups(self) -> list:
        return list(self._groups)

    def _syncButtons(self) -> None:
        ready = self._picker is not None
        active = ready and self._picker.active
        for button in (self._pick,):
            button.setEnabled(ready and not active)
        self._accept.setEnabled(active)
        self._abort.setEnabled(active)
        for button in (self._add, self._members, self._rename, self._remove,
                       self._coherence):
            button.setEnabled(ready)
        self._labels.setEnabled(ready)
        for button in self._filterButtons.values():
            button.setEnabled(ready)

    def _say(self, text: str) -> None:
        self._status.setText(text)

    # -- the model ---------------------------------------------------------- #

    def showModel(self) -> None:
        if self.busy():
            return
        try:
            controller = self._controller = self._controller or self._provider()
        except Exception as error:                   # noqa: BLE001
            self._say(str(error))
            return
        directory = tempfile.mkdtemp(prefix='foammesh-entities-')
        self._say(self.tr('Opening the geometry…'))
        self._start('model', lambda: controller.entity_model(directory))

    def setModel(self, rows, vtp_path) -> None:
        """Install the entity table and draw the model (also used by tests)."""
        entity_map = (entities.EntityMap.from_vtp(vtp_path, rows)
                      if vtp_path else
                      entities.EntityMap.from_session(rows))
        self._picker = entities.EntityPicker(entity_map)
        self._picker.set_filter(self.filterName())
        self._picker.subscribe(self._onPickerChange)
        if self._view_provider is not None:
            from foammesh.rendering.gmsh_entity_view import GmshEntityView

            host = self._view_provider()
            if host is not None and vtp_path:
                if self._view is not None:
                    self._view.dispose()
                self._view = GmshEntityView(host, self._picker)
                self._view.show(vtp_path)
        self._syncButtons()
        self._refreshChips()
        if self._queued is not None:
            self._queued = None
            self.beginPick()

    # -- picking ------------------------------------------------------------ #

    def filterName(self) -> str:
        for name, button in self._filterButtons.items():
            if button.isChecked():
                return name
        return 'Surfaces'

    def setFilter(self, name: str) -> None:
        self._filterButtons[name].setChecked(True)
        if self._picker is not None:
            self._picker.set_filter(name)

    def beginPick(self) -> None:
        if self._picker is None:
            return
        self._picker.begin(self.filterName())
        self._say(self.tr('Click %s in the viewport; shift-click adds more; '
                          'Enter accepts, Escape cancels.')
                  % self.filterName().lower())
        self._syncButtons()

    def hasModel(self) -> bool:
        return self._picker is not None

    # -- the one Pick tool (Plan 42 SG13) ------------------------------------ #

    def registerAsPicker(self, service=None) -> None:
        """Become the picker that serves the application's PickRequests."""
        if service is None:
            from .shared_pick import selection_service
            service = selection_service()
        self._service = service
        service.set_pick_driver(self.serveRequest, source=self)
        service.state_changed.connect(self._onServiceState)

    def serveRequest(self, request) -> bool:
        """Start a page's :class:`PickRequest`; load the model if needed."""
        name = next((n for n in request.filters if n in self._filterButtons),
                    None)
        if name is None:
            self._service.refuse(self.tr('Nothing here can be picked as %s.')
                                 % ', '.join(request.filters))
            return False
        self._request = request
        self.setFilter(name)
        if self._picker is None:
            self._queued = request
            self.showModel()
            return True
        self._queued = None
        self.beginPick()
        return True

    def _endRequest(self, accepted=None) -> None:
        request, self._request, self._queued = self._request, None, None
        service = self._service
        if request is None or service is None:
            return
        if service.active_pick is request:
            if accepted is None:
                service.cancel_pick()
            else:
                service.accept_pick(accepted)

    def pickFor(self, filter_name: str | None = None) -> bool:
        """Start a pick on behalf of another panel (GN3's entity fields).

        Returns False, with the reason in the status line, when the model is
        not shown yet.  The result arrives as :attr:`pickAccepted`.
        """
        if self._picker is None:
            self._say(self.tr('Show the model first; there is nothing to '
                              'pick yet.'))
            return False
        if filter_name in self._filterButtons:
            self.setFilter(filter_name)
        self.beginPick()
        return True

    def acceptPick(self) -> None:
        if self._picker is None:
            return
        try:
            chosen = self._picker.accept()
        except entities.PickRefusal as error:
            self._say(str(error))
            return
        self._pending = chosen
        self._say(self.tr('%d picked.') % len(chosen))
        self._syncButtons()
        self.pickAccepted.emit(tuple(chosen))
        self._endRequest(tuple(chosen))

    def abortPick(self) -> None:
        if self._picker is not None:
            self._picker.abort()
        self._pending = ()
        self._say(self.tr('Pick cancelled.'))
        self._syncButtons()
        self._endRequest()

    _pending: tuple = ()
    _reseedAgain: bool = False
    _note: str = ''

    def pickedEntities(self) -> tuple:
        """What a button acts on: the live chips, else the accepted pick."""
        if self._picker is not None and self._picker.active:
            return self._picker.selection
        return tuple(self._pending)

    def _onPickerChange(self, what: str) -> None:
        if what in ('selection', 'begin', 'accept', 'abort', 'filter'):
            self._refreshChips()
        self._syncButtons()

    def _refreshChips(self) -> None:
        self._chips.clear()
        if self._picker is None:
            return
        shown = (self._picker.selection if self._picker.active
                 else tuple(self._pending))
        for entity in shown:
            self._chips.addItem(entities.chip_text(
                entity, self._picker.map.name_of(entity)))

    def _dropChip(self, item) -> None:
        row = self._chips.row(item)
        shown = self.pickedEntities()
        if self._picker is not None and 0 <= row < len(shown):
            if self._picker.active:
                self._picker.remove_chip(shown[row])
            else:
                self._pending = tuple(e for e in shown if e != shown[row])
                self._refreshChips()

    # -- physical groups ---------------------------------------------------- #

    def _selectedGroup(self):
        row = self._table.currentRow()
        if 0 <= row < len(self._groups):
            return self._groups[row]
        return None

    def addGroup(self) -> None:
        picked, name = self.pickedEntities(), self._name.text()
        self._edit(lambda editor: editor.add(name, picked))

    def isShared(self, group) -> bool:
        """True for a surface group seeded from the shared surface names."""
        return (group is not None and group.dim == 2
                and group.tag in self._origins)

    def _refuseShared(self) -> None:
        self._say(self.tr('This group is a shared surface name. Change its '
                          'members or remove it in the Geometry step '
                          '(Boundaries); rename works here.'))

    def setMembers(self) -> None:
        group, picked = self._selectedGroup(), self.pickedEntities()
        if group is None:
            self._say(self.tr('Select a group in the table first.'))
            return
        if self.isShared(group):
            self._refuseShared()
            return
        self._edit(lambda editor: editor.set_members(group.dim, group.tag,
                                                     picked))

    def renameGroup(self) -> None:
        group, name = self._selectedGroup(), self._name.text()
        if group is None:
            self._say(self.tr('Select a group in the table first.'))
            return
        if self.isShared(group):
            self.renameShared(self._origins[group.tag], name)
            return
        self._edit(lambda editor: editor.rename(group.dim, group.tag, name))

    def removeGroup(self) -> None:
        group = self._selectedGroup()
        if group is None:
            self._say(self.tr('Select a group in the table first.'))
            return
        if self.isShared(group):
            self._refuseShared()
            return
        self._edit(lambda editor: editor.remove(group.dim, group.tag))

    def renameShared(self, patch_uuid: str, name: str) -> None:
        """Rename a shared surface group through the shared naming step.

        The manifest is the one store of the name; this asks the same
        operation the Geometry step's Rename does, and the session group
        follows when the selection service republishes the scopes.
        """
        name = (name or '').strip()
        if not name:
            self._say(self.tr('A physical group needs a name.'))
            return
        from foammesh.app import app
        from foammesh.view.facade_client import FailedResult, submit

        def done(result):
            if isinstance(result, FailedResult):
                self._say(result.message)
                return
            self._say(self.tr('Renamed in the shared surface names.'))
            self.reseed()

        submit(app.facadeClient, 'geometry.patches.rename',
               {'patch_uuid': patch_uuid, 'name': name}, then=done)

    def reseed(self) -> None:
        """Seed the shared surface names into the open model and re-read."""
        if self.busy():
            self._reseedAgain = True
            return
        try:
            controller = self._controller = self._controller or self._provider()
        except Exception as error:                   # noqa: BLE001
            self._say(str(error))
            return
        seed = getattr(controller, 'seed_shared_groups', None)
        if seed is None:
            self.loadGroups()
            return
        self._start('seed', seed)

    def _onServiceState(self, *_args) -> None:
        """A shared rename shows in the groups table without a click."""
        service = self._service
        if service is None or self._picker is None:
            return
        from foammesh.core.selection import SelectionKind

        signature = tuple(sorted(
            (e.stable_id, e.label, str(dict(e.metadata).get('solver_name')))
            for e in service.entities(kinds=(SelectionKind.SURFACE_GROUP,))))
        if signature == self._shared_signature:
            return
        first = self._shared_signature is None
        self._shared_signature = signature
        if not first:
            self.reseed()

    def _edit(self, change) -> None:
        if self.busy():
            return
        try:
            controller = self._controller = self._controller or self._provider()
        except Exception as error:                   # noqa: BLE001
            self._say(str(error))
            return

        def work():
            editor = controller.physical_editor()
            change(editor)
            return editor.groups()

        self._start('groups', work)

    def loadGroups(self) -> None:
        if self.busy():
            return
        controller = self._controller = self._controller or self._provider()
        self._start('groups_load',
                    lambda: controller.physical_editor().groups())

    def setGroups(self, groups) -> None:
        self._groups = list(groups)
        self._table.setRowCount(len(self._groups))
        for row, group in enumerate(self._groups):
            members = ', '.join(entities.chip_text((group.dim, t))
                                for t in group.entities)
            origin = (self.tr('Shared') if self.isShared(group)
                      else self.tr('Gmsh'))
            for column, text in enumerate((
                    humanise_option(entities._DIM_NAME[group.dim]),
                    str(group.tag), group.name, members, origin)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self._table.setItem(row, column, item)
        self.groupsChanged.emit()

    # -- named selections of entities (GN23) -------------------------------- #

    def _store(self):
        getter = self._storeProvider
        try:
            return getter() if callable(getter) else None
        except Exception:                            # noqa: BLE001
            return None

    def selectionNames(self) -> list:
        return [self._selCombo.itemText(i)
                for i in range(self._selCombo.count())]

    def refreshSelections(self, keep: str = '') -> None:
        from foammesh.core.gmsh import named_selections as store_

        store = self._store()
        found = sorted(n for n, r in (store_.load(store).items() if store
                                      else ())
                       if r.get('kind') == store_.ENTITY)
        self._selCombo.clear()
        self._selCombo.addItems(found)
        if keep in found:
            self._selCombo.setCurrentText(keep)

    def saveSelection(self) -> bool:
        from foammesh.core.gmsh import named_selections as store_

        store = self._store()
        if store is None:
            self._say(self.tr('Selections are kept with the case; open a '
                              'project first.'))
            return False
        name = self._selName.text().strip()
        try:
            store_.save(store, name, entities=self.pickedEntities())
        except store_.SelectionError as error:
            self._say(self.tr('Not saved: %s.') % error)
            return False
        self.refreshSelections(name)
        self._say(self.tr('Selection "%s" saved.') % name)
        return True

    def showSelection(self) -> bool:
        from foammesh.core.gmsh import named_selections as store_

        store, name = self._store(), self._selCombo.currentText()
        try:
            record = store_.get(store, name) if store else None
        except store_.SelectionError as error:
            self._say(str(error))
            return False
        if record is None or record.get('kind') != store_.ENTITY:
            self._say(self.tr('Choose a saved entity selection first.'))
            return False
        if self._picker is not None and self._picker.active:
            self._picker.abort()
        self._pending = tuple(tuple(e) for e in record['entities'])
        self._refreshChips()
        self._say(self.tr('Selection "%s": %d entities.')
                  % (name, len(self._pending)))
        return True

    def deleteSelection(self) -> bool:
        from foammesh.core.gmsh import named_selections as store_

        store, name = self._store(), self._selCombo.currentText()
        if store is None or not store_.delete(store, name):
            self._say(self.tr('Choose a saved entity selection first.'))
            return False
        self.refreshSelections()
        self._say(self.tr('Selection "%s" deleted.') % name)
        return True

    # -- coherence, labels -------------------------------------------------- #

    def runCoherence(self) -> None:
        if self.busy():
            return
        controller = self._controller = self._controller or self._provider()
        self._say(self.tr('Removing duplicate entities…'))
        self._start('coherence', controller.coherence)

    def setLabels(self, on: bool) -> None:
        if self._view is None:
            return
        count = self._view.setLabels(bool(on))
        self._say(self.tr('%d labels shown.') % count if on
                  else self.tr('Labels hidden.'))

    # -- the worker --------------------------------------------------------- #

    def _start(self, token: str, work) -> None:
        def run():
            try:
                self._bridge.done.emit(token, work())
            except Exception as error:               # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=run, name='gmsh-entities',
                                        daemon=True)
        self._worker.start()

    def _onDone(self, token, result) -> None:
        self._worker = None                  # the thread has delivered
        again, self._reseedAgain = self._reseedAgain, False
        if token == 'model':
            rows, path = result
            self.setModel(rows, path)
            self._say((self._note + ' ' if self._note else '')
                      + self.tr('%d entities.') % len(rows))
            self._note = ''
            self.reseed()
        elif token == 'seed':
            self._origins = {int(t): u for t, u in
                             (result.get('origins') or {}).items()}
            self.setGroups(result.get('groups') or [])
        elif token == 'groups_load':
            self.setGroups(result or [])
        elif token == 'groups':
            self.setGroups(result or [])
            self._say(self.tr('%d physical groups.') % len(self._groups))
        elif token == 'coherence':
            removed = sum(result['removed'].values())
            self._note = (self.tr('Coherence removed %d duplicate entities.')
                          % removed)
            self._say(self._note)
            self.showModel()
        if again and token != 'model':
            self.reseed()

    def _onFailed(self, message: str) -> None:
        self._say(message)
