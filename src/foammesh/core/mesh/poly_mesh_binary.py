"""Binary and compact list encodings of ``constant/polyMesh`` (Plan 37 UF10a).

The CR2 reader (:mod:`poly_mesh_boundary`) reads ASCII and ``.gz`` and refuses
everything else by name. A worker that builds a section from the mesh the user
actually has cannot ask them to run ``foamFormatConvert`` first, so this
module decodes what the installed OpenFOAM 13 writes -- observed on
2026-09-30, see ``tests/unit/plan37_uf10a_fixtures.py``:

* a binary list is ``N`` then ``(`` then ``N`` raw entries then ``)``; an empty
  one is the bare count ``0`` with no parentheses;
* binary ``faces`` are always ``class faceCompactList``: an offsets list of
  ``nFaces + 1`` labels, then the flat vertex list. An ASCII
  ``faceCompactList`` is the same two lists in text;
* a binary ``cellZones`` file is dictionary text in which each
  ``cellLabels List<label>`` is a binary block.

A DPInt32 build writes no ``arch`` entry in the header, so the label width is
not stated anywhere. It is inferred: each candidate width must close the list
exactly where the count says, and what follows must be what the file allows
(whitespace and comments, or the next list, or ``;``). A body that more than
one width satisfies is refused as ``binary_width_ambiguous``; a big-endian
``arch`` is refused as ``unsupported_encoding``. Nothing here guesses.

Every function takes the whole member as bytes and never writes anything.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np

from foammesh.core.mesh.poly_mesh_boundary import (
    _FOAMFILE, PolyMeshReadError, Zone, _parse_header, _read_bytes)

LABEL_WIDTHS = (4, 8)
SCALAR_WIDTHS = (8, 4)
_ARCH = re.compile(r'(LSB|MSB)|label\s*=\s*(\d+)|scalar\s*=\s*(\d+)')
_SPACE = b' \t\r\n\f\v'


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #

def arch_widths(header: dict, path: Path) -> tuple[int | None, int | None]:
    """``(label bytes, scalar bytes)`` stated by an ``arch`` entry, else None.

    A big-endian file is refused: nothing OpenFOAM 13 writes on the hosts we
    support is big-endian, and byte-swapping a guess is how a mesh gets
    silently wrong.
    """
    arch = header.get('arch')
    if not arch:
        return None, None
    label = scalar = None
    for order, label_bits, scalar_bits in _ARCH.findall(arch):
        if order == 'MSB':
            raise PolyMeshReadError(
                'unsupported_encoding',
                f'{path} is written big-endian ({arch}); only little-endian '
                'binary is decoded', path=path)
        if label_bits:
            label = int(label_bits) // 8
        if scalar_bits:
            scalar = int(scalar_bits) // 8
    if label not in (None, *LABEL_WIDTHS) or scalar not in (None, *SCALAR_WIDTHS):
        raise PolyMeshReadError(
            'unsupported_encoding',
            f'{path} states arch {arch!r}; only 32/64-bit labels and '
            '32/64-bit scalars are decoded', path=path)
    return label, scalar


def split_member(path: Path) -> tuple[dict, bytes, int]:
    """``(FoamFile header, the raw bytes, where the body starts)``.

    The body is not comment-stripped: a binary block can hold ``//`` or
    ``/*`` as data, so comments are skipped only between tokens.
    """
    raw = _read_bytes(path)
    header = _parse_header(raw, path)
    return header, raw, _FOAMFILE.search(raw).end()


# --------------------------------------------------------------------------- #
# Tokens between binary blocks
# --------------------------------------------------------------------------- #

def skip_blank(raw: bytes, at: int) -> int:
    """Skip whitespace and comments; return the next significant offset."""
    size = len(raw)
    while at < size:
        byte = raw[at]
        if byte in _SPACE:
            at += 1
        elif raw.startswith(b'//', at):
            end = raw.find(b'\n', at)
            at = size if end < 0 else end + 1
        elif raw.startswith(b'/*', at):
            end = raw.find(b'*/', at + 2)
            at = size if end < 0 else end + 2
        else:
            break
    return at


def _count(raw: bytes, at: int, hint: str) -> tuple[int, int]:
    at = skip_blank(raw, at)
    end = at
    while end < len(raw) and 48 <= raw[end] <= 57:
        end += 1
    if end == at:
        raise PolyMeshReadError('malformed_list', f'{hint} has no list length')
    return int(raw[at:end]), end


def only_blank_after(raw: bytes, at: int) -> bool:
    return skip_blank(raw, at) == len(raw)


# --------------------------------------------------------------------------- #
# One binary list
# --------------------------------------------------------------------------- #

def binary_list(raw: bytes, at: int, *, components: int, widths,
                hint: str, follows: Callable[[int, int], bool]
                ) -> tuple[bytes, int, int, int]:
    """Locate one binary list starting at or after ``at``.

    Returns ``(block, count, width, offset after the list)``; the block is a
    read-only view of ``raw``. ``widths``
    are the candidate bytes per component; ``follows(offset, width)`` says
    whether what comes after a candidate's closing ``)`` is what the file
    allows there.
    """
    count, at = _count(raw, at, hint)
    # OpenFOAM writes ``N\n(`` -- whitespace only, never a comment, between.
    while at < len(raw) and raw[at] in _SPACE:
        at += 1
    if at >= len(raw) or raw[at:at + 1] != b'(':
        if raw[at:at + 1] == b'{':
            raise PolyMeshReadError(
                'unsupported_encoding',
                f'{hint} is a uniform binary list, which OpenFOAM 13 does '
                'not write and this reader does not decode')
        if count == 0:
            return b'', 0, widths[0], at
        raise PolyMeshReadError(
            'malformed_list', f'{hint} declares {count} entries but has no '
            'list body')
    open_at = at + 1
    fits = []
    for width in widths:
        close = open_at + count * components * width
        if raw[close:close + 1] == b')' and follows(close + 1, width):
            fits.append((width, close))
    if count == 0 and fits:
        return b'', 0, widths[0], fits[0][1] + 1
    if not fits:
        stated = ' or '.join(str(width) for width in widths)
        raise PolyMeshReadError(
            'malformed_list',
            f'{hint} declares {count} entries but its binary body does not '
            f'close after {count} entries of {stated} bytes')
    if len(fits) > 1:
        raise PolyMeshReadError(
            'binary_width_ambiguous',
            f'{hint}: the binary body closes cleanly as both '
            f'{fits[0][0]}- and {fits[1][0]}-byte entries and the header has '
            'no arch entry to say which')
    width, close = fits[0]
    # A view, not a slice: slicing would copy a block of up to gigabytes
    # that ``labels_from`` / ``scalars_from`` copy again anyway.
    return memoryview(raw)[open_at:close], count, width, close + 1


#: Entries per thread when a list is widened in parts (see ``_widen``).
WIDEN_PART = 4_000_000


def _widen(source: np.ndarray, dtype) -> np.ndarray:
    """A new ``dtype`` copy of ``source``, the same as ``source.astype``.

    A list of tens of millions of entries is copied in parts on threads:
    NumPy releases the GIL while it copies, and the copy is most of what
    reading a large binary mesh costs.
    """
    count = source.size
    parts = min(8, os.cpu_count() or 1, -(-count // WIDEN_PART))
    if parts <= 1:
        return source.astype(dtype)
    out = np.empty(count, dtype=dtype)
    bounds = np.linspace(0, count, parts + 1).astype(np.int64)

    def copy(index: int) -> None:
        first, last = int(bounds[index]), int(bounds[index + 1])
        np.copyto(out[first:last], source[first:last], casting='safe')

    with ThreadPoolExecutor(max_workers=parts) as pool:
        list(pool.map(copy, range(parts)))
    return out


def labels_from(block, width: int) -> np.ndarray:
    return _widen(np.frombuffer(block, dtype='<i4' if width == 4 else '<i8'),
                  np.int64)


def scalars_from(block, width: int) -> np.ndarray:
    return _widen(np.frombuffer(block, dtype='<f8' if width == 8 else '<f4'),
                  np.float64)


def _widths(stated: int | None, known: int | None, default) -> tuple:
    if stated is not None:
        return (stated,)
    if known is not None:
        return (known,)
    return tuple(default)


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #

def read_binary_points(path: Path) -> tuple[np.ndarray, int]:
    """``(points (n, 3) float64, scalar width)`` of a binary ``points``."""
    header, raw, start = split_member(path)
    _label, scalar = arch_widths(header, path)
    block, count, width, _end = binary_list(
        raw, start, components=3, widths=_widths(scalar, None, SCALAR_WIDTHS),
        hint='points', follows=lambda at, _w: only_blank_after(raw, at))
    values = scalars_from(block, width)
    if not np.all(np.isfinite(values)):
        raise PolyMeshReadError(
            'malformed_list', 'points holds a non-finite coordinate')
    return values.reshape(count, 3), width


def read_binary_labels(path: Path, name: str, *,
                       label_width: int | None = None
                       ) -> tuple[np.ndarray, int]:
    """``(labels int64, label width)`` of a binary ``labelList`` member."""
    header, raw, start = split_member(path)
    stated, _scalar = arch_widths(header, path)
    block, _count_, width, _end = binary_list(
        raw, start, components=1,
        widths=_widths(stated, label_width, LABEL_WIDTHS), hint=name,
        follows=lambda at, _w: only_blank_after(raw, at))
    return labels_from(block, width), width


def read_binary_compact_faces(path: Path, *, label_width: int | None = None
                              ) -> tuple[np.ndarray, np.ndarray, int]:
    """``(flat vertices, offsets, label width)`` of a binary faceCompactList."""
    header, raw, start = split_member(path)
    if header.get('class', 'faceList') != 'faceCompactList':
        raise PolyMeshReadError(
            'unsupported_encoding',
            f'{path} is a binary {header.get("class")}; OpenFOAM 13 writes '
            'binary faces only as faceCompactList', path=path)
    stated, _scalar = arch_widths(header, path)
    widths = _widths(stated, label_width, LABEL_WIDTHS)

    def second_list_closes(at: int, width: int) -> bool:
        try:
            _block, _n, _w, end = binary_list(
                raw, at, components=1, widths=(width,), hint='faces',
                follows=lambda after, _x: only_blank_after(raw, after))
        except PolyMeshReadError:
            return False
        return end <= len(raw)

    block, _n, width, end = binary_list(
        raw, start, components=1, widths=widths, hint='faces offsets',
        follows=second_list_closes)
    offsets = labels_from(block, width)
    flat_block, _m, _w, _end = binary_list(
        raw, end, components=1, widths=(width,), hint='faces',
        follows=lambda at, _x: only_blank_after(raw, at))
    flat = labels_from(flat_block, width)
    check_compact(offsets, flat, 'faces')
    return flat, offsets, width


def check_compact(offsets: np.ndarray, flat: np.ndarray, hint: str) -> None:
    """A faceCompactList's offsets start at 0, never fall, end at the flat size."""
    if offsets.size == 0:
        if flat.size:
            raise PolyMeshReadError(
                'malformed_list', f'{hint} has vertices but no offsets')
        return
    if (offsets[0] != 0 or offsets[-1] != flat.size
            or np.any(np.diff(offsets) < 0)):
        raise PolyMeshReadError(
            'malformed_list',
            f'{hint} offsets do not run from 0 to its {flat.size} vertices')


def read_ascii_compact_faces(payload: bytes) -> tuple[np.ndarray, np.ndarray]:
    """``(flat vertices, offsets)`` of an ASCII faceCompactList payload.

    ``payload`` is comment-stripped text after the FoamFile header: two
    plain label lists, the ``nFaces + 1`` offsets and the flat vertices.
    """
    from foammesh.core.mesh.poly_mesh_boundary import _leading_count, _numbers

    lists = []
    rest = payload
    for hint in ('faces offsets', 'faces'):
        count, open_at = _leading_count(rest, hint)
        close = rest.find(b')', open_at)
        if close < 0:
            raise PolyMeshReadError(
                'malformed_list', f'{hint} has no closing paren')
        values = _numbers(rest[open_at + 1:close], np.int64)
        if values.size != count:
            raise PolyMeshReadError(
                'malformed_list',
                f'{hint} declares {count} entries but holds {values.size}')
        lists.append(values)
        rest = rest[close + 1:]
    offsets, flat = lists
    check_compact(offsets, flat, 'faces')
    return flat, offsets


def read_binary_zones(path: Path, name: str, *, label_width: int | None = None
                      ) -> tuple[Zone, ...]:
    """Zones from a binary ``cellZones`` (dictionary text, binary labels)."""
    header, raw, start = split_member(path)
    stated, _scalar = arch_widths(header, path)
    widths = _widths(stated, label_width, LABEL_WIDTHS)
    key = {'cellZones': b'cellLabels', 'faceZones': b'faceLabels',
           'pointZones': b'pointLabels'}.get(name, b'cellLabels')
    default_type = name[:-1] if name.endswith('s') else name
    count, at = _count(raw, start, name)
    at = skip_blank(raw, at)
    if raw[at:at + 1] != b'(':
        raise PolyMeshReadError('malformed_list', f'{name} has no list body')
    at += 1
    zones = []
    for _index in range(count):
        at = skip_blank(raw, at)
        word = re.compile(rb'[A-Za-z_][\w.\-:]*').match(raw, at)
        if word is None:
            raise PolyMeshReadError(
                'malformed_list', f'{name} zone {_index} has no name')
        zone_name = word.group().decode('ascii')
        at = skip_blank(raw, word.end())
        if raw[at:at + 1] != b'{':
            raise PolyMeshReadError(
                'malformed_list', f'{name} zone {zone_name} has no body')
        at += 1
        zone_type = default_type
        labels = np.empty(0, dtype=np.int64)
        while True:
            at = skip_blank(raw, at)
            if raw[at:at + 1] == b'}':
                at += 1
                break
            entry = re.compile(rb'[A-Za-z_]\w*').match(raw, at)
            if entry is None:
                raise PolyMeshReadError(
                    'malformed_list',
                    f'{name} zone {zone_name} has an unreadable entry')
            entry_key = entry.group()
            at = skip_blank(raw, entry.end())
            if entry_key == key:
                if raw.startswith(b'List<', at):
                    at = raw.index(b'>', at) + 1
                hint = f'{name} zone {zone_name}'
                block, _n, width, at = binary_list(
                    raw, at, components=1, widths=widths, hint=hint,
                    follows=lambda after, _w: raw[skip_blank(raw, after):
                                                  skip_blank(raw, after) + 1]
                    == b';')
                widths = (width,)
                labels = labels_from(block, width)
                at = skip_blank(raw, at) + 1          # the ';'
            else:
                end = raw.find(b';', at)
                if end < 0:
                    raise PolyMeshReadError(
                        'malformed_list',
                        f'{name} zone {zone_name} has an unterminated entry')
                if entry_key == b'type':
                    zone_type = raw[at:end].strip().decode('ascii', 'replace')
                at = end + 1
        zones.append(Zone(name=zone_name, zone_type=zone_type, labels=labels))
    return tuple(zones)
