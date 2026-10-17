"""Plan 44 MF23: the on-model inspector card (the mockup's layout C).

In layout C of the Mesh quality tab the inspector is not a column of the
console band: it is a 250 px card on the model, at the left of the clear
area (``final-layout.html`` ``.qinsp``). The card is a second view of the
tab's own inspector. It owns no state: every value is read from the
``MeshQualityTab`` and every press is handed back to the tab's own control,
so the card and the tab column always show the same limit, the same cells
and the same toggles, and no tool exists only in one of them.

The card paints itself (background, border, the ``.qbtn`` buttons) from the
theme tokens, with the pre-blended fallbacks of Plan 44 section 3, so it
needs no style sheet and nothing from the theming package beyond tokens.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from foammesh.view.theming.metrics import FORM_MARGIN, GAP, GAP_TIGHT

#: The width of the card (mockup ``.qinsp``).
CARD_WIDTH = 250
#: Distance from the left edge of the clear area, and from the top of the view.
CARD_LEFT = 16
CARD_TOP = 56
CARD_RADIUS = 6
BUTTON_RADIUS = 3

#: theme token -> pre-blended dark fallback (Plan 44 section 3), used when no
#: theme is running (tools and tests). Every name is a token both themes
#: define, so the light theme paints a light card (Plan 44 MF24 item 7).
_FALLBACKS = {
    'overlay.legend': '#191c22',        # the card plate
    'border.legend': '#363b44',         # the card frame
    'foreground.legend': '#c9ced6',     # values
    'foreground.sub': '#8b919a',        # title and metric captions
    'foreground.strong': '#e6e8eb',     # a lit or hovered button's words
    'background.neutral': '#24272d',    # a button
    'border.default': '#3a3f48',        # a button's frame
    'selection.background': '#2f5ca8',  # a lit button
    'accent.default': '#5b93f0',        # a lit or hovered frame, the focus ring
}


def _token(name: str) -> QColor:
    """The running theme's colour for *name*, else the mockup's value."""
    value = _FALLBACKS[name]
    try:
        from foammesh.view.theming.patch_palette import live_tokens
        tokens = live_tokens()
        if tokens is not None:
            value = tokens.values.get(name, value)
    except Exception:  # noqa: BLE001 - no theme in tools and tests
        pass
    return QColor(value)


class QuickButton(QPushButton):
    """The mockup's ``.qbtn``: 20 px tall, 3 px radius, 1 px border, lit
    (``on``) when checked. Painted, so no style sheet is involved."""

    HEIGHT = 20

    def __init__(self, text: str, parent=None, *, checkable: bool = False):
        super().__init__(text, parent)
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._hover = False

    def sizeHint(self) -> QSize:
        width = self.fontMetrics().horizontalAdvance(self.text()) + 2 * 9 + 2
        return QSize(width, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        lit = self.isChecked()
        fill = _token('selection.background' if lit else 'background.neutral')
        edge = _token('accent.default') if (lit or self._hover) \
            else _token('border.default')
        ink = _token('foreground.strong') if (lit or self._hover) \
            else _token('foreground.legend')
        if not self.isEnabled():
            painter.setOpacity(0.45)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(
            QRect(0, 0, self.width() - 1, self.height() - 1),
            BUTTON_RADIUS, BUTTON_RADIUS)
        painter.setPen(ink)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         self.text())
        if self.hasFocus():
            painter.setPen(QPen(_token('accent.default'), 1,
                                Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                QRect(1, 1, self.width() - 3, self.height() - 3),
                BUTTON_RADIUS, BUTTON_RADIUS)


class InspectorCard(QFrame):
    """The 250 px card. ``tab`` is the ``MeshQualityTab`` it mirrors."""

    def __init__(self, tab, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tab = tab
        self._clear_of = None
        self._watcher = None
        self.setObjectName('inspectorCard')
        self.setAccessibleName('Inspect cells')
        self.setFixedWidth(CARD_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)

        column = QVBoxLayout(self)
        column.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        column.setSpacing(GAP_TIGHT)

        head = QHBoxLayout()
        head.setSpacing(GAP)
        self._title = QLabel(self.tr('Inspect cells').upper(), self)
        self._title.setObjectName('inspectorCardCaps')  # a caps label, not a page heading (DP-219)
        caps = QFont(self._title.font())
        caps.setPixelSize(10)
        caps.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        self._title.setFont(caps)
        self._metric = QLabel(self)
        self._metric.setObjectName('inspectorCardMetric')
        self._metric.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self._title, 1)
        head.addWidget(self._metric)
        column.addLayout(head)

        limit = QHBoxLayout()
        limit.setSpacing(GAP)
        self._limit_label = QLabel(self.tr('Limit'), self)
        self._limit = QLineEdit(self)
        self._limit.setObjectName('inspectorCardLimit')
        self._limit.setAccessibleName(self.tr('Limit for the chosen metric'))
        self._limit.setPlaceholderText(self.tr('none'))
        self._limit.setFixedHeight(QuickButton.HEIGHT)
        self._limit.setMinimumWidth(56)
        self._mesher = QuickButton(self.tr('Mesher'), self)
        self._mesher.setObjectName('inspectorCardMesher')
        self._mesher.setToolTip(self.tr("Use the mesher's limit"))
        self._check = QuickButton(self.tr('Check'), self)
        self._check.setObjectName('inspectorCardCheck')
        self._check.setToolTip(self.tr("Use the check's limit"))
        limit.addWidget(self._limit_label)
        limit.addWidget(self._limit, 1)
        limit.addWidget(self._mesher)
        limit.addWidget(self._check)
        column.addLayout(limit)

        beyond = QHBoxLayout()
        beyond.setSpacing(GAP)
        self._beyond = QuickButton(self.tr('Show cells beyond limit'), self,
                                   checkable=True)
        self._beyond.setObjectName('inspectorCardBeyond')
        beyond.addWidget(self._beyond)
        beyond.addStretch(1)
        column.addLayout(beyond)

        walk = QHBoxLayout()
        walk.setSpacing(GAP)
        self._walk_label = QLabel(self)
        self._walk_label.setObjectName('inspectorCardBeyondCount')
        self._walk_label.setWordWrap(True)
        column.addWidget(self._walk_label)
        self._previous = QuickButton('◀', self)
        self._previous.setObjectName('inspectorCardPrevious')
        self._previous.setToolTip(self.tr('Previous cell in the list'))
        self._position = QLabel(self)
        self._position.setObjectName('inspectorCardPosition')
        self._next = QuickButton('▶', self)
        self._next.setObjectName('inspectorCardNext')
        self._next.setToolTip(self.tr('Next cell in the list'))
        self._worst = QuickButton(self.tr('Worst 100'), self)
        self._worst.setObjectName('inspectorCardWorst')
        self._worst.setToolTip(self.tr('Find the worst N cells'))
        walk.addWidget(self._previous)
        walk.addWidget(self._position)
        walk.addWidget(self._next)
        walk.addStretch(1)
        walk.addWidget(self._worst)
        column.addLayout(walk)

        pick = QHBoxLayout()
        pick.setSpacing(GAP)
        self._inspect = QuickButton(self.tr('Inspect element: click a cell'),
                                    self, checkable=True)
        self._inspect.setObjectName('inspectorCardInspect')
        self._save = QuickButton(self.tr('Save listed cells'), self)
        self._save.setObjectName('inspectorCardSave')
        pick.addWidget(self._inspect)
        pick.addWidget(self._save)
        column.addLayout(pick)

        self._readout = QLabel(self)
        self._readout.setObjectName('inspectorCardReadout')
        self._readout.setWordWrap(True)
        self._readout.setVisible(False)
        column.addWidget(self._readout)

        self._refreshing = False
        # card -> tab: each press is the tab's own control being used.
        self._limit.editingFinished.connect(
            lambda: tab.apply_limit_text(self._limit.text()))
        self._mesher.clicked.connect(lambda: tab.use_limit_source('mesher'))
        self._check.clicked.connect(lambda: tab.use_limit_source('check'))
        self._beyond.toggled.connect(self._on_beyond)
        self._previous.clicked.connect(lambda: tab.step_listed(-1))
        self._next.clicked.connect(lambda: tab.step_listed(1))
        self._worst.clicked.connect(tab.request_worst)
        self._inspect.toggled.connect(self._on_inspect)
        self._save.clicked.connect(tab.save_listed)
        # tab -> card: one signal covers every change the card shows.
        tab.inspectorChanged.connect(self.refresh)
        self.hide()
        self.refresh()

    # -- card -> tab ---------------------------------------------------------- #

    def _on_beyond(self, on: bool) -> None:
        if not self._refreshing:
            self._tab.toggle_beyond(on)

    def _on_inspect(self, on: bool) -> None:
        if not self._refreshing:
            self._tab.set_picking_element(on)

    # -- tab -> card ---------------------------------------------------------- #

    def refresh(self) -> None:
        """Show what the tab's inspector shows now."""
        state = self._tab.inspector_state()
        self._refreshing = True
        try:
            self._metric.setText(state['metric'])
            if not self._limit.hasFocus():
                self._limit.setText(state['limit'])
            self._mesher.setEnabled(state['mesher_enabled'])
            self._check.setEnabled(state['check_enabled'])
            self._beyond.setChecked(state['beyond'])
            self._walk_label.setText(state['beyond_text'])
            self._position.setText(state['position'])
            self._previous.setEnabled(state['listed'] > 0)
            self._next.setEnabled(state['listed'] > 0)
            self._worst.setText(state['worst_text'])
            self._inspect.setChecked(state['picking'])
            self._save.setEnabled(state['listed'] > 0)
            self._readout.setText(state['readout'])
            self._readout.setVisible(bool(state['readout']))
        finally:
            self._refreshing = False
        want = state['card_wanted']
        parent = self.parentWidget()
        if parent is not None and want != self.isVisibleTo(parent):
            self.setVisible(want)
        if want:
            self.adjustSize()
            self.place()
        self.update()

    # -- placement ------------------------------------------------------------ #

    def set_clear_rect_source(self, clear_of) -> None:
        """``clear_of()`` is the clear area as a QRect in the parent's
        coordinates (or None for the whole parent)."""
        self._clear_of = clear_of
        self.place()

    def clear_rect(self) -> QRect:
        parent = self.parentWidget()
        rect = self._clear_of() if self._clear_of is not None else None
        if rect is None or rect.isNull():
            rect = parent.rect() if parent is not None else QRect()
        return QRect(rect)

    def target_rect(self) -> QRect:
        """Where the card goes: clear-left + 16, top 56, kept inside the
        clear area."""
        clear = self.clear_rect()
        height = self.sizeHint().height() if not self.isVisible() \
            else self.height()
        height = max(height, self.minimumSizeHint().height())
        x = clear.left() + CARD_LEFT
        y = max(clear.top(), CARD_TOP)
        if y + height > clear.bottom() + 1:
            y = max(clear.top(), clear.bottom() + 1 - height)
        return QRect(QPoint(x, y), QSize(CARD_WIDTH, height))

    def place(self) -> None:
        if self.parentWidget() is None:
            return
        self.setGeometry(self.target_rect())
        self.raise_()

    def follow(self, host: QWidget) -> None:
        """Re-place when *host* is resized."""
        self._watcher = _HostWatcher(self)
        host.installEventFilter(self._watcher)

    # -- painting ------------------------------------------------------------- #

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(_token('border.legend'), 1))
        painter.setBrush(_token('overlay.legend'))
        painter.drawRoundedRect(
            QRect(0, 0, self.width() - 1, self.height() - 1),
            CARD_RADIUS, CARD_RADIUS)
        muted = _token('foreground.sub')
        ink = _token('foreground.legend')
        for label in (self._title, self._metric):
            _ink(label, muted)
        for label in (self._limit_label, self._walk_label, self._position,
                      self._readout):
            _ink(label, ink)


def _ink(label: QLabel, colour: QColor) -> None:
    palette = label.palette()
    if palette.windowText().color() != colour:
        palette.setColor(palette.ColorRole.WindowText, colour)
        label.setPalette(palette)


class _HostWatcher(QObject):
    """Keeps the card in the clear area while the view is resized."""

    def __init__(self, card: InspectorCard) -> None:
        super().__init__(card)
        self._card = card

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move) \
                and self._card.isVisible():
            self._card.place()
        return False
