"""Plan 44 MF18 (VS9, B9). A notice: an icon, one line, and "More…".

The task page said a refused write and the cost of an edit with a wrapped
paragraph in orange (``foammeshStatus="warning"``), three or four lines tall,
above the first setting. The mockup has no paragraph above the form: a note
is one quiet line. A ``NoticeCard`` is a small card in the theme's notice
look with a 14 px glyph in the status colour and the sentence on one line,
elided; when the sentence does not fit, a "More…" link opens it in full and
"Less" folds it back. Collapsed it is never taller than one line of text plus
its padding.

It answers the ``QLabel`` calls the pages already make on a note --
``setText``, ``text``, ``clear``, ``setWordWrap`` -- so a page that swaps its
label for one changes nothing else.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout)

from foammesh.view.theming.metrics import FORM_MARGIN, GAP, MARGIN_TIGHT

INFO = 'info'
WARNING = 'warning'
ERROR = 'error'

#: The property the style sheet keys the card on (``forms.qss.tmpl``).
NOTICE_PROPERTY = 'foammeshNotice'
ICON_SIZE = 14

_GLYPHS = {
    INFO: ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
           '<circle cx="8" cy="8" r="7" fill="{c}"/>'
           '<rect x="7" y="7" width="2" height="5" fill="#000" '
           'fill-opacity=".75"/><rect x="7" y="4" width="2" height="2" '
           'fill="#000" fill-opacity=".75"/></svg>'),
    WARNING: ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
              '<path d="M8 1 15.5 14.5H.5z" fill="{c}"/>'
              '<rect x="7" y="5.5" width="2" height="5" fill="#000" '
              'fill-opacity=".75"/><rect x="7" y="11.5" width="2" height="2" '
              'fill="#000" fill-opacity=".75"/></svg>'),
    ERROR: ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            '<circle cx="8" cy="8" r="7" fill="{c}"/>'
            '<path d="M5.2 5.2l5.6 5.6M10.8 5.2l-5.6 5.6" stroke="#000" '
            'stroke-opacity=".75" stroke-width="2"/></svg>'),
}
_TOKENS = {INFO: ('status.info', '#6da0ff'),
           WARNING: ('status.warning', '#d2a106'),
           ERROR: ('status.error', '#ec837d')}
#: (kind, colour, device pixel ratio) -> the glyph drawn in that colour
_PIXMAPS: dict = {}


class NoticeCard(QFrame):
    """One notice: glyph, one elided line, and a More… / Less link."""

    def __init__(self, parent=None, *, kind: str = INFO,
                 name: str = '') -> None:
        super().__init__(parent)
        from foammesh.view.workflow_controls.lock_chip import ElidedLabel
        if name:
            self.setObjectName(name)
        self._kind = INFO
        self._text = ''
        self._expanded = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Maximum)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(FORM_MARGIN, MARGIN_TIGHT, FORM_MARGIN,
                                 MARGIN_TIGHT)
        outer.setSpacing(GAP)
        self.icon = QLabel(self)
        self.icon.setObjectName('noticeIcon')
        self.icon.setFixedSize(ICON_SIZE, ICON_SIZE)
        outer.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.line = ElidedLabel(self)
        self.line.setObjectName('noticeLine')
        self.full = QLabel(self)
        self.full.setObjectName('noticeFull')
        self.full.setWordWrap(True)
        self.full.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.full.setVisible(False)
        column.addWidget(self.line)
        column.addWidget(self.full)
        outer.addLayout(column, 1)
        self.more = QPushButton(self.tr('More…'), self)
        self.more.setObjectName('noticeMore')
        self.more.setFlat(True)
        self.more.setProperty('foammeshRole', 'link')
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.setFixedHeight(ICON_SIZE + 2)
        self.more.setVisible(False)
        self.more.clicked.connect(self.toggleExpanded)
        outer.addWidget(self.more, 0, Qt.AlignmentFlag.AlignTop)
        self.setKind(kind)

    # -- the QLabel calls a page makes on a note -------------------------- #

    def setText(self, text: str) -> None:
        self._text = str(text or '')
        self.line.setFullText(self._text)
        self.full.setText(self._text)
        self.setAccessibleDescription(self._text)
        self.setToolTip(self._text)
        self._follow()

    def text(self) -> str:
        return self._text

    def clear(self) -> None:
        self.setText('')

    def setWordWrap(self, _wrap: bool) -> None:
        """A notice is one line until it is opened; there is nothing to set."""

    def setTextInteractionFlags(self, flags) -> None:
        self.full.setTextInteractionFlags(flags)

    # -- the card's own ------------------------------------------------------ #

    def kind(self) -> str:
        return self._kind

    def setKind(self, kind: str) -> None:
        kind = kind if kind in _GLYPHS else INFO
        self._kind = kind
        self.setProperty(NOTICE_PROPERTY, kind)
        from foammesh.view.workflow_controls.lock_chip import repolish
        repolish(self)
        self._paint_icon()

    def setProperty(self, name, value):
        # A page that still says ``setProperty('foammeshStatus', 'warning')``
        # on its note is asking for the warning card.
        if name == 'foammeshStatus' and value in _GLYPHS and (
                getattr(self, '_kind', None) is not None
                and value != self._kind):
            self.setKind(value)
            return True
        return super().setProperty(name, value)

    def isExpanded(self) -> bool:
        return self._expanded

    def toggleExpanded(self) -> None:
        self.setExpanded(not self._expanded)

    def setExpanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self._follow()

    def isElided(self) -> bool:
        """Whether the one line could not hold the whole sentence."""
        if not self._text:
            return False
        if '\n' in self._text:
            return True
        width = self.line.width()
        return self.line.fontMetrics().horizontalAdvance(self._text) > width

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._follow()

    def event(self, event):
        from PySide6.QtCore import QEvent
        if event.type() in (QEvent.Type.StyleChange,
                            QEvent.Type.PaletteChange):
            self._paint_icon()
        return super().event(event)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(min(hint.width(), 200), hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def _follow(self) -> None:
        elided = self.isElided()
        expanded = self._expanded and elided
        self.line.setVisible(not expanded)
        self.full.setVisible(expanded)
        self.more.setVisible(elided)
        self.more.setText(self.tr('Less') if expanded else self.tr('More…'))

    def _paint_icon(self) -> None:
        from foammesh.view.workflow_controls.lock_chip import _token, svg_icon
        name, fallback = _TOKENS[self._kind]
        colour = _token(name, fallback)
        screen = QApplication.primaryScreen() if QApplication.instance() else None
        ratio = screen.devicePixelRatio() if screen is not None else 1.0
        key = (self._kind, colour, ratio)
        # Plan 45 W1. A theme switch re-polishes the tree once per ancestor,
        # so a card hears StyleChange a dozen times or more in one switch;
        # each used to draw its SVG again. The glyph is the same for every
        # card of a kind in a colour, so it is drawn once and kept.
        if getattr(self, '_painted', None) == key:
            return
        pixmap = _PIXMAPS.get(key)
        if pixmap is None:
            pixmap = svg_icon(_GLYPHS[self._kind], colour, ICON_SIZE).pixmap(
                ICON_SIZE, ICON_SIZE)
            _PIXMAPS[key] = pixmap
        self._painted = key
        self.icon.setPixmap(pixmap)


__all__ = ['ERROR', 'INFO', 'NOTICE_PROPERTY', 'NoticeCard', 'WARNING']
