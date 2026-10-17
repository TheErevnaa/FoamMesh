"""Plan 44 MF1: the one place a surface moves.

``slide`` animates ``geometry`` over 150-200 ms with an OutCubic curve. It is a
no-op that lands the widget on its target synchronously when

* a render hold is active (DP-347: the VTK window must not be resized in
  slices while a scene swap holds it),
* the Appearance says ``reduced`` (Settings > Reduce motion, decision Q4),
* Windows' "Show animations" is off, or
* a test (or ``FOAMMESH_MOTION=off``) has turned motion off.

Motion is off for the whole suite (tests/conftest.py); a test that wants it
calls ``set_enabled_override(True)``.
"""
from __future__ import annotations

import os
import sys
import weakref

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect

from . import appearance

_FALLBACK_MOTION_MS = 200
_FALLBACK_FAST_MS = 150
_override: bool | None = None
_running: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()


def _metric(name: str, default: int) -> int:
    from . import metrics
    return int(getattr(metrics, name, default))


def set_enabled_override(value: bool | None) -> None:
    """Force motion on or off (``None`` hands the decision back to the app)."""
    global _override
    _override = value


def _windows_animations_on() -> bool:
    if not sys.platform.startswith('win'):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        flag = wintypes.BOOL(1)
        # SPI_GETCLIENTAREAANIMATION
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(flag), 0)
        return bool(flag.value) if ok else True
    except Exception:  # noqa: BLE001 - an unreadable setting means "animate"
        return True


def _holding() -> bool:
    try:
        from foammesh.support.vtk_threads import isRenderingHold
        return bool(isRenderingHold())
    except Exception:  # noqa: BLE001
        return False


def motion_enabled() -> bool:
    """Whether a surface may animate right now."""
    if _override is not None:
        return _override and not _holding()
    if os.environ.get('FOAMMESH_MOTION', '').lower() in ('off', '0', 'reduced'):
        return False
    if appearance.current().motion != appearance.MOTION_ON:
        return False
    if _holding():
        return False
    return _windows_animations_on()


def _stop(widget) -> None:
    previous = _running.pop(widget, None)
    if previous is not None:
        try:
            previous.stop()
        except RuntimeError:
            pass


def slide(widget, to_rect: QRect, ms: int | None = None):
    """Move/resize ``widget`` to ``to_rect``; returns the animation or None.

    ``None`` means the geometry was set at once (motion off, widget hidden, or
    already there).
    """
    to_rect = QRect(to_rect)
    _stop(widget)
    if (not motion_enabled() or not widget.isVisible()
            or widget.geometry() == to_rect):
        widget.setGeometry(to_rect)
        return None
    duration = ms if ms is not None else _metric('MOTION', _FALLBACK_MOTION_MS)
    animation = QPropertyAnimation(widget, b'geometry', widget)
    animation.setDuration(max(1, int(duration)))
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.setStartValue(widget.geometry())
    animation.setEndValue(to_rect)

    def finished():
        if _running.get(widget) is animation:
            _running.pop(widget, None)
        animation.deleteLater()

    animation.finished.connect(finished)
    _running[widget] = animation
    animation.start()
    return animation


def fast_ms() -> int:
    return _metric('MOTION_FAST', _FALLBACK_FAST_MS)


def fade(top_level, to: float = 1.0, ms: int | None = None):
    """Fade a *top-level* window's opacity. Native children cannot be faded
    (windowOpacity is top-level only), so anything else is set at once."""
    _stop(top_level)
    if not top_level.isWindow() or not motion_enabled() or not top_level.isVisible():
        top_level.setWindowOpacity(to)
        return None
    animation = QPropertyAnimation(top_level, b'windowOpacity', top_level)
    animation.setDuration(max(1, int(ms if ms is not None else fast_ms())))
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.setStartValue(top_level.windowOpacity())
    animation.setEndValue(float(to))

    def finished():
        if _running.get(top_level) is animation:
            _running.pop(top_level, None)
        animation.deleteLater()

    animation.finished.connect(finished)
    _running[top_level] = animation
    animation.start()
    return animation
