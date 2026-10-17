"""Plan 40 UXD5 (F3e): the help pane at the foot of a settings page.

A collapsible strip that follows keyboard focus. When a field's editor has
focus it shows what the registry says about that field: the two-line
explanation, a small drawing for the geometric ones, the OpenFOAM or Gmsh
keyword and a link to the manual. The text is read from the field's
``FieldDescriptor``, the same record the tooltip is built from, so the two
cannot disagree.

Plan 44 MF17. Collapsed it is one chip, "▸ Help", and nothing else: the
footer row used to carry the field's words after it, elided against Proceed
until they read "Click a setting to see" (audit defect 10). The words now live
only in the opened pane, which floats *above* the footer and wraps every line,
so no help text is ever cut short and the footer row holds no text of its own.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from foammesh.view.theming.metrics import BAR_INSET_H, BAR_INSET_V, GAP_TIGHT

_EMPTY = 'Click a setting to see what it does.'

#: Plan 44 MF17 (M:467). The chip's two faces: folded and opened.
CHIP_CLOSED = '▸ Help'
CHIP_OPEN = '▾ Help'


def field_editor_of(widget):
    """The registry-backed editor ``widget`` sits in, or ``None``."""
    node = widget
    while node is not None:
        descriptor = getattr(node, 'descriptor', None)
        if descriptor is not None and hasattr(descriptor, 'explanation'):
            return node
        node = node.parentWidget() if hasattr(node, 'parentWidget') else None
    return None


class DiagramWidget(QWidget):
    """A small line drawing of the geometric idea behind a field."""

    KINDS = ('layers', 'angle', 'nonorth', 'skew', 'size', 'curvature',
             'snap', 'levels', 'box')

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName('helpPaneDiagram')
        self._kind = ''
        self.setFixedSize(QSize(132, 76))
        self.setAccessibleName('Picture of the setting')

    def kind(self) -> str:
        return self._kind

    def setKind(self, kind: str) -> None:
        kind = kind if kind in self.KINDS else ''
        self._kind = kind
        self.setVisible(bool(kind))
        self.update()

    def paintEvent(self, _event) -> None:
        if not self._kind:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = self.palette().color(self.foregroundRole())
        accent = self.palette().color(self.palette().ColorRole.Highlight)
        soft = QColor(ink)
        soft.setAlpha(110)
        w, h = float(self.width()), float(self.height())
        getattr(self, '_draw_' + self._kind)(painter, w, h, ink, accent, soft)
        painter.end()

    # Each drawing is a few strokes; none carries a number.
    def _draw_layers(self, p, w, h, ink, accent, soft):
        wall = h - 10
        p.setPen(QPen(ink, 2))
        p.drawLine(QPointF(8, wall), QPointF(w - 8, wall))
        y, step = wall, 6.0
        p.setPen(QPen(soft, 1))
        for index in range(5):
            top = y - step
            p.drawLine(QPointF(8, top), QPointF(w - 8, top))
            if index == 0:
                p.setPen(QPen(accent, 1.5))
                p.drawLine(QPointF(w - 14, y), QPointF(w - 14, top))
                p.setPen(QPen(soft, 1))
            y, step = top, step * 1.25

    def _draw_angle(self, p, w, h, ink, accent, soft):
        corner = QPointF(w * 0.35, h - 14)
        p.setPen(QPen(ink, 2))
        p.drawLine(corner, QPointF(w - 10, h - 14))
        end = QPointF(corner.x() + 52 * math.cos(math.radians(50)),
                      corner.y() - 52 * math.sin(math.radians(50)))
        p.drawLine(corner, end)
        p.setPen(QPen(accent, 1.5))
        p.drawArc(QRectF(corner.x() - 20, corner.y() - 20, 40, 40),
                  0, 50 * 16)

    def _draw_nonorth(self, p, w, h, ink, accent, soft):
        a, b = QPointF(w * 0.25, h * 0.75), QPointF(w * 0.78, h * 0.35)
        p.setPen(QPen(soft, 1))
        p.drawLine(QPointF(w * 0.5, 6), QPointF(w * 0.5, h - 6))
        p.setPen(QPen(ink, 2))
        p.drawLine(a, b)
        p.setBrush(ink)
        for centre in (a, b):
            p.drawEllipse(centre, 3, 3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(accent, 1.5))
        p.drawLine(QPointF(w * 0.5, h * 0.55), QPointF(w * 0.5 + 34, h * 0.55))
        p.drawArc(QRectF(w * 0.5 - 14, h * 0.55 - 14, 28, 28), 0, 35 * 16)

    def _draw_skew(self, p, w, h, ink, accent, soft):
        p.setPen(QPen(soft, 1))
        p.drawRect(QRectF(10, 10, w / 2 - 12, h - 20))
        p.drawRect(QRectF(w / 2 - 2, 10, w / 2 - 8, h - 20))
        p.setPen(QPen(ink, 2))
        p.drawLine(QPointF(w / 2 - 2, 10), QPointF(w / 2 - 2, h - 10))
        p.setPen(QPen(accent, 1.5))
        p.drawLine(QPointF(w * 0.25, h / 2), QPointF(w * 0.75, h / 2))
        p.drawLine(QPointF(w / 2 - 2, h / 2 - 4), QPointF(w / 2 - 2, h / 2 + 4))

    def _draw_size(self, p, w, h, ink, accent, soft):
        p.setPen(QPen(soft, 1))
        cell = 16.0
        x = 10.0
        while x + cell <= w - 10:
            y = 10.0
            while y + cell <= h - 10:
                p.drawRect(QRectF(x, y, cell, cell))
                y += cell
            x += cell
        p.setPen(QPen(accent, 2))
        p.drawLine(QPointF(10, h - 8), QPointF(10 + cell, h - 8))

    def _draw_curvature(self, p, w, h, ink, accent, soft):
        centre, radius = QPointF(w / 2, h / 2), min(w, h) / 2 - 8
        p.setPen(QPen(soft, 1))
        p.drawEllipse(centre, radius, radius)
        p.setPen(QPen(ink, 2))
        sides = 12
        points = [QPointF(centre.x() + radius * math.cos(2 * math.pi * i / sides),
                          centre.y() + radius * math.sin(2 * math.pi * i / sides))
                  for i in range(sides)]
        for index, point in enumerate(points):
            p.drawLine(point, points[(index + 1) % sides])

    def _draw_snap(self, p, w, h, ink, accent, soft):
        p.setPen(QPen(ink, 2))
        p.drawLine(QPointF(8, h * 0.7), QPointF(w - 8, h * 0.3))
        p.setPen(QPen(soft, 1))
        steps = 6
        for index in range(steps):
            x0 = 8 + (w - 16) * index / steps
            x1 = 8 + (w - 16) * (index + 1) / steps
            y = h * 0.7 - (h * 0.4) * (index + 0.5) / steps + (6 if index % 2 else -6)
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
        p.setPen(QPen(accent, 1.5))
        p.drawLine(QPointF(w / 2, h * 0.5 + 6), QPointF(w / 2, h * 0.5 - 2))

    def _draw_levels(self, p, w, h, ink, accent, soft):
        p.setPen(QPen(soft, 1))
        for inset, size in ((8, 56), (22, 28), (30, 14)):
            p.drawRect(QRectF(w / 2 - size, inset, size * 2, h - 2 * inset))
        p.setPen(QPen(accent, 1.5))
        p.drawRect(QRectF(w / 2 - 7, 30, 14, h - 60))

    def _draw_box(self, p, w, h, ink, accent, soft):
        p.setPen(QPen(ink, 2))
        p.drawEllipse(QRectF(w * 0.32, h * 0.3, w * 0.36, h * 0.4))
        p.setPen(QPen(soft, 1.5, Qt.PenStyle.DashLine))
        p.drawRect(QRectF(8, 8, w - 16, h - 16))
        p.setPen(QPen(accent, 1.5))
        p.drawLine(QPointF(w * 0.68, h / 2), QPointF(w - 8, h / 2))


class HelpPane(QFrame):
    """The strip: header with a fold button, then the body for one field."""

    def __init__(self, parent=None, *, overlay=None, anchor=None) -> None:
        """``overlay`` is the widget the opened body floats over, just above
        ``anchor``; without one the body is laid out under the header."""
        super().__init__(parent)
        self._overlay = overlay
        self._anchor = anchor
        self.setObjectName('helpPane')
        self.setAccessibleName('Help for the setting in focus')
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Maximum)
        self._descriptor = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._toggle = QToolButton(self)
        self._toggle.setObjectName('helpPaneToggle')
        self._toggle.setCheckable(True)
        # Plan 44 MF17. A chip drawn like the footer's other buttons (the
        # mockup's `.btn`), its disclosure triangle part of its words.
        self._toggle.setAutoRaise(False)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._toggle.setText(self.tr(CHIP_CLOSED))
        self._toggle.setAccessibleName(self.tr('Show or hide the help'))
        self._toggle.setToolTip(self.tr(
            'The help follows the setting you are editing. Show it to read '
            'the explanation, the keyword and the manual link.'))
        self._toggle.toggled.connect(self._set_open)
        # Plan 44 MF17. No words beside the chip: the footer's spacer follows.
        head.addWidget(self._toggle)
        outer.addLayout(head)

        self._body = QFrame(overlay if overlay is not None else self)
        self._body.setObjectName('helpPaneBody')
        self._body.setAutoFillBackground(True)
        self._body.setFrameShape(QFrame.Shape.StyledPanel)
        body = QHBoxLayout(self._body)
        body.setContentsMargins(BAR_INSET_H, BAR_INSET_V, BAR_INSET_H, BAR_INSET_V)
        text = QVBoxLayout()
        text.setSpacing(GAP_TIGHT)
        self._name = QLabel(self._body)
        self._name.setObjectName('helpPaneName')
        self._name.setWordWrap(True)
        font = self._name.font()
        font.setBold(True)
        self._name.setFont(font)
        self._explanation = QLabel(self.tr(_EMPTY), self._body)
        self._explanation.setObjectName('helpPaneExplanation')
        self._explanation.setWordWrap(True)
        self._keyword = QLabel(self._body)
        self._keyword.setObjectName('helpPaneKeyword')
        self._keyword.setWordWrap(True)
        self._keyword.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._link = QLabel(self._body)
        self._link.setObjectName('helpPaneLink')
        self._link.setTextFormat(Qt.TextFormat.RichText)
        self._link.linkActivated.connect(self._open_link)
        for label in (self._name, self._explanation, self._keyword,
                      self._link):
            text.addWidget(label)
        body.addLayout(text, 1)
        self._diagram = DiagramWidget(self._body)
        self._diagram.setVisible(False)
        body.addWidget(self._diagram, 0, Qt.AlignmentFlag.AlignTop)
        if overlay is None:
            outer.addWidget(self._body)
        else:
            overlay.installEventFilter(self)
        self._body.setVisible(False)
        self._show_descriptor(None)

    # ---- what it shows ------------------------------------------------
    def descriptor(self):
        return self._descriptor

    def is_open(self) -> bool:
        return self._toggle.isChecked()

    def setOpen(self, opened: bool) -> None:
        self._toggle.setChecked(bool(opened))

    def explanation_text(self) -> str:
        return self._explanation.text()

    def keyword_text(self) -> str:
        return self._keyword.text()

    def link_text(self) -> str:
        return self._link.text()

    def diagram(self) -> DiagramWidget:
        return self._diagram

    def show_field(self, descriptor) -> None:
        """Show ``descriptor``'s words. ``None`` shows the empty prompt."""
        self._show_descriptor(descriptor)

    def _show_descriptor(self, descriptor) -> None:
        self._descriptor = descriptor
        if descriptor is None:
            self._name.setText('')
            self._explanation.setText(self.tr(_EMPTY))
            self._keyword.setText('')
            self._link.setText('')
            self._diagram.setKind('')
            for label in (self._name, self._keyword, self._link):
                label.setVisible(False)
            return
        name = descriptor.title or descriptor.id.rsplit('.', 1)[-1]
        explanation = descriptor.explanation or self.tr(
            'No description has been written for this setting.')
        self._name.setText(name)
        self._explanation.setText(explanation)
        keyword = descriptor.keyword
        self._keyword.setText(self.tr('Keyword: %1').replace('%1', keyword)
                              if keyword else '')
        url = descriptor.manual_url
        self._link.setText(
            f'<a href="{url}">{self.tr("Manual")}</a>' if url else '')
        self._name.setVisible(True)
        self._keyword.setVisible(bool(keyword))
        self._link.setVisible(bool(url))
        self._diagram.setKind(descriptor.diagram)
        self._place()

    def help_labels(self) -> tuple:
        """The labels that carry help text; all of them sit in the pane."""
        return (self._name, self._explanation, self._keyword, self._link)

    # ---- folding and focus -------------------------------------------
    def _set_open(self, opened: bool) -> None:
        self._toggle.setText(self.tr(CHIP_OPEN if opened else CHIP_CLOSED))
        self._body.setVisible(bool(opened))
        self._place()

    def eventFilter(self, watched, event):
        if watched is self._overlay and event.type() in (
                QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._place()
        return super().eventFilter(watched, event)

    def _place(self) -> None:
        """Float the opened body over the bottom of the overlay host."""
        if self._overlay is None or not self._body.isVisible():
            return
        host = self._overlay
        width = host.width()
        self._body.setFixedWidth(width)
        # Every label wraps, so the height follows the width it is given and
        # no line of help is cut short (Plan 44 MF17).
        layout = self._body.layout()
        height = (layout.heightForWidth(width) if layout.hasHeightForWidth()
                  else -1)
        height = max(height, self._body.sizeHint().height())
        bottom = host.height()
        if self._anchor is not None and self._anchor.parentWidget() is not None:
            bottom = self._anchor.mapTo(host, self._anchor.rect().topLeft()).y()
        self._body.setGeometry(0, max(0, bottom - height), width, height)
        self._body.raise_()

    def follow(self, application=None) -> None:
        """Follow keyboard focus in ``application`` (the running one)."""
        application = application or QApplication.instance()
        if application is not None:
            application.focusChanged.connect(self._focus_changed)

    def _focus_changed(self, _old, new) -> None:
        if new is None:
            return
        editor = field_editor_of(new)
        if editor is None:
            return
        if not self.isAncestorOf(editor):
            self.show_field(editor.descriptor)

    def _open_link(self, url: str) -> None:
        if url:
            QDesktopServices.openUrl(QUrl(url))


def install_help_pane(ui, action_bar, overlay) -> HelpPane:
    """Put the Help chip at the left of the footer row; follow focus.

    The collapsed chip costs the page column no height; opened, the body
    floats over the bottom of the column, just above the footer, and goes
    away when it is closed. Plan 44 MF17: the chip takes only its own width,
    and the footer's spacer, not the pane, fills the row up to Back.
    """
    pane = HelpPane(action_bar, overlay=overlay, anchor=action_bar)
    pane.setSizePolicy(QSizePolicy.Policy.Maximum,
                       QSizePolicy.Policy.Maximum)
    action_bar.layout().insertWidget(0, pane, 0)
    pane.follow()
    ui.regionBHelpPane = pane
    return pane


__all__ = ['CHIP_CLOSED', 'CHIP_OPEN', 'HelpPane', 'DiagramWidget',
           'field_editor_of', 'install_help_pane']
