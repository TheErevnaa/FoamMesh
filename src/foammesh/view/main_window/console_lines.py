# -*- coding: utf-8 -*-
"""What a console line is: its level, whether it opens a stage, what in it is a link.

Plan 40 UXC6 (F6a). The console used to be a wall of text: no way to show only
the problems, to fold a stage away, or to open the file a line names. These
helpers read a line and say so; the widget in ``console_view`` applies it.
They are plain Python so a gate can run them over a 20 000-line log without a
window.
"""
from __future__ import annotations

import os
import re

INFO, WARNING, ERROR = 0, 1, 2

_ERROR = re.compile(
    r'\b(error|fatal|(?<!0 )failed|failure|exception|traceback|aborting)\b', re.I)
_WARNING = re.compile(r'\bwarning\b|^\s*\*\*\*|\bskipped\b', re.I)

#: A line that opens a stage of a run. The OpenFOAM utilities print ``Exec :``
#: once per run, snappyHexMesh names each phase, and the runners print their
#: own ``Stage`` / ``Running`` lines and ``=== title ===`` rules.
_HEADING = re.compile(
    r'^\s*(?:Exec\s*:|Running\b|Stage\b|={3,}\s*\S|-{3,}\s+\S.*\S\s+-{3,}\s*$'
    r'|(?:Surface|Shell|Feature)\s+refinement\s+iteration\b'
    r'|(?:Morph|Snap|Layer)\w*\s+(?:phase|iteration)\b'
    r'|Adding\s+layers\b|Checking\s+mesh\b)')

_WINDOWS_PATH = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|*?,;()]+')
_POSIX_PATH = re.compile(r'(?<![\w.])/(?:[\w.\-+@~]+/)*[\w.\-+@~]+')
_WORD = re.compile(r'[A-Za-z0-9_.:\-]+')
_WSL_MOUNT = re.compile(r'^/mnt/([a-zA-Z])(?:/(.*))?$')


def line_level(text: str) -> int:
    if _ERROR.search(text):
        return ERROR
    if _WARNING.search(text):
        return WARNING
    return INFO


def is_heading(text: str) -> bool:
    return bool(_HEADING.match(text))


def host_path(candidate: str) -> str:
    """A path a line printed, as the host can open it (``/mnt/c/x`` -> ``C:/x``)."""
    mount = _WSL_MOUNT.match(candidate)
    if mount and os.name == 'nt':
        # DP-1440: a space-free alias of the user's folder shows as their own.
        from foammesh.core.wsl_paths import host_from_runtime
        return host_from_runtime(candidate)
    return candidate


def path_at(text: str, column: int) -> tuple[int, int, str] | None:
    """The existing file or folder named at ``column`` of ``text``.

    Returns ``(start, end, host_path)``. A name that is not on disk is not a
    link: a clickable word that opens nothing is worse than plain text.
    """
    for pattern in (_WINDOWS_PATH, _POSIX_PATH):
        for match in pattern.finditer(text):
            if not match.start() <= column <= match.end():
                continue
            raw = match.group(0).rstrip('.:')
            opened = host_path(raw)
            if os.path.exists(opened):
                return match.start(), match.start() + len(raw), opened
    return None


def word_at(text: str, column: int) -> tuple[int, int, str] | None:
    for match in _WORD.finditer(text):
        if match.start() <= column <= match.end():
            return match.start(), match.end(), match.group(0)
    return None
