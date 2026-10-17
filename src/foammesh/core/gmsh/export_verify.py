"""Read a written mesh file back and count what it holds (Plan 41 GN15).

Gmsh writes thirty formats and reads about half of them.  A written file is
checked two ways, and which one applies is recorded per format in
:mod:`export_writers`:

* ``gmsh``  -- Gmsh itself reads the file back in a process of its own
  (``resources/gmsh/readback_v1.py``, started by :mod:`export_service`), and
  the node and element counts are compared.
* ``text``  -- Gmsh has no reader for the format (Abaqus ``.inp``, LS-DYNA
  ``.key``, Radioss ``.rad``, Tochnog ``.dat``, ``.ir3``, Matlab ``.m``,
  ``.mail``, SU2 ``.su2``, Gambit ``.neu``).  The file is read by the plain
  reader below, which counts the nodes the file declares.  Where the header
  also states the element count it is returned.

This module is standard library only so the checks run on the host and in a
fresh WSL Python alike.
"""
from __future__ import annotations

import re
from pathlib import Path


def _lines(path) -> list[str]:
    return Path(path).read_text(encoding='utf-8',
                                errors='replace').splitlines()


def _count_after(lines, header: str, stop_prefixes: tuple) -> int:
    """Rows after the line starting with *header* up to the next keyword."""
    inside = False
    count = 0
    for line in lines:
        text = line.strip()
        if not inside:
            if text.upper().startswith(header):
                inside = True
            continue
        if not text or text.startswith(('**', '$#', '#')):
            continue
        if text.startswith(stop_prefixes):
            break
        count += 1
    return count


def _abaqus_like(path, header='*NODE') -> dict:
    lines = _lines(path)
    nodes = _count_after(lines, header, ('*',))
    return {'nodes': nodes}


def _rad(path) -> dict:
    lines = _lines(path)
    return {'nodes': _count_after(lines, '/NODE', ('/',))}


def _su2(path) -> dict:
    nodes = elements = None
    for line in _lines(path):
        text = line.strip()
        if text.startswith('NPOIN='):
            nodes = int(text.split('=')[1].split()[0])
        elif text.startswith('NELEM=') and elements is None:
            elements = int(text.split('=')[1].split()[0])
    return {'nodes': nodes, 'elements': elements}


def _neu(path) -> dict:
    lines = _lines(path)
    for index, line in enumerate(lines):
        if 'NUMNP' in line and 'NELEM' in line:
            values = lines[index + 1].split()
            return {'nodes': int(values[0]), 'elements': int(values[1])}
    return {'nodes': None}


def _dat(path) -> dict:
    return {'nodes': sum(1 for line in _lines(path)
                         if line.startswith('node '))}


def _ir3(path) -> dict:
    lines = _lines(path)
    values = lines[1].split()
    return {'nodes': int(values[0]), 'elements': int(values[2])
            if len(values) > 2 else None}


def _matlab(path) -> dict:
    for line in _lines(path):
        found = re.match(r'\s*msh\.nbNod\s*=\s*(\d+)', line)
        if found:
            return {'nodes': int(found.group(1))}
    return {'nodes': None}


def _mail(path) -> dict:
    values = _lines(path)[0].split()
    return {'nodes': int(values[0]), 'elements': None}


def _celum(path) -> dict:
    return {'nodes': None}


#: extension -> the plain reader.
TEXT_READERS = {
    '.inp': _abaqus_like,
    '.key': _abaqus_like,
    '.rad': _rad,
    '.su2': _su2,
    '.neu': _neu,
    '.dat': _dat,
    '.ir3': _ir3,
    '.m': _matlab,
    '.mail': _mail,
    '.celum': _celum,
}


def text_census(path) -> dict | None:
    """Nodes (and elements where the header says) declared by *path*.

    ``None`` when no plain reader exists for the extension.
    """
    reader = TEXT_READERS.get(Path(path).suffix.lower())
    if reader is None:
        return None
    return reader(path)
