"""Appearance choices: UI scale, density, font and the HiDPI rounding policy.

Plan 40 section 4.6 item 7 (UXE3, F7f). ``AppSettings.getScale`` used to have
no reader. The four choices live in the settings file and are read here, once,
before the ``QApplication`` exists:

* **Scale** (90 to 150 %) is Qt's own ``QT_SCALE_FACTOR``, so every pixel the
  application draws, text and icons included, grows together. It is read at
  start; changing it asks for a restart.
* **HiDPI policy** is ``QGuiApplication.setHighDpiScaleFactorRoundingPolicy``,
  also only possible before the application is built.
* **Density**, **font**, **motion** and **base size** apply live: the theme
  is rendered again (Plan 44 MF1 added the last two).

A value that is not one of the offered ones reads as the default; nothing here
raises on a hand-edited settings file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

SCALE_MIN = 0.9
SCALE_MAX = 1.5
SCALE_STEPS = (0.9, 1.0, 1.1, 1.25, 1.5)

DENSITY_COMFORTABLE = 'comfortable'
DENSITY_COMPACT = 'compact'
DENSITIES = (DENSITY_COMFORTABLE, DENSITY_COMPACT)

FONT_PRETENDARD = 'pretendard'
FONT_SYSTEM = 'system'
FONTS = (FONT_PRETENDARD, FONT_SYSTEM)

#: Qt's rounding policies by the name stored. ``pass_through`` is Qt 6's own
#: default, so an untouched settings file changes nothing.
HIDPI_POLICIES = ('pass_through', 'round', 'round_prefer_floor', 'ceil', 'floor')
HIDPI_DEFAULT = 'pass_through'

#: Plan 44 MF1 (decision Q4). Animation is on by default, 150-200 ms, and
#: follows the Windows "Show animations" setting; "reduced" turns it off.
MOTION_ON = 'on'
MOTION_REDUCED = 'reduced'
MOTIONS = (MOTION_ON, MOTION_REDUCED)

#: Plan 44 MF1 (decision Q3). The application font's base size in points.
#: 9.5 pt is about 12.7 px, the mockup's 12.5-13 px body text.
BASE_SIZES = (9.5, 10.0)
BASE_SIZE_DEFAULT = 9.5

#: Plan 44 MF19 (decision Q1, DP-189 restated as one control height per
#: density). Compact is the default: the mockup's 24 px controls. Comfortable
#: keeps the 28 px of DP-189.
CONTROL_HEIGHT_COMFORTABLE = 28
CONTROL_HEIGHT_COMPACT = 24
DENSITY_DEFAULT = DENSITY_COMPACT

#: Pixels compact takes off the height of every control, split evenly above
#: and below the text (so 2 px comes off each vertical padding). It is applied
#: once, to the comfortable sheet, and never again on top of itself.
COMPACT_TRIM_PX = CONTROL_HEIGHT_COMFORTABLE - CONTROL_HEIGHT_COMPACT


def control_height(density: str | None = None) -> int:
    """The one control height of ``density`` (the live choice when omitted)."""
    density = current().density if density is None else density
    return (CONTROL_HEIGHT_COMPACT if density == DENSITY_COMPACT
            else CONTROL_HEIGHT_COMFORTABLE)


def coerce_scale(value) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    if scale != scale:                                   # NaN
        return 1.0
    return min(SCALE_MAX, max(SCALE_MIN, scale))


def coerce_base_size(value) -> float:
    """One of :data:`BASE_SIZES`; anything else reads as the default."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return BASE_SIZE_DEFAULT
    return size if size in BASE_SIZES else BASE_SIZE_DEFAULT


def _choice(value, allowed, default):
    text = str(value).strip().lower() if value is not None else ''
    return text if text in allowed else default


@dataclass(frozen=True)
class Appearance:
    scale: float = 1.0
    density: str = DENSITY_DEFAULT
    font: str = FONT_PRETENDARD
    hidpi: str = HIDPI_DEFAULT
    motion: str = MOTION_ON
    base_size: float = BASE_SIZE_DEFAULT

    @classmethod
    def from_values(cls, scale=1.0, density=None, font=None, hidpi=None,
                    motion=None, base_size=None):
        return cls(coerce_scale(scale),
                   _choice(density, DENSITIES, DENSITY_DEFAULT),
                   _choice(font, FONTS, FONT_PRETENDARD),
                   _choice(hidpi, HIDPI_POLICIES, HIDPI_DEFAULT),
                   _choice(motion, MOTIONS, MOTION_ON),
                   coerce_base_size(base_size))

    @classmethod
    def from_settings(cls, settings):
        def read(name, default):
            reader = getattr(settings, name, None)
            return reader() if callable(reader) else default

        return cls.from_values(read('getScale', 1.0), read('getDensity', None),
                               read('getUiFont', None), read('getHiDpiPolicy', None),
                               read('getMotion', None), read('getBaseSize', None))


_current = Appearance()


def current() -> Appearance:
    return _current


def configure(appearance: Appearance) -> None:
    global _current
    _current = appearance


def set_live(density=None, font=None, motion=None, base_size=None) -> Appearance:
    """Change the choices that apply without a restart."""
    global _current
    _current = Appearance.from_values(
        _current.scale, density or _current.density, font or _current.font,
        _current.hidpi, motion or _current.motion,
        _current.base_size if base_size is None else base_size)
    return _current


def scale_percent(scale: float) -> int:
    return int(round(coerce_scale(scale) * 100))


def prepare_application(appearance: Appearance, environ=None) -> dict:
    """Apply the choices that must precede ``QApplication``.

    Returns what was done, for the log and the tests. A ``QT_SCALE_FACTOR`` the
    user set in the environment wins over the stored scale.
    """
    environ = os.environ if environ is None else environ
    configure(appearance)
    done = {'scale': None, 'hidpi': None}
    if 'QT_SCALE_FACTOR' not in environ and abs(appearance.scale - 1.0) > 1e-6:
        environ['QT_SCALE_FACTOR'] = f'{appearance.scale:g}'
        done['scale'] = appearance.scale
    if appearance.hidpi != HIDPI_DEFAULT:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        policy = {
            'round': Qt.HighDpiScaleFactorRoundingPolicy.Round,
            'round_prefer_floor':
                Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor,
            'ceil': Qt.HighDpiScaleFactorRoundingPolicy.Ceil,
            'floor': Qt.HighDpiScaleFactorRoundingPolicy.Floor,
        }[appearance.hidpi]
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(policy)
        done['hidpi'] = appearance.hidpi
    return done
