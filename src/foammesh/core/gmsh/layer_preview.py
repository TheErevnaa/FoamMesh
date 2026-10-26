"""Ghost prisms for the layer stacks (Plan 41 GN10, M-21).

A preview, not the mesh: the surface of a patch is offset along its vertex
normals to each layer boundary of the patch's stack, so the user sees how deep
the stack reaches and whether it will meet the next one before a run.  The
normals are turned toward ``toward`` (the model's centre) because a surface
carries no inside; the runner takes the real direction from the solid.
"""
from __future__ import annotations

import numpy as np


def vertex_normals(points, triangles):
    """Unit area-weighted normal of every vertex."""
    p = np.asarray(points, float)
    t = np.asarray(triangles, int).reshape(-1, 3)
    n = np.zeros_like(p)
    face = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    for k in range(3):
        np.add.at(n, t[:, k], face)
    length = np.linalg.norm(n, axis=1)
    length[length == 0] = 1.0
    return n / length[:, None]


def offset_shells(points, triangles, depths, toward=None):
    """One offset copy of the surface per depth (metres, positive inward).

    Returns ``[(depth, points)]``; the triangles are the input's.
    """
    p = np.asarray(points, float)
    normals = vertex_normals(p, triangles)
    if toward is not None:
        inward = np.asarray(toward, float) - p
        flip = np.einsum('ij,ij->i', normals, inward) < 0
        normals = np.where(flip[:, None], -normals, normals)
    else:
        normals = -normals
    return [(float(d), p + normals * float(d)) for d in depths]


def stack_depths(stack) -> list:
    """Positive depth of every layer boundary of one derived stack."""
    return [abs(float(v)) for v in stack.get('cumulativeHeights') or ()]


def reach(stack) -> float:
    depths = stack_depths(stack)
    return depths[-1] if depths else 0.0


# -- Plan 48 GM6: the layer table of an extrusion ------------------------------ #
#
# Gmsh's ``Layers{{n1, n2}, {h1, h2}}``: row k carries ``n_k`` layers up to the
# cumulative height ``h_k`` (a fraction of the whole extrusion, the last 1).
# The table is edited as rows, kept as text (``2@0.4, 3@1``) and sent as
# ``layer_counts`` and ``layer_heights``; ``sweep_preview`` (the session) says
# where the layer boundaries fall before anything is built.

import math

GRADINGS = (
    ('uniform', 'Uniform'),
    ('geometric', 'Geometric ratio'),
    ('first_height', 'First height and ratio'),
)

MAX_LAYERS = 1000


class LayerTableRefusal(ValueError):
    """The table cannot be sent; the message says what to fix."""


def parse_table(text) -> list:
    """``'2@0.4, 3@1'`` to ``[(2, 0.4), (3, 1.0)]``; blank is empty."""
    rows = []
    for part in str(text or '').replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        count, sep, height = part.partition('@')
        if not sep:
            raise LayerTableRefusal(
                '%r is not a layer row; write count@cumulative height, for '
                'example 2@0.4, 3@1' % part)
        try:
            rows.append((int(count), float(height)))
        except ValueError:
            raise LayerTableRefusal(
                '%r is not a layer row; the count is a whole number and the '
                'height a number' % part) from None
    validate(rows)
    return rows


def table_text(rows) -> str:
    return ', '.join('%d@%s' % (int(c), ('%.10g' % float(h)))
                     for c, h in rows)


def validate(rows) -> None:
    last = 0.0
    total = 0
    for count, height in rows:
        if int(count) < 1:
            raise LayerTableRefusal('every row needs at least one layer')
        if not last < float(height) <= 1.0 + 1e-9:
            raise LayerTableRefusal(
                'the cumulative heights must rise and stay at or below 1 '
                '(1 is the whole extrusion)')
        last = float(height)
        total += int(count)
    if rows and abs(last - 1.0) > 1e-9:
        raise LayerTableRefusal('the last cumulative height must be 1, the '
                                'whole extrusion')
    if total > MAX_LAYERS:
        raise LayerTableRefusal('%d layers is more than the %d the table '
                                'takes' % (total, MAX_LAYERS))


def counts_heights(rows) -> tuple:
    """The two lists ``session.build`` takes."""
    validate(rows)
    return [int(c) for c, _h in rows], [float(h) for _c, h in rows]


def grade(preset: str, layers: int, ratio: float = 1.0,
          first: float | None = None, total: float = 1.0) -> list:
    """A layer table from a grading preset.

    ``uniform``: *layers* equal layers (one row).  ``geometric``: *layers*
    layers, each *ratio* times thicker than the one before, filling the whole
    extrusion (one row per layer).  ``first_height``: the first layer is
    *first* thick (same unit as *total*), each next *ratio* times thicker,
    until *total* is reached (the last layer is cut to fit).
    """
    layers = int(layers)
    if preset == 'uniform':
        if layers < 1:
            raise LayerTableRefusal('give at least one layer')
        return [(layers, 1.0)]
    ratio = float(ratio)
    if ratio <= 0:
        raise LayerTableRefusal('the growth ratio must be greater than zero')
    if preset == 'geometric':
        if layers < 1:
            raise LayerTableRefusal('give at least one layer')
        if layers > MAX_LAYERS:
            raise LayerTableRefusal('%d layers is more than the %d the '
                                    'table takes' % (layers, MAX_LAYERS))
        weights = [ratio ** i for i in range(layers)]
        whole = sum(weights)
        rows, running = [], 0.0
        for i, w in enumerate(weights):
            running += w / whole
            rows.append((1, 1.0 if i == layers - 1 else running))
        return rows
    if preset == 'first_height':
        total = float(total)
        if first is None or float(first) <= 0 or total <= 0:
            raise LayerTableRefusal('give a first height and a total '
                                    'thickness greater than zero')
        if float(first) >= total:
            raise LayerTableRefusal('the first height must be smaller than '
                                    'the total thickness')
        rows, running, thick = [], 0.0, float(first)
        while running < total * (1 - 1e-9):
            running = running + thick
            # a sliver left over joins this layer instead of being a layer
            if total - running < 0.5 * thick * ratio:
                running = total
            rows.append((1, min(1.0, running / total)))
            thick *= ratio
            if len(rows) > MAX_LAYERS:
                raise LayerTableRefusal(
                    'more than %d layers are needed to reach the total '
                    'thickness; raise the ratio' % MAX_LAYERS)
        rows[-1] = (1, 1.0)
        return rows
    raise LayerTableRefusal('the grading must be one of %s'
                            % ', '.join(g for g, _t in GRADINGS))


def layer_fractions(rows) -> list:
    """The cumulative position (0..1) of every layer boundary, in order."""
    out, last = [], 0.0
    for count, height in rows:
        for i in range(1, int(count) + 1):
            out.append(last + (float(height) - last) * i / int(count))
        last = float(height)
    return out


def thicknesses(rows, total: float = 1.0) -> list:
    """The thickness of every layer for an extrusion of length *total*."""
    marks = [0.0] + layer_fractions(rows)
    return [(b - a) * float(total) for a, b in zip(marks, marks[1:])]


def summary(rows, total: float = 1.0) -> dict:
    """What the table editor shows next to the rows."""
    thick = thicknesses(rows, total)
    if not thick:
        return {'layers': 0, 'first': 0.0, 'last': 0.0, 'growth': 1.0}
    return {'layers': len(thick), 'first': thick[0], 'last': thick[-1],
            'growth': (thick[-1] / thick[0]) if thick[0] else math.inf}


def preview_segments(preview: dict) -> list:
    """Line segments for the viewport from a ``sweep_preview`` answer: the
    arrow (or the axis), and a short tick across the sweep at every layer
    boundary of every picked entity.  ``[[p0, p1], ...]``."""
    segments = []
    arrow = preview.get('arrow')
    if arrow:
        o, v = arrow['origin'], arrow['vector']
        segments.append([list(o), [o[i] + v[i] for i in range(3)]])
    axis = preview.get('axis')
    if axis:
        p, d = axis['point'], axis['direction']
        segments.append([[p[i] - d[i] for i in range(3)],
                         [p[i] + d[i] for i in range(3)]])
    for entity in preview.get('entities', ()):
        previous = entity['origin']
        for layer in entity['layers']:
            segments.append([list(previous), list(layer['point'])])
            previous = layer['point']
    return segments
