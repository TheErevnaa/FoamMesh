#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The snapshot: one picture of the view, in any of six formats (Plan 43 LY15).

Capture (a numbered PNG with a sidecar in the case), Save view as (a PNG at
1x, 2x or 4x, with or without a transparent background and the overlay) and
Save image as (PNG, JPEG, SVG, PDF, EPS, PS) are one popover.  This module holds
what is not Qt: the options, the numbered name, the pixel size and the line the
status bar says after a save.  Presentation-neutral on purpose: the CLI and the
report writer read the same names and the same sidecar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

FORMATS = ('PNG', 'JPEG', 'SVG', 'PDF', 'EPS', 'PS')
RASTER = ('PNG', 'JPEG')
SUFFIX = {'PNG': '.png', 'JPEG': '.jpg', 'SVG': '.svg', 'PDF': '.pdf',
          'EPS': '.eps', 'PS': '.ps'}
SCALES = (1, 2, 4)
DEFAULT_SCALE = 2
#: The Include boxes.  ``transparent`` is a property of the file, not of the view.
INCLUDES = ('legend', 'worst', 'caption', 'triad', 'transparent')
FORMAT_LABELS = {'PNG': 'PNG image', 'JPEG': 'JPEG image', 'SVG': 'SVG drawing',
                 'PDF': 'PDF document', 'EPS': 'Encapsulated PostScript',
                 'PS': 'PostScript'}


def default_includes() -> dict:
    return {'legend': True, 'worst': True, 'caption': True, 'triad': True,
            'transparent': False}


@dataclass
class SnapshotOptions:
    """Everything the popover can set."""

    name: str = ''
    #: ``None`` means the case's ``captures`` folder, with its sidecar.
    directory: str | None = None
    format: str = 'PNG'
    scale: int = DEFAULT_SCALE
    font: str = 'pretendard'
    text_size: str = 'normal'
    includes: dict = field(default_factory=default_includes)
    #: The Qt overlay (the card, the tool rail, the progress strip), an option
    #: of Save view as that stays.
    overlay: bool = False

    def normalised(self) -> 'SnapshotOptions':
        fmt = str(self.format).upper()
        fmt = 'JPEG' if fmt == 'JPG' else fmt
        if fmt not in FORMATS:
            raise ValueError(f'{self.format} is not a snapshot format')
        scale = int(self.scale) if int(self.scale) in SCALES else DEFAULT_SCALE
        includes = default_includes()
        includes.update({k: bool(v) for k, v in dict(self.includes).items()
                         if k in INCLUDES})
        if fmt not in ('PNG', 'SVG'):
            includes['transparent'] = False       # only these two have a clear background
        return SnapshotOptions(self.name, self.directory, fmt,
                               scale if fmt in RASTER else 1, self.font,
                               self.text_size, includes,
                               bool(self.overlay) and fmt in RASTER)


def is_raster(fmt: str) -> bool:
    return str(fmt).upper() in RASTER


def pixel_size(width: int, height: int, scale: int) -> tuple[int, int]:
    """The size of the picture in pixels at ``scale`` x."""
    return max(int(width), 1) * int(scale), max(int(height), 1) * int(scale)


def _slug(text: str) -> str:
    text = re.sub(r'[^0-9A-Za-z]+', '_', str(text)).strip('_').lower()
    return text or 'view'


def suggest_stem(case_name: str = '', scalar: str = '') -> str:
    """``pipe_skewness`` from the case and what the model is coloured by."""
    return '_'.join(part for part in (_slug(case_name) if case_name else '',
                                      _slug(scalar) if scalar else '') if part
                    ) or 'view'


def numbered_name(directory: Path | str, stem: str, fmt: str,
                  day: date | None = None) -> str:
    """``pipe_skewness_2026-10-12_01.png``: the first number not yet used.

    The extension follows the format; the number counts per name and day over
    every format, so a PNG and an SVG of one view do not collide.
    """
    directory = Path(directory)
    stamp = (day or date.today()).isoformat()
    suffix = SUFFIX[str(fmt).upper()]
    stem = _slug(stem)
    taken = {path.stem for path in directory.glob(f'{stem}_{stamp}_*.*')}
    number = 1
    while f'{stem}_{stamp}_{number:02d}' in taken:
        number += 1
    return f'{stem}_{stamp}_{number:02d}{suffix}'


def unique_path(directory: Path | str, name: str) -> Path:
    """*name* in *directory*, numbered further if the file already exists."""
    directory = Path(directory)
    path = directory / Path(name).name
    base, suffix, count = path.stem, path.suffix, 1
    while path.exists():
        count += 1
        path = directory / f'{base}_{count}{suffix}'
    return path


def includes_phrase(includes: dict) -> str:
    """``the legend, numbers, caption and triad`` for the status line."""
    names = [text for key, text in (('legend', 'legend'), ('worst', 'numbers'),
                                    ('caption', 'caption'), ('triad', 'triad'))
             if includes.get(key)]
    if not names:
        return 'no labels'
    return 'the ' + ', '.join(names[:-1]) + (' and ' if len(names) > 1 else '') + names[-1]


def status_line(path: Path | str, options: SnapshotOptions, size_px,
                where: str) -> str:
    """What was written, for the status bar."""
    path = Path(path)
    size = (' ({0} × {1})'.format(*size_px) if size_px and
            is_raster(options.format) else '')
    return 'Saved {0}{1} into {2}, with {3}.'.format(
        path.name, size, where, includes_phrase(options.includes))


def sidecar_fields(options: SnapshotOptions, size_px) -> dict:
    """The part of the sidecar the snapshot adds (see ``CaptureRecord``)."""
    return {'format': options.format,
            'scale': int(options.scale) if is_raster(options.format) else 0,
            'size_px': list(size_px) if size_px and is_raster(options.format)
            else [],
            'font': options.font, 'text_size': options.text_size,
            'includes': dict(options.includes)}
