#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json

from PySide6.QtCore import QCoreApplication, QEvent, QMargins, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QTextCursor

from PySide6.QtWidgets import QPlainTextEdit, QWidget, QVBoxLayout
from PySide6QtAds import CDockWidget


#: The line prefix the meshing runners use to report progress as JSON.
#: See ``src/resources/gmsh/runner_v1.py`` (``Reporter.emit``).
PROGRESS_PREFIX = 'FOAMMESH_PROGRESS '


def readable_line(text):
    """Turn a progress record into a line of English (F12).

    The runners report stage progress as one JSON object per line so the job
    manager can parse it, and the console showed that object verbatim -- a wall
    of ``{"schema_version": 1, "sequence": 8, ...}`` in the middle of the
    utility's own output. The job manager writes every raw line to the stage
    log *before* it publishes, so the record itself is not lost by rendering
    it here.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped.startswith(PROGRESS_PREFIX):
        return text
    try:
        record = json.loads(stripped[len(PROGRESS_PREFIX):])
    except ValueError:
        return text
    if not isinstance(record, dict):
        return text
    message = str(record.get('message')
                  or record.get('stage_id')
                  or record.get('kind') or '').strip()
    try:
        percent = '[{0:3.0f}%] '.format(float(record['fraction']) * 100)
    except (KeyError, TypeError, ValueError):
        percent = ''
    return (percent + message).rstrip() or text


def _is_progress(line):
    return line.lstrip().startswith(PROGRESS_PREFIX)


def readable_batch(text):
    """Render a batch of output lines for the console (Plan 35 CR4).

    Output arrives in batches of up to 500 lines. Only the *last* progress
    record of a batch is parsed and shown -- it supersedes the ones before
    it, and parsing every record was a ``json.loads`` per line on the GUI
    thread. The earlier records stay in the stage log.
    """
    if not isinstance(text, str) or '\n' not in text:
        return readable_line(text)
    lines = text.split('\n')
    last = next((index for index in range(len(lines) - 1, -1, -1)
                 if _is_progress(lines[index])), None)
    if last is None:
        return text
    return '\n'.join(readable_line(line) if index == last else line
                     for index, line in enumerate(lines)
                     if index == last or not _is_progress(line))


#: CR4. Appends arriving within this window of the last one are held and
#: written as one block of text; a flood also flushes at this many lines.
APPEND_BATCH_MS = 100
APPEND_BATCH_LINES = 500
#: The console keeps the last CONSOLE_LINES lines. It trims itself in one
#: cut once CONSOLE_TRIM_SLACK more have arrived: the document's own
#: ``maximumBlockCount`` trims a block at a time on every append, which cost
#: 6 ms per 500-line batch against 0.8 ms for this (CR4).
CONSOLE_LINES = 20_000
CONSOLE_TRIM_SLACK = 10_000


class Console(QWidget):
    def __init__(self):
        super().__init__()
        self._view = QPlainTextEdit(self)
        self._view.setProperty('foammeshConsole', True)

        charFormat = self._view.currentCharFormat()
        fixedFont = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        charFormat.setFont(fixedFont)
        self._view.setCurrentCharFormat(charFormat)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(QMargins(0, 0, 0, 0))
        layout.addWidget(self._view)

        # CR4 (F4). A stage run hands the console every line through
        # `on_line`; one `appendPlainText` per line is a layout pass per
        # line on the GUI thread. The first append after a quiet spell is
        # written at once; the ones that follow within APPEND_BATCH_MS are
        # held and written together when the window closes.
        self._pending: list[str] = []
        self._pendingLines = 0
        self._batchTimer = QTimer(self)
        self._batchTimer.setSingleShot(True)
        self._batchTimer.setInterval(APPEND_BATCH_MS)
        self._batchTimer.timeout.connect(self.flush)

    def clear(self):
        self._pending.clear()
        self._pendingLines = 0
        self._view.clear()

    def append(self, text):
        text = '' if text is None else str(text)
        if not self._batchTimer.isActive():
            self._write(readable_batch(text))
            self._batchTimer.start()
            return
        self._pending.append(text)
        self._pendingLines += text.count('\n') + 1
        if self._pendingLines >= APPEND_BATCH_LINES:
            self.flush()

    def flush(self):
        """Write the held appends as one block of text."""
        if not self._pending:
            return
        text = '\n'.join(self._pending)
        self._pending.clear()
        self._pendingLines = 0
        self._write(readable_batch(text))
        self._batchTimer.start()

    def _write(self, text):
        self._view.appendPlainText(text)
        document = self._view.document()
        excess = document.blockCount() - CONSOLE_LINES
        if excess > CONSOLE_TRIM_SLACK:
            cursor = QTextCursor(document)
            cursor.setPosition(document.findBlockByNumber(excess).position(),
                               QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

    def appendError(self, text):
        self.flush()
        original = self._view.currentCharFormat()
        error_format = self._view.currentCharFormat()
        error_format.setForeground(QColor('#ef5350'))
        self._view.setCurrentCharFormat(error_format)
        self._write(text)
        self._view.setCurrentCharFormat(original)


class ConsoleView(CDockWidget):
    def __init__(self):
        super().__init__(self._title())

        self.setWidget(Console())

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.setWindowTitle(self._title())

        super().changeEvent(event)

    def clear(self):
        self.widget().clear()

    def _title(self):
        return QCoreApplication.translate('ConsoleView', 'Console')
