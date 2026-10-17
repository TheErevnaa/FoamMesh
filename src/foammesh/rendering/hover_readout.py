"""What the cell or face under the cursor is, as a few lines (Plan 39 VI15, I2).

The viewport's 180 ms hover pick used to highlight a part and name it. The
readout says what is under the cursor: for a cell of a volume its id, zone,
refinement level, the four quality values and its size; for a face of a
boundary patch the patch's name and type, and the deviation when the patch is
painted by one. It reads the arrays already on the picked dataset, so a value
shown is the value the colouring and the worst-cell list use; nothing is
measured again.

Pure: it takes a ``vtkDataSet``, a cell id and a few optional facts, and
returns text and a dictionary. The view decides when to ask.
"""
from __future__ import annotations

import math

from foammesh.core.mesh import cell_inspect

#: Cell-data arrays the readout knows, in the order it prints them.
LEVEL_ARRAY = 'cellLevel'
ZONE_ARRAY = 'cellZone'
ZONE_NAMES = 'cellZoneNames'
ORIGINAL_ID_ARRAYS = ('vtkOriginalCellIds', 'cellId', 'cellIds')
#: (array, label, format)
QUALITY_ROWS = (
    ('nonOrthoAngle', 'Non-orthogonality', '{:.1f}°'),
    ('skewness', 'Skewness', '{:.3g}'),
    ('cellAspectRatio', 'Aspect ratio', '{:.3g}'),
    ('cellVolume', 'Volume', '{:.4g}'),
    ('gmshSICN', 'SICN', '{:.3g}'),
    ('gmshSIGE', 'SIGE', '{:.3g}'),
    ('gmshGamma', 'Gamma', '{:.3g}'),
    ('gmshMinIsotropy', 'Isotropy', '{:.3g}'),
    ('gmshMinDetJac', 'Min Jacobian', '{:.3g}'),
    ('gmshVolume', 'Volume', '{:.4g}'),
)
_VOLUME_ARRAYS = ('cellVolume', 'gmshVolume')


def _value(array, index):
    if array is None or index < 0 or index >= array.GetNumberOfTuples():
        return None
    value = array.GetTuple1(index) if array.GetNumberOfComponents() == 1 else None
    return value


def cell_fields(data, cell: int) -> dict:
    """The raw values of one cell: ``{array name: value}`` for what is there."""
    out: dict = {}
    if data is None or cell < 0 or cell >= data.GetNumberOfCells():
        return out
    cells = data.GetCellData()
    for name in (LEVEL_ARRAY, ZONE_ARRAY) + tuple(
            row[0] for row in QUALITY_ROWS):
        array = cells.GetArray(name)
        value = _value(array, cell)
        if value is not None:
            out[name] = value
    return out


def original_id(data, cell: int) -> int:
    """The cell's id in the mesh: its stored original id, else its index."""
    cells = data.GetCellData()
    for name in ORIGINAL_ID_ARRAYS:
        value = _value(cells.GetArray(name), cell)
        if value is not None:
            return int(value)
    return int(cell)


def zone_name(data, value) -> str:
    """The name of a cell-zone code, or the code when there is no name."""
    labels = data.GetFieldData().GetAbstractArray(ZONE_NAMES)
    code = int(value)
    if labels is not None and 0 <= code < labels.GetNumberOfValues():
        return str(labels.GetValue(code))
    return str(code) if code >= 0 else ''


def cell_size(data, cell: int, fields: dict) -> float:
    """A length for the cell: the cube root of its volume, else its extent."""
    for name in _VOLUME_ARRAYS:
        if name in fields:
            return float(abs(fields[name])) ** (1.0 / 3.0)
    bounds = data.GetCell(cell).GetBounds()
    return math.sqrt(sum((bounds[i + 1] - bounds[i]) ** 2 for i in (0, 2, 4)))


def is_volume_cell(fields: dict) -> bool:
    """Whether the picked cell carries volume-mesh arrays."""
    return any(name in fields for name in (
        LEVEL_ARRAY, ZONE_ARRAY, 'nonOrthoAngle', 'skewness',
        'cellAspectRatio', 'cellVolume', 'gmshSICN', 'gmshVolume'))


def centre(data, cell: int):
    """The mean of a cell's points, or ``None``."""
    if data is None or cell < 0 or cell >= data.GetNumberOfCells():
        return None
    points = data.GetCell(cell).GetPoints()
    count = points.GetNumberOfPoints() if points is not None else 0
    if count <= 0:
        return None
    total = [0.0, 0.0, 0.0]
    for index in range(count):
        point = points.GetPoint(index)
        for axis in range(3):
            total[axis] += point[axis]
    return tuple(value / count for value in total)


def _centre_line(point) -> str:
    return 'Centre: ({0:.4g}, {1:.4g}, {2:.4g})'.format(*point)


def volume_cell(picked, cell: int, volume):
    """The volume cell a picked cell of a cut stands for.

    VI14 (hover). A cut or clipped volume is drawn as the surface of what
    survived the cut, and that surface carries only the arrays the volume had
    when it was cut -- often none. When the volume is at hand and the picked
    cell remembers the volume cell it came from (``vtkOriginalCellIds``), the
    answer is ``(cell id in the volume, volume)``; otherwise ``None``.
    """
    if volume is None or picked is None:
        return None
    array = picked.GetCellData().GetArray(ORIGINAL_ID_ARRAYS[0])
    value = _value(array, cell)
    if value is None:
        return None
    index = int(value)
    if index < 0 or index >= volume.GetNumberOfCells():
        return None
    return index


def _element_lines(data, cell: int, name: str, fields: dict) -> list[str]:
    """A cell of the Gmsh session's mesh (Plan 41 GN17, A-13).

    Gmsh's Inspect element lists tag, type, nodes and quality; the hover says
    the same from the arrays the hand-over carries, with the patch's name when
    the cell is drawn as one.
    """
    found = cell_inspect.describe_cell(data, cell)
    lines = []
    if name and name != 'internalMesh' and not name.endswith('/internalMesh'):
        lines.append('Patch {0}'.format(name))
    lines.append('Element {0:,} ({1})'.format(found['tag'], found['type']))
    lines.append('Entity: dimension {0}, tag {1}'.format(
        found['dimension'], found['entity']))
    if found['physical']:
        lines.append('Physical group: {0}'.format(found['physical']))
    if found['partition']:
        lines.append('Partition: {0}'.format(found['partition']))
    if found['nodes']:
        lines.append('Nodes: {0}'.format(', '.join(
            str(node) for node in found['nodes'])))
    point = centre(data, cell)
    if point is not None:
        lines.append(_centre_line(point))
    for array, label, pattern in QUALITY_ROWS:
        if array.startswith('gmsh') and array in fields:
            lines.append('{0}: {1}'.format(label, pattern.format(fields[array])))
    return lines


def describe(data, cell: int, name: str = '', *, patch_type: str = '',
             deviation_array: str | None = None,
             deviation_unit: str = '', volume=None) -> list[str]:
    """The readout's lines for one picked cell or face. Empty if nothing.

    ``volume`` is the complete volume the picked surface was cut from; with
    it, a cell picked on a cut says its volume id, centre and every cell
    array the volume carries even when the cut surface dropped them.
    """
    if data is None or cell < 0 or cell >= data.GetNumberOfCells():
        return []
    fields = cell_fields(data, cell)
    lines: list[str] = []
    if cell_inspect.has_identity(data):
        return _element_lines(data, cell, name, fields)
    source = volume_cell(data, cell, volume)
    if source is not None:
        # The volume's own values win: they are the cell's, not a copy of
        # whatever the cut kept.
        fields = {**fields, **cell_fields(volume, source)}
        lines.append('Cell {0:,}'.format(source))
        point = centre(volume, source)
        if point is not None:
            lines.append(_centre_line(point))
        if ZONE_ARRAY in fields:
            zone = zone_name(volume, fields[ZONE_ARRAY])
            if zone:
                lines.append('Zone: {0}'.format(zone))
        if LEVEL_ARRAY in fields:
            lines.append('Level: {0:d}'.format(int(fields[LEVEL_ARRAY])))
        for array, label, pattern in QUALITY_ROWS:
            if array in fields:
                lines.append('{0}: {1}'.format(label, pattern.format(fields[array])))
        lines.append('Size: {0:.4g}'.format(cell_size(volume, source, fields)))
        return lines
    if is_volume_cell(fields):
        lines.append('Cell {0:,}'.format(original_id(data, cell)))
        point = centre(data, cell)
        if point is not None:
            lines.append(_centre_line(point))
        if ZONE_ARRAY in fields:
            zone = zone_name(data, fields[ZONE_ARRAY])
            if zone:
                lines.append('Zone: {0}'.format(zone))
        if LEVEL_ARRAY in fields:
            lines.append('Level: {0:d}'.format(int(fields[LEVEL_ARRAY])))
        for array, label, pattern in QUALITY_ROWS:
            if array in fields:
                lines.append('{0}: {1}'.format(label, pattern.format(fields[array])))
        lines.append('Size: {0:.4g}'.format(cell_size(data, cell, fields)))
        return lines
    if not name:
        return []
    if name == 'internalMesh' or name.endswith('/internalMesh'):
        # The surface of a cut volume that remembers nothing about the volume
        # is not a patch; naming it one is a wrong answer.
        return []
    lines.append('Patch {0}'.format(name))
    lines.append('Face {0:,} of the patch'.format(original_id(data, cell)))
    point = centre(data, cell)
    if point is not None:
        lines.append(_centre_line(point))
    if patch_type:
        lines.append('Type: {0}'.format(patch_type))
    if deviation_array:
        value = _value(data.GetCellData().GetArray(deviation_array), cell)
        if value is not None:
            lines.append('Deviation: ' + (
                'not measured' if value != value
                else '{0:+.4g}{1}'.format(value, ' ' + deviation_unit
                                          if deviation_unit else '')))
    return lines
