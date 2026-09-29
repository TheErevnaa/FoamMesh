"""Plan 35 CR9. The unsaved-changes offer and the not-protected warning.

Two bars across the top of the window, both non-modal:

* ``RecoveryOfferBar`` -- a case opened with change sets the last session
  journalled but never saved. [Restore unsaved changes (K edits)] replays
  them as one undoable change set, [Open last saved] drops them, and
  [Show what changed] lists them without choosing either.
* ``ProtectionBar`` -- the autosave writer cannot reach the disk. It says so
  with the reason for as long as that is true ("Your changes are not being
  protected: the disk is full.") and offers [Save now] [Save as...]. The
  writer retries by itself; the bar goes when a retry succeeds.

``offer_recovery(window, autosave, ...)`` is the one entry point: it builds
whichever bars apply, puts them at the top of *layout*, and returns an
``AutosaveBars`` whose ``dispose()`` takes them down when the case closes.
Nothing here writes; the choices call back into ``Autosave``.
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from foammesh.view.theming.metrics import GAP, apply_bar_metrics

logger = logging.getLogger(__name__)


def _when(timestamp) -> str:
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
        except (OverflowError, OSError, ValueError):
            return ''
    return str(timestamp or '')


def describe_changes(entries: list[dict]) -> str:
    """The offered change sets, one per line, oldest first."""
    lines = []
    for entry in entries:
        action = entry.get('action') or entry.get('kind') or 'edit'
        when = _when(entry.get('timestamp'))
        head = f"{entry.get('seq')}. {action}" + (f'  ({when})' if when else '')
        lines.append(head)
        changes = entry.get('changes') or []
        for path in changes[:12]:
            lines.append(f'      {path}')
        if len(changes) > 12:
            lines.append(f'      ... and {len(changes) - 12} more')
    return '\n'.join(lines)


class ChangesDialog(QDialog):
    """[Show what changed]: the list, non-modal, choosing nothing."""

    def __init__(self, entries: list[dict], dropped: int = 0, parent=None):
        super().__init__(parent)
        self.setObjectName('recoveryChangesDialog')
        self.setWindowTitle(self.tr('Unsaved changes'))
        self.setModal(False)
        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            'These changes were made after the case was last saved and were '
            'kept when FoamMesh stopped unexpectedly.'), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.text = QPlainTextEdit(self)
        self.text.setObjectName('recoveryChangesText')
        self.text.setReadOnly(True)
        body = describe_changes(entries)
        if dropped:
            body += '\n\n' + self.tr(
                '{0} further record(s) were damaged and cannot be restored.').format(dropped)
        self.text.setPlainText(body)
        layout.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.resize(560, 360)


class _Bar(QFrame):
    def __init__(self, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._layout = QHBoxLayout(self)
        apply_bar_metrics(self, self._layout)
        self._layout.setSpacing(GAP)
        self._text = QLabel(self)
        self._text.setWordWrap(True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addWidget(self._text, 1)

    def text(self) -> str:
        return self._text.text()

    def _button(self, text: str, object_name: str, slot) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(object_name)
        button.clicked.connect(slot)
        self._layout.addWidget(button)
        return button


class RecoveryOfferBar(_Bar):
    """[Restore unsaved changes (K edits)] [Open last saved] [Show what changed]."""

    restored = Signal(int, bool)     # how many, whether geometry came back
    discarded = Signal()

    def __init__(self, autosave, parent=None):
        super().__init__('recoveryOfferBar', parent)
        self.setAccessibleName(self.tr('Unsaved changes'))
        self._autosave = autosave
        self._dialog: ChangesDialog | None = None
        offer = autosave.pending_recovery()
        count = offer.count if offer is not None else 0
        self._text.setText(self.tr(
            'FoamMesh stopped before this case was saved. {0} unsaved '
            'change(s) were kept.').format(count))
        self.restoreButton = self._button(
            self.tr('Restore unsaved changes ({0} edits)').format(count),
            'recoveryRestore', self._restore)
        self.discardButton = self._button(
            self.tr('Open last saved'), 'recoveryDiscard', self._discard)
        self.showButton = self._button(
            self.tr('Show what changed'), 'recoveryShow', self.showChanges)

    def showChanges(self) -> ChangesDialog | None:
        offer = self._autosave.pending_recovery()
        if offer is None:
            return None
        if self._dialog is None:
            self._dialog = ChangesDialog(offer.describe(), offer.dropped_records,
                                         self.window())
        self._dialog.show()
        self._dialog.raise_()
        return self._dialog

    def _restore(self) -> None:
        offer = self._autosave.pending_recovery()
        if offer is None:
            self._finish()
            return
        files = any(record.payload.get('files') for record in offer.records)
        try:
            count = self._autosave.restore()
        except Exception as error:  # noqa: BLE001 - say it; keep the offer
            logger.exception('restoring unsaved changes failed')
            self._text.setText(self.tr(
                'The unsaved changes could not be restored: {0}').format(error))
            return
        self._finish()
        self.restored.emit(count, files)

    def _discard(self) -> None:
        try:
            self._autosave.discard_recovery()
        except Exception:  # noqa: BLE001
            logger.exception('dropping unsaved changes failed')
        self._finish()
        self.discarded.emit()

    def _finish(self) -> None:
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None
        self.hide()


class _StatusRelay(QObject):
    """Carries writer-thread status onto the GUI thread (queued)."""
    changed = Signal(bool, str)


class ProtectionBar(_Bar):
    """Your changes are not being protected: <reason>. [Save now] [Save as...]"""

    saveRequested = Signal()
    saveAsRequested = Signal()

    def __init__(self, autosave, parent=None):
        super().__init__('autosaveProtectionBar', parent)
        self.setAccessibleName(self.tr('Changes not protected'))
        self.saveButton = self._button(self.tr('Save now'), 'autosaveSaveNow',
                                       self.saveRequested.emit)
        self.saveAsButton = self._button(self.tr('Save as…'), 'autosaveSaveAs',
                                         self.saveAsRequested.emit)
        relay = self._relay = _StatusRelay(self)
        relay.changed.connect(self.setStatus)

        def _status(ok, reason):
            # The writer thread outlives a closed window; a bar that is gone
            # has nobody left to tell.
            if shiboken6.isValid(relay):
                relay.changed.emit(bool(ok), reason or '')
        self._remove = autosave.add_status_listener(_status)
        failure = autosave.failure
        self.setStatus(failure is None, failure or '')

    def setStatus(self, ok: bool, reason: str) -> None:
        if ok:
            self.hide()
            return
        self._text.setText(self.tr(
            'Your changes are not being protected: {0}.').format(reason.rstrip('.')))
        self.show()

    def detach(self) -> None:
        remove, self._remove = self._remove, None
        if remove is not None:
            remove()


class AutosaveBars:
    """What ``offer_recovery`` put up, and how to take it down."""

    def __init__(self, offer: RecoveryOfferBar | None, protection: ProtectionBar):
        self.offer = offer
        self.protection = protection

    def dispose(self) -> None:
        self.protection.detach()
        for bar in (self.offer, self.protection):
            if bar is None or not shiboken6.isValid(bar):
                continue
            if isinstance(bar, RecoveryOfferBar):
                bar._finish()
            bar.hide()
            bar.setParent(None)
            bar.deleteLater()
        self.offer = None


def offer_recovery(window, autosave, *, layout=None,
                   on_restored: Callable[[int, bool], None] | None = None,
                   on_save: Callable[[], None] | None = None,
                   on_save_as: Callable[[], None] | None = None) -> AutosaveBars | None:
    """Put up the recovery offer (if one is pending) and the protection bar.

    *layout* is a box layout the bars go at the top of; the window's central
    widget layout when it is not given. Returns ``None`` when there is no
    autosave to speak for.
    """
    if autosave is None:
        return None
    if layout is None:
        central = window.centralWidget() if hasattr(window, 'centralWidget') else None
        layout = central.layout() if central is not None else None
    parent = layout.parentWidget() if layout is not None else window
    protection = ProtectionBar(autosave, parent)
    if on_save is not None:
        protection.saveRequested.connect(on_save)
    if on_save_as is not None:
        protection.saveAsRequested.connect(on_save_as)
    offer = None
    if autosave.pending_recovery() is not None:
        offer = RecoveryOfferBar(autosave, parent)
        if on_restored is not None:
            offer.restored.connect(on_restored)
    if layout is not None:
        layout.insertWidget(0, protection)
        if offer is not None:
            layout.insertWidget(0, offer)
    return AutosaveBars(offer, protection)
