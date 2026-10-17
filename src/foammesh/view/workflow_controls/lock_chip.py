"""Plan 44 MF18 (VS5, K1-K3, B10, B14). The lock chip and the read-only look.

The mockup (``final-layout.html`` ``.frow`` / ``.lockt`` / ``.page-body.ro``)
puts the completion lock in the filter row: one 24 px chip beside the filter
box. Locked it is green and reads "Unlock to edit"; pressed, it turns red,
the filter box gives its place to one line saying what the unlock discards,
and a small ✕ keeps it locked. A second press on the red chip is the unlock.

The page used to say the same thing with a paragraph that wrapped to three or
four lines ("Locked: this step is finished, ...") above a full-size button,
and it made the page read-only with ``setEnabled(False)`` on the whole body,
which greyed every label and value until the page was hard to read. A locked
page now keeps its text at full contrast: each editor carries
``foammeshReadOnly`` (a dashed border in ``forms.qss.tmpl``) and
:class:`ReadOnlyGuard` refuses the input and the keyboard focus instead.
"""
from __future__ import annotations

from PySide6.QtCore import (QByteArray, QCoreApplication, QEvent, QObject,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractSlider, QAbstractSpinBox, QApplication,
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QRadioButton, QScrollBar, QSizePolicy, QTabBar, QTextEdit,
    QWidget)

from foammesh.view.theming.metrics import GAP

#: The chip's height (mockup ``.lockt{height:24px}``).
CHIP_HEIGHT = 24

LOCKED = 'locked'
OPEN = 'open'
CONFIRM = 'confirm'

#: The style-sheet property each part of the chip is keyed on.
LOCK_PROPERTY = 'foammeshLock'
#: The property that gives a locked editor its dashed border.
READ_ONLY_PROPERTY = 'foammeshReadOnly'

_LOCK_CLOSED = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path fill="{c}" d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2h.5A1.5 1.5 0 0 1 13.5 '
    '8.5v5A1.5 1.5 0 0 1 12 15H4a1.5 1.5 0 0 1-1.5-1.5v-5A1.5 1.5 0 0 1 4 7z'
    'm1.5 0h4V5a2 2 0 0 0-4 0z"/></svg>')
_LOCK_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path fill="{c}" d="M10 7V5a2 2 0 0 0-4 0H4.5a3.5 3.5 0 0 1 7 0v2h.5A1.5 '
    '1.5 0 0 1 13.5 8.5v5A1.5 1.5 0 0 1 12 15H4a1.5 1.5 0 0 1-1.5-1.5v-5A1.5 '
    '1.5 0 0 1 4 7z"/></svg>')

#: (chip text token, icon) per state. The colours are the ``lock.*`` tokens.
_INK = {LOCKED: ('lock.ok.fg', '#bfe3cd', _LOCK_CLOSED),
        OPEN: ('lock.open.fg', '#c9ced6', _LOCK_OPEN),
        CONFIRM: ('accent.text', '#ffffff', _LOCK_OPEN)}


def _token(name: str, fallback: str) -> str:
    try:
        from foammesh.view.theming.elevation import token
        value = token(name, fallback) or fallback
    except Exception:                                        # noqa: BLE001
        value = fallback
    return str(value)[:7]


def svg_icon(template: str, colour: str, size: int = 12) -> QIcon:
    """*template* (an SVG with a ``{c}`` fill) drawn in *colour*."""
    from PySide6.QtSvg import QSvgRenderer

    screen = QApplication.primaryScreen() if QApplication.instance() else None
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    renderer = QSvgRenderer(QByteArray(template.format(c=colour).encode()))
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


def repolish(widget: QWidget) -> None:
    """Make the style sheet read a property that just changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class ElidedLabel(QLabel):
    """One line that ends in an ellipsis instead of wrapping or widening.

    Mockup ``.frow .ask{white-space:nowrap;text-overflow:ellipsis}``. The
    whole sentence is the tooltip; :meth:`fullText` returns it.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._full = ''
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Fixed)
        self.setWordWrap(False)

    def setFullText(self, text: str, tooltip: str = '') -> None:
        self._full = str(text or '')
        self.setToolTip(str(tooltip or self._full))
        self.setAccessibleName(str(tooltip or self._full))
        self._elide()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def _elide(self) -> None:
        width = max(0, self.width())
        shown = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, width) if width else ''
        if shown != self.text():
            QLabel.setText(self, shown)


class LockChip(QWidget):
    """The completion lock as one chip in the filter row.

    Three states, as in the mockup's ``renderPage``:

    * ``locked`` -- green "Unlock to edit". Pressing it asks (``confirm``).
    * ``confirm`` -- a red "Unlock" with the ask line before it and a ✕
      after it. Pressing the red chip emits :attr:`unlockConfirmed`; the ✕
      or Escape goes back to ``locked``.
    * ``open`` -- grey "Lock", for a host that can lock again
      (:attr:`lockRequested`).

    ``chip`` is the one push button the three states share, so the object
    name a test or the facade presses (``engineTaskUnlock`` on a task page)
    stays on it.
    """

    unlockConfirmed = Signal()
    lockRequested = Signal()
    #: True when the ask line is up (the host hides its filter box).
    confirmingChanged = Signal(bool)

    def __init__(self, parent=None, *, chip_name: str = 'lockChip',
                 ask_provider=None) -> None:
        super().__init__(parent)
        self._state = LOCKED
        #: ``() -> (short sentence, whole sentence)`` read when the chip asks.
        self._ask_provider = ask_provider
        self.setFixedHeight(CHIP_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP)
        self.ask = ElidedLabel(self)
        self.ask.setObjectName(chip_name + 'Ask')
        self.ask.setProperty(LOCK_PROPERTY, 'ask')
        self.ask.setFixedHeight(CHIP_HEIGHT)
        self.ask.setVisible(False)
        self.chip = QPushButton(self)
        self.chip.setObjectName(chip_name)
        self.chip.setFixedHeight(CHIP_HEIGHT)
        self.chip.setIconSize(QSize(12, 12))
        self.chip.setSizePolicy(QSizePolicy.Policy.Fixed,
                                QSizePolicy.Policy.Fixed)
        self.chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chip.clicked.connect(self._on_chip)
        self.cancel = QPushButton('✕', self)
        self.cancel.setObjectName(chip_name + 'Cancel')
        self.cancel.setProperty(LOCK_PROPERTY, 'cancel')
        self.cancel.setFixedHeight(CHIP_HEIGHT)
        self.cancel.setSizePolicy(QSizePolicy.Policy.Fixed,
                                  QSizePolicy.Policy.Fixed)
        self.cancel.setToolTip(self.tr('Keep it locked'))
        self.cancel.setAccessibleName(self.tr('Keep it locked'))
        self.cancel.setVisible(False)
        self.cancel.clicked.connect(self.cancelConfirm)
        row.addWidget(self.ask, 1)
        row.addWidget(self.chip, 0)
        row.addWidget(self.cancel, 0)
        self._descriptions = {LOCKED: '', OPEN: ''}
        self._paint_state()

    # -- state ------------------------------------------------------------ #

    def state(self) -> str:
        return self._state

    def setState(self, state: str) -> None:
        if state not in (LOCKED, OPEN, CONFIRM):
            raise ValueError(f'unknown lock chip state: {state}')
        was_confirming = self._state == CONFIRM
        self._state = state
        self._paint_state()
        if was_confirming != (state == CONFIRM):
            self.confirmingChanged.emit(state == CONFIRM)

    def isConfirming(self) -> bool:
        return self._state == CONFIRM

    def setDescription(self, state: str, text: str) -> None:
        """The tooltip and accessible description the chip carries in *state*."""
        self._descriptions[state] = str(text or '')
        if self._state == state:
            self._paint_state()

    def setAskProvider(self, provider) -> None:
        self._ask_provider = provider

    def askChip(self) -> None:
        """Turn red and say what the unlock would discard."""
        short, whole = self.tr('Deletes the mesh and the quality results'), ''
        if callable(self._ask_provider):
            try:
                answer = self._ask_provider()
            except Exception:                                # noqa: BLE001
                answer = None
            if answer:
                short, whole = answer
        short = str(short or '').rstrip('.?')
        self.ask.setFullText(short + '?', whole or short + '?')
        self.setState(CONFIRM)
        self.chip.setFocus(Qt.FocusReason.OtherFocusReason)

    def cancelConfirm(self) -> None:
        if self._state == CONFIRM:
            self.setState(LOCKED)
            self.chip.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_chip(self) -> None:
        if self._state == LOCKED:
            self.askChip()
        elif self._state == CONFIRM:
            self.setState(LOCKED)
            self.unlockConfirmed.emit()
        else:
            self.lockRequested.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._state == CONFIRM:
            self.cancelConfirm()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event) -> bool:
        if event.type() in (QEvent.Type.StyleChange,
                            QEvent.Type.PaletteChange):
            self._paint_icon()
        return super().event(event)

    def _paint_state(self) -> None:
        state = self._state
        texts = {LOCKED: self.tr('Unlock to edit'), OPEN: self.tr('Lock'),
                 CONFIRM: self.tr('Unlock')}
        self.chip.setText(texts[state])
        self.chip.setProperty(LOCK_PROPERTY, state)
        tips = {
            LOCKED: self._descriptions.get(LOCKED) or self.tr(
                'Locked: finished, view only. Unlock to edit.'),
            OPEN: self._descriptions.get(OPEN) or self.tr(
                'Editing. Lock it again when you are done.'),
            CONFIRM: self.tr('Unlock now. Press ✕ or Escape to keep it '
                             'locked.'),
        }
        self.chip.setToolTip(tips[state])
        self.chip.setAccessibleDescription(tips[state])
        self.ask.setVisible(state == CONFIRM)
        self.cancel.setVisible(state == CONFIRM)
        repolish(self.chip)
        self._paint_icon()

    def _paint_icon(self) -> None:
        name, fallback, template = _INK[self._state]
        self.chip.setIcon(svg_icon(template, _token(name, fallback)))


def unlock_ask_text(preview: dict, task_id: str = '') -> tuple:
    """``(short, whole)`` for the ask line, from ``mesh.workflow.unlock_preview``.

    The short line names what goes and the later steps that reopen (mockup
    ``.ask``: "Deletes the mesh" / "Reopens 6. Snap, 7. Layers"); the whole
    sentence is its tooltip.
    """
    preview = dict(preview or {})
    titles = [str(t) for t in (preview.get('titles') or preview.get('scope')
                               or ()) if t]
    name = titles[0] if titles else str(task_id or '')
    later = titles[1:]
    meshed = bool(preview.get('artifacts'))
    listed = ', '.join(later)
    tr = lambda text: QCoreApplication.translate('LockChip', text)  # noqa: E731
    if preview.get('resets_values'):
        short = tr('Deletes the mesh, restarts all later steps') if meshed \
            else tr('Restarts all later steps')
        whole = tr('Unlock "{0}"? Every later step starts over, as for new '
                   'geometry.').format(name)
        if meshed:
            whole += ' ' + tr('The mesh and the quality results are deleted.')
        return short, whole
    if meshed:
        short = tr('Deletes the mesh; reopens {0}').format(listed) if later \
            else tr('Deletes the mesh and the quality results')
        whole = tr('Unlock "{0}"? The mesh and the quality results are '
                   'deleted.').format(name)
    elif later:
        short = tr('Reopens {0}').format(listed)
        whole = tr('Unlock "{0}"?').format(name)
    elif preview:
        short = tr('Reopens this step')
        whole = tr('Unlock "{0}"?').format(name)
    else:
        short = tr('Deletes the mesh and the quality results')
        whole = tr('Unlock this step? The mesh and the quality results are '
                   'deleted; later steps keep their values but go back to '
                   'not done.')
        return short, whole
    if later:
        whole += ' ' + tr('{0} keep their values but must be confirmed '
                          'again.').format(listed)
    return short, whole


class FilterRow(QWidget):
    """The filter box and the lock chip on one row (mockup ``.frow``, gap 6).

    The row is on screen while either of the two is, so a page with no
    settings and no lock spends no line on it.
    """

    def __init__(self, filter_box: QWidget, chip: QWidget, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP)
        self._parts = (filter_box, chip)
        filter_box.setParent(self)
        chip.setParent(self)
        row.addWidget(filter_box, 1)
        row.addWidget(chip, 0)
        for part in self._parts:
            part.installEventFilter(self)
        self._follow()

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.ShowToParent,
                            QEvent.Type.HideToParent, QEvent.Type.Show,
                            QEvent.Type.Hide):
            self._follow()
        return False

    def _follow(self) -> None:
        wanted = any(part.isVisibleTo(self) for part in self._parts)
        parent = self.parentWidget()
        shown = self.isVisibleTo(parent) if parent is not None else (
            not self.isHidden())
        if wanted != shown:
            self.setVisible(wanted)


# --------------------------------------------------------------------------- #
# the read-only look: input refused, focus refused, text at full contrast
# --------------------------------------------------------------------------- #

#: Widgets that hold a value and get the dashed border.
_EDITOR_TYPES = (QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit,
                 QTextEdit, QCheckBox, QRadioButton)

_INPUT_EVENTS = frozenset({
    QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick, QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease, QEvent.Type.ContextMenu, QEvent.Type.Drop,
    QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.InputMethod,
    QEvent.Type.TabletPress, QEvent.Type.TouchBegin})

_FOCUS_KEY = '_foammeshLockedFocusPolicy'


def is_editor(widget: QWidget) -> bool:
    """Whether *widget* holds a value the read-only look dashes."""
    if isinstance(widget, _EDITOR_TYPES):
        return True
    return widget.property('foammeshRole') == 'segmentGroup'


def _changes_value_on_wheel(widget: QWidget, stop: QWidget) -> bool:
    node = widget
    while node is not None and node is not stop:
        if isinstance(node, (QAbstractSpinBox, QComboBox, QTabBar)):
            return True
        if isinstance(node, QAbstractSlider) and not isinstance(
                node, QScrollBar):
            return True
        node = node.parentWidget()
    return False


class ReadOnlyGuard(QObject):
    """Hold a widget tree read-only without disabling it.

    Mockup ``.page-body.ro``: ``pointer-events:none`` and dashed editors whose
    text stays readable. ``setEnabled(False)`` would grey every label and
    value (B10, DP-148), so the tree stays enabled and this filter refuses
    what would change it: presses, keys, drops, a context menu, and a wheel
    over a spin box or a combo (that wheel scrolls the page instead). Keyboard
    focus never lands inside: every widget's focus policy is ``NoFocus``
    while locked, and focus given to one some other way (a programmatic
    ``setFocus``) is handed to ``focus_target``.

    Widgets added to the tree while it is locked are taken in as they arrive.
    ``exempt(widget)`` names the ones that stay live (a disclosure header, the
    Details link): they only show or hide what is already there.
    """

    def __init__(self, root: QWidget, *, exempt=None, focus_target=None,
                 wheel_target=None) -> None:
        super().__init__(root)
        self._root = root
        self._exempt = exempt or (lambda _widget: False)
        self._focus_target = focus_target or (lambda: None)
        self._wheel_target = wheel_target or (lambda: None)
        self._locked = False
        self._held: list = []

    def isLocked(self) -> bool:
        return self._locked

    def setLocked(self, locked: bool) -> None:
        locked = bool(locked)
        if locked == self._locked:
            if locked:
                self._take_in(self._root)
            return
        self._locked = locked
        if locked:
            self._take_in(self._root)
        else:
            self._release()

    def heldWidgets(self) -> list:
        return [widget for widget in self._held if _alive(widget)]

    # -- taking widgets in and letting them go ---------------------------- #

    def _exempt_tree(self, widget: QWidget) -> bool:
        node = widget
        while node is not None and node is not self._root:
            if self._exempt(node):
                return True
            node = node.parentWidget()
        return False

    def _take_in(self, top: QWidget) -> None:
        widgets = [top] + top.findChildren(QWidget)
        for widget in widgets:
            if widget.property(_FOCUS_KEY) is not None:
                continue
            if widget is not self._root and self._exempt_tree(widget):
                continue
            widget.setProperty(_FOCUS_KEY, int(widget.focusPolicy().value))
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.installEventFilter(self)
            if widget.hasFocus():
                self._hand_focus_on()
            if is_editor(widget):
                widget.setProperty(READ_ONLY_PROPERTY, True)
                repolish(widget)
            self._held.append(widget)

    def _release(self) -> None:
        held, self._held = self._held, []
        for widget in held:
            if not _alive(widget):
                continue
            widget.removeEventFilter(self)
            policy = widget.property(_FOCUS_KEY)
            widget.setProperty(_FOCUS_KEY, None)
            if policy is not None:
                widget.setFocusPolicy(Qt.FocusPolicy(int(policy)))
            if widget.property(READ_ONLY_PROPERTY):
                widget.setProperty(READ_ONLY_PROPERTY, None)
                repolish(widget)

    def _late(self, child) -> None:
        if self._locked and _alive(child) and _alive(self._root) and (
                child is self._root or self._root.isAncestorOf(child)):
            self._take_in(child)

    def _hand_focus_on(self) -> None:
        target = self._focus_target()
        if target is not None and target.isVisible():
            target.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            focused = QApplication.focusWidget()
            if focused is not None:
                focused.clearFocus()

    # -- the filter ------------------------------------------------------- #

    def eventFilter(self, watched, event) -> bool:
        if not self._locked:
            return False
        kind = event.type()
        if kind in (QEvent.Type.ChildAdded, QEvent.Type.ChildPolished):
            child = event.child()
            if isinstance(child, QWidget):
                if kind == QEvent.Type.ChildPolished:
                    self._take_in(child)
                else:
                    # A child announces itself from inside its constructor,
                    # before its own type is complete: take it in once the
                    # constructor has returned.
                    QTimer.singleShot(0, lambda c=child: self._late(c))
            return False
        if kind == QEvent.Type.FocusIn:
            self._hand_focus_on()
            return True
        if kind == QEvent.Type.Wheel:
            if _changes_value_on_wheel(watched, self._root):
                target = self._wheel_target()
                if target is not None:
                    QApplication.sendEvent(target, event)
                return True
            return False
        if kind in _INPUT_EVENTS:
            return True
        return False


def _alive(widget) -> bool:
    try:
        widget.objectName()
    except RuntimeError:
        return False
    return True


__all__ = ['CHIP_HEIGHT', 'CONFIRM', 'ElidedLabel', 'FilterRow', 'LOCKED',
           'LockChip', 'OPEN', 'READ_ONLY_PROPERTY', 'ReadOnlyGuard',
           'is_editor', 'repolish', 'svg_icon', 'unlock_ask_text']
