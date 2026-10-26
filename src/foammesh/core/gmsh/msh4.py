"""Reader of Gmsh MSH 4.x files, ASCII and binary (Plan 41 GN15), and of
the ASCII MSH 3.0 and 4.0 files the Export page writes (Plan 48 GM4).

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


#: Dimension of each Gmsh element type (``getElementProperties``, 4.15.2).
#: MSH 3.0 stores an element's entity tag only, so the dimension of the entity
#: comes from the element type.
DIM_PER_TYPE = {
    1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 3, 7: 3, 8: 1, 9: 2, 10: 2, 11: 3,
    12: 3, 13: 3, 14: 3, 15: 0, 16: 2, 17: 3, 18: 3, 19: 3, 20: 2, 21: 2,
    22: 2, 23: 2, 24: 2, 25: 2, 26: 1, 27: 1, 28: 1, 29: 3, 30: 3, 31: 3,
    32: 3, 33: 3, 34: 2, 35: 3, 36: 2, 37: 2, 38: 2, 39: 2, 40: 2, 41: 2,
    42: 2, 43: 2, 44: 2, 45: 2, 46: 2, 47: 2, 48: 2, 49: 2, 50: 2, 51: 2,
    52: 2, 53: 2, 54: 2, 55: 2, 56: 2, 57: 2, 58: 2, 59: 2, 60: 2, 61: 2,
    62: 1, 63: 1, 64: 1, 65: 1, 66: 1, 69: 2, 71: 3, 72: 3, 73: 3, 74: 3,
    75: 3, 79: 3, 80: 3, 81: 3, 82: 3, 83: 3, 84: 1, 85: 2, 86: 2, 87: 3,
    88: 3, 89: 3, 92: 3, 93: 3, 94: 3, 95: 3, 96: 3, 97: 3, 98: 3, 99: 3,
    100: 3, 101: 3, 102: 3, 103: 3, 104: 3, 105: 3, 118: 3, 119: 3, 120: 3,
    121: 3, 122: 3, 123: 3, 124: 3, 125: 3, 126: 3, 127: 3, 128: 3, 129: 3,
    130: 3, 131: 3, 132: 3, 133: 0, 134: 1, 135: 2, 136: 3, 137: 3,
}


class Msh4Error(ValueError):
    pass


def is_msh4(head: bytes) -> bool:
    """Whether the first bytes of a file are a 3.x or 4.x ``$MeshFormat`` header.

    The name is the module's; versions 3.0 and 4.0 are read here too (Plan 48
    GM4), because the Export page writes them and the viewport, the
    publisher and the fidelity check all open ``mesh.msh``.
    """
    lines = head.split(b'\n', 2)
    if len(lines) < 2 or lines[0].strip() != b'$MeshFormat':
        return False
    first = lines[1].split()[:1]
    return bool(first) and first[0].startswith((b'3', b'4'))


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
    version = 4.1

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
                if not fields[0].startswith(('3', '4')):
                    raise Msh4Error(f'this reader implements MSH 3 and 4; the '
                                    f'file declares {fields[0]}')
                version = float(fields[0])
                binary = fields[1] == '1'
                if binary and 4.0 <= version < 4.1:
                    raise Msh4Error(
                        f'a binary MSH {fields[0]} file is not read here: '
                        'Gmsh 4.15.2 does not write one (MSH 4.0 is ASCII '
                        'only); write ASCII or MSH 4.1')
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
                if version < 4.0:
                    _read_entities_30(cursor, entity_physical)
                elif version < 4.1:
                    _read_entities_40(cursor, entity_physical)
                else:
                    _read_entities(cursor, entity_physical)
            elif header == '$Nodes':
                if version < 4.0:
                    _read_nodes_30(cursor, node_ids, coordinates)
                elif version < 4.1:
                    _read_nodes_40(cursor, node_ids, coordinates)
                else:
                    _read_nodes(cursor, node_ids, coordinates)
            elif header == '$Elements':
                if version < 4.0:
                    _read_elements_30(cursor, entity_physical, elements)
                elif version < 4.1:
                    _read_elements_40(cursor, entity_physical, elements)
                else:
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


# -- MSH 4.0 and 3.0, ASCII (Plan 48 GM4) ---------------------------------------- #

def _read_entities_40(cursor: _Cursor, entity_physical: dict) -> None:
    """4.0: every entity row is ``tag, 6 box values, nPhysical, tags...``."""
    counts = [int(value) for value in cursor.tokens()]
    for dim, count in enumerate(counts):
        for _ in range(count):
            row = cursor.tokens()
            physical_count = int(row[7])
            entity_physical[(dim, int(row[0]))] = [
                int(v) for v in row[8:8 + physical_count]]
    cursor.line()                                              # $EndEntities


def _read_entities_30(cursor: _Cursor, entity_physical: dict) -> None:
    """3.0: a point is ``tag nPhys tags``; the others list their boundary
    first: ``tag nBounding ids... nPhys tags...``."""
    counts = [int(value) for value in cursor.tokens()]
    for dim, count in enumerate(counts):
        for _ in range(count):
            row = cursor.tokens()
            offset = 1 if dim == 0 else 2 + int(row[1])
            physical_count = int(row[offset])
            entity_physical[(dim, int(row[0]))] = [
                int(v) for v in row[offset + 1:offset + 1 + physical_count]]
    cursor.line()                                              # $EndEntities


def _read_nodes_40(cursor: _Cursor, node_ids: list,
                   coordinates: list) -> None:
    """4.0: ``tag dim parametric n`` then ``n`` lines of ``tag x y z``."""
    blocks = int(cursor.tokens()[0])
    for _ in range(blocks):
        row = cursor.tokens()
        dim, parametric, count = int(row[1]), int(row[2]), int(row[3])
        width = 4 + (dim if parametric else 0)
        rows = [cursor.tokens() for _ in range(count)]
        values = np.array([item[:width] for item in rows], dtype=np.float64
                          ).reshape(count, width)
        node_ids.append(values[:, 0].astype(np.int64))
        coordinates.append(values[:, 1:4])
    cursor.line()                                                # $EndNodes


def _read_nodes_30(cursor: _Cursor, node_ids: list,
                   coordinates: list) -> None:
    """3.0: ``n`` then ``tag x y z`` and one trailing value per line.

    Plan 48 FIX3 (DP-4112): binary 3.0 is ``n`` as a text line, then ``n``
    records of an int32 tag, three float64 coordinates and one int32 (32
    bytes, measured on a file Gmsh 4.15.2 wrote).
    """
    count = int(cursor.tokens()[0])
    if cursor.binary:
        # 32 bytes a node: the tag, three float64 and the one int32 value
        # the ASCII line also carries
        record = np.dtype([('tag', '<i4'), ('xyz', '<f8', (3,)),
                           ('extra', '<i4')])
        values = cursor.array(record, count)
        node_ids.append(values['tag'].astype(np.int64))
        coordinates.append(np.array(values['xyz'], dtype=np.float64))
        cursor.skip_binary_newline()
        cursor.line()                                            # $EndNodes
        return
    rows = [cursor.tokens()[:4] for _ in range(count)]
    values = np.array(rows, dtype=np.float64).reshape(count, 4)
    node_ids.append(values[:, 0].astype(np.int64))
    coordinates.append(values[:, 1:4])
    cursor.line()                                                # $EndNodes


def _read_elements_40(cursor: _Cursor, entity_physical: dict,
                      elements: list) -> None:
    """4.0: blocks of ``entityTag dim type n`` (4.1 swaps the first two)."""
    blocks = int(cursor.tokens()[0])
    for _ in range(blocks):
        row = cursor.tokens()
        tag, dim, element_type, count = (int(row[0]), int(row[1]),
                                         int(row[2]), int(row[3]))
        nodes = [[int(v) for v in cursor.tokens()[1:]] for _ in range(count)]
        physical = entity_physical.get((dim, tag)) or [0]
        for tag_number in physical:
            for row_nodes in nodes:
                elements.append((element_type, int(tag_number), row_nodes))
    cursor.line()                                             # $EndElements


def _read_elements_30(cursor: _Cursor, entity_physical: dict,
                      elements: list) -> None:
    """3.0: ``tag type entityTag nNodes nodes...`` (measured on Gmsh 4.15.2).

    Binary 3.0 (Plan 48 FIX3, DP-4112) is the same row as int32 values.
    """
    count = int(cursor.tokens()[0])
    if cursor.binary:
        for _ in range(count):
            head = cursor.array('<i4', 4)
            element_type, entity, node_count = (
                int(head[1]), int(head[2]), int(head[3]))
            nodes = [int(v) for v in cursor.array('<i4', node_count)]
            dim = DIM_PER_TYPE.get(element_type)
            if dim is None:
                raise Msh4Error(f'element type {element_type} is not known '
                                'to this reader')
            for tag_number in entity_physical.get((dim, entity)) or [0]:
                elements.append((element_type, int(tag_number), nodes))
        cursor.skip_binary_newline()
        cursor.line()                                         # $EndElements
        return
    for _ in range(count):
        row = [int(v) for v in cursor.tokens()]
        element_type, entity, node_count = row[1], row[2], row[3]
        nodes = row[4:4 + node_count]
        dim = DIM_PER_TYPE.get(element_type)
        if dim is None:
            raise Msh4Error(f'element type {element_type} is not known to '
                            'this reader')
        physical = entity_physical.get((dim, entity)) or [0]
        for tag_number in physical:
            elements.append((element_type, int(tag_number), nodes))
    cursor.line()                                             # $EndElements


def read_msh1(path: str | Path) -> tuple:
    """``(points, node_ids, elements, names)`` of an ASCII MSH 1 file.

    Plan 48 FIX3 (DP-4112). MSH 1 is ``$NOD`` (``n`` then ``id x y z``) and
    ``$ELM`` (``n`` then ``id type physical elementary nNodes nodes...``). The
    type numbers are the ones every later version uses. It has no physical
    names.
    """
    try:
        lines = Path(path).read_text(encoding='utf-8',
                                     errors='replace').splitlines()
    except OSError as error:
        raise Msh4Error(f'mesh file could not be read: {error}') from error
    node_ids: list = []
    coordinates: list = []
    elements: list = []
    seen_elements = False
    index = 0
    try:
        while index < len(lines):
            line = lines[index].strip()
            if line == '$NOD':
                count = int(lines[index + 1])
                rows = [lines[index + 2 + n].split()[:4] for n in range(count)]
                values = np.array(rows, dtype=np.float64).reshape(count, 4)
                node_ids = values[:, 0].astype(np.int64)
                coordinates = values[:, 1:4]
                index += 2 + count
            elif line == '$ELM':
                seen_elements = True
                count = int(lines[index + 1])
                for n in range(count):
                    row = [int(v) for v in lines[index + 2 + n].split()]
                    node_count = row[4]
                    elements.append((row[1], row[2], row[5:5 + node_count]))
                index += 2 + count
            else:
                index += 1
    except (ValueError, IndexError) as error:
        raise Msh4Error(f'mesh file could not be read as MSH 1: '
                        f'{error}') from error
    if len(node_ids) == 0:
        raise Msh4Error('mesh file contains no nodes')
    if not seen_elements:
        raise Msh4Error('MSH 1 file has no $ELM section')
    return (np.asarray(coordinates, dtype=np.float64).reshape(-1, 3),
            np.asarray(node_ids, dtype=np.int64), elements, {})
