"""Gmsh's per-element numbers, keyed to the cells the viewport draws.

Plan 41 GN14 (section 4.3, T-06).  The session's hand-over (``mesh.vtu``)
carries one value per element for every measure ``getElementQualities``
accepts, plus the element's Gmsh tag, type and entity.  This module reads those
arrays back as plain numpy, and from them builds

* the **statistics** Gmsh's own Statistics window shows (nodes, elements by
  type, min / avg / max of each measure, a histogram to plot), and
* the inputs of the quality gate (:func:`foammesh.core.gmsh.quality.
  assess_arrays`), which therefore judges every element rather than the
  capped list ``result.json`` carries.

No Qt: a VTK reader and numpy.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

#: Hand-over array -> (name Gmsh knows it by, label shown to the user).
MEASURES = (
    ('gmshSICN', 'minSICN', 'SICN'),
    ('gmshSIGE', 'minSIGE', 'SIGE'),
    ('gmshGamma', 'gamma', 'Gamma'),
    ('gmshMinIsotropy', 'minIsotropy', 'Isotropy'),
    ('gmshMinDetJac', 'minDetJac', 'Smallest Jacobian'),
    ('gmshMaxDetJac', 'maxDetJac', 'Largest Jacobian'),
    ('gmshVolume', 'volume', 'Volume'),
)
#: The three Gmsh's Statistics window and ``session.measure`` report.
STATISTICS_MEASURES = ('minSICN', 'minSIGE', 'gamma')
#: Measures whose range is 0..1 and so histogram on that range.
UNIT_RANGE = ('minSICN', 'minSIGE', 'gamma', 'minIsotropy', 'disto')

#: Gmsh element type -> the name ``getElementProperties`` gives it.
ELEMENT_TYPE_NAMES = {
    1: 'Line 2', 2: 'Triangle 3', 3: 'Quadrangle 4', 4: 'Tetrahedron 4',
    5: 'Hexahedron 8', 6: 'Prism 6', 7: 'Pyramid 5', 8: 'Line 3',
    9: 'Triangle 6', 10: 'Quadrangle 9', 11: 'Tetrahedron 10',
    12: 'Hexahedron 27', 13: 'Prism 18', 14: 'Pyramid 14', 15: 'Point 1',
    16: 'Quadrangle 8', 17: 'Hexahedron 20', 18: 'Prism 15',
    19: 'Pyramid 13', 21: 'Triangle 10', 23: 'Triangle 15',
    25: 'Triangle 21', 26: 'Line 4', 27: 'Line 5', 28: 'Line 6',
}
#: Names the statistics lists in this order, as Gmsh's window does.
FAMILIES = ('Point', 'Line', 'Triangle', 'Quadrangle', 'Tetrahedron',
            'Hexahedron', 'Prism', 'Pyramid')

HISTOGRAM_BINS = 20


def element_type_name(code) -> str:
    return ELEMENT_TYPE_NAMES.get(int(code), f'type {int(code)}')


def family_of(name: str) -> str:
    return str(name).split(' ')[0]


class HandoverError(ValueError):
    pass


def read_handover_arrays(path) -> dict:
    """Every cell array of ``mesh.vtu`` as numpy, plus centroids and the node count.

    The returned mapping holds the arrays by their hand-over name, and two
    entries that are not arrays of the file: ``'__centroids'`` (one point per
    cell) and ``'__nodes'`` (the node count).
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkFiltersCore import vtkCellCenters
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    path = Path(path)
    if not path.is_file():
        raise HandoverError(f'{path.name}: the session left no mesh to read')
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    if grid is None or grid.GetNumberOfCells() < 1:
        raise HandoverError(f'{path.name} holds no elements')
    data = grid.GetCellData()
    arrays = {}
    for index in range(data.GetNumberOfArrays()):
        array = data.GetArray(index)
        if array is not None and array.GetName():
            arrays[array.GetName()] = np.array(vtk_to_numpy(array))
    centers = vtkCellCenters()
    centers.SetInputData(grid)
    centers.Update()
    arrays['__centroids'] = np.array(
        vtk_to_numpy(centers.GetOutput().GetPoints().GetData()))
    arrays['__nodes'] = int(grid.GetNumberOfPoints())
    arrays['__bounds'] = tuple(float(item) for item in grid.GetBounds())
    arrays['__names'] = _group_names(path)
    return arrays


def _group_names(path: Path) -> dict:
    """``{'2:5': 'inlet'}`` from the ``mesh.names.json`` beside the file."""
    import json

    try:
        text = (path.parent / 'mesh.names.json').read_text(encoding='utf-8')
        return dict(json.loads(text).get('physical') or {})
    except (OSError, ValueError):
        return {}


def mesh_info(arrays: dict) -> dict:
    """The Mesh info dialog's facts for a mesh that has no polyMesh.

    ``bounds`` is ``{'min', 'max', 'span'}`` over the nodes; ``groups`` lists
    every physical group as ``{'name', 'dim', 'kind', 'elements'}``, kind
    ``patch`` for surface groups and ``zone`` for volume groups, the groups
    of no name appearing as ``surface<tag>`` / ``volume<tag>`` as the viewport
    names them.
    """
    out: dict = {'bounds': None, 'groups': []}
    box = arrays.get('__bounds')
    if box is not None and len(box) == 6:
        low = [float(box[0]), float(box[2]), float(box[4])]
        high = [float(box[1]), float(box[3]), float(box[5])]
        out['bounds'] = {'min': low, 'max': high,
                         'span': [h - l for l, h in zip(low, high)]}
    dims = np.asarray(arrays.get('gmshEntityDim', ()))
    physical = np.asarray(arrays.get('gmshPhysical', ()))
    names = arrays.get('__names') or {}
    for dim, kind, fallback in ((2, 'patch', 'surface'), (3, 'zone', 'volume')):
        mask = dims == dim
        for tag in sorted({int(item) for item in np.unique(physical[mask])}
                          if len(physical) else ()):
            if not tag:
                continue
            label = str(names.get(f'{dim}:{tag}', '') or '').strip()                 or f'{fallback}{tag}'
            out['groups'].append({
                'name': label, 'dim': dim, 'kind': kind,
                'elements': int(np.count_nonzero(mask & (physical == tag)))})
    return out


def _spread(values) -> dict:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if not len(values):
        return {}
    return {'min': float(values.min()), 'avg': float(values.mean()),
            'max': float(values.max())}


def _with_disto(arrays: dict) -> dict:
    """The arrays plus ``disto``, when both Jacobian arrays are present."""
    low, high = arrays.get('gmshMinDetJac'), arrays.get('gmshMaxDetJac')
    if low is None or high is None or 'disto' in arrays:
        return arrays
    from .quality import disto_from_jacobians

    return dict(arrays, disto=disto_from_jacobians(low, high))


def histogram(values, name: str, bins: int = HISTOGRAM_BINS) -> dict:
    """Counts of *values* over equal bins (0..1 for the unit-range measures)."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if not len(values):
        return {'edges': [], 'counts': []}
    if name in UNIT_RANGE:
        low, high = min(0.0, float(values.min())), max(1.0, float(values.max()))
    else:
        low, high = float(values.min()), float(values.max())
    if high <= low:
        high = low + 1.0
    counts, edges = np.histogram(values, bins=bins, range=(low, high))
    return {'edges': [float(item) for item in edges],
            'counts': [int(item) for item in counts]}


def statistics(arrays: dict, passes=()) -> dict:
    """Gmsh's Statistics window, from the hand-over's arrays.

    ``counts`` names every element type present (``Tetrahedron 4``) as
    ``session.measure`` does; ``quality[dim]`` holds the element count and the
    min / avg / max of each measure over the elements of that dimension;
    ``histograms[dim][measure]`` is the plot.  *passes* is the stage timeline
    (``[{'title', 'seconds'}]``), reported as it was run.
    """
    arrays = _with_disto(arrays)
    types = np.asarray(arrays.get('gmshElementType', ()))
    dims = np.asarray(arrays.get('gmshEntityDim', ()))
    counts: dict = {}
    for code in np.unique(types):
        counts[element_type_name(code)] = int(np.count_nonzero(types == code))
    by_family = {family: 0 for family in FAMILIES}
    for name, count in counts.items():
        by_family[family_of(name)] = by_family.get(family_of(name), 0) + count

    quality: dict = {}
    plots: dict = {}
    names = [(key, gmsh, label) for key, gmsh, label in MEASURES]
    if 'disto' in arrays:
        names.append(('disto', 'disto', 'Distortion'))
    for dim in sorted({int(item) for item in np.unique(dims)}):
        if dim < 2:
            continue
        mask = dims == dim
        block = {'elements': int(np.count_nonzero(mask))}
        plot = {}
        for key, gmsh, _label in names:
            if key not in arrays:
                continue
            values = np.asarray(arrays[key])[mask]
            spread = _spread(values)
            if spread:
                block[gmsh] = spread
                plot[gmsh] = histogram(values, gmsh)
        quality[str(dim)] = block
        plots[str(dim)] = plot
    info = mesh_info(arrays)
    return {
        'bounds': info['bounds'],
        'groups': info['groups'],
        'nodes': int(arrays.get('__nodes', 0) or 0),
        'counts': counts,
        'families': by_family,
        'elements': int(len(types)),
        'quality': quality,
        'histograms': plots,
        'passes': [dict(item) for item in passes or ()],
    }


def labels() -> dict:
    """Gmsh measure name -> the label the statistics table uses."""
    table = {gmsh: label for _key, gmsh, label in MEASURES}
    table['disto'] = 'Distortion'
    return table
