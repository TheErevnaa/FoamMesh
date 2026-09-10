#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD file-format detection (pure; no OCCT needed)."""
from __future__ import annotations

from pathlib import Path

from foammesh.core.format_registry import geometry_suffix_formats

# suffix -> canonical format name, projected from the authoritative registry.
_SUFFIX_FORMAT = geometry_suffix_formats()

CAD_SUFFIXES = tuple(_SUFFIX_FORMAT.keys())


def detect_format(path) -> str:
    """Return 'step' | 'iges' | 'brep' for *path*, else raise ValueError."""
    suffix = Path(path).suffix.lower()
    try:
        return _SUFFIX_FORMAT[suffix]
    except KeyError:
        raise ValueError(
            f'not a recognised CAD file: {suffix!r} '
            f'(supported: {", ".join(CAD_SUFFIXES)})')
