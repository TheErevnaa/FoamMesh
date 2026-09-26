"""Load FoamMesh's bundled UI fonts exactly once per process."""
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

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
    font = QFont()
    if hasattr(font, 'setFamilies'):
        font.setFamilies(list(families))
    else:
        font.setFamily(families[0])
    font.setPointSize(10)
    application.setFont(font)
    return families
