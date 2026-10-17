"""Keyed Region C output tabs.

Permanent Mesh/Console pages and dynamic reports share one registry so opening
the same output twice focuses its existing tab instead of duplicating it.
"""
from __future__ import annotations

import weakref
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QTabBar, QTabWidget, QToolButton,
    QWidget)

from foammesh.view.theming.metrics import FORM_MARGIN, GAP, GAP_TIGHT

#: DP-110. U+00D7, spelled rather than pasted -- this tree has already had a
#: U+2192 turned into three cp1252 characters by something in its toolchain,
#: and a close button is not worth the risk.
#:
#: MEASURED on the Mesh check tab: the close button the style draws once a
#: stylesheet has taken over `QTabBar::tab` is a light-theme pixmap, and on
#: this dark tab bar it rendered as a pale filled square with a pale cross
#: inside it -- indistinguishable from the box a font substitution failure
#: leaves behind. It drew that on the one tab whose whole job is to say
#: whether something is wrong.
_CLOSE_GLYPH = chr(0x00D7)


class OutputTabRegistry:
    def __init__(self, tabs: QTabWidget):
        self._tabs = tabs
        self._pages: dict[str, QWidget] = {}
        #: Keys registered through :meth:`register_permanent`. Recorded rather
        #: than hard-coded: the old ``{'mesh', 'console'}`` literal meant every
        #: later permanent tab was programmatically closable despite having no
        #: close button, so the set silently went stale as tabs were added.
        self._permanent: set[str] = set()
        self._tabs.setTabsClosable(True)
        # Plan 44 MF6: the tab bar is the left of the console's one header row,
        # so it draws pills on the header's surface, not a bordered strip.
        self._tabs.setDocumentMode(True)
        self._tabs.tabBar().setDrawBase(False)
        self._tabs.tabCloseRequested.connect(self._on_close_requested)

    @property
    def tabs(self) -> QTabWidget:
        return self._tabs

    def register_permanent(
            self, key: str, page: QWidget, title: str) -> QWidget:
        if key in self._pages:
            return self._pages[key]
        index = self._tabs.indexOf(page)
        if index < 0:
            index = self._tabs.addTab(page, title)
        else:
            self._tabs.setTabText(index, title)
        page.setProperty('outputTabKey', key)
        self._pages[key] = page
        self._permanent.add(key)
        bar = self._tabs.tabBar()
        bar.setTabButton(index, QTabBar.ButtonPosition.LeftSide, None)
        bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, None)
        return page

    def show(
            self, key: str, title: str | None = None,
            factory: Callable[[], QWidget] | None = None,
            *, replace: bool = False) -> QWidget | None:
        """Focus ``key``, creating -- or with ``replace``, rebuilding -- its page.

        ``replace`` exists because the default is a trap for every caller that
        builds a fresh page each time it is invoked. ``Mesh info`` and the
        checkMesh dashboard both do: they run the operation, construct a dialog
        from the result, and hand it over. Without ``replace`` the second
        invocation found the key already present, returned the *first* page and
        dropped the new one on the floor -- so re-running the command silently
        re-displayed stale numbers, and when its tab was already in front it
        looked like the menu item did nothing at all.

        A permanent page is never replaced. Those are owned by the window and
        live for the session; swapping one out would delete a widget the window
        still holds a reference to.
        """
        page = self._pages.get(key)
        if page is not None and replace and key not in self._permanent:
            index = self._tabs.indexOf(page)
            if index >= 0 and factory is not None:
                # Read the existing label *before* the tab goes, or the
                # fallback resolves against a widget that is no longer in the
                # bar and silently relabels the tab with its key.
                label = title or self._tabs.tabText(index)
                fresh = factory()
                fresh.setProperty('outputTabKey', key)
                self._tabs.removeTab(index)
                self._tabs.insertTab(index, fresh, label)
                self._pages[key] = fresh
                self._attach_close_button(index, key)
                page.deleteLater()
                page = fresh
        if page is None:
            if factory is None:
                return None
            page = factory()
            page.setProperty('outputTabKey', key)
            index = self._tabs.addTab(page, title or key)
            self._pages[key] = page
            self._attach_close_button(index, key)
        self._tabs.setCurrentWidget(page)
        return page

    def close_dynamic(self, key: str) -> bool:
        if key in self._permanent:
            return False
        page = self._pages.pop(key, None)
        if page is None:
            return False
        index = self._tabs.indexOf(page)
        if index >= 0:
            self._tabs.removeTab(index)
        page.deleteLater()
        return True

    def key_for_current(self) -> str:
        page = self._tabs.currentWidget()
        return str(page.property('outputTabKey') or '') if page else ''

    def contains(self, key: str) -> bool:
        return key in self._pages

    def page(self, key: str) -> QWidget | None:
        return self._pages.get(key)

    def _attach_close_button(self, index: int, key: str) -> None:
        """Put this registry's own close button on a dynamic tab (DP-110).

        The style's pixmap is not usable here -- see `_CLOSE_GLYPH`. A button
        of our own also lets the tab say what closing it does, which the
        style's nameless icon never did.
        """
        bar = self._tabs.tabBar()
        button = QToolButton(bar)
        button.setObjectName('outputTabClose')
        button.setText(_CLOSE_GLYPH)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Not translated through `tr`: this registry is a plain object, not
        # a QObject, and the two tabs it names are themselves untranslated.
        label = 'Close this report'
        button.setToolTip(label)
        button.setAccessibleName(label)
        # Weak, deliberately. The button belongs to the tab bar and Qt
        # deletes it *asynchronously* on `removeTab`; a lambda holding
        # `self` therefore keeps the whole registry -- and through it the
        # tab widget and every page -- alive until that deferred delete
        # runs, so the tree is torn down from inside the delete queue
        # while the queue is being walked. MEASURED: that aborts the
        # process with no message at all.
        registry = weakref.ref(self)

        def close(*_args, name: str = key) -> None:
            owner = registry()
            if owner is not None:
                owner.close_dynamic(name)

        button.clicked.connect(close)
        bar.setTabButton(index, QTabBar.ButtonPosition.LeftSide, None)
        bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, button)

    def _on_close_requested(self, index: int) -> None:
        page = self._tabs.widget(index)
        key = str(page.property('outputTabKey') or '') if page else ''
        self.close_dynamic(key)


# --------------------------------------------------------------------------- #
# Plan 44 MF6: the console's one 28 px header row.
# --------------------------------------------------------------------------- #

#: The header row, and the folded chip, are this tall (px).
CONSOLE_HEADER_HEIGHT = 28
#: ...of which the card's own 1 px top edge is one, so the row inside is this.
CONSOLE_HEADER_ROW = CONSOLE_HEADER_HEIGHT - 1
_EM_DASH = chr(0x2014)
_UP_TRIANGLE = chr(0x25B4)


def _rule(parent: QWidget, name: str) -> QFrame:
    """A 1 x 16 vertical rule between header groups."""
    line = QFrame(parent)
    line.setObjectName(name)
    line.setFixedSize(1, 16)
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


class _HeaderBackdrop(QFrame):
    """The header's surface, rule and rounded top corners, behind the tab bar
    and the right-hand tools. A plain frame the style sheet paints; it takes no
    mouse input, so the bar and the tools above it keep theirs."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName('consoleHeader')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFrameShape(QFrame.Shape.NoFrame)


class _HeaderTools(QWidget):
    """The right-hand part of the header row: filters, verdict, size, minimise.

    It is the tab widget's corner widget, and the tab widget sizes a corner
    from its ``sizeHint`` -- so the hint is "whatever the tabs leave", which
    makes the row one row however many tabs are open.
    """

    def __init__(self, tabs: QTabWidget) -> None:
        super().__init__(tabs)
        self._tabs = tabs
        self.setObjectName('consoleHeaderTools')
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        floor = self.minimumSizeHint().width()
        bar = self._tabs.tabBar()
        spare = self._tabs.width() - bar.sizeHint().width()
        return QSize(max(floor, spare), CONSOLE_HEADER_ROW)

    def minimumSizeHint(self) -> QSize:
        base = super().minimumSizeHint()
        return QSize(base.width(), CONSOLE_HEADER_ROW)


class ConsoleHeader(QObject):
    """Merges the verdict strip, the tab bar and the filter row into one row.

    Open, the row is the tab widget's own: tabs on the left, then a rule and the
    current page's filter chips, a gap, the verdict, a rule, the size read-out
    and the minimise chip. Folded, the tab widget is hidden and the same verdict
    strip sits in a one-line chip beside "Console", so the verdict is on screen
    in both states and it is the same widget (its facade ids and signals do not
    move).
    """

    #: The minimise chip was pressed / the folded chip was pressed.
    minimiseRequested = Signal()
    restoreRequested = Signal()

    def __init__(self, card: QWidget, tabs: QTabWidget,
                 verdict: QWidget) -> None:
        super().__init__(card)
        self._card = card
        self._tabs = tabs
        self._verdict = verdict
        self._filters_owner: QWidget | None = None
        self._folded = False

        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        tabs.tabBar().setExpanding(False)
        tabs.tabBar().setObjectName('consoleHeaderTabs')

        self.backdrop = _HeaderBackdrop(tabs)
        self.backdrop.setGeometry(0, 0, tabs.width(), CONSOLE_HEADER_ROW)
        self.backdrop.lower()

        self.tools = _HeaderTools(tabs)
        row = QHBoxLayout(self.tools)
        row.setContentsMargins(0, 0, FORM_MARGIN, 0)
        row.setSpacing(GAP)
        self._rule_filters = _rule(self.tools, 'consoleHeaderRule')
        self.filters = QWidget(self.tools)
        self.filters.setObjectName('consoleHeaderFilters')
        self._filters_row = QHBoxLayout(self.filters)
        self._filters_row.setContentsMargins(0, 0, 0, 0)
        self._filters_row.setSpacing(GAP_TIGHT)
        self._rule_verdict = _rule(self.tools, 'consoleHeaderRule')
        self.size_readout = QLabel('', self.tools)
        self.size_readout.setObjectName('consoleSizeReadout')
        self.size_readout.setAccessibleName('Console size')
        self.minimise_button = QToolButton(self.tools)
        self.minimise_button.setObjectName('consoleMinimiseButton')
        self.minimise_button.setText(_EM_DASH)
        self.minimise_button.setAutoRaise(True)
        self.minimise_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.minimise_button.setToolTip('Minimise the console (Ctrl+`)')
        self.minimise_button.setAccessibleName('Minimise the console')
        self.minimise_button.clicked.connect(self.minimiseRequested.emit)
        row.addWidget(self._rule_filters)
        row.addWidget(self.filters)
        row.addStretch(1)
        self._verdict_index = row.count()
        row.addWidget(verdict, 0)
        row.addWidget(self._rule_verdict)
        row.addWidget(self.size_readout)
        row.addWidget(self.minimise_button)
        self._tools_row = row
        tabs.setCornerWidget(self.tools, Qt.Corner.TopRightCorner)

        # The folded chip: "up-triangle Console" and the verdict.
        self.chip = QFrame(card)
        self.chip.setObjectName('consoleHeaderChip')
        self.chip.setFixedHeight(CONSOLE_HEADER_ROW)
        chip_row = QHBoxLayout(self.chip)
        chip_row.setContentsMargins(FORM_MARGIN, 0, FORM_MARGIN, 0)
        chip_row.setSpacing(GAP)
        self.restore_button = QToolButton(self.chip)
        self.restore_button.setObjectName('consoleRestoreButton')
        self.restore_button.setText(_UP_TRIANGLE + ' Console')
        self.restore_button.setAutoRaise(True)
        self.restore_button.setToolTip('Show the console (Ctrl+J)')
        self.restore_button.setAccessibleName('Show the console')
        self.restore_button.clicked.connect(self.restoreRequested.emit)
        chip_row.addWidget(self.restore_button)
        chip_row.addStretch(1)
        self._chip_row = chip_row
        self.chip.hide()

        tabs.installEventFilter(self)
        card.installEventFilter(self)
        tabs.currentChanged.connect(lambda _index: self.sync_filters())
        self._update_readout()

    # -- state ------------------------------------------------------------- #

    @property
    def card(self) -> QWidget:
        return self._card

    def set_folded(self, folded: bool) -> None:
        """Folded: the chip carries the verdict. Open: the header row does."""
        folded = bool(folded)
        self._folded = folded
        if folded:
            if self._verdict.parent() is not self.chip:
                self._tools_row.removeWidget(self._verdict)
                self._verdict.setParent(self.chip)
                self._chip_row.insertWidget(1, self._verdict)
        elif self._verdict.parent() is not self.tools:
            self._chip_row.removeWidget(self._verdict)
            self._verdict.setParent(self.tools)
            self._tools_row.insertWidget(self._verdict_index, self._verdict)
        self._verdict.show()
        self.chip.setVisible(folded)

    @property
    def folded(self) -> bool:
        return self._folded

    # -- the page's filter chips -------------------------------------------- #

    def sync_filters(self) -> None:
        """Show the current page's header controls, if it offers any.

        A page offers them with ``headerControls()``, which hands over a widget
        built for the header (the Console's level filter, Fold stages and
        Find). Other pages have none, and the filter group and its rule hide.
        """
        page = self._tabs.currentWidget()
        offered = getattr(page, 'headerControls', None)
        controls = offered() if callable(offered) else None
        if self._filters_owner is not None and (
                controls is not self._filters_owner):
            self._filters_owner.hide()
        self._filters_owner = controls
        if controls is not None:
            if controls.parent() is not self.filters:
                controls.setParent(self.filters)
                self._filters_row.addWidget(controls)
            controls.show()
        self.filters.setVisible(controls is not None)
        self._rule_filters.setVisible(controls is not None)

    # -- geometry ----------------------------------------------------------- #

    def _update_readout(self) -> None:
        self.size_readout.setText(
            '{0} {1} {2}'.format(self._card.width(), chr(0x00D7),
                                 self._card.height()))

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj is self._tabs and kind in (QEvent.Type.Resize,
                                          QEvent.Type.Show):
            self.backdrop.setGeometry(
                0, 0, self._tabs.width(), CONSOLE_HEADER_ROW)
            self.backdrop.lower()
            self._tabs.updateGeometry()
        elif obj is self._card and kind == QEvent.Type.Resize:
            self._update_readout()
        return False
