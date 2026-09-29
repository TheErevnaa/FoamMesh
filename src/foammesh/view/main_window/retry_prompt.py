"""Plan 35 CR6. [Retry] and [Retry with fewer cores] for a failed run.

Asked by :class:`DesktopFacadeClient` when a meshing run failed in a way the
retry policy (``foammesh.core.jobs.retry``) offers to run again: the WSL
transport broke, or the run was killed for memory. The box is window-modal
but never blocks the event loop: the answer arrives on a future.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from foammesh.core.jobs.retry import RETRY, RETRY_FEWER_CORES


def retry_text(result, offer: dict) -> tuple[str, str]:
    """The sentence and the informative line for *offer*."""
    payload = getattr(result, 'payload', None) or {}
    reason = str(payload.get('reason') or 'The run failed.').strip()
    if offer.get('kind') == 'oom':
        detail = ('The run was stopped because the computer ran out of memory. '
                  'It can be run again from where it started')
        if RETRY_FEWER_CORES in (offer.get('actions') or ()):
            detail += (f", or on {offer.get('fewer_cores')} core(s) instead of "
                       f"{offer.get('ranks')}, which needs less memory")
        detail += '.'
    else:
        detail = ('The connection to the WSL runtime was lost while the run was '
                  'starting or running. It can be run again from where it '
                  'started once the runtime answers.')
    return reason, detail + ' A run is retried at most once.'


class RetryPrompt:
    """``await RetryPrompt(parent)(operation, result, offer)`` -> action | None."""

    def __init__(self, parent=None):
        self._parent = parent
        self.box: QMessageBox | None = None

    async def __call__(self, operation, result, offer) -> str | None:
        reason, detail = retry_text(result, offer)
        box = self.box = QMessageBox(QMessageBox.Icon.Warning,
                                     'Run failed', reason, parent=self._parent)
        box.setObjectName('retryPrompt')
        box.setInformativeText(detail)
        box.setWindowModality(Qt.WindowModality.WindowModal)
        actions = list(offer.get('actions') or ())
        buttons = {}
        if RETRY in actions:
            buttons[box.addButton('Retry', QMessageBox.ButtonRole.AcceptRole)] = RETRY
        if RETRY_FEWER_CORES in actions:
            buttons[box.addButton(
                f"Retry with fewer cores ({offer.get('fewer_cores')})",
                QMessageBox.ButtonRole.AcceptRole)] = RETRY_FEWER_CORES
        close = box.addButton(QMessageBox.StandardButton.Close)
        box.setDefaultButton(close)
        future = asyncio.get_running_loop().create_future()

        def clicked(button):
            if not future.done():
                future.set_result(buttons.get(button))

        def finished(_code):
            if not future.done():
                future.set_result(None)
        box.buttonClicked.connect(clicked)
        box.finished.connect(finished)
        box.show()
        try:
            return await future
        finally:
            self.box = None
            box.deleteLater()
