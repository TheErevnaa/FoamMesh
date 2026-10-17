#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Entry problems shown where the entry is, not in a modal box.

Plan 40 UXD6 (section 4.3.7, F4c). A refused entry used to raise a modal
"Input error" box. Now:

* a field that holds a refused entry gets a red outline (the
  ``foammeshInvalid`` property, styled by the theme) and a one-line note
  just under it, and an entry that is fine but is not a plain number (``10 mm``,
  ``=diag/40``) gets the same note in the neutral tone with the SI value;
* a dialog or page whose refusal belongs to no single field gets a one-line
  banner at the foot of its own layout.

Nothing here blocks: the reader keeps typing, and the note clears as soon as
the entry is good.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QFormLayout, QGridLayout, QLabel, QWidget,
)

INVALID_PROPERTY = 'foammeshInvalid'
_NOTE = 'inlineNote'
_BANNER = 'inlineBanner'


def _repolish(widget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _status(label: QLabel, status: str) -> None:
    label.setProperty('foammeshStatus', status)
    _repolish(label)


class _Follower(QObject):
    """Keeps a note under its field and hides it with the field."""

    def __init__(self, field: QWidget, note: QLabel):
        super().__init__(note)
        self.field, self.note = field, note
        field.installEventFilter(self)
        window = field.window()
        if window is not None and window is not field:
            window.installEventFilter(self)

    def eventFilter(self, obj, event):          # noqa: N802
        kind = event.type()
        try:
            if kind in (QEvent.Type.Hide, QEvent.Type.Close):
                if obj is self.field:
                    self.note.hide()
            elif kind in (QEvent.Type.Move, QEvent.Type.Resize,
                          QEvent.Type.LayoutRequest, QEvent.Type.Show):
                if self.note.isVisibleTo(self.note.parentWidget()) and \
                        self.field.isVisible():
                    _place(self.field, self.note)
                    self.note.show()
        except RuntimeError:
            pass
        return False


def _note_for(field: QWidget) -> QLabel | None:
    window = field.window()
    if window is None:
        return None
    note = field.property('_inlineNote')
    if isinstance(note, QLabel):
        try:
            note.objectName()
            return note
        except RuntimeError:
            pass
    note = QLabel(window)
    note.setObjectName(_NOTE)
    note.setTextFormat(Qt.TextFormat.PlainText)
    note.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    note.setAutoFillBackground(True)
    note.hide()
    field.setProperty('_inlineNote', note)
    _Follower(field, note)
    return note


def _place(field: QWidget, note: QLabel) -> None:
    window = field.window()
    note.adjustSize()
    below = field.mapTo(window, field.rect().bottomLeft())
    x = max(0, min(below.x(), window.width() - note.width() - 2))
    y = below.y() + 1
    if y + note.height() > window.height():
        above = field.mapTo(window, field.rect().topLeft())
        y = max(0, above.y() - note.height() - 1)
    note.move(x, y)
    note.raise_()


def _show_note(field: QWidget, text: str, status: str) -> None:
    note = _note_for(field)
    if note is None:
        return
    note.setText(text)
    _status(note, status)
    if field.isVisible():
        _place(field, note)
        note.show()


def set_invalid(field: QWidget, message: str) -> None:
    """Outline *field* in the error colour and say why, one line, under it."""
    field.setProperty(INVALID_PROPERTY, True)
    field.setAccessibleDescription(message)
    _repolish(field)
    _show_note(field, message, 'error')


def set_hint(field: QWidget, text: str) -> None:
    """A neutral note under *field* (the SI value of what was typed)."""
    if is_invalid(field):
        clear_invalid(field)
    _show_note(field, text, 'info')


def is_invalid(field: QWidget) -> bool:
    return bool(field.property(INVALID_PROPERTY))


def message_of(field: QWidget) -> str:
    note = field.property('_inlineNote')
    try:
        return note.text() if isinstance(note, QLabel) and note.isVisibleTo(
            note.parentWidget()) else ''
    except RuntimeError:
        return ''


def clear_invalid(field: QWidget) -> None:
    """Take the outline and the note away."""
    was = is_invalid(field)
    if was:
        field.setProperty(INVALID_PROPERTY, False)
        field.setAccessibleDescription('')
        _repolish(field)
    note = field.property('_inlineNote')
    try:
        if isinstance(note, QLabel):
            note.hide()
            note.setText('')
    except RuntimeError:
        pass


# -- a refusal that belongs to no single field ------------------------------ #

class _Dismisser(QObject):
    """Takes a banner away at the reader's next key or click inside its host."""

    _live: dict = {}

    @classmethod
    def arm(cls, host: QWidget, banner: QLabel) -> None:
        if id(banner) in cls._live:
            return
        app = QApplication.instance()
        if app is None:
            return
        self = cls(banner)
        self.host, self.banner = host, banner
        cls._live[id(banner)] = self
        app.installEventFilter(self)

    def _disarm(self) -> None:
        self._live.pop(id(self.banner), None)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, obj, event):          # noqa: N802
        if event.type() not in (QEvent.Type.KeyPress,
                                QEvent.Type.MouseButtonPress):
            return False
        try:
            inside = isinstance(obj, QWidget) and (
                obj is self.host or self.host.isAncestorOf(obj))
            if not inside:
                return False
            if event.type() == QEvent.Type.KeyPress and event.key() in (
                    Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt):
                return False
            self.banner.setVisible(False)
            self.banner.setText('')
        except RuntimeError:
            pass
        self._disarm()
        return False


def _banner_of(host: QWidget) -> QLabel | None:
    for child in host.findChildren(QLabel, _BANNER):
        return child
    return None


def show(host: QWidget, message: str, *fields: QWidget) -> None:
    """Say *message* in place: a banner at the foot of *host*.

    Any *fields* named are outlined too. Replaces the modal "Input error"
    box; the caller returns as it did after the box.
    """
    banner = _banner_of(host)
    if banner is None:
        banner = QLabel(host)
        banner.setObjectName(_BANNER)
        banner.setWordWrap(True)
        banner.setTextFormat(Qt.TextFormat.PlainText)
        layout = host.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(banner)
        elif isinstance(layout, QGridLayout):
            layout.addWidget(banner, layout.rowCount(), 0, 1,
                             max(layout.columnCount(), 1))
        elif isinstance(layout, QBoxLayout):
            layout.addWidget(banner)
        else:
            banner.setGeometry(0, max(host.height() - 24, 0),
                               host.width(), 24)
    banner.setText(str(message))
    _status(banner, 'error')
    banner.setVisible(True)
    _Dismisser.arm(host, banner)
    for field in fields:
        set_invalid(field, str(message))


def clear(host: QWidget) -> None:
    """Take the banner away (a new attempt starts clean)."""
    banner = _banner_of(host)
    if banner is not None:
        banner.setVisible(False)
        banner.setText('')


def banner_text(host: QWidget) -> str:
    banner = _banner_of(host)
    if banner is None or banner.isHidden():
        return ''
    return banner.text()
