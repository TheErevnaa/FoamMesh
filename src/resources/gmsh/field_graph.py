#!/usr/bin/env python3
"""The Gmsh size fields a plan asks for, as an explicit dependency graph.

Plan 31 CP-08 item 1. Every size-field row expands to more than one Gmsh
field: a ``distance_threshold`` row is a Distance feeding a Threshold, a
``restrict`` row is a Constant feeding a Restrict, a ``curvature`` row is a
Distance feeding a Curvature feeding a Threshold. That wiring used to be
written inline in the runner, tag by tag, and nothing checked it. Two failure
modes followed, and both were measured on this worktree with Gmsh 4.15.2:

* MEASURED a ``restrict`` row changed nothing at all. The Constant it pointed
  at was given ``VIn`` and no entity list, and a Gmsh Constant field with an
  empty list returns ``VOut`` -- which defaults to 1e22 -- at every point. The
  Restrict clipped a field that was already infinite, so the row was inert.
  On a unit box a Restrict carrying 0.05 on one face took the surface from 14
  triangles to 942 once the Constant was given a value the field could return;
  before that the mesh was byte-identical to one with no field at all.
* a field whose input never got created, or two fields pointing at each other,
  would have reached Gmsh as a job that meshes something nobody asked for.

So the wiring is built here as a graph and checked before a single Gmsh field
exists: every reference must name a node that is in the graph, the references
must not form a cycle, and every node must reach an output. The runner then
walks the graph in dependency order and does no wiring of its own.

Pure Python, no Gmsh: it sits beside the runner so the host and the runtime
build the same graph from the same code, the way ``shell_topology`` does.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

CALCULATION_VERSION = 'gmsh.field_graph.v1'


class FieldGraphError(ValueError):
    """The requested fields cannot be wired into a mesh size."""


@dataclass(frozen=True)
class FieldNode:
    """One Gmsh field, and what it needs before it can be created.

    ``inputs`` maps a Gmsh option to the node that must exist first --
    ``InField`` for a Threshold, ``FieldsList`` for a combiner. Those are the
    edges of the graph; everything else is a number, a string, or a scope the
    runner resolves against the geometry it imported.
    """

    node_id: str
    #: The authored row this came from, so a refusal can name what the user
    #: typed rather than a Gmsh tag.
    row: str
    #: The Gmsh field type, exactly as ``mesh.field.add`` spells it.
    kind: str
    numbers: tuple = ()
    strings: tuple = ()
    #: ``(option, node_id)`` -- a single-field reference.
    inputs: tuple = ()
    #: ``(option, (node_id, ...))`` -- a list-of-fields reference.
    input_lists: tuple = ()
    #: ``(option, dimension, token, explicit_tags)``. ``dimension`` is
    #: ``'surfaces'`` or ``'volumes'``; ``explicit_tags`` are tags the row
    #: named itself, which need no prepared scope.
    scopes: tuple = ()
    #: True for the node that carries the row's size, i.e. the one the
    #: background combiner reads. A row's other nodes feed this one.
    output: bool = False
    #: A node the run cannot proceed without a resolved scope for.
    requires_scope: bool = False


@dataclass(frozen=True)
class FieldGraph:
    nodes: tuple = ()
    #: Node ids in an order where every reference is already built.
    order: tuple = ()
    #: The output node of each row, in row order.
    outputs: tuple = ()
    calculation_version: str = CALCULATION_VERSION

    def by_id(self) -> dict:
        return {node.node_id: node for node in self.nodes}

    def to_dict(self) -> dict:
        return {
            'nodes': [
                {'nodeId': node.node_id, 'row': node.row, 'kind': node.kind,
                 'inputs': [list(item) for item in node.inputs],
                 'inputLists': [[option, list(ids)]
                                for option, ids in node.input_lists],
                 'scopes': [[option, dimension, token, list(tags)]
                            for option, dimension, token, tags in node.scopes],
                 'output': node.output}
                for node in self.nodes],
            'order': list(self.order),
            'outputs': list(self.outputs),
            'calculation_version': self.calculation_version,
        }


def _references(node: FieldNode):
    for _option, target in node.inputs:
        yield target
    for _option, targets in node.input_lists:
        for target in targets:
            yield target


def validate_graph(nodes) -> tuple:
    """Reject missing references and cycles, and return a build order.

    Generic on purpose. The expansions below are acyclic by construction, but
    a graph is only worth calling a graph if something checks it, and this is
    the check that runs on every job -- including one whose rows a later
    change wires differently.
    """
    nodes = tuple(nodes)
    index: dict = {}
    for node in nodes:
        if node.node_id in index:
            raise FieldGraphError(
                f'two size fields are both called {node.node_id!r}; each field '
                'in the graph needs its own name')
        index[node.node_id] = node

    for node in nodes:
        for target in _references(node):
            if target not in index:
                raise FieldGraphError(
                    f'size field {node.row!r} reads a field {target!r} that is '
                    'not in this plan')
            if target == node.node_id:
                raise FieldGraphError(
                    f'size field {node.row!r} reads itself')

    # Kahn's algorithm: what is left when nothing can be built next is exactly
    # the cycle, so the refusal can name the rows involved.
    remaining = {node.node_id: set(_references(node)) for node in nodes}
    order: list = []
    ready = sorted(key for key, needs in remaining.items() if not needs)
    while ready:
        key = ready.pop(0)
        order.append(key)
        remaining.pop(key)
        freed = []
        for other, needs in remaining.items():
            if key in needs:
                needs.discard(key)
                if not needs:
                    freed.append(other)
        ready = sorted(ready + freed)
    if remaining:
        involved = sorted({index[key].row for key in remaining})
        raise FieldGraphError(
            'these size fields depend on each other in a cycle, so none of '
            'them can be built: ' + ', '.join(involved))

    # An orphan is a field Gmsh would create and never read: harmless to the
    # mesh, but it means the plan said something the mesh does not carry, and
    # this package exists because that kind of silence was being called
    # evidence.
    read_by: set = set()
    for node in nodes:
        read_by.update(_references(node))
    orphans = [node.node_id for node in nodes
               if not node.output and node.node_id not in read_by]
    if orphans:
        raise FieldGraphError(
            'these size fields feed nothing and would never affect the mesh: '
            + ', '.join(sorted(orphans)))
    return tuple(order)


# --------------------------------------------------------------------------- #
# Row -> nodes
# --------------------------------------------------------------------------- #

def _number(row, key, default=0.0):
    try:
        return float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _triple(row, key, default=(0.0, 0.0, 0.0)):
    value = row.get(key)
    if isinstance(value, dict):
        return (float(value.get('x', default[0])),
                float(value.get('y', default[1])),
                float(value.get('z', default[2])))
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(float(item) for item in value)
    return tuple(float(item) for item in default)


def _explicit(row):
    return tuple(int(tag) for tag in (row.get('surfaces') or ()))


def _distance_node(node_id, name, row):
    return FieldNode(
        node_id=node_id, row=name, kind='Distance',
        numbers=(('Sampling', float(int(_number(row, 'sampling', 20) or 20))),),
        scopes=(('SurfacesList', 'surfaces', str(row.get('scopeToken') or ''),
                 _explicit(row)),),
        requires_scope=True)


def _row_nodes(row, index):
    """The Gmsh fields one authored row becomes."""
    name = str(row.get('name') or f'field-{index}')
    kind = str(row.get('fieldType') or '')
    base = str(row.get('controlId') or index)
    prefix = f'{base}:{name}'

    if kind == 'distance_threshold':
        distance = f'{prefix}/distance'
        return (
            _distance_node(distance, name, row),
            FieldNode(
                node_id=f'{prefix}/threshold', row=name, kind='Threshold',
                numbers=(('SizeMin', _number(row, 'sizeInside')),
                         ('SizeMax', _number(row, 'sizeOutside')),
                         ('DistMin', _number(row, 'distanceMin')),
                         ('DistMax', _number(row, 'distanceMax'))),
                inputs=(('InField', distance),), output=True),
        )

    if kind == 'restrict':
        inside = _number(row, 'sizeInside')
        constant = f'{prefix}/constant'
        return (
            FieldNode(
                node_id=constant, row=name, kind='Constant',
                # MEASURED. Both ends carry the size. A Gmsh Constant field
                # answers with VOut wherever the point is not inside its own
                # entity list, and this one is given no list -- the Restrict
                # below is what does the clipping. Setting VIn alone left the
                # field returning its 1e22 default everywhere, so the Restrict
                # clipped infinity and the row changed nothing.
                numbers=(('VIn', inside), ('VOut', inside))),
            FieldNode(
                node_id=f'{prefix}/restrict', row=name, kind='Restrict',
                inputs=(('InField', constant),),
                # A restrict row names a prepared scope and nothing else:
                # unlike a distance row it has no per-surface form, so there
                # are no explicit tags to prefer over the scope map.
                scopes=(('SurfacesList', 'surfaces',
                         str(row.get('scopeToken') or ''), ()),
                        ('VolumesList', 'volumes',
                         str(row.get('scopeToken') or ''), ())),
                requires_scope=True, output=True),
        )

    if kind == 'curvature':
        distance = f'{prefix}/distance'
        bend = f'{prefix}/curvature'
        return (
            _distance_node(distance, name, row),
            FieldNode(
                node_id=bend, row=name, kind='Curvature',
                numbers=(('Delta', _number(row, 'curvatureDelta', 0.001)),),
                inputs=(('InField', distance),)),
            FieldNode(
                node_id=f'{prefix}/threshold', row=name, kind='Threshold',
                # Curvature runs the other way to distance: the most curved
                # place is the one that needs the small cell, so the inside
                # size sits at the maximum.
                numbers=(('SizeMin', _number(row, 'sizeOutside')),
                         ('SizeMax', _number(row, 'sizeInside')),
                         ('DistMin', _number(row, 'curvatureMin')),
                         ('DistMax', _number(row, 'curvatureMax', 1.0))),
                inputs=(('InField', bend),), output=True),
        )

    if kind == 'box':
        x0, y0, z0 = _triple(row, 'boxMin')
        x1, y1, z1 = _triple(row, 'boxMax')
        return (FieldNode(
            node_id=f'{prefix}/box', row=name, kind='Box',
            numbers=(('VIn', _number(row, 'sizeInside')),
                     ('VOut', _number(row, 'sizeOutside')),
                     ('XMin', x0), ('YMin', y0), ('ZMin', z0),
                     ('XMax', x1), ('YMax', y1), ('ZMax', z1)),
            output=True),)

    if kind == 'ball':
        cx, cy, cz = _triple(row, 'centre')
        numbers = [('VIn', _number(row, 'sizeInside')),
                   ('VOut', _number(row, 'sizeOutside')),
                   ('XCenter', cx), ('YCenter', cy), ('ZCenter', cz),
                   ('Radius', _number(row, 'radius'))]
        if _number(row, 'thickness'):
            numbers.append(('Thickness', _number(row, 'thickness')))
        return (FieldNode(node_id=f'{prefix}/ball', row=name, kind='Ball',
                          numbers=tuple(numbers), output=True),)

    if kind == 'cylinder':
        cx, cy, cz = _triple(row, 'centre')
        ax, ay, az = _triple(row, 'axis', (0.0, 0.0, 1.0))
        return (FieldNode(
            node_id=f'{prefix}/cylinder', row=name, kind='Cylinder',
            numbers=(('VIn', _number(row, 'sizeInside')),
                     ('VOut', _number(row, 'sizeOutside')),
                     ('XCenter', cx), ('YCenter', cy), ('ZCenter', cz),
                     ('XAxis', ax), ('YAxis', ay), ('ZAxis', az),
                     ('Radius', _number(row, 'radius'))),
            output=True),)

    if kind == 'frustum':
        cx, cy, cz = _triple(row, 'centre')
        ax, ay, az = _triple(row, 'axis', (0.0, 0.0, 1.0))
        inside = _number(row, 'sizeInside')
        outside = _number(row, 'sizeOutside')
        return (FieldNode(
            node_id=f'{prefix}/frustum', row=name, kind='Frustum',
            # The inner radii are zero: the refinement runs from the axis out
            # to the end radius, and the two ends carry their own radius --
            # that is what makes it a frustum rather than a tube.
            numbers=(('X1', cx), ('Y1', cy), ('Z1', cz),
                     ('X2', cx + ax), ('Y2', cy + ay), ('Z2', cz + az),
                     ('R1_inner', 0.0), ('R1_outer', _number(row, 'radius')),
                     ('R2_inner', 0.0), ('R2_outer', _number(row, 'radiusEnd')),
                     ('V1_inner', inside), ('V1_outer', outside),
                     ('V2_inner', inside), ('V2_outer', outside)),
            output=True),)

    if kind == 'math_eval':
        return (FieldNode(
            node_id=f'{prefix}/matheval', row=name, kind='MathEval',
            # Already validated by name and costed across the domain, so what
            # reaches Gmsh is an expression it can parse.
            strings=(('F', str(row.get('expression') or '')),),
            output=True),)

    raise FieldGraphError(f'unknown size-field type {kind!r}')


def build_graph(rows) -> FieldGraph:
    """The graph of Gmsh fields the given size-field rows ask for."""
    nodes: list = []
    outputs: list = []
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        produced = _row_nodes(row, index)
        nodes.extend(produced)
        outputs.extend(node.node_id for node in produced if node.output)
    order = validate_graph(nodes)
    return FieldGraph(nodes=tuple(nodes), order=order, outputs=tuple(outputs))
