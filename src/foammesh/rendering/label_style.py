#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The font and size of every label drawn on the model (Plan 43 LY15, section 5).

The legend, the worst values on the model, the selected-cell tag and the
statistics caption share one font and one size.  The choice is part of the
colour standard (``AppSettings.getLabelStyle``), so the CLI and the API draw
the same labels.  The app's own UI font (``SettingKey.UI_FONT``) is separate.

No Qt here: the style is applied to ``vtkTextProperty`` objects, and the
vector writers read the family and the size from the same place.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Font key -> the label a menu shows (each item is set in its own face).
FONTS = {
    'pretendard': 'Pretendard',
    'arial': 'Arial',
    'times': 'Times New Roman',
    'courier': 'Courier New',
}
#: Size key -> (menu label, multiplier of the size the label was built with).
SIZES = {
    'small': ('Small', 0.88),
    'normal': ('Normal', 1.0),
    'large': ('Large', 1.25),
    'xlarge': ('Extra large', 1.5),
}
DEFAULT_FONT = 'pretendard'
DEFAULT_SIZE = 'normal'

#: The family name each vector format writes for a font.  Pretendard is not a
#: PostScript or PDF base font, so those two fall back to Helvetica; the SVG
#: names Pretendard first and lets the viewer fall back.
SVG_FAMILY = {
    'pretendard': 'Pretendard, Helvetica, Arial, sans-serif',
    'arial': 'Arial, Helvetica, sans-serif',
    'times': "'Times New Roman', Times, serif",
    'courier': "'Courier New', Courier, monospace",
}
POSTSCRIPT_FONT = {
    'pretendard': 'Helvetica', 'arial': 'Helvetica',
    'times': 'Times-Roman', 'courier': 'Courier',
}


@dataclass(frozen=True)
class LabelStyle:
    font: str = DEFAULT_FONT
    size: str = DEFAULT_SIZE

    @property
    def factor(self) -> float:
        return SIZES[self.size][1]

    def as_dict(self) -> dict:
        return {'font': self.font, 'size': self.size, 'factor': self.factor}


def normalise(font=None, size=None) -> LabelStyle:
    """A valid style; an unknown name falls back to the app default."""
    font = str(font or DEFAULT_FONT).lower()
    size = str(size or DEFAULT_SIZE).lower()
    return LabelStyle(font if font in FONTS else DEFAULT_FONT,
                      size if size in SIZES else DEFAULT_SIZE)


def pretendard_file() -> Path | None:
    """The bundled Pretendard font file, or None when it is not shipped."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / 'resources' / 'PretendardVariable.ttf'
        if candidate.is_file():
            return candidate
    return None


def load() -> LabelStyle:
    """The user's labels font and size, or the app standard."""
    try:
        from foammesh.settings.app_settings import appSettings

        stored = appSettings.getLabelStyle()
    except Exception:                                         # noqa: BLE001
        stored = {}
    return normalise(stored.get('font'), stored.get('size'))


def save(style: LabelStyle) -> None:
    try:
        from foammesh.settings.app_settings import appSettings

        appSettings.updateLabelStyle(font=style.font, size=style.size)
    except Exception:                                         # noqa: BLE001
        pass


def apply_font(prop, font: str) -> None:
    """Set the family of one ``vtkTextProperty``."""
    if prop is None:
        return
    font = font if font in FONTS else DEFAULT_FONT
    if font == 'arial':
        prop.SetFontFamilyToArial()
    elif font == 'times':
        prop.SetFontFamilyToTimes()
    elif font == 'courier':
        prop.SetFontFamilyToCourier()
    else:
        path = pretendard_file()
        if path is None:
            prop.SetFontFamilyToArial()
        else:
            prop.SetFontFamilyAsString('File')
            prop.SetFontFile(str(path))


def apply(prop, style: LabelStyle, base_size: int) -> None:
    """Family and size (``base_size`` x the style's multiplier) on *prop*."""
    if prop is None:
        return
    apply_font(prop, style.font)
    prop.SetFontSize(max(1, int(round(base_size * style.factor))))


def text_properties(actor) -> list:
    """Every ``vtkTextProperty`` a label actor draws with."""
    found = []
    for name in ('GetTextProperty', 'GetLabelTextProperty',
                 'GetTitleTextProperty', 'GetAnnotationTextProperty'):
        getter = getattr(actor, name, None)
        if getter is None:
            continue
        try:
            prop = getter()
        except TypeError:
            continue
        if prop is not None and all(prop is not other for other in found):
            found.append(prop)
    return found
