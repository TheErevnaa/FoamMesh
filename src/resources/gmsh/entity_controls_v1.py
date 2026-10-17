#!/usr/bin/env python3
"""Apply the per-entity meshing controls in any Gmsh runtime (Plan 41 GN9).

Shared by the batch runner and the resident session.  Input is the list the
host derives from the stored rows (``core/gmsh/entity_controls``)::

    {"name": "baffle", "kind": "embed", "dim": 2, "tags": [7], "enabled": true,
     "params": [["in_dim", 3.0], ["in_tag", 1.0]]}

Every call reaches Gmsh as written; where Gmsh has a getter the value is read
back (``getEmbedded``, the two compound options), and where it has none the
ledger says so.  The geometry-side measurement after meshing (triangles and
quadrangles on a surface) is ``families``.
"""
from __future__ import annotations

#: Gmsh element type numbers.
TRIANGLE, QUADRANGLE = 2, 3


class BuildError(ValueError):
    """Gmsh rejected a control, or an entity it names is not in the model."""


def _dimtag_words(dim: int) -> str:
    return ('point', 'curve', 'surface', 'volume')[dim]


def _params(op) -> dict:
    return {n: v for n, v in op.get('params') or ()}


def _check_entities(gmsh, ops) -> None:
    present = {(int(d), int(t)) for d, t in gmsh.model.getEntities()}
    for op in ops:
        if not op.get('enabled', True):
            continue
        dim, params = int(op['dim']), _params(op)
        wanted = [(dim, int(t)) for t in op['tags']]
        if op['kind'] == 'embed':
            wanted.append((int(params['in_dim']), int(params['in_tag'])))
        missing = [d for d in wanted if d not in present]
        if missing:
            raise BuildError(
                f'entity control {op["name"]!r}: the model has no '
                + ', '.join(f'{_dimtag_words(d)} {t}' for d, t in missing)
                + ' (the entities come from the geometry as it was imported '
                'or built)')


def apply(gmsh, ops) -> dict:
    """Set every enabled control.  Returns ``applied``: one entry per call,
    ``(name, option, requested, effective)``."""
    ops = [dict(op) for op in ops or ()]
    _check_entities(gmsh, ops)
    mesh = gmsh.model.mesh
    applied: list = []
    for op in ops:
        if not op.get('enabled', True):
            continue
        name, kind, dim = op['name'], op['kind'], int(op['dim'])
        tags = [int(t) for t in op['tags']]
        params = _params(op)
        try:
            if kind == 'embed':
                in_dim, in_tag = int(params['in_dim']), int(params['in_tag'])
                mesh.embed(dim, tags, in_dim, in_tag)
                have = {(int(d), int(t))
                        for d, t in mesh.getEmbedded(in_dim, in_tag)}
                got = [t for t in tags if (dim, t) in have]
                if len(got) != len(tags):
                    raise BuildError(
                        f'entity control {name!r}: Gmsh did not keep '
                        f'{_dimtag_words(dim)} '
                        + ', '.join(str(t) for t in tags if t not in got)
                        + f' embedded in {_dimtag_words(in_dim)} {in_tag}')
                applied.append((name, 'embed', f'{tags} in {in_dim}:{in_tag}',
                                f'embedded {sorted(got)}'))
            elif kind == 'recombine':
                for tag in tags:
                    mesh.setRecombine(2, tag, float(params['angle']))
                applied.append((name, 'recombine',
                                f'{tags} angle {params["angle"]:g}',
                                'set (Gmsh has no getter)'))
            elif kind == 'algorithm':
                for tag in tags:
                    mesh.setAlgorithm(2, tag, int(params['value']))
                applied.append((name, 'algorithm',
                                f'{tags} = {int(params["value"])}',
                                'set (Gmsh has no getter)'))
            elif kind == 'smoothing':
                for tag in tags:
                    mesh.setSmoothing(2, tag, int(params['steps']))
                applied.append((name, 'smoothing',
                                f'{tags} = {int(params["steps"])}',
                                'set (Gmsh has no getter)'))
            elif kind == 'sizeFromBoundary':
                for tag in tags:
                    mesh.setSizeFromBoundary(2, tag, int(params['on']))
                applied.append((name, 'sizeFromBoundary',
                                f'{tags} = {int(params["on"])}',
                                'set (Gmsh has no getter)'))
            elif kind == 'reverse':
                for tag in tags:
                    mesh.setReverse(dim, tag, True)
                applied.append((name, 'reverse', f'{_dimtag_words(dim)} {tags}',
                                'set (Gmsh has no getter)'))
            elif kind == 'outwardOrientation':
                for tag in tags:
                    mesh.setOutwardOrientation(tag)
                applied.append((name, 'outwardOrientation', f'volume {tags}',
                                'set (Gmsh has no getter)'))
            elif kind == 'compound':
                for option, key in (('Mesh.CompoundClassify', 'classify'),
                                    ('Mesh.CompoundMeshSizeFactor',
                                     'size_factor')):
                    wanted = float(params[key])
                    gmsh.option.setNumber(option, wanted)
                    applied.append((name, option, wanted,
                                    gmsh.option.getNumber(option)))
                mesh.setCompound(dim, tags)
                applied.append((name, 'compound',
                                f'{_dimtag_words(dim)} {tags}',
                                'set (Gmsh has no getter)'))
            else:
                raise BuildError(f'entity control {name!r}: unknown kind '
                                 f'{kind!r}')
        except BuildError:
            raise
        except Exception as error:                      # noqa: BLE001
            raise BuildError(
                f'entity control {name!r} ({kind}): Gmsh refused: {error}'
            ) from error
    return {'applied': applied, 'count': len([o for o in ops
                                              if o.get('enabled', True)])}


def families(gmsh, dim: int, tag: int) -> dict:
    """Elements on one entity by family, read from the mesh."""
    types, _tags, _nodes = gmsh.model.mesh.getElements(dim, tag)
    counts = {int(t): len(x) for t, x in zip(types, _tags)}
    return {'triangles': counts.get(TRIANGLE, 0),
            'quadrangles': counts.get(QUADRANGLE, 0),
            'other': sum(n for t, n in counts.items()
                         if t not in (TRIANGLE, QUADRANGLE))}


def physical_cells(gmsh, dim: int, physical_tag: int) -> int:
    """Elements of a physical group, summed over its entities."""
    total = 0
    for tag in gmsh.model.getEntitiesForPhysicalGroup(dim, physical_tag):
        _t, elements, _n = gmsh.model.mesh.getElements(dim, int(tag))
        total += sum(len(x) for x in elements)
    return total
