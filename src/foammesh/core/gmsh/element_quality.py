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


# -- Plan 48 GM12: quality of a file with no case (R7) ------------------------ #
#
# A bare ``.msh`` (or an SU2 export) has no session hand-over and no polyMesh.
# Its quality is read by loading it into a headless Gmsh of the qualified
# runtime and asking ``getElementQualities`` for every measure, so the arrays
# are exactly the ones a session hand-over carries and every consumer above
# (statistics, the quality gate, the colouring) takes them unchanged.

#: Hand-over array -> the name ``getElementQualities`` takes (all of 4.15.2's).
QUALITY_ARRAYS = (
    ('gmshSICN', 'minSICN'), ('gmshSIGE', 'minSIGE'), ('gmshGamma', 'gamma'),
    ('gmshMinIsotropy', 'minIsotropy'), ('gmshMinEdge', 'minEdge'),
    ('gmshMaxEdge', 'maxEdge'), ('gmshVolume', 'volume'),
    ('gmshInnerRadius', 'innerRadius'), ('gmshOuterRadius', 'outerRadius'),
    ('gmshMinDetJac', 'minDetJac'), ('gmshMaxDetJac', 'maxDetJac'))

#: VTK cell type (the SU2 element codes) -> Gmsh element type.
SU2_TO_GMSH = {3: 1, 5: 2, 9: 3, 10: 4, 12: 5, 13: 6, 14: 7}

#: Run in the distro's python3 as ``-c <source> <job.json>``. The job names the
#: ``mesh`` to read, the ``dims`` to measure and the ``out`` folder; the worker
#: writes ``quality.npz`` and ``quality.json`` there and prints ``RESULT {}``.
MSH_QUALITY_WORKER = r'''
import json, sys
import numpy as np
import gmsh

KEYS = __KEYS__

def physical_table():
    table = {}
    for dim, tag in gmsh.model.getPhysicalGroups():
        for entity in gmsh.model.getEntitiesForPhysicalGroup(dim, tag):
            table.setdefault((dim, int(entity)), int(tag))
    return table

def partition_of(dim, entity):
    try:
        found = gmsh.model.getPartitions(dim, entity)
        return int(found[0]) if len(found) else 0
    except Exception:
        return 0

def main(job):
    gmsh.initialize([], readConfigFiles=False, run=False)
    gmsh.option.setNumber('General.Terminal', 0)
    out = {'error': None}
    try:
        gmsh.model.add('gm12_quality')
        gmsh.merge(job['mesh'])
        tags, coords, _ = gmsh.model.mesh.getNodes()
        tags = np.asarray(tags, dtype=np.int64)
        coords = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
        lookup = np.full(int(tags.max()) + 1 if len(tags) else 1, -1, np.int64)
        if len(tags):
            lookup[tags] = np.arange(len(tags))
        physical = physical_table()
        cols = {name: [] for name in (
            'gmshElementTag', 'gmshElementType', 'gmshEntityDim',
            'gmshEntityTag', 'gmshPhysical', 'gmshPartition')}
        for name, _key in KEYS:
            cols[name] = []
        centroids, orders = [], []
        conn = {}
        for dim in job.get('dims', [2, 3]):
            for _d, entity in gmsh.model.getEntities(dim):
                etypes, etags, enodes = gmsh.model.mesh.getElements(dim, entity)
                part = partition_of(dim, entity)
                for etype, el, nodes in zip(etypes, etags, enodes):
                    props = gmsh.model.mesh.getElementProperties(int(etype))
                    size = int(props[3])
                    el = np.asarray(el, dtype=np.int64)
                    nodes = np.asarray(nodes, dtype=np.int64).reshape(-1, size)
                    count = len(el)
                    centroids.append(coords[lookup[nodes]].mean(axis=1))
                    held = conn.setdefault(int(etype), [])
                    held.append((lookup[nodes][:, :int(props[5])], el,
                                 np.full(count, physical.get((dim, entity), 0), np.int32),
                                 np.full(count, dim, np.int32)))
                    orders.append(np.full(count, int(props[2]), np.int32))
                    cols['gmshElementTag'].append(el)
                    cols['gmshElementType'].append(np.full(count, int(etype), np.int32))
                    cols['gmshEntityDim'].append(np.full(count, dim, np.int32))
                    cols['gmshEntityTag'].append(np.full(count, entity, np.int32))
                    cols['gmshPhysical'].append(np.full(count, physical.get((dim, entity), 0), np.int32))
                    cols['gmshPartition'].append(np.full(count, part, np.int32))
                    for name, key in KEYS:
                        try:
                            values = gmsh.model.mesh.getElementQualities(el.tolist(), key)
                        except Exception:
                            values = [float('nan')] * count
                        cols[name].append(np.asarray(values, dtype=np.float64))
        arrays = {k: np.concatenate(v) for k, v in cols.items() if v}
        if centroids:
            arrays['centroids'] = np.concatenate(centroids)
            arrays['order'] = np.concatenate(orders)
        for etype, held in conn.items():
            arrays['nodes_%d' % etype] = np.concatenate([h[0] for h in held])
            arrays['tags_%d' % etype] = np.concatenate([h[1] for h in held])
            arrays['phys_%d' % etype] = np.concatenate([h[2] for h in held])
            arrays['dim_%d' % etype] = np.concatenate([h[3] for h in held])
        arrays['bounds'] = np.concatenate(
            [coords.min(axis=0), coords.max(axis=0)]) if len(coords) else np.zeros(6)
        np.savez(job['out'] + '/quality.npz', **arrays)
        names = {'%d:%d' % (d, t): gmsh.model.getPhysicalName(d, t)
                 for d, t in gmsh.model.getPhysicalGroups()}
        out.update(nodes=int(len(tags)), names=names,
                   version=gmsh.option.getString('General.Version'),
                   elements=int(sum(len(c) for c in cols['gmshElementTag'])))
    except Exception as error:
        out['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        gmsh.finalize()
    with open(job['out'] + '/quality.json', 'w') as handle:
        json.dump(out, handle)
    print('RESULT ' + json.dumps(out))

main(json.load(open(sys.argv[1])))
'''.replace('__KEYS__', repr(QUALITY_ARRAYS))


class QualityFileError(RuntimeError):
    """The file could not be measured; the sentence says why."""


def su2_to_msh(path, target) -> Path:
    """Write the SU2 mesh at *path* as a MSH 2.2 ASCII file Gmsh can read.

    Gmsh writes SU2 but does not read it, so an SU2 export is measured by
    handing the same cells to Gmsh in a format it does read. Markers become
    physical groups of the same name (tag = marker order from 1); the volume
    cells carry no physical group.
    """
    from foammesh.rendering.su2_reader import read_su2

    mesh = read_su2(path)
    lines = ['$MeshFormat', '2.2 0 8', '$EndMeshFormat']
    dim = int(mesh.dimension)
    names = list(mesh.markers)
    lines.append('$PhysicalNames')
    lines.append(str(len(names)))
    for index, name in enumerate(names, 1):
        lines.append('%d %d "%s"' % (dim - 1, index,
                                     str(name).replace('"', "'")))
    lines.append('$EndPhysicalNames')
    lines.append('$Nodes')
    lines.append(str(len(mesh.points)))
    for index, point in enumerate(mesh.points, 1):
        lines.append('%d %.17g %.17g %.17g' % (index, *map(float, point)))
    lines.append('$EndNodes')
    rows = []

    def add(cells, physical, entity):
        for number in range(cells.count):
            low = int(cells.offsets[number])
            high = int(cells.offsets[number + 1])
            nodes = cells.connectivity[low:high] + 1
            kind = SU2_TO_GMSH.get(int(cells.types[number]))
            if kind is None:
                raise QualityFileError(
                    'SU2 element code %d has no Gmsh type'
                    % int(cells.types[number]))
            rows.append((kind, physical, entity, nodes))

    add(mesh.cells, 0, 1)
    for index, name in enumerate(names, 1):
        add(mesh.markers[name], index, 100 + index)
    lines.append('$Elements')
    lines.append(str(len(rows)))
    for number, (kind, physical, entity, nodes) in enumerate(rows, 1):
        lines.append('%d %d 2 %d %d %s' % (
            number, kind, physical, entity,
            ' '.join(str(int(n)) for n in nodes)))
    lines.append('$EndElements')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines) + '\n', encoding='ascii')
    return target


def _profile(profile):
    if profile is not None:
        return profile
    from .launch_profiles import configured_profiles
    found = configured_profiles({})
    if not found:
        raise QualityFileError('no Gmsh runtime is configured on this host')
    return found[0]


def run_worker(source: str, job: dict, folder, *, profile=None,
               timeout: float = 3600.0) -> dict:
    """Run *source* in the qualified runtime with *job*; its ``RESULT`` dict.

    The job file is written in *folder* (a host folder with no space in its
    path); host paths inside the job must already be runtime paths.
    """
    import json
    import subprocess

    profile = _profile(profile)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    job_file = folder / 'job.json'
    job_file.write_text(json.dumps(job), encoding='utf-8')
    argv = profile.python_argv(source, profile.translate_host_path(job_file))
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualityFileError('the Gmsh worker could not run: %s' % error)
    for line in reversed((done.stdout or '').splitlines()):
        if line.startswith('RESULT '):
            try:
                return json.loads(line[7:])
            except ValueError:
                break
    raise QualityFileError('the Gmsh worker printed no result: %s' % (
        (done.stderr or done.stdout or '').strip()[-300:]))


def measure_file(path, *, profile=None, dims=(2, 3), work=None,
                 runner=None) -> dict:
    """The quality arrays of a mesh file, in the shape of a session hand-over.

    *path* is a ``.msh`` (any version Gmsh reads) or an SU2 mesh. The result
    is what :func:`read_handover_arrays` returns for ``mesh.vtu`` -- the
    Gmsh arrays, ``__centroids``, ``__nodes``, ``__bounds``, ``__names`` --
    plus ``__source`` (file, format, Gmsh version, element order), so
    :func:`statistics` and the quality gate take it unchanged. Nothing is
    filled when Gmsh cannot read the file: :class:`QualityFileError` carries
    the reason. *runner* replaces the worker call (tests).
    """
    import tempfile

    path = Path(path).resolve()
    if not path.is_file():
        raise QualityFileError('%s: no such file' % path.name)
    folder = (Path(work) if work else Path(tempfile.mkdtemp(prefix='gm12q'))
              ).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    source = path
    kind = 'su2' if path.suffix.lower() == '.su2' else 'msh'
    if kind == 'su2':
        source = su2_to_msh(path, folder / 'from_su2.msh')
    if runner is None:
        profile = _profile(profile)
        job = {'mesh': profile.translate_host_path(source),
               'dims': [int(d) for d in dims],
               'out': profile.translate_host_path(folder)}
        report = run_worker(MSH_QUALITY_WORKER, job, folder, profile=profile)
    else:
        report = runner(source, folder, dims)
    if report.get('error'):
        raise QualityFileError('%s: Gmsh could not read it: %s' % (
            path.name, report['error']))
    table = np.load(folder / 'quality.npz')
    held = ('nodes_', 'tags_', 'phys_', 'dim_')
    arrays = {name: np.array(table[name]) for name in table.files
              if name not in ('centroids', 'bounds', 'order')
              and not name.startswith(held)}
    connectivity = {}
    for name in table.files:
        if name.startswith('nodes_'):
            code = int(name[6:])
            connectivity[code] = {
                'nodes': np.array(table[name]),
                'tags': np.array(table['tags_%d' % code]),
                'physical': np.array(table['phys_%d' % code]),
                'dim': np.array(table['dim_%d' % code])}
    arrays['__connectivity'] = connectivity
    if 'gmshElementTag' not in arrays:
        raise QualityFileError('%s holds no elements of dimension %s' % (
            path.name, ', '.join(str(d) for d in dims)))
    arrays['__centroids'] = np.array(table['centroids'])
    arrays['__nodes'] = int(report.get('nodes', 0))
    arrays['__bounds'] = tuple(float(v) for v in table['bounds'])
    arrays['__names'] = dict(report.get('names') or {})
    order = np.array(table['order']) if 'order' in table.files else np.zeros(0)
    arrays['__source'] = {
        'file': str(path), 'format': kind,
        'gmsh_version': report.get('version', ''),
        'order': int(order.max()) if order.size else 1}
    return arrays


def file_quality(path, *, profile=None, dims=(2, 3), work=None,
                 runner=None) -> dict:
    """Statistics of a file: :func:`statistics` plus where the numbers came from."""
    arrays = measure_file(path, profile=profile, dims=dims, work=work,
                          runner=runner)
    out = statistics(arrays)
    out['source'] = dict(arrays['__source'])
    out['measured_by'] = 'Gmsh getElementQualities on the file'
    return out


#: Hand-over arrays a viewport colours by (`core.quality.cell_fields`).
VIEWPORT_ARRAYS = tuple(name for name, _key in QUALITY_ARRAYS)
#: The measures where a *low* value is the worst.
_WORST_IS_HIGH = ('gmshMaxEdge', 'gmshOuterRadius', 'gmshMaxDetJac')


def rank_worst(arrays: dict, count: int = 50, *, dimension=None) -> dict:
    """The worst *count* elements of every measure, as the ranking table shows.

    The same shape ``quality.cell_fields`` answers with for an OpenFOAM case
    (``{metric: [{'tag', 'cell', 'metric', 'value', 'centroid', 'size',
    'zone'}]}``), built from a file's own arrays so a bare ``.msh`` can be
    ranked without a case.  The highest-dimension elements are ranked unless
    *dimension* says otherwise.
    """
    dims = np.asarray(arrays.get('gmshEntityDim', ()))
    if not len(dims):
        return {}
    wanted = int(dims.max()) if dimension is None else int(dimension)
    rows = np.nonzero(dims == wanted)[0]
    tags = np.asarray(arrays['gmshElementTag'])[rows]
    centres = np.asarray(arrays.get('__centroids'))[rows]
    volume = np.abs(np.asarray(arrays.get('gmshVolume', np.zeros(len(dims)))))[rows]
    physical = np.asarray(arrays.get('gmshPhysical', np.zeros(len(dims))))[rows]
    names = arrays.get('__names') or {}
    out: dict = {}
    for name in VIEWPORT_ARRAYS:
        values = arrays.get(name)
        if values is None:
            continue
        values = np.asarray(values, dtype=np.float64)[rows]
        finite = np.nonzero(np.isfinite(values))[0]
        order = finite[np.argsort(
            -values[finite] if name in _WORST_IS_HIGH else values[finite],
            kind='stable')][:int(count)]
        out[name] = [
            {'tag': str(int(tags[i])), 'cell': int(tags[i]), 'metric': name,
             'value': float(values[i]),
             'centroid': [float(x) for x in centres[i]],
             'size': float(np.cbrt(volume[i])) if volume.size else 0.0,
             'zone': (str(names.get('%d:%d' % (wanted, int(physical[i])), '')
                          or '') if int(physical[i]) else '')}
            for i in order]
    return out


def attach_quality_by_tag(data_set, arrays: dict) -> dict:
    """Put a file's quality arrays on a viewport dataset, matched by element tag.

    The viewport reads a native ``.msh`` itself and carries each cell's Gmsh
    element tag (``gmshElementTag``); the arrays here come from Gmsh measuring
    the same file.  They are matched **by tag**, never by position.  A cell
    whose tag was not measured keeps NaN, and a dataset with no tags is
    refused: ``{'attached': 0, 'reason': ...}``.
    """
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy

    cell_data = data_set.GetCellData()
    held = cell_data.GetArray('gmshElementTag')
    if held is None:
        return {'attached': 0, 'missing': 0, 'reason':
                'The cells on screen carry no Gmsh element tag, so the '
                'measured qualities cannot be matched to them.'}
    wanted = np.asarray(vtk_to_numpy(held), dtype=np.int64)
    tags = np.asarray(arrays['gmshElementTag'], dtype=np.int64)
    order = np.argsort(tags, kind='stable')
    position = np.searchsorted(tags[order], wanted)
    position = np.clip(position, 0, max(len(tags) - 1, 0))
    found = (tags[order][position] == wanted) if len(tags) else np.zeros(
        len(wanted), bool)
    source = order[position]
    attached = 0
    for name in VIEWPORT_ARRAYS:
        values = arrays.get(name)
        if values is None:
            continue
        column = np.full(len(wanted), np.nan, dtype=np.float64)
        column[found] = np.asarray(values, dtype=np.float64)[source[found]]
        array = numpy_to_vtk(column, deep=True)
        array.SetName(name)
        if cell_data.GetArray(name) is not None:
            cell_data.RemoveArray(name)
        cell_data.AddArray(array)
        attached += 1
    data_set.Modified()
    return {'attached': attached, 'missing': int(np.count_nonzero(~found)),
            'reason': ''}
