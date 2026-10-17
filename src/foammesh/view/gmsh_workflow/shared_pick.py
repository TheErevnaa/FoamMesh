"""The one Pick tool, as a page asks for it (Plan 42 SG13).

Structure and Size at points each kept a hidden entity panel so they could
pick corners and points. They hold this instead: a small object that turns a
page's ``pickFor('Points')`` into a :class:`PickRequest` on the one
:class:`SelectionService`. The picker that serves it is the visible panel on
the Geometry row.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from foammesh.core.selection import SelectionService
from foammesh.core.selection.pick import PickRequest

_FALLBACK: SelectionService | None = None


def selection_service() -> SelectionService:
    """The application's selection service (a private one without an app)."""
    global _FALLBACK
    try:
        from foammesh.app import app

        service = app.selectionService()
        if service is not None:
            return service
    except Exception:                                # noqa: BLE001
        pass
    if _FALLBACK is None:
        _FALLBACK = SelectionService()
    return _FALLBACK


class SharedPick(QObject):
    """A page's handle on the Pick tool; same calls the hidden panel had."""

    pickAccepted = Signal(object)

    def __init__(self, requester: str = '', ordered: bool = False,
                 parent=None, service_provider=None):
        super().__init__(parent)
        self._requester = requester
        self._ordered = ordered
        self._provider = service_provider or selection_service
        self._status = ''

    def service(self) -> SelectionService:
        return self._provider()

    def pickFor(self, filter_name: str | None = None) -> bool:
        service = self.service()
        request = PickRequest(
            (filter_name or 'Surfaces',), requester=self._requester,
            ordered=self._ordered, on_accept=self.pickAccepted.emit)
        started = service.begin_pick(request, engine='gmsh')
        self._status = '' if started else service.pick_reason
        return started

    def cancel(self) -> None:
        self.service().cancel_pick()

    def status(self) -> str:
        return self._status

    def _source(self):
        return self.service().pick_source

    def hasModel(self) -> bool:
        source = self._source()
        return bool(source is not None and source.hasModel())

    def picker(self):
        source = self._source()
        return source.picker() if source is not None else None
