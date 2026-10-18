"""Load FoamMesh's bundled UI fonts exactly once per process."""
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

from . import appearance

_LOADED = False


def install_application_fonts(application) -> tuple[str, ...]:
    global _LOADED
    resources = Path(__file__).parents[3] / 'resources'
    if not _LOADED:
        # DP-131. Latch on success, not on having tried. `addApplicationFont`
        # answers -1 when it cannot read or parse the file, and the flag used
        # to be set regardless -- so one failure (a half-written install, a
        # file lock, a missing resource) disabled every later attempt for the
        # life of the process and the whole UI quietly fell back to whatever
        # `families` below resolves to next. That fallback is roughly 1.76x
        # wider than Pretendard at the same point size, which is enough to
        # wrap form rows and clip values: exactly the damage DP-130 fixed on
        # one page, arriving here on every page at once. Leaving the flag
        # down costs a retry per theme apply, which is a handful per session.
        handles = [
            QFontDatabase.addApplicationFont(str(resources / filename))
            for filename in ('PretendardVariable.ttf', 'Pretendard-Bold.ttf')
        ]
        _LOADED = all(handle != -1 for handle in handles)
    families = ('Pretendard Variable', 'Pretendard', 'Segoe UI', 'Arial', 'sans-serif')
    if appearance.current().font == appearance.FONT_SYSTEM:
        # The desktop's own face: what the platform would have used.
        families = (QFontDatabase.systemFont(
            QFontDatabase.SystemFont.GeneralFont).family() or 'Segoe UI',
            'Arial', 'sans-serif')
    font = QFont()
    if hasattr(font, 'setFamilies'):
        font.setFamilies(list(families))
    else:
        font.setFamily(families[0])
    font.setPointSizeF(appearance.current().base_size)
    application.setFont(font)
    return families


def warm_application_font() -> None:
    """Plan 45 start-up: pay the first use of the application font now.

    The first text measured after ``install_application_fonts`` costs ~0.3 s
    (measured live, 17 October 2026: Qt resolves the font's fallback faces
    once). Asked for before the window is built, it is a turn of its own
    instead of part of the window's longest one. Later uses are cached.
    """
    from PySide6.QtGui import QFontMetrics, QGuiApplication

    if QGuiApplication.instance() is None:
        return
    QFontMetrics(QGuiApplication.font()).horizontalAdvance('FoamMesh 0.5 mm')


# -- Plan 44 MF1: mono, caps and heading faces -----------------------------
# The fixed-pitch face is Cascadia Mono, which ships with Windows 11 (user
# decision 2026-10-14: no font files are bundled). Consolas, then the generic
# monospace family, cover older systems.

MONO_FAMILIES = ('Cascadia Mono', 'Consolas', 'monospace')


def _pixel_font(font: QFont, px) -> QFont:
    font.setPixelSize(max(1, int(round(px))))
    return font


def mono_font(px: float = 12) -> QFont:
    """The fixed-pitch face (digits of equal width) at ``px`` pixels."""
    font = QFont()
    if hasattr(font, 'setFamilies'):
        font.setFamilies(list(MONO_FAMILIES))
    else:
        font.setFamily(MONO_FAMILIES[0])
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    return _pixel_font(font, px)


def caps_font(px: float = 10.5) -> QFont:
    """Small-caps caption face: upper case with 108 % letter spacing."""
    from PySide6.QtGui import QGuiApplication

    font = QFont(QGuiApplication.font())
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
    font.setWeight(QFont.Weight.DemiBold)
    return _pixel_font(font, px)


def heading_font(level: int = 3) -> QFont:
    """Section heading face: level 2 is 16 px, level 3 is 14 px, demibold."""
    from PySide6.QtGui import QGuiApplication

    from . import metrics

    px = {2: getattr(metrics, 'TYPE_H2', 16), 3: getattr(metrics, 'TYPE_H3', 14)}.get(
        level, getattr(metrics, 'TYPE_BODY', 12.5))
    font = QFont(QGuiApplication.font())
    font.setWeight(QFont.Weight.DemiBold)
    return _pixel_font(font, px)
