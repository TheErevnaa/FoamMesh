#!/usr/bin/env python3
"""Build the size fields the user made, in any Gmsh runtime (Plan 41 GN6).

Shared by the batch runner and the resident session, so a field is built the
same way whichever of them meshes.  Input is the list of nodes the host
derives from the stored rows (``core/gmsh/field_catalogue.NativeFields``)::

    {"name": "wall", "type": "Distance", "enabled": true, "background": false,
     "numbers": [["Sampling", 40.0]], "strings": [], "lists": [["SurfacesList",
     [1, 2]]], "refs": [], "refLists": []}

A reference is the *name* of another node.  Nothing here knows what a
Threshold is: every option reaches Gmsh as written and is read back, so the
ledger can show requested against effective.

A ``PostView`` node may carry ``data`` (Plan 48 GM8, decision D6): the
reference of a ``.pos`` / ``.msh`` file with a dataset, a time step and a
unit.  ``build`` loads it (``load_size_data``) as a view of its own and gives
the field that view's tag, so the stored project never holds a view tag.

The preview samples a field where Gmsh itself asks for it.  Gmsh has no
function that evaluates a field at a point, but it calls the mesh-size
callback at every point it needs a size, with the size it has worked out from
the background field; a callback that records those and returns them
unchanged reads the field exactly as the mesher does.
"""
from __future__ import annotations

import os
import random
import tempfile

#: Combiners for the background, by the family of the fields combined.
COMBINERS = {'min': 'Min', 'max': 'Max'}
#: A size above this is Gmsh's "no value" (1e22).
NO_VALUE = 1.0e21


class BuildError(ValueError):
    """Gmsh rejected a field, or the nodes cannot be built."""


def build_order(nodes) -> list:
    """Enabled nodes, each after every node it reads."""
    live = {n['name']: n for n in nodes if n.get('enabled', True)}
    needs = {}
    for name, node in live.items():
        deps = {t for _o, t in node.get('refs') or ()}
        for _o, targets in node.get('refLists') or ():
            deps.update(targets)
        missing = sorted(d for d in deps if d not in live)
        if missing:
            raise BuildError(
                f'size field {name!r} reads {", ".join(missing)}, which is '
                'not an enabled field')
        needs[name] = deps
    order = []
    ready = sorted(n for n, deps in needs.items() if not deps)
    while ready:
        name = ready.pop(0)
        order.append(name)
        needs.pop(name)
        freed = []
        for other, deps in needs.items():
            if name in deps:
                deps.discard(name)
                if not deps:
                    freed.append(other)
        ready = sorted(ready + freed)
    if needs:
        raise BuildError('these size fields depend on each other in a cycle: '
                         + ', '.join(sorted(needs)))
    return [live[name] for name in order]


#: Element codes of a list-based view and their node counts.
LIST_NODES = {'P': 1, 'L': 2, 'T': 3, 'Q': 4, 'S': 4, 'H': 8, 'I': 6, 'Y': 5}
#: Prefix of the model that keeps a data file's own mesh (never meshed).
HIDDEN_PREFIX = '__sizeData_'
#: What the last build loaded; a new build replaces every field, so it also
#: takes that data away first (the resident session calls build repeatedly).
_MADE: dict = {'views': [], 'models': [], 'data': {}, 'tags': {}}


def _step_values(chunks, nodes, step, factor):
    """One time step of list data, coordinates and sizes in metres."""
    out = []
    for element in chunks:
        coords = 3 * nodes
        width = len(element)
        steps = (width - coords) // nodes
        if steps <= step:
            raise BuildError(
                f'the dataset has {steps} time '
                f"{'step' if steps == 1 else 'steps'}; step {step} is not "
                'one of them')
        first = coords + step * nodes
        out.extend(v * factor for v in element[:coords])
        out.extend(v * factor for v in element[first:first + nodes])
    return out


def load_size_data(gmsh, ref, *, check_overlap: bool = True) -> dict:
    """Load the dataset a stored reference names; returns the view and facts.

    ``ref`` is ``field_catalogue.size_data_node`` output.  The file is read
    into a view of its own: list-based data (the chosen time step, in metres)
    by default, so re-meshing or clearing the mesh cannot destroy it.  A
    ``.msh`` whose data is attached to its own mesh is first read in a hidden
    model and written back as list data; the ``hidden`` mode (and ``auto``
    for a file above the limit) keeps that model instead, which only holds
    time step 0 in metres.
    """
    path = ref['file']
    suffix = os.path.splitext(path)[1].lower()
    mode = ref.get('mode', 'auto')
    factor = float(ref.get('factor', 1.0))
    step = int(ref.get('step', 0))
    held = None
    current = None
    before = set(gmsh.view.getTags())
    hidden = None
    try:
        if suffix == '.msh':
            current = gmsh.model.getCurrent()
            hidden = HIDDEN_PREFIX + str(len(before) + 1)
            gmsh.model.add(hidden)
            gmsh.merge(path)
        else:
            gmsh.merge(path)
    except Exception as error:                        # noqa: BLE001
        _back(gmsh, current, hidden, drop=True)
        raise BuildError(f'size data {os.path.basename(path)!r} could not '
                         f'be read: {_reason(error)}') from error
    new = [t for t in gmsh.view.getTags() if t not in before]
    try:
        if not new:
            raise BuildError(f'size data {os.path.basename(path)!r} holds '
                             'no data view')
        chosen = _choose(gmsh, new, ref)
        keep_hidden = mode == 'hidden'
        if suffix == '.msh' and mode == 'auto':
            keep_hidden = _count_model_values(gmsh, chosen) \
                > int(ref.get('limit') or 4000000)
        if keep_hidden and suffix == '.msh':
            if step != 0 or factor != 1.0:
                raise BuildError(
                    'size data kept as its own mesh holds time step 0 in '
                    'metres; use the list mode for another step or unit')
            for tag in new:
                if tag != chosen:
                    gmsh.view.remove(tag)
            _back(gmsh, current, hidden, drop=False)
            held = hidden
            facts = {'view': chosen, 'mode': 'hidden', 'model': hidden}
            facts.update(_model_stats(gmsh, chosen))
            return facts
        if suffix == '.msh':
            temp = os.path.join(tempfile.mkdtemp(prefix='sizedata_'), 'd.pos')
            gmsh.view.write(chosen, temp)
            _back(gmsh, current, hidden, drop=True)
            hidden = None
            before2 = set(gmsh.view.getTags())
            gmsh.merge(temp)
            new = [t for t in gmsh.view.getTags() if t not in before2]
            chosen = new[0] if new else chosen
        facts = _to_list_view(gmsh, chosen, step, factor, ref)
        for tag in new:
            if tag != chosen and tag in gmsh.view.getTags():
                gmsh.view.remove(tag)
        if check_overlap:
            _check_overlap(gmsh, facts)
        facts['mode'] = 'list'
        return facts
    except BuildError:
        _back(gmsh, current, hidden, drop=held is None)
        for tag in new:
            if tag in gmsh.view.getTags():
                gmsh.view.remove(tag)
        raise
    except Exception as error:                        # noqa: BLE001
        _back(gmsh, current, hidden, drop=held is None)
        for tag in new:
            if tag in gmsh.view.getTags():
                gmsh.view.remove(tag)
        raise BuildError(f'size data {os.path.basename(path)!r} could not '
                         f'be used: {_reason(error)}') from error


def _back(gmsh, current, hidden, *, drop: bool) -> None:
    """Return to the project's model; forget the hidden one when told to."""
    if hidden is None:
        return
    try:
        if drop:
            gmsh.model.remove()
        if current is not None:
            gmsh.model.setCurrent(current)
    except Exception:                                 # noqa: BLE001
        pass


def _choose(gmsh, tags, ref) -> int:
    name = ref.get('dataset') or ''
    index = int(ref.get('datasetIndex', 0))
    labelled = [(t, gmsh.view.option.getString(t, 'Name')) for t in tags]
    if 0 <= index < len(labelled) and (not name
                                       or labelled[index][1] == name):
        return labelled[index][0]
    for tag, label in labelled:
        if name and label == name:
            return tag
    raise BuildError(
        f'size data has no dataset {name or index!r}; the file holds '
        + ', '.join(repr(label) for _t, label in labelled))


def _model_values(gmsh, tag):
    kind, entity, data, _time, comps = gmsh.view.getModelData(tag, 0)
    return kind, data, int(comps)


def _count_model_values(gmsh, tag) -> int:
    _kind, data, comps = _model_values(gmsh, tag)
    if comps != 1:
        raise BuildError('the dataset is not scalar; a size is one number '
                         'per point')
    return sum(len(d) for d in data)


def _model_stats(gmsh, tag) -> dict:
    _kind, data, _comps = _model_values(gmsh, tag)
    flat = [float(v) for chunk in data for v in chunk]
    if not flat:
        raise BuildError('the dataset holds no values')
    if min(flat) <= 0:
        raise BuildError('the sizes must be above 0; the dataset holds '
                         f'{min(flat):g}')
    return {'values': len(flat), 'min': min(flat), 'max': max(flat)}


def _to_list_view(gmsh, tag, step, factor, ref) -> dict:
    """Rebuild *tag* as a list-based view with one step, in metres."""
    types, counts, data = gmsh.view.getListData(tag)
    scalar = [(t, n, d) for t, n, d in zip(types, counts, data)
              if t[:1] == 'S' and n]
    if not scalar:
        raise BuildError('the dataset is not scalar; a size is one number '
                         'per point')
    name = gmsh.view.option.getString(tag, 'Name')
    view = gmsh.view.add(name or 'sizeData')
    low = [float('inf')] * 3
    high = [float('-inf')] * 3
    lo_v, hi_v, total = float('inf'), float('-inf'), 0
    for code, count, values in scalar:
        nodes = LIST_NODES.get(code[1:2])
        if not nodes:
            continue
        count = int(count)
        width = len(values) // count
        rows = [list(values[i * width:(i + 1) * width])
                for i in range(count)]
        flat = _step_values(rows, nodes, step, factor)
        per = 4 * nodes
        for i in range(count):
            base = i * per
            for axis in range(3):
                for n in range(nodes):
                    v = flat[base + axis * nodes + n]
                    low[axis] = min(low[axis], v)
                    high[axis] = max(high[axis], v)
            for n in range(nodes):
                v = flat[base + 3 * nodes + n]
                lo_v, hi_v = min(lo_v, v), max(hi_v, v)
                total += 1
        gmsh.view.addListData(view, code, count, flat)
    gmsh.view.remove(tag)
    if not total:
        gmsh.view.remove(view)
        raise BuildError('the dataset holds no values')
    if lo_v <= 0:
        gmsh.view.remove(view)
        raise BuildError('the sizes must be above 0; the dataset holds '
                         f'{lo_v:g} after the unit is applied')
    return {'view': view, 'values': total, 'min': lo_v, 'max': hi_v,
            'bboxLow': low, 'bboxHigh': high}


def _check_overlap(gmsh, facts) -> None:
    """Refuse data that cannot touch the model (usually a wrong unit)."""
    try:
        box = gmsh.model.getBoundingBox(-1, -1)
    except Exception:                                 # noqa: BLE001
        return
    low, high = box[:3], box[3:]
    if all(h <= l for l, h in zip(low, high)):
        return
    for axis in range(3):
        if facts['bboxHigh'][axis] < low[axis] or \
                facts['bboxLow'][axis] > high[axis]:
            raise BuildError(
                'the size data does not overlap the model: its extent '
                f"{[round(v, 6) for v in facts['bboxLow']]} to "
                f"{[round(v, 6) for v in facts['bboxHigh']]} metres, the "
                f'model {[round(v, 6) for v in low]} to '
                f'{[round(v, 6) for v in high]}; check the unit')


def build(gmsh, nodes) -> dict:
    """Create the fields.  Returns tags, the background tags and a ledger.

    ``applied`` has one entry per option set: ``(field, option, requested,
    effective)`` where *effective* is what Gmsh reads back.  ``views`` lists
    the views and ``models`` the hidden models that loaded size data made;
    ``data`` the facts of each (``remove_data`` takes them away again).
    """
    field = gmsh.model.mesh.field
    tags: dict = {}
    applied: list = []
    views: list = []
    models: list = []
    data: dict = {}
    order = build_order(nodes)
    remove_data(gmsh, _MADE['views'], _MADE['models'])
    _MADE['views'], _MADE['models'] = views, models
    _MADE['data'] = data
    _MADE['tags'] = tags
    try:
        return _build(gmsh, nodes, order, tags, applied, views, models, data)
    except Exception:
        remove_data(gmsh, views, models)
        _MADE['views'], _MADE['models'] = [], []
        raise


def _build(gmsh, nodes, order, tags, applied, views, models, data) -> dict:
    field = gmsh.model.mesh.field
    for node in order:
        name = node['name']
        try:
            tag = field.add(node['type'])
        except Exception as error:                    # noqa: BLE001
            raise BuildError(
                f'size field {name!r}: Gmsh has no field type '
                f'{node["type"]!r} ({_reason(error)})') from error
        tags[name] = tag
        try:
            if node.get('data'):
                loaded = load_size_data(gmsh, node['data'])
                views.append(loaded['view'])
                if loaded.get('model'):
                    models.append(loaded['model'])
                data[name] = loaded
                field.setNumber(tag, 'ViewTag', float(loaded['view']))
                applied.append((name, 'ViewTag', node['data'].get(
                    'dataset') or node['data'].get('file'),
                    int(field.getNumber(tag, 'ViewTag'))))
            for option, value in node.get('numbers') or ():
                field.setNumber(tag, option, float(value))
                applied.append((name, option, float(value),
                                field.getNumber(tag, option)))
            for option, value in node.get('strings') or ():
                field.setString(tag, option, str(value))
                applied.append((name, option, str(value),
                                field.getString(tag, option)))
            for option, values in node.get('lists') or ():
                field.setNumbers(tag, option, [float(v) for v in values])
                applied.append((name, option, [int(v) for v in values],
                                [int(v) for v in field.getNumbers(
                                    tag, option)]))
            for option, values in node.get('reals') or ():
                field.setNumbers(tag, option, [float(v) for v in values])
                applied.append((name, option, [float(v) for v in values],
                                [float(v) for v in field.getNumbers(
                                    tag, option)]))
            for option, target in node.get('refs') or ():
                field.setNumber(tag, option, float(tags[target]))
                applied.append((name, option, target,
                                int(field.getNumber(tag, option))))
            for option, targets in node.get('refLists') or ():
                field.setNumbers(tag, option,
                                 [float(tags[t]) for t in targets])
                applied.append((name, option, list(targets),
                                [int(v) for v in field.getNumbers(
                                    tag, option)]))
        except Exception as error:                    # noqa: BLE001
            raise BuildError(
                f'size field {name!r} ({node["type"]}) was rejected by '
                f'Gmsh: {_reason(error)}') from error
    background = [tags[n['name']] for n in nodes
                  if n.get('enabled', True) and n.get('background')
                  and n['type'] != 'BoundaryLayer']
    layer = [tags[n['name']] for n in nodes
             if n.get('enabled', True) and n.get('background')
             and n['type'] == 'BoundaryLayer']
    return {'tags': tags, 'background': background, 'layer': layer,
            'applied': applied, 'views': views, 'models': models,
            'data': data,
            'anisotropic': [tags[n['name']] for n in nodes
                            if n.get('enabled', True)
                            and n.get('background')
                            and n['type'] in ANISOTROPIC]}


ANISOTROPIC = frozenset({'AttractorAnisoCurve', 'MathEvalAniso', 'MinAniso',
                         'IntersectAniso'})


def set_background(gmsh, tags, *, combiner: str = 'min',
                   anisotropic: bool = False) -> int | None:
    """Make the given fields the one background mesh.  Returns its tag."""
    if not tags:
        return None
    field = gmsh.model.mesh.field
    if len(tags) == 1 and not anisotropic:
        chosen = tags[0]
    elif len(tags) == 1:
        chosen = tags[0]
    else:
        kind = 'MinAniso' if anisotropic else COMBINERS.get(
            str(combiner).lower(), 'Min')
        chosen = field.add(kind)
        field.setNumbers(chosen, 'FieldsList', [float(t) for t in tags])
    field.setAsBackgroundMesh(chosen)
    return chosen


def remove(gmsh, tags) -> None:
    field = gmsh.model.mesh.field
    made = set((_MADE.get('tags') or {}).values())
    for tag in tags:
        try:
            field.remove(tag)
        except Exception:                             # noqa: BLE001
            pass
    if made and made & set(tags):
        # The fields of the last build are going: so is the data they read.
        remove_data(gmsh, _MADE['views'], _MADE['models'])
        _MADE.update(views=[], models=[], data={}, tags={})


def last_tags() -> dict:
    """Field name -> Gmsh tag of the last build (the session resolves names)."""
    return dict(_MADE.get('tags') or {})


def last_data() -> dict:
    """Facts of the size data the last build loaded, by field name."""
    return dict(_MADE.get('data') or {})


def remove_data(gmsh, views=(), models=()) -> None:
    """Take away the views and hidden models that loaded size data made."""
    for tag in views:
        try:
            gmsh.view.remove(tag)
        except Exception:                             # noqa: BLE001
            pass
    if not models:
        return
    try:
        current = gmsh.model.getCurrent()
    except Exception:                                 # noqa: BLE001
        return
    for name in models:
        try:
            gmsh.model.setCurrent(name)
            gmsh.model.remove()
        except Exception:                             # noqa: BLE001
            pass
    try:
        gmsh.model.setCurrent(current)
    except Exception:                                 # noqa: BLE001
        pass


def preview(gmsh, *, dim: int = 2, limit: int = 20000, seed: int = 1) -> dict:
    """Sample the background field where Gmsh asks for a size.

    The model must have no mesh (the preview meshes curves, and surfaces for
    ``dim`` 2, with the field alone deciding the size).  Returns
    ``{"points": [[x, y, z], ...], "sizes": [...], "asked": n, "error": ""}``.
    The mesh is cleared again, and every option touched is put back.
    """
    option = gmsh.option
    names = ('Mesh.MeshSizeFromPoints', 'Mesh.MeshSizeExtendFromBoundary',
             'Mesh.MeshSizeFromCurvature', 'Mesh.MeshSizeMin',
             'Mesh.MeshSizeMax', 'Mesh.MeshSizeFactor')
    saved = {name: option.getNumber(name) for name in names}
    rng = random.Random(seed)
    points: list = []
    sizes: list = []
    asked = [0]

    def record(_dim, _tag, x, y, z, size):
        asked[0] += 1
        if size < NO_VALUE:
            if len(points) < limit:
                points.append((x, y, z))
                sizes.append(size)
            else:
                slot = rng.randrange(asked[0])
                if slot < limit:
                    points[slot] = (x, y, z)
                    sizes[slot] = size
        return size

    error = ''
    try:
        option.setNumber('Mesh.MeshSizeFromPoints', 0)
        option.setNumber('Mesh.MeshSizeExtendFromBoundary', 0)
        option.setNumber('Mesh.MeshSizeFromCurvature', 0)
        option.setNumber('Mesh.MeshSizeMin', 0)
        option.setNumber('Mesh.MeshSizeMax', 1.0e22)
        option.setNumber('Mesh.MeshSizeFactor', 1)
        gmsh.model.mesh.setSizeCallback(record)
        try:
            gmsh.model.mesh.generate(max(1, min(int(dim), 2)))
        except Exception as caught:                   # noqa: BLE001
            error = _reason(caught)
    finally:
        try:
            gmsh.model.mesh.removeSizeCallback()
        except Exception:                             # noqa: BLE001
            pass
        for name, value in saved.items():
            option.setNumber(name, value)
        try:
            gmsh.model.mesh.clear()
        except Exception:                             # noqa: BLE001
            pass
    return {'points': [list(p) for p in points], 'sizes': list(sizes),
            'asked': asked[0], 'error': error}


def preview_field(gmsh, tag, *, dim: int = 2, limit: int = 20000,
                  seed: int = 1) -> dict:
    """Sample one field alone: make it the background, sample, report.

    The caller puts its own background back (``session._compose_background``).
    A boundary-layer field is not a size and a metric (anisotropic) field has
    no single size; the host does not offer them, and this refuses them.
    """
    kind = gmsh.model.mesh.field.getType(int(tag))
    if kind == 'BoundaryLayer' or kind in ANISOTROPIC:
        raise BuildError(f'a {kind} field has no single size to show')
    gmsh.model.mesh.field.setAsBackgroundMesh(int(tag))
    result = preview(gmsh, dim=dim, limit=limit, seed=seed)
    result['field'] = int(tag)
    result['type'] = kind
    return result


def mesh_exists(gmsh) -> bool:
    nodes, _coords, _par = gmsh.model.mesh.getNodes()
    return len(nodes) > 0


def _reason(error) -> str:
    text = str(error).strip().splitlines()
    return text[-1][:200] if text else error.__class__.__name__
