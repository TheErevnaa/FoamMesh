"""Keyed Region C output tabs.

Permanent Mesh/Console pages and dynamic reports share one registry so opening
the same output twice focuses its existing tab instead of duplicating it.
"""
from __future__ import annotations

import weakref
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTabBar, QTabWidget, QToolButton, QWidget

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

