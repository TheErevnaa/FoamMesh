#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json

from PySide6.QtCore import QCoreApplication, QEvent, QMargins
from PySide6.QtGui import QColor, QFontDatabase

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


class Console(QWidget):
    def __init__(self):
        super().__init__()
        self._view = QPlainTextEdit(self)
        self._view.setProperty('foammeshConsole', True)
        self._view.document().setMaximumBlockCount(20000)

        charFormat = self._view.currentCharFormat()
        fixedFont = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        charFormat.setFont(fixedFont)
        self._view.setCurrentCharFormat(charFormat)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(QMargins(0, 0, 0, 0))
        layout.addWidget(self._view)

    def clear(self):
        self._view.clear()

    def append(self, text):
        self._view.appendPlainText(readable_line(text))

    def appendError(self, text):
        original = self._view.currentCharFormat()
        error_format = self._view.currentCharFormat()
        error_format.setForeground(QColor('#ef5350'))
        self._view.setCurrentCharFormat(error_format)
        self._view.appendPlainText(text)
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
