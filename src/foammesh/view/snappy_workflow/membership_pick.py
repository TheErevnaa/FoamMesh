"""Tick the rows of a live membership list by clicking them in the viewport.

Plan 43 LY13 D1 (register rows C-074, C-089, C-154). The legacy refinement and
layer dialogs opened ``MultiSelectorDialog(..., app.selectionService)``, which
attaches an editor to the selection service so a click on a surface in the
viewport adds or removes it from the dialog's selection. The live guided
membership lists are plain checklists; this is the same hook for them.

While the "Pick in view" toggle is on, the list is the service's active editor:
a viewport pick of a surface that has a row ticks that row, and a second pick
unticks it. The list, its eligibility rules and its bindings are not touched --
only the check state of rows that were already listed changes, and the
membership's own ``pending`` / ``commit`` write the difference as before.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QListWidget


def _default_service():
    try:
        from foammesh.app import app
        return getattr(app, 'selectionService', None)
    except Exception:                                        # noqa: BLE001
        return None


class ViewportPick:
    """The "Pick in view" toggle of one checklist.

    *rows_for(stable_id)* returns the list-row keys a picked entity stands
    for (empty when it has none); *covered()* returns every stable id that
    has a row, which is what the service is told it may call back for.
    """

    def __init__(self, listWidget: QListWidget, rows_for, covered,
                 service=None, parent=None, name='Pick in view'):
        self._list = listWidget
        self._rowsFor = rows_for
        self._covered = covered
        self._service = service
        self._token = None
        self.toggle = QCheckBox(name, parent)
        self.toggle.setToolTip(
            QCheckBox.tr('While this is on, clicking a surface in the 3D '
                         'view ticks it in the list, and clicking it again '
                         'unticks it'))
        self.toggle.setAccessibleName(name)
        self.toggle.toggled.connect(self._toggled)

    def service(self):
        return self._service if self._service is not None \
            else _default_service()

    def active(self) -> bool:
        return self._token is not None

    # -- attaching --------------------------------------------------------- #

    def _toggled(self, on: bool) -> None:
        if on:
            self.attach()
        else:
            self.detach()

    def attach(self) -> None:
        """(Re)attach as the service's active editor; call after a reload."""
        service = self.service()
        self.detach()
        if service is None:
            return
        self._token = service.attach_editor(self._picked, self._covered())

    def detach(self) -> None:
        service, token = self.service(), self._token
        self._token = None
        if service is not None and token:
            service.detach_editor(token)

    def refresh(self) -> None:
        """The list was reloaded: tell the service the new allowed set."""
        if self.toggle.isChecked():
            self.attach()

    def setVisible(self, visible: bool) -> None:
        self.toggle.setVisible(visible)
        if not visible:
            self.detach()

    # -- picking ----------------------------------------------------------- #

    def _picked(self, stable_ids) -> None:
        wanted = []
        for stable_id in stable_ids:
            for key in self._rowsFor(stable_id):
                if key not in wanted:
                    wanted.append(key)
        for index in range(self._list.count()):
            item = self._list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) not in wanted:
                continue
            if not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
                continue
            item.setCheckState(
                Qt.CheckState.Unchecked
                if item.checkState() == Qt.CheckState.Checked
                else Qt.CheckState.Checked)
