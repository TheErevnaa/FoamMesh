"""Load FoamMesh's bundled UI fonts exactly once per process."""
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

_LOADED = False


def install_application_fonts(application) -> tuple[str, ...]:
    global _LOADED
    resources = Path(__file__).parents[3] / 'resources'
    if not _LOADED:
        for filename in ('PretendardVariable.ttf', 'Pretendard-Bold.ttf'):
            QFontDatabase.addApplicationFont(str(resources / filename))
        _LOADED = True
    families = ('Pretendard Variable', 'Pretendard', 'Segoe UI', 'Arial', 'sans-serif')
    font = QFont()
    if hasattr(font, 'setFamilies'):
        font.setFamilies(list(families))
    else:
        font.setFamily(families[0])
    font.setPointSize(10)
    application.setFont(font)
    return families
