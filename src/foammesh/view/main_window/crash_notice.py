#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR0 steps 6 and 7: saying that the last session died, and the report.

:class:`CrashNoticeBanner` is the non-modal strip across the top of the
window at the start after a session that did not exit cleanly: "FoamMesh
closed unexpectedly at <time> during <last operation>", with [Send report]
[Open logs folder] [Dismiss]. It blocks nothing and asks nothing. When the
session died drawing (plan 35 CR8: its last operation was ``render:*``, or
its dump faulted in a GPU driver) it also offers [Start in safe mode next
time].

:class:`CrashReportDialog` shows what a crash report would hold before a byte
is written -- file by file, with what was left out and why -- and where it
will be saved. "Send report" means exactly that and no more: the zip is
written locally (§9 D4) for the user to attach wherever they choose.
"""
from __future__ import annotations

import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)


def _when(record: dict) -> str:
    text = record.get('ended_at') or ''
    try:
        moment = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return text or '?'
    if moment.date() == datetime.date.today():
        return moment.strftime('%H:%M')
    return moment.strftime('%H:%M on %Y-%m-%d')


def last_operation(record: dict) -> str:
    """What the session was last seen doing, in words a user can read."""
    operation = (record.get('last_op') or '').strip()
    if not operation:
        recent = record.get('recent_operations') or []
        if recent:
            # Recorded as "<ISO time> <operation>".
            operation = str(recent[-1]).split(' ', 1)[-1]
    if operation.startswith('facade:'):
        operation = operation[len('facade:'):]
    return operation


def notice_text(records: list[dict], tr=lambda text: text) -> str:
    """The banner's sentence for the most recent of ``records``."""
    record = max(records, key=lambda item: item.get('ended_at') or '')
    operation = last_operation(record)
    if record.get('hang'):
        text = tr('FoamMesh stopped responding and was closed at {0}')
    else:
        text = tr('FoamMesh closed unexpectedly at {0}')
    text = text.format(_when(record))
    if operation:
        text += tr(' during {0}').format(operation)
    text += '.'
    if len(records) > 1:
        text += ' ' + tr('({0} earlier sessions also ended this way.)').format(
            len(records) - 1)
    return text


class CrashNoticeBanner(QFrame):
    reportRequested = Signal()
    openLogsRequested = Signal()
    #: Plan 35 CR8. "Start in safe mode next time" was chosen.
    safeModeRequested = Signal()

    OBJECT_NAME = 'crashNoticeBanner'

    def __init__(self, records: list[dict], parent=None, *,
                 offerSafeMode: bool | None = None):
        super().__init__(parent)
        self.records = list(records)
        if offerSafeMode is None:
            from foammesh.support import safe_mode
            decision = safe_mode.current()
            offerSafeMode = not decision.active and (decision.offer or any(
                safe_mode.is_render_attributed(record) for record in self.records))
        self.setObjectName(self.OBJECT_NAME)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty('severity', 'warning')

        self.message = QLabel(notice_text(self.records, self.tr), self)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        details = [record.get('ended') for record in self.records
                   if record.get('ended')]
        if details:
            self.message.setToolTip('\n'.join(details))

        self.reportButton = QPushButton(self.tr('Send report'), self)
        self.reportButton.setObjectName('crashNoticeReport')
        self.reportButton.setToolTip(self.tr(
            'Save a crash report to your Desktop; you see its contents first '
            'and nothing is sent anywhere'))
        self.logsButton = QPushButton(self.tr('Open logs folder'), self)
        self.logsButton.setObjectName('crashNoticeLogs')
        self.dismissButton = QPushButton(self.tr('Dismiss'), self)
        self.dismissButton.setObjectName('crashNoticeDismiss')
        self.safeModeButton = QPushButton(
            self.tr('Start in safe mode next time'), self)
        self.safeModeButton.setObjectName('crashNoticeSafeMode')
        self.safeModeButton.setToolTip(self.tr(
            'The session ended while drawing the viewport. Safe mode starts '
            'with the viewport off and draws with the simplest settings '
            'once you show it.'))
        self.safeModeButton.setVisible(bool(offerSafeMode))
        self.safeModeButton.clicked.connect(self._safeModeChosen)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self.message, 1)
        for button in (self.safeModeButton, self.reportButton, self.logsButton,
                       self.dismissButton):
            layout.addWidget(button)

        self.reportButton.clicked.connect(self.reportRequested)
        self.logsButton.clicked.connect(self.openLogsRequested)
        self.dismissButton.clicked.connect(self.dismiss)

    def offersSafeMode(self) -> bool:
        return not self.safeModeButton.isHidden()

    def _safeModeChosen(self) -> None:
        self.safeModeButton.setEnabled(False)
        self.safeModeButton.setText(self.tr('Safe mode next time: chosen'))
        self.safeModeRequested.emit()

    def dismiss(self) -> None:
        self.hide()
        self.deleteLater()


class CrashReportDialog(QDialog):
    """The confirmation: this is what will be written, and where."""

    OBJECT_NAME = 'crashReportDialog'

    def __init__(self, lines: list[str], destination, total_text: str,
                 parent=None):
        super().__init__(parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setWindowTitle(self.tr('Create crash report'))
        intro = QLabel(self.tr(
            'The report is a zip file saved to {0}. It holds the files below '
            '({1}); no mesh or geometry is included, and nothing is sent '
            'anywhere -- attach it to a message yourself if you want to share '
            'it.').format(destination, total_text), self)
        intro.setWordWrap(True)
        self.contents = QPlainTextEdit('\n'.join(lines), self)
        self.contents.setReadOnly(True)
        self.contents.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.contents, 1)
        layout.addWidget(self.buttons)
        self.resize(640, 420)
