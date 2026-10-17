"""The Python and ``.geo`` script of the model in the session (Plan 41 GN19).

The resident session writes every Gmsh API call that changes the model to
``script.calls.jsonl`` (``session_v1._Recorder``): the literal call, its
arguments and what it returned.  This module turns that list into

* a Python script that rebuilds the same model in bare Gmsh, call for call
  (:func:`to_python`), and
* a Gmsh ``.geo`` script (:func:`to_geo`) for the calls that have a ``.geo``
  statement.  A call with none is kept as a comment and the script says it is
  not complete (``Script.rebuildable`` is ``False``); the Python script is
  always complete unless a call carried more data than the file keeps.

Nothing here depends on the target solver or on Gmsh being installed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from foammesh.core.quantities import agreeing, count_text

CALLS_NAME = 'script.calls.jsonl'


@dataclass
class Script:
    text: str
    #: True when the script rebuilds the whole model.
    rebuildable: bool = True
    #: One sentence for each call the script could not carry.
    gaps: list = field(default_factory=list)
    calls: int = 0


def calls_path(case_path: str | Path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / CALLS_NAME


def read_calls(case_path: str | Path) -> list[dict]:
    path = calls_path(case_path)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'f' in value:
            rows.append(value)
    return rows


# -- Python ------------------------------------------------------------------ #

_PRELUDE = '''\
"""Rebuilds the FoamMesh Gmsh session in bare Gmsh (Gmsh 4.15.2 Python API).

Written by FoamMesh from the Gmsh calls the session made.  Run it with
``python this_file.py``; files it reads keep the paths the session used.
"""
import gmsh

gmsh.initialize(['', '-noenv'], readConfigFiles=False)
gmsh.option.setNumber('General.Terminal', 1)
gmsh.model.add('foammesh')


def _geo_text(text):
    import os, tempfile
    handle, path = tempfile.mkstemp(suffix='.geo')
    with os.fdopen(handle, 'w', encoding='utf-8') as out:
        out.write(text)
    try:
        gmsh.merge(path)
    finally:
        os.remove(path)

'''


def _literal(value) -> str:
    return repr(value)


def _call_text(row: dict) -> str:
    args = [_literal(a) for a in row.get('a', [])]
    args += ['%s=%s' % (k, _literal(v)) for k, v in
             (row.get('k') or {}).items()]
    return 'gmsh.%s(%s)' % (row['f'], ', '.join(args))


def to_python(calls: list, *, write: str = '') -> Script:
    """The calls as a Python script; *write* adds a final ``gmsh.write``."""
    lines = [_PRELUDE]
    gaps = []
    for row in calls:
        if row.get('omitted'):
            gaps.append('%s carried %d bytes of data, more than the call '
                        'file keeps' % (row['f'], row['omitted']))
            lines.append('# NOT REBUILT: %s (its arguments were too large '
                         'to keep)' % row['f'])
        elif row['f'] == '#geo':
            lines.append('_geo_text(%s)' % _literal(row['a'][0]))
        else:
            lines.append(_call_text(row))
    if write:
        lines.append('')
        lines.append('gmsh.write(%s)' % _literal(write))
    lines.append('')
    lines.append('if __name__ == "__main__":')
    lines.append('    pass  # add gmsh.fltk.run() here to open the Gmsh window')
    lines.append('gmsh.finalize()')
    return Script('\n'.join(lines) + '\n', not gaps, gaps, len(calls))


# -- .geo -------------------------------------------------------------------- #

_KIND = {0: 'Point', 1: 'Curve', 2: 'Surface', 3: 'Volume'}


def _num(value) -> str:
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _list(values) -> str:
    return '{' + ', '.join(_num(v) for v in values) + '}'


def _dimtags(pairs) -> str:
    """``Volume{1, 2}; Surface{5};`` for ``[(3,1),(3,2),(2,5)]``."""
    by_dim: dict = {}
    for pair in pairs or []:
        by_dim.setdefault(int(pair[0]), []).append(int(pair[1]))
    return ' '.join('%s{%s};' % (_KIND[d], ', '.join(str(t) for t in tags))
                    for d, tags in sorted(by_dim.items()))


def _arg(row: dict, index: int, name: str, default=None):
    args = row.get('a', [])
    if len(args) > index:
        return args[index]
    return (row.get('k') or {}).get(name, default)


def _tag(row: dict, index: int) -> int:
    """The tag a creation call used: the argument, else what Gmsh returned."""
    given = _arg(row, index, 'tag', -1)
    if isinstance(given, int) and given > 0:
        return given
    result = row.get('r')
    return result if isinstance(result, int) else 0


def _quote(text) -> str:
    return '"%s"' % str(text).replace('\\', '\\\\').replace('"', '\\"')


def _point(row):
    return 'Point(%d) = {%s, %s, %s, %s};' % (
        _tag(row, 4), _num(_arg(row, 0, 'x', 0)), _num(_arg(row, 1, 'y', 0)),
        _num(_arg(row, 2, 'z', 0)), _num(_arg(row, 3, 'meshSize', 0.0)))


def _line(row):
    return 'Line(%d) = {%d, %d};' % (
        _tag(row, 2), _arg(row, 0, 'startTag'), _arg(row, 1, 'endTag'))


def _arc(row):
    return 'Circle(%d) = {%d, %d, %d};' % (
        _tag(row, 3), _arg(row, 0, 'startTag'), _arg(row, 1, 'centerTag'),
        _arg(row, 2, 'endTag'))


def _listed(word):
    def build(row):
        return '%s(%d) = %s;' % (word, _tag(row, 1), _list(_arg(row, 0, 'x', [])))
    return build


#: name -> (.geo word, ((index, kwarg) of each value), (tag index, kwarg))
_SOLIDS = {
    'addBox': ('Box', tuple(enumerate(('x', 'y', 'z', 'dx', 'dy', 'dz'))), 6),
    'addSphere': ('Sphere', ((0, 'xc'), (1, 'yc'), (2, 'zc'), (3, 'radius'),
                             (5, 'angle1'), (6, 'angle2'), (7, 'angle3')), 4),
    'addCylinder': ('Cylinder', tuple(enumerate((
        'x', 'y', 'z', 'dx', 'dy', 'dz', 'r'))) + ((8, 'angle'),), 7),
    'addCone': ('Cone', tuple(enumerate((
        'x', 'y', 'z', 'dx', 'dy', 'dz', 'r1', 'r2'))) + ((9, 'angle'),), 8),
    'addTorus': ('Torus', tuple(enumerate((
        'x', 'y', 'z', 'r1', 'r2'))) + ((6, 'angle'),), 5),
    'addWedge': ('Wedge', tuple(enumerate((
        'x', 'y', 'z', 'dx', 'dy', 'dz'))) + ((7, 'ltx'),), 6),
    'addRectangle': ('Rectangle', tuple(enumerate((
        'x', 'y', 'z', 'dx', 'dy'))) + ((6, 'roundedRadius'),), 5),
    'addDisk': ('Disk', tuple(enumerate((
        'xc', 'yc', 'zc', 'rx', 'ry'))), 5),
}
_SIMPLE = {'addPoint': _point, 'addLine': _line, 'addCircleArc': _arc,
           'addSpline': _listed('Spline'), 'addBSpline': _listed('BSpline'),
           'addBezier': _listed('Bezier'),
           'addCurveLoop': _listed('Curve Loop'),
           'addWire': _listed('Wire'),
           'addPlaneSurface': _listed('Plane Surface'),
           'addSurfaceLoop': _listed('Surface Loop'),
           'addVolume': _listed('Volume')}
_BOOLEANS = {'cut': 'BooleanDifference', 'fuse': 'BooleanUnion',
             'intersect': 'BooleanIntersection',
             'fragment': 'BooleanFragments'}


def _creation(name: str, row: dict) -> str:
    if name in _SIMPLE:
        return _SIMPLE[name](row)
    word, values, tag_index = _SOLIDS[name]
    given = []
    for index, kwarg in values:
        value = _arg(row, index, kwarg, None)
        if value is not None:
            given.append(value)
    return '%s(%d) = %s;' % (word, _tag(row, tag_index), _list(given))


def to_geo(calls: list) -> Script:
    """The calls as a ``.geo`` script; calls without a statement are comments."""
    out = ['// Written by FoamMesh from the Gmsh calls the session made.',
           '// Open it in Gmsh, or in FoamMesh with Reload script.', '']
    gaps = []
    names = {}                          # (dim, tag) -> name set later
    for row in calls:
        if row['f'] == 'model.setPhysicalName' and len(row.get('a', [])) >= 3:
            names[(int(row['a'][0]), int(row['a'][1]))] = row['a'][2]
    factory = ''

    def use(kernel):
        nonlocal factory
        if kernel != factory:
            out.append('SetFactory(%s);' % _quote(
                'OpenCASCADE' if kernel == 'occ' else 'Built-in'))
            factory = kernel

    def gap(row, why='no .geo statement'):
        gaps.append('%s: %s' % (row['f'], why))
        out.append('// NOT IN THIS SCRIPT (%s): %s' % (why, _call_text(row)))

    for row in calls:
        f = row['f']
        a = row.get('a', [])
        if row.get('omitted'):
            gap(row, 'its arguments were too large to keep')
            continue
        if f == '#geo':
            out.append(a[0].rstrip())
            continue
        parts = f.split('.')
        last = parts[-1]
        kernel = parts[1] if len(parts) > 2 and parts[0] == 'model' and \
            parts[1] in ('occ', 'geo') else ''
        if f in ('option.setNumber', 'option.setColor'):
            if f == 'option.setNumber':
                out.append('%s = %s;' % (a[0], _num(a[1])))
            else:
                gap(row)
        elif f == 'option.setString':
            out.append('%s = %s;' % (a[0], _quote(a[1])))
        elif f == 'model.add':
            out.append('// model %s' % a[0] if a else '// model')
        elif f == 'clear':
            out.append('Delete All;')
            factory = ''
        elif f == 'merge':
            out.append('Merge %s;' % _quote(a[0]))
        elif kernel and last in ('synchronize',):
            continue
        elif kernel and last == 'importShapes':
            use('occ')
            out.append('Merge %s;' % _quote(a[0]))
        elif kernel and (last in _SIMPLE or last in _SOLIDS):
            use(kernel)
            out.append(_creation(last, row))
        elif kernel == 'occ' and last in _BOOLEANS:
            use('occ')
            objects, tools = a[0], a[1]
            remove_object = _arg(row, 3, 'removeObject', True)
            remove_tool = _arg(row, 4, 'removeTool', True)
            out.append('%s{ %s%s }{ %s%s }' % (
                _BOOLEANS[last], _dimtags(objects),
                ' Delete;' if remove_object else '',
                _dimtags(tools), ' Delete;' if remove_tool else ''))
        elif kernel and last == 'translate':
            use(kernel)
            out.append('Translate {%s, %s, %s} { %s }' % (
                _num(a[1]), _num(a[2]), _num(a[3]), _dimtags(a[0])))
        elif kernel and last == 'rotate':
            use(kernel)
            out.append('Rotate {{%s, %s, %s}, {%s, %s, %s}, %s} { %s }' % (
                _num(a[4]), _num(a[5]), _num(a[6]), _num(a[1]), _num(a[2]),
                _num(a[3]), _num(a[7]), _dimtags(a[0])))
        elif kernel and last == 'dilate':
            use(kernel)
            out.append('Dilate {{%s, %s, %s}, {%s, %s, %s}} { %s }' % (
                _num(a[1]), _num(a[2]), _num(a[3]), _num(a[4]), _num(a[5]),
                _num(a[6]), _dimtags(a[0])))
        elif kernel and last == 'remove':
            use(kernel)
            recursive = _arg(row, 1, 'recursive', False)
            out.append('%sDelete { %s }' % (
                'Recursive ' if recursive else '', _dimtags(a[0])))
        elif f == 'model.addPhysicalGroup':
            dim, tags = int(a[0]), a[1]
            tag = _arg(row, 2, 'tag', -1)
            tag = tag if isinstance(tag, int) and tag > 0 else (
                row.get('r') or 0)
            name = _arg(row, 3, 'name', '') or names.get((dim, tag), '')
            out.append('Physical %s(%s) = %s;' % (
                _KIND[dim], (_quote(name) + ', ' + str(tag)) if name else tag,
                _list(tags)))
        elif f == 'model.setPhysicalName':
            continue                    # folded into addPhysicalGroup
        elif f == 'model.mesh.setSize':
            points = [t for d, t in a[0] if d == 0]
            if points and len(points) == len(a[0]):
                out.append('MeshSize {%s} = %s;' % (
                    ', '.join(str(t) for t in points), _num(a[1])))
            else:
                gap(row, 'sizes only at points')
        elif f == 'model.mesh.setTransfiniteCurve':
            kind = _arg(row, 2, 'meshType', 'Progression') or 'Progression'
            coef = _arg(row, 3, 'coef', 1.0)
            out.append('Transfinite Curve {%s} = %d Using %s %s;' % (
                a[0], a[1], kind, _num(coef)))
        elif f == 'model.mesh.setTransfiniteSurface':
            corners = _arg(row, 2, 'cornerTags', [])
            arrangement = _arg(row, 1, 'arrangement', 'Left') or 'Left'
            out.append('Transfinite Surface {%s} = %s %s;' % (
                a[0], _list(corners), arrangement))
        elif f == 'model.mesh.setTransfiniteVolume':
            corners = _arg(row, 1, 'cornerTags', [])
            out.append('Transfinite Volume {%s} = %s;' % (
                a[0], _list(corners)))
        elif f == 'model.mesh.setRecombine' and int(a[0]) == 2:
            angle = _arg(row, 2, 'angle', 45.0)
            out.append('Recombine Surface {%s} = %s;' % (a[1], _num(angle)))
        elif f == 'model.mesh.setAlgorithm' and int(a[0]) == 2:
            out.append('MeshAlgorithm Surface {%s} = %s;' % (a[1], _num(a[2])))
        elif f == 'model.mesh.setSmoothing' and int(a[0]) == 2:
            out.append('Smoothing Surface {%s} = %s;' % (a[1], _num(a[2])))
        elif f == 'model.mesh.field.add':
            out.append('Field[%d] = %s;' % (_arg(row, 1, 'tag', 0) if
                                           _arg(row, 1, 'tag', 0) > 0 else
                                           row.get('r', 0), a[0]))
        elif f == 'model.mesh.field.setNumber':
            out.append('Field[%d].%s = %s;' % (a[0], a[1], _num(a[2])))
        elif f == 'model.mesh.field.setString':
            out.append('Field[%d].%s = %s;' % (a[0], a[1], _quote(a[2])))
        elif f == 'model.mesh.field.setNumbers':
            out.append('Field[%d].%s = %s;' % (a[0], a[1], _list(a[2])))
        elif f == 'model.mesh.field.setAsBackgroundMesh':
            out.append('Background Field = %d;' % a[0])
        elif f == 'model.mesh.field.setAsBoundaryLayer':
            out.append('BoundaryLayer Field = %d;' % a[0])
        elif f == 'model.mesh.generate':
            out.append('Mesh %d;' % int(_arg(row, 0, 'dim', 3)))
        elif f == 'model.mesh.refine':
            out.append('RefineMesh;')
        elif f == 'model.mesh.recombine':
            out.append('RecombineMesh;')
        elif f == 'model.mesh.setOrder':
            out.append('SetOrder %d;' % int(a[0]))
        elif f == 'model.mesh.optimize':
            out.append('OptimizeMesh %s;' % _quote(a[0]))
        elif f == 'model.mesh.partition':
            out.append('PartitionMesh %d;' % int(a[0]))
        else:
            gap(row)
    out.append('')
    if gaps:
        count = len(gaps)
        out.insert(2, f'// This script is NOT complete: '
                      f'{count_text(count, "call")} '
                      f'{agreeing(count, "has", "have")} no .geo '
                      f'statement and {agreeing(count, "is", "are")} '
                      f'{agreeing(count, "a comment", "comments")} below.')
    return Script('\n'.join(out) + '\n', not gaps, gaps, len(calls))


# -- files ------------------------------------------------------------------- #

def save_text(path: str | Path, text: str) -> str:
    """Write *text* to *path*, making the folder; the path as a string."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    return str(target)


def model_geo_path(case_path: str | Path) -> Path:
    """Where an edited ``.geo`` of the model is kept for Reload script."""
    return Path(case_path) / 'foammesh' / 'gmsh' / 'model.geo'
