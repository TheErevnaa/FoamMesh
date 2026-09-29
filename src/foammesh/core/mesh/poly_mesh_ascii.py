"""Vectorised scanning of the ASCII lists in a polyMesh (Plan 35 CR2b).

The tolerant reader in :mod:`poly_mesh_boundary` splits a list into Python
``bytes`` tokens and casts each one with ``int()`` or ``float()``: a Python
object per token, and about 0.1 to 0.2 us each. A 5 M-cell mesh holds some
135 M tokens. This module reads the same lists without a Python list of
them:

* The list is streamed in blocks and cut into pieces of :data:`PIECE` bytes,
  each ending on a separator, so every temporary array is a few hundred
  kilobytes whatever the file's size.
* A label is at most 16 digits. Its digits are read as one or two unaligned
  64-bit words, and eight ASCII digits become an integer in three multiplies
  (the usual SWAR reduction). Exact.
* A scalar is read by ``float()`` itself, into an array with no list of
  Python floats in between (see :func:`scan_scalars`).

It is strict by design. Anything it is not certain it reads exactly the way
``int()``/``float()`` over ``bytes.split()`` would -- a character outside the
list alphabet, a sign on a label, a 17-digit label, a token ``float()`` would
reject -- raises :class:`Irregular`, and the caller reads that list with the
tolerant parser instead. This module can make a read faster; it cannot change
what a read returns.

Nothing here imports the rest of FoamMesh.
"""
from __future__ import annotations

import numpy as np

#: Bytes per scanned piece. Measured on a 93 MB ``faces``: 16 KiB 0.40 s,
#: 64 KiB 0.33 s, 1 MiB 1.2 s -- past the cache, every numpy temporary is a
#: fresh, page-faulted allocation.
PIECE = 64 * 1024
#: Bytes read from the file per call, then cut into pieces.
BLOCK = 4 * 1024 * 1024

#: ``bytes.split()`` whitespace, and the two parens the tolerant parser blanks
#: before it splits.
SEPARATORS = b' \t\n\r\x0b\x0c()'
_LABEL_ALPHABET = b'0123456789' + SEPARATORS

_OPEN = 0x28
_PAD = 16

_ZERO8 = np.uint64(0x3030303030303030)
_MASK_LO = np.uint64(0x000000FF000000FF)
_MUL_A = np.uint64(100 + (1000000 << 32))
_MUL_B = np.uint64(1 + (10000 << 32))
_TEN = np.uint64(10)
_S8, _S16, _S32 = np.uint64(8), np.uint64(16), np.uint64(32)
_E8 = np.uint64(100_000_000)
#: ``_KEEP[n]`` keeps the top ``n`` bytes of a little-endian word: the ``n``
#: bytes just before a token's end, which are its last ``n`` digits.
_KEEP = np.array(
    [0] + [((1 << 64) - 1) ^ ((1 << (8 * (8 - n))) - 1) for n in range(1, 9)],
    dtype=np.uint64)
#: The rest of the word reads as ASCII ``0``: a short number is left-padded.
_FILL = _ZERO8 & ~_KEEP
_PARENS = bytes.maketrans(b'()', b'  ')


class Irregular(Exception):
    """The input is not in a form this scanner reads exactly."""


# --------------------------------------------------------------------------- #
# Pieces
# --------------------------------------------------------------------------- #

def _cut(data: bytes, start: int, end: int) -> int:
    """Index of a separator in ``data[start:end]``, as late as cheaply found.

    A newline is looked for first: every list OpenFOAM writes has one a line,
    so the other separators are searched for only in a stretch without.
    """
    at = data.rfind(b'\n', start, end)
    if at >= 0:
        return at
    return max(data.rfind(bytes((byte,)), start, end) for byte in SEPARATORS)


def pieces(stream, *, strip_comments, first: bytes = b''):
    """Yield the rest of ``stream`` in pieces that each end on a separator.

    ``first`` is data already read that comes ahead of the stream. From the
    block holding the first ``/`` on, the rest of the file is put through
    ``strip_comments`` in one go. No comment can begin before the first
    ``/``, so that is exactly what stripping the whole file would have done
    to it; in a file OpenFOAM wrote, it is only the closing banner.
    """
    carry = first
    while True:
        block = stream.read(BLOCK)
        data = carry + block if carry else block
        if not data:
            return
        if b'/' in data:
            yield from _split(strip_comments(data + stream.read()))
            return
        if not block:
            yield from _split(data)
            return
        at = _cut(data, max(0, len(data) - PIECE), len(data))
        if at < 0:
            at = _cut(data, 0, len(data))
        if at < 0:
            carry = data
            continue
        yield from _split(data[:at + 1])
        carry = data[at + 1:]


def _split(data: bytes):
    size = len(data)
    position = 0
    while position < size:
        end = position + PIECE
        if end >= size:
            yield data[position:]
            return
        at = _cut(data, position, end)
        if at < 0:
            at = _cut(data, position, size)
            if at < 0:
                yield data[position:]
                return
        yield data[position:at + 1]
        position = at + 1


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

def _padded(piece: bytes) -> np.ndarray:
    size = len(piece)
    buffer = np.empty(size + 2 * _PAD, dtype=np.uint8)
    buffer[:_PAD] = 0x20
    buffer[_PAD:_PAD + size] = np.frombuffer(piece, dtype=np.uint8)
    buffer[_PAD + size:] = 0x20
    return buffer


def _words(buffer: np.ndarray) -> np.ndarray:
    """The unaligned little-endian 64-bit word at every offset of ``buffer``."""
    return np.ndarray(shape=(buffer.size - 7,), dtype=np.uint64,
                      buffer=buffer, strides=(1,))


def _runs(token: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(starts, ends)`` of the runs of ``True``; ``ends`` exclusive."""
    edges = np.flatnonzero(token[1:] != token[:-1]) + 1
    return edges[0::2], edges[1::2]


def _reduce8(word: np.ndarray) -> np.ndarray:
    """Eight ASCII digits, most significant at the lowest address, as uint64."""
    word -= _ZERO8
    word = word * _TEN + (word >> _S8)
    return ((word & _MASK_LO) * _MUL_A
            + ((word >> _S16) & _MASK_LO) * _MUL_B) >> _S32


def _digits(words: np.ndarray, ends: np.ndarray,
            lengths: np.ndarray) -> np.ndarray:
    """Value of the ``lengths`` (0..16) digits that end at ``ends``."""
    short = np.minimum(lengths, 8)
    word = words[ends - 8]
    word &= _KEEP[short]
    word |= _FILL[short]
    value = _reduce8(word)
    long_ = np.flatnonzero(lengths > 8)
    if long_.size:
        extra = lengths[long_] - 8
        high = words[ends[long_] - 16]
        high &= _KEEP[extra]
        high |= _FILL[extra]
        value[long_] += _reduce8(high) * _E8
    return value


def scan_labels(piece: bytes, *, heads: bool = False):
    """The labels in ``piece``, as ``int()`` over ``bytes.split()`` reads them.

    With ``heads``, also whether each label is followed at once by ``(``: in
    a ``faceList`` those are the face sizes.
    """
    if piece.translate(None, _LABEL_ALPHABET):
        raise Irregular('a label list holds a character a label cannot')
    buffer = _padded(piece)
    starts, ends = _runs(buffer >= 0x30)
    lengths = ends - starts
    if lengths.size and int(lengths.max()) > 16:
        raise Irregular('a label longer than 16 digits')
    values = _digits(_words(buffer), ends, lengths).view(np.int64)
    if not heads:
        return values
    return values, buffer[ends] == _OPEN


def scan_scalars(piece: bytes) -> np.ndarray:
    """The scalars in ``piece``: ``float()`` over ``bytes.split()``, verbatim.

    A vectorised decimal reader (Clinger's fast path over SWAR digits) was
    built and measured, and lost: a ``points`` token is short, ``float()``
    costs about 0.1 us, and thirty numpy passes over the bytes cost more. What
    is left of the old cost is the Python list of floats, which ``fromiter``
    does without. Every value is ``float()``'s own, so nothing can differ.
    """
    try:
        return np.fromiter(map(float, piece.translate(_PARENS).split()),
                           dtype=np.float64)
    except ValueError:
        raise Irregular('a scalar list holds a token float() refuses') from None


def scan(data: bytes, kind: str) -> np.ndarray:
    """Every number in an in-memory ``data``: ``kind`` is label or scalar."""
    parse = scan_labels if kind == 'label' else scan_scalars
    parts = [parse(piece) for piece in _split(data)]
    if not parts:
        return np.empty(0, dtype=np.int64 if kind == 'label' else np.float64)
    return parts[0] if len(parts) == 1 else np.concatenate(parts)
