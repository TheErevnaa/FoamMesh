"""Plan 44 MF3 (Q5): the window's caption in the chrome colour.

The native title bar stays (the window is not frameless, so snap, the system
menu and the resize borders are the system's own). What is themed is its
colour, through ``DwmSetWindowAttribute``:

* attribute 20, ``DWMWA_USE_IMMERSIVE_DARK_MODE``: dark or light caption.
  Windows 10 (build 19041 and later) honours only this one, so there the
  caption follows light and dark and nothing more;
* attributes 34 and 35, ``DWMWA_BORDER_COLOR`` and ``DWMWA_CAPTION_COLOR``:
  the exact chrome and border colours, Windows 11 (build 22000) and later.

Everything here is a no-op off Windows and never raises: a caption that keeps
the system colour is a cosmetic loss, a crash on theme change is not.
"""
from __future__ import annotations

import sys

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35

#: The first Windows 11 build; 34 and 35 do nothing before it.
WINDOWS_11_BUILD = 22000


def colorref(value: str) -> int:
    """``#rrggbb`` as the ``COLORREF`` (``0x00BBGGRR``) DWM wants. An alpha
    byte (``#rrggbbaa``) is ignored."""
    text = str(value).strip().lstrip('#')
    if len(text) not in (6, 8):
        raise ValueError(f'not a colour: {value!r}')
    red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def windows_build() -> int:
    """The Windows build number, 0 off Windows."""
    if not sys.platform.startswith('win'):
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except (AttributeError, OSError, ValueError):
        return 0


def _set(hwnd: int, attribute: int, value: int) -> bool:
    import ctypes
    from ctypes import wintypes

    data = wintypes.DWORD(value)
    result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(hwnd), wintypes.DWORD(attribute),
        ctypes.byref(data), ctypes.sizeof(data))
    return result == 0


def apply_caption(window, *, dark: bool, caption: str | None = None,
                  border: str | None = None) -> bool:
    """Colour *window*'s caption. ``dark`` picks the light or dark caption
    (every supported Windows); *caption* and *border* (``#rrggbb``) are used
    on Windows 11 and later. True when the dark/light attribute was taken."""
    try:
        build = windows_build()
        if build <= 0:
            return False
        hwnd = int(window.winId())
        done = _set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
        if build >= WINDOWS_11_BUILD:
            if caption:
                _set(hwnd, DWMWA_CAPTION_COLOR, colorref(caption))
            if border:
                _set(hwnd, DWMWA_BORDER_COLOR, colorref(border))
        return done
    except Exception:                                    # noqa: BLE001
        return False


def apply_to_all(application, tokens, resolved: str) -> None:
    """Colour every top-level window of *application* from the theme."""
    try:
        caption = tokens.value('background.chrome')
        border = tokens.value('border.default')
    except Exception:                                    # noqa: BLE001
        return
    from PySide6.QtWidgets import QApplication
    for window in QApplication.topLevelWidgets():
        if window.isWindow() and window.isVisible():
            apply_caption(window, dark=(resolved == 'dark'),
                          caption=caption, border=border)
