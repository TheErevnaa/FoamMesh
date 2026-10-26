#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Colour mesh by > Layers grown (Plan 39 VI16, I6).

The most common snappy question is "did my layers grow, and where did they
collapse". The answer already exists per patch in the layer report
(`core/quality/layer_report.py`); this module decides what each patch is
painted with, with no Qt and no VTK scene:

* a patch that asked for layers is painted with the count the run **achieved**
  on it, on one table spanning 0 to the largest request, so a collapsed patch
  is the red end and a complete one the green end, and two patches with the
  same count have the same colour;
* a patch that asked for none (nothing requested, or frozen) is grey -- not on
  the table, because "0 of 0" is not a collapse;
* a patch the report does not name is grey too: nobody measured it.
"""
from __future__ import annotations

from dataclasses import dataclass

from vtkmodules.vtkCommonCore import vtkLookupTable

#: The colour of a patch that asked for no layers (r, g, b).
GREY = (0.62, 0.62, 0.62)

#: The key the legend is filed under in the viewport's stack of legends.
LEGEND_KEY = 'layersGrown'

#: The choices of *Colour mesh by*: (key, label).
SOLID = 'solid'
LAYERS_GROWN = 'layers_grown'
#: The volume's exterior, by the section's own choices (`section_colour`).
VOLUME_CHOICES = (('cell_type', 'Cell type'),
                  ('cell_level', 'Refinement level'),
                  ('cell_zone', 'Region / zone'),
                  ('quality.cellAspectRatio', 'Quality: Aspect ratio'),
                  ('quality.nonOrthoAngle', 'Quality: Non-orthogonality'),
                  ('quality.skewness', 'Quality: Skewness'),
                  ('quality.cellVolume', 'Quality: Cell volume'))
CHOICES = ((SOLID, 'Solid'), (LAYERS_GROWN, 'Layers grown'),
           *VOLUME_CHOICES)


def is_volume_choice(key) -> bool:
    return any(key == choice for choice, _label in VOLUME_CHOICES)


@dataclass(frozen=True)
class LayerPaint:
    """What one patch is painted with."""
    patch: str
    #: The achieved layer count, or ``None`` when the patch is grey.
    achieved: float | None
    requested: float = 0.0


def asked(row) -> bool:
    """Whether a layer-report row is a patch that asked for layers."""
    if getattr(row, 'frozen', False):
        return False
    # A report that recorded no request still shows layers that grew.
    return (float(getattr(row, 'requested', 0) or 0) > 0
            or float(getattr(row, 'layers', 0) or 0) > 0)


def plan(rows, actorIds) -> dict:
    """``actor id -> LayerPaint`` for every patch actor in *actorIds*.

    *rows* are the `view_modes.LayerRow`s of the stored report. An actor id is
    a patch name, or ``region:patch`` on a multi-region mesh.
    """
    byPatch = {str(row.patch): row for row in rows or ()}
    painted = {}
    for actorId in actorIds:
        name = str(actorId).rsplit(':', 1)[-1]
        row = byPatch.get(name)
        if row is not None and asked(row):
            painted[actorId] = LayerPaint(
                name, float(row.layers),
                max(float(row.requested or 0), float(row.layers)))
        else:
            painted[actorId] = LayerPaint(name, None)
    return painted


def top(painted) -> float:
    """The top of the table: the largest request among the painted patches."""
    return max([p.requested for p in painted.values()
                if p.achieved is not None] or [1.0])


def lookup_table(high: float) -> vtkLookupTable:
    """The standard scale, red where nothing grew."""
    from foammesh.support import colour_standard
    # Low coverage is the red end.
    return colour_standard.lookup_table(
        'layers', 0.0, max(float(high), 1.0), worst_is_high=False)


def colour_of(table: vtkLookupTable, value: float) -> tuple:
    """The (r, g, b) the table paints *value* with."""
    rgb = [0.0, 0.0, 0.0]
    table.GetColor(float(value), rgb)
    return tuple(rgb)


def legend_title() -> str:
    return 'Layers grown'


# -- Plan 48 GM12: layer coverage on a Gmsh mesh (plan 4.10.1) ------------------ #
#
# Only snappy writes a layer-run report, so Layer coverage / Layers grown were
# dead on Gmsh.  Here the layers are *measured from the mesh*: from each wall
# face, the stack of prisms (or hexahedra) grown on it is walked through its
# opposite faces until it meets anything that is not a layer cell.  That count is
# a measurement and needs nothing but the mesh.  What the run *asked for* is not
# in a bare file; it comes from the project's record (`core.provenance`), and when
# it is not there it is reported as unavailable with its reason -- never as 0.

from itertools import zip_longest

import numpy as np

#: Gmsh element types of the layer cells and the faces they are grown on.
HEX, PRISM = 5, 6
TRIANGLE, QUADRANGLE = 2, 3
#: Local faces (Gmsh node order) of each layer cell, as opposite pairs.
LAYER_FACE_PAIRS = {
    PRISM: (((0, 1, 2), (3, 4, 5)),),
    HEX: (((0, 1, 2, 3), (4, 5, 6, 7)),
          ((0, 1, 5, 4), (3, 2, 6, 7)),
          ((0, 3, 7, 4), (1, 2, 6, 5))),
}
#: Walk at most this many layers (a guard, far above any real stack).
MAX_LAYERS = 4096

_MIX = np.array([0x9E3779B97F4A7C15, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9,
                 0x27D4EB2F165667C5], dtype=np.uint64)


def _keys(nodes) -> np.ndarray:
    """An order-free 64-bit key for each row of node indices."""
    rows = np.sort(np.asarray(nodes, dtype=np.int64), axis=1).astype(np.uint64)
    key = np.zeros(len(rows), dtype=np.uint64)
    with np.errstate(over='ignore'):
        for column in range(rows.shape[1]):
            key = key * np.uint64(1099511628211) + (rows[:, column] + np.uint64(1)) * _MIX[column]
            key ^= key >> np.uint64(29)
    return key


def layer_counts(wall_nodes, solids: dict) -> np.ndarray:
    """Layers grown on each wall face.

    *wall_nodes* is an ``(n, 3)`` or ``(n, 4)`` array of node indices (a
    triangle or a quadrangle each); *solids* is ``{gmsh type: (m, k) node
    indices}`` for the layer cells present (``PRISM``, ``HEX``).  The result is
    one integer per wall face: the number of consecutive layer cells stacked on
    it, 0 where the cell on it is not a layer cell.
    """
    wall_nodes = np.asarray(wall_nodes, dtype=np.int64)
    count = len(wall_nodes)
    layers = np.zeros(count, dtype=np.int64)
    if not count:
        return layers
    size = wall_nodes.shape[1]
    # every layer-cell face of this size: key, owner (type, cell), opposite face
    keys, owners, opposite, heres = [], [], [], []
    for etype, nodes in solids.items():
        nodes = np.asarray(nodes, dtype=np.int64)
        for pair in LAYER_FACE_PAIRS.get(int(etype), ()):
            if len(pair[0]) != size or not len(nodes):
                continue
            first, second = nodes[:, list(pair[0])], nodes[:, list(pair[1])]
            for here, there in ((first, second), (second, first)):
                keys.append(_keys(here))
                owners.append(np.stack([np.full(len(nodes), int(etype)),
                                        np.arange(len(nodes))], axis=1))
                opposite.append(there)
                heres.append(here)
    if not keys:
        return layers
    keys = np.concatenate(keys)
    owners = np.concatenate(owners)
    opposite = np.concatenate(opposite)
    heres = np.concatenate(heres)
    order = np.argsort(keys, kind='stable')
    keys, owners = keys[order], owners[order]
    opposite, heres = opposite[order], heres[order]

    face = wall_nodes.copy()
    previous = np.full((count, 2), -1, dtype=np.int64)
    active = np.arange(count)
    for _step in range(MAX_LAYERS):
        if not len(active):
            break
        wanted = _keys(face[active])
        low = np.searchsorted(keys, wanted, side='left')
        high = np.searchsorted(keys, wanted, side='right')
        chosen = np.full(len(active), -1, dtype=np.int64)
        for slot in range(2):
            index = low + slot
            inside = index < high
            candidate = np.where(inside, index, 0)
            same = (owners[candidate] == previous[active]).all(axis=1)
            take = inside & ~same & (chosen < 0)
            # the candidate must really be this face (a key can collide)
            match = np.zeros(len(active), dtype=bool)
            match[take] = (np.sort(heres[candidate[take]], axis=1)
                           == np.sort(face[active][take], axis=1)).all(axis=1)
            chosen = np.where(take & match, candidate, chosen)
        moved = chosen >= 0
        if not moved.any():
            break
        rows = active[moved]
        pick = chosen[moved]
        layers[rows] += 1
        previous[rows] = owners[pick]
        face[rows] = opposite[pick]
        active = rows
    return layers


@dataclass(frozen=True)
class GmshLayerRow:
    """One wall patch's measured layers; the shape `plan` reads (`asked`)."""

    patch: str
    #: Mean layers present on the patch's faces (what `plan` paints).
    layers: float
    #: Layers asked for, or 0 where the request is unknown (`requested_known`).
    requested: float = 0.0
    requested_known: bool = False
    frozen: bool = False
    faces: int = 0
    minimum: int = 0
    maximum: int = 0
    #: Faces carrying the full requested count (None while the request is unknown).
    full: int | None = None
    histogram: tuple = ()

    def to_dict(self) -> dict:
        from foammesh.core import provenance

        asked = (provenance.measured(int(self.requested), 'project record')
                 if self.requested_known else provenance.unavailable(
                     provenance.NO_LAYER_REQUEST))
        return {
            'patch': self.patch, 'faces': self.faces,
            'layers': {'mean': self.layers, 'min': self.minimum,
                       'max': self.maximum,
                       'histogram': [int(v) for v in self.histogram]},
            'requested': asked.to_dict(),
            'full_faces': (None if self.full is None else int(self.full)),
            'coverage': (None if self.full is None or not self.faces
                         else self.full / self.faces),
        }


def gmsh_layer_rows(arrays: dict, requested=None) -> list:
    """`GmshLayerRow`s for the wall patches of a measured file.

    *arrays* is :func:`foammesh.core.gmsh.element_quality.measure_file`'s
    result (it carries the connectivity); *requested* is a
    ``provenance.Value`` holding ``{patch: layers asked for}`` or unavailable.
    A patch the run did not ask for is listed with the layers found on it.
    """
    connectivity = arrays.get('__connectivity') or {}
    names = arrays.get('__names') or {}
    solids = {t: connectivity[t]['nodes'] for t in (PRISM, HEX)
              if t in connectivity}
    asked = dict(requested.value) if requested is not None \
        and requested.is_measured else None
    rows = []
    for etype in (TRIANGLE, QUADRANGLE):
        block = connectivity.get(etype)
        if block is None:
            continue
        surface = block['dim'] == 2
        physical = block['physical']
        for tag in sorted({int(t) for t in physical[surface]}):
            pick = surface & (physical == tag)
            counts = layer_counts(block['nodes'][pick], solids)
            patch = (names.get('2:%d' % tag) or '').strip() or 'surface%d' % tag
            want = None if asked is None else asked.get(patch)
            top = int(counts.max()) if counts.size else 0
            histogram = np.bincount(counts, minlength=top + 1)
            rows.append(GmshLayerRow(
                patch=patch, layers=float(counts.mean()) if counts.size else 0.0,
                requested=float(want or 0.0), requested_known=want is not None,
                faces=int(counts.size), minimum=int(counts.min()) if counts.size else 0,
                maximum=top,
                full=(None if want is None else int(np.count_nonzero(counts >= want))),
                histogram=tuple(int(v) for v in histogram)))
    merged = {}
    for row in rows:
        old = merged.get(row.patch)
        if old is None:
            merged[row.patch] = row
            continue
        total = old.faces + row.faces
        merged[row.patch] = GmshLayerRow(
            patch=row.patch,
            layers=(old.layers * old.faces + row.layers * row.faces) / max(total, 1),
            requested=old.requested, requested_known=old.requested_known,
            faces=total, minimum=min(old.minimum, row.minimum),
            maximum=max(old.maximum, row.maximum),
            full=(None if old.full is None else old.full + (row.full or 0)),
            histogram=tuple(
                int(a) + int(b) for a, b in zip_longest(
                    old.histogram, row.histogram, fillvalue=0)))
    return list(merged.values())


def gmsh_layer_coverage(path, roots=(), *, profile=None, work=None,
                        runner=None) -> dict:
    """Layer coverage of a mesh file: measured layers beside the request.

    The layers present are always measured.  The requested counts come from
    the project record found in *roots*; with none, every patch's ``requested``
    is unavailable with the reason, and ``requested_unavailable`` says so once.
    """
    from foammesh.core import provenance
    from foammesh.core.gmsh import element_quality

    arrays = element_quality.measure_file(
        path, profile=profile, dims=(2, 3), work=work, runner=runner)
    lookup = provenance.find(roots, path)
    request = provenance.requested_layers(lookup)
    rows = gmsh_layer_rows(arrays, request)
    return {
        'mesh': str(path), 'rows': [row.to_dict() for row in rows],
        'objects': rows,
        'requested': request.to_dict(),
        'layer_cells': {str(t): int(len(arrays['__connectivity'][t]['nodes']))
                        for t in (PRISM, HEX)
                        if t in arrays.get('__connectivity', {})},
        'measured_by': 'layers counted on the mesh from each wall face',
    }
