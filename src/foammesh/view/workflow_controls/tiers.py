"""Plan 40 UXD4: whether the Expert tier is on screen, and the tier chrome.

One switch for the whole application, *Show expert settings*, kept per user.
Q3 (recommendation, pending the user's decision): hidden for a new install,
shown for a user upgrading from 1.1.x, so nothing they used disappears. A
settings file that already holds other keys when this key is first read is an
upgrade; a file with none is a new install.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QCheckBox

#: The QSettings key. Written the first time the answer is needed, so the
#: decision made for an upgrade does not flip when other keys appear later.
SETTINGS_KEY = 'ui/showExpertSettings'

#: The dynamic property that marks a row a tier keeps off the form.
TIER_HIDDEN_PROPERTY = 'foammeshTierHidden'


def _settings():
    from PySide6.QtCore import QSettings
    return QSettings('FoamMesh', 'FoamMesh')


class ExpertVisibility(QObject):
    """The application-wide *Show expert settings* switch."""

    changed = Signal(bool)

    def __init__(self, settings_factory=None):
        super().__init__()
        self._factory = settings_factory or _settings
        self._shown: bool | None = None

    def _read(self) -> bool:
        try:
            store = self._factory()
            if store.contains(SETTINGS_KEY):
                value = store.value(SETTINGS_KEY)
                return str(value).strip().lower() in ('true', '1', 'yes')
            upgrading = len(store.allKeys()) > 0
            store.setValue(SETTINGS_KEY, bool(upgrading))
            return bool(upgrading)
        except Exception:                                    # noqa: BLE001
            return False

    def shown(self) -> bool:
        if self._shown is None:
            self._shown = self._read()
        return self._shown

    def setShown(self, shown: bool) -> None:
        shown = bool(shown)
        if self._shown is not None and shown == self._shown:
            return
        self._shown = shown
        try:
            self._factory().setValue(SETTINGS_KEY, shown)
        except Exception:                                    # noqa: BLE001
            pass
        self.changed.emit(shown)

    def useSettings(self, settings_factory) -> None:
        """Read from another store (a test's), forgetting the cached answer."""
        self._factory = settings_factory
        self._shown = None


_VISIBILITY: ExpertVisibility | None = None


def expert_visibility() -> ExpertVisibility:
    """The one switch, created on first use."""
    global _VISIBILITY
    if _VISIBILITY is None:
        _VISIBILITY = ExpertVisibility()
    return _VISIBILITY


def make_expert_toggle(parent=None) -> QCheckBox:
    """The *Show expert settings* checkbox, kept in step with the switch."""
    from PySide6.QtCore import QCoreApplication
    box = QCheckBox(
        QCoreApplication.translate('tiers', 'Show expert settings'), parent)
    box.setObjectName('showExpertSettings')
    box.setToolTip(QCoreApplication.translate(
        'tiers', 'Show the settings only a specialist changes. Hidden '
        'settings keep their values.'))
    visibility = expert_visibility()
    box.setChecked(visibility.shown())
    box.toggled.connect(visibility.setShown)

    def follow(shown):
        # The switch outlives every page; a page's box goes with its page.
        # The connection is a plain function, which Qt does not drop when
        # the box is destroyed, so the first change after that drops it.
        from shiboken6 import isValid
        if not isValid(box):
            visibility.changed.disconnect(follow)
            return
        if box.isChecked() != shown:
            blocked = box.blockSignals(True)
            box.setChecked(shown)
            box.blockSignals(blocked)
    visibility.changed.connect(follow)
    return box


__all__ = ['ExpertVisibility', 'SETTINGS_KEY', 'TIER_HIDDEN_PROPERTY',
           'expert_visibility', 'make_expert_toggle']
