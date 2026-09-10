"""Keyed Region C output tabs.

Permanent Mesh/Console pages and dynamic reports share one registry so opening
the same output twice focuses its existing tab instead of duplicating it.
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QTabBar, QTabWidget, QWidget


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
        builds a fresh page each time it is invoked. ``Mesh Info`` and the
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
                page.deleteLater()
                page = fresh
        if page is None:
            if factory is None:
                return None
            page = factory()
            page.setProperty('outputTabKey', key)
            self._tabs.addTab(page, title or key)
            self._pages[key] = page
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

    def _on_close_requested(self, index: int) -> None:
        page = self._tabs.widget(index)
        key = str(page.property('outputTabKey') or '') if page else ''
        self.close_dynamic(key)

