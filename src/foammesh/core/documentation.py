"""Stable local-first documentation destinations for branded shell actions."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QUrl


FALLBACK_TUTORIAL_URL = 'https://wiki.openfoam.com/Meshing'


def document_path(name: str, *, roots: tuple[Path, ...] | None = None) -> Path | None:
    if Path(name).name != name:
        raise ValueError('document name must not contain a path')
    if roots is None:
        roots = tuple(filter(None, (
            Path(getattr(sys, '_MEIPASS')) if hasattr(sys, '_MEIPASS') else None,
            Path(__file__).resolve().parents[3],
        )))
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def document_text(name: str, *, roots: tuple[Path, ...] | None = None) -> str:
    path = document_path(name, roots=roots)
    return path.read_text(encoding='utf-8', errors='replace') if path is not None else f'{name} is unavailable.'


def tutorial_url(*, environment: dict[str, str] | None = None,
                 roots: tuple[Path, ...] | None = None) -> QUrl:
    environment = environment if environment is not None else os.environ
    configured = environment.get('FOAMMESH_TUTORIAL_URL', '').strip()
    if configured:
        return QUrl.fromUserInput(configured)
    if roots is None:
        roots = tuple(filter(None, (
            Path(getattr(sys, '_MEIPASS')) if hasattr(sys, '_MEIPASS') else None,
            Path(__file__).resolve().parents[3],
        )))
    for root in roots:
        guide = root / 'docs' / 'user_manual' / 'getting_started.md'
        if guide.is_file():
            return QUrl.fromLocalFile(str(guide.resolve()))
    return QUrl(FALLBACK_TUTORIAL_URL)
