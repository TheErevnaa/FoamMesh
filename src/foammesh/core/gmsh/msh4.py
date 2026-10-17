"""Reader of Gmsh MSH 4.x files, ASCII and binary (Plan 41 GN15).

The OpenFOAM writer of :mod:`publish` used to read only MSH 2.2 ASCII because
the runner pinned that version.  The Export page writes any version, so the
writer reads 4.1 as well; this module is the whole of that reading.  It is
pure Python and numpy, like the 2.2 reader, and returns the same three things:
the nodes, the elements as ``(type, physical tag, node tags)`` and the
physical names.

An entity that belongs to several physical groups gives one element row per
group, which is what Gmsh itself writes in version 2.2, so both readers hand
the publisher the same rows for the same mesh.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

#: Nodes per element type of Gmsh 4.15.2 (``getElementProperties``).
NODES_PER_TYPE = {
    1: 2, 2: 3, 3: 4, 4: 4, 5: 8, 6: 6, 7: 5, 8: 3, 9: 6, 10: 9, 11: 10,
    12: 27, 13: 18, 14: 14, 15: 1, 16: 8, 17: 20, 18: 15, 19: 13, 20: 9,
    21: 10, 22: 12, 23: 15, 24: 15, 25: 21, 26: 4, 27: 5, 28: 6, 29: 20,
    30: 35, 31: 56, 32: 22, 33: 28, 36: 16, 37: 25, 38: 36, 39: 12, 40: 16,
    41: 20, 42: 28, 43: 36, 44: 45, 45: 55, 46: 66, 47: 49, 48: 64, 49: 81,
    50: 100, 51: 121, 52: 18, 53: 21, 54: 24, 55: 27, 56: 30, 57: 24,
    58: 28, 59: 32, 60: 36, 61: 40, 62: 7, 63: 8, 64: 9, 65: 10, 66: 11,
    71: 84, 72: 120, 73: 165, 74: 220, 75: 286, 79: 34, 80: 40, 81: 46,
    82: 52, 83: 58, 84: 1, 85: 1, 86: 1, 87: 1, 88: 1, 89: 1, 92: 64,
    93: 125, 94: 216, 95: 343, 96: 512, 97: 729, 98: 1000, 99: 32, 100: 44,
    101: 56, 102: 68, 103: 80, 104: 92, 105: 104, 118: 30, 119: 55,
    120: 91, 121: 140, 122: 204, 123: 285, 124: 385, 125: 21, 126: 29,
    127: 37, 128: 45, 129: 53, 130: 61, 131: 69, 132: 1, 137: 16,
}


class Msh4Error(ValueError):
    pass


def is_msh4(head: bytes) -> bool:
    """Whether the first bytes of a file are a 4.x ``$MeshFormat`` header."""
    lines = head.split(b'\n', 2)
    if len(lines) < 2 or lines[0].strip() != b'$MeshFormat':
        return False
    return lines[1].split()[:1] and lines[1].split()[0].startswith(b'4')


class _Cursor:
    """A position in the file's bytes, with the typed reads binary needs."""

    def __init__(self, data: bytes, position: int, binary: bool,
                 size_t: int):
        self.data = data
        self.position = position
        self.binary = binary
        self.size_t = size_t
        self._size_format = '<Q' if size_t == 8 else '<I'

    def line(self) -> str:
        end = self.data.index(b'\n', self.position)
        text = self.data[self.position:end].decode('utf-8', 'replace')
        self.position = end + 1
        return text.strip()

    def tokens(self) -> list:
        return self.line().split()

    def integer(self) -> int:
        value = struct.unpack_from('<i', self.data, self.position)[0]
        self.position += 4
        return value

    def size(self) -> int:
        value = struct.unpack_from(self._size_format, self.data,
                                   self.position)[0]
        self.position += self.size_t
        return value

    def array(self, dtype: str, count: int) -> np.ndarray:
        width = np.dtype(dtype).itemsize
        values = np.frombuffer(self.data, dtype=dtype, count=count,
                               offset=self.position)
        self.position += width * count
        return values

    def sizes(self, count: int) -> np.ndarray:
        return self.array('<u8' if self.size_t == 8 else '<u4', count)

    def skip_binary_newline(self) -> None:
        if self.position < len(self.data) and self.data[self.position:
                                                       self.position + 1] == b'\n':
            self.position += 1


def read_msh4(path: str | Path) -> tuple:
    """``(points, node_ids, elements, names)`` of an MSH 4.x file."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise Msh4Error(f'mesh file could not be read: {error}') from error
    cursor = _Cursor(data, 0, False, 8)
    names: dict = {}
    entity_physical: dict = {}
    node_ids: list = []
    coordinates: list = []
    elements: list = []
    binary = False
    saw_format = False

    # A section that does not parse is a file this reader refuses, with
    # its reason, rather than a ValueError out of the middle of a loop
    # (a 2.2 body under a 4.1 header used to end in int('$EndNodes')).
    try:
        while cursor.position < len(data):
            try:
                header = cursor.line()
            except ValueError:
                break
            if header == '$MeshFormat':
                fields = cursor.tokens()
                if not fields[0].startswith('4'):
                    raise Msh4Error(f'this reader implements MSH 4; the file '
                                    f'declares {fields[0]}')
                binary = fields[1] == '1'
                cursor.size_t = int(fields[2])
                cursor._size_format = '<Q' if cursor.size_t == 8 else '<I'
                cursor.binary = binary
                if binary:
                    one = cursor.integer()
                    if one != 1:
                        raise Msh4Error('binary MSH 4 file with the other byte '
                                        'order is not supported')
                    cursor.skip_binary_newline()
                cursor.line()                                     # $EndMeshFormat
                saw_format = True
            elif header == '$PhysicalNames':
                count = int(cursor.line())
                for _ in range(count):
                    parts = cursor.line().split(None, 2)
                    names[(int(parts[0]), int(parts[1]))] = parts[2].strip(
                    ).strip('"')
                cursor.line()
            elif header == '$Entities':
                _read_entities(cursor, entity_physical)
            elif header == '$Nodes':
                _read_nodes(cursor, node_ids, coordinates)
            elif header == '$Elements':
                _read_elements(cursor, entity_physical, elements)
            elif header.startswith('$') and not header.startswith('$End'):
                end = '$End' + header[1:]
                while cursor.position < len(data):
                    if binary:
                        marker = data.find(end.encode(), cursor.position)
                        if marker < 0:
                            cursor.position = len(data)
                        else:
                            cursor.position = data.index(b'\n', marker) + 1
                        break
                    if cursor.line() == end:
                        break
    except Msh4Error:
        raise
    except (ValueError, IndexError, KeyError, struct.error) as error:
        raise Msh4Error(f'mesh file could not be read as MSH 4: '
                        f'{error}') from error
    if not saw_format:
        raise Msh4Error('mesh file has no $MeshFormat section')
    if not node_ids:
        raise Msh4Error('mesh file contains no nodes')
    points = (np.concatenate(coordinates) if coordinates
              else np.zeros((0, 3)))
    return (points.reshape(-1, 3), np.concatenate(node_ids), elements, names)


def _read_entities(cursor: _Cursor, entity_physical: dict) -> None:
    if cursor.binary:
        counts = cursor.sizes(4)
        counts = [int(value) for value in counts]
    else:
        counts = [int(value) for value in cursor.tokens()]
    for dim, count in enumerate(counts):
        for _ in range(count):
            if cursor.binary:
                tag = cursor.integer()
                cursor.array('<f8', 3 if dim == 0 else 6)
                physical_count = cursor.size()
                physical = [int(v) for v in cursor.array('<i4', physical_count)]
                if dim:
                    bound = cursor.size()
                    cursor.array('<i4', bound)
            else:
                row = cursor.tokens()
                tag = int(row[0])
                offset = 4 if dim == 0 else 7
                physical_count = int(row[offset])
                physical = [int(v) for v in row[offset + 1:
                                                offset + 1 + physical_count]]
            entity_physical[(dim, tag)] = physical
    if cursor.binary:
        cursor.skip_binary_newline()
    cursor.line()                                              # $EndEntities


def _read_nodes(cursor: _Cursor, node_ids: list, coordinates: list) -> None:
    if cursor.binary:
        blocks = cursor.size()
        cursor.size(), cursor.size(), cursor.size()
    else:
        blocks = int(cursor.tokens()[0])
    for _ in range(blocks):
        if cursor.binary:
            dim, _tag, parametric = (cursor.integer(), cursor.integer(),
                                     cursor.integer())
            count = cursor.size()
            tags = cursor.sizes(count).astype(np.int64)
            width = 3 + (dim if parametric else 0)
            values = cursor.array('<f8', count * width).reshape(count, width)
        else:
            row = cursor.tokens()
            dim, parametric, count = int(row[0]), int(row[2]), int(row[3])
            tags = np.array([int(cursor.line()) for _ in range(count)],
                            dtype=np.int64)
            width = 3 + (dim if parametric else 0)
            values = np.array([cursor.tokens() for _ in range(count)],
                              dtype=np.float64).reshape(count, width)
        node_ids.append(tags)
        coordinates.append(values[:, :3])
    if cursor.binary:
        cursor.skip_binary_newline()
    cursor.line()                                                # $EndNodes


def _read_elements(cursor: _Cursor, entity_physical: dict,
                   elements: list) -> None:
    if cursor.binary:
        blocks = cursor.size()
        cursor.size(), cursor.size(), cursor.size()
    else:
        blocks = int(cursor.tokens()[0])
    for _ in range(blocks):
        if cursor.binary:
            dim, tag, element_type = (cursor.integer(), cursor.integer(),
                                      cursor.integer())
            count = cursor.size()
            width = NODES_PER_TYPE.get(element_type)
            if width is None:
                raise Msh4Error(f'element type {element_type} is not known '
                                'to this reader')
            rows = cursor.sizes(count * (1 + width)).reshape(count, 1 + width)
            nodes = rows[:, 1:].astype(np.int64).tolist()
        else:
            row = cursor.tokens()
            dim, tag, element_type, count = (int(row[0]), int(row[1]),
                                             int(row[2]), int(row[3]))
            nodes = [[int(v) for v in cursor.tokens()[1:]]
                     for _ in range(count)]
        physical = entity_physical.get((dim, tag)) or [0]
        for tag_number in physical:
            for row_nodes in nodes:
                elements.append((element_type, int(tag_number), row_nodes))
    if cursor.binary:
        cursor.skip_binary_newline()
    cursor.line()                                             # $EndElements
