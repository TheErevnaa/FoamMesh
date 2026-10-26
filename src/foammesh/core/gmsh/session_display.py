"""What the viewport shows of the Gmsh model, and which entities are visible.

Plan 41 GN17 (G-16, G-17, T-03, T-04, T-05, O-08, I-03, I-04, F-17).  Gmsh's
own GUI has a *Visibility* window (filter entities by dimension, tag range,
physical group; show or hide them; "visibility affects meshing" is
``Mesh.MeshOnlyVisible``) and a *Mesh > Visibility / Options* tree (element
kinds, edges and faces, labels, normals and tangents, colours by element type,
entity, physical group or partition, explode, light, point and line size, six
clipping planes, Voronoi, skin only) and a *Manipulator* (rotation,
translation, scale typed as numbers).  This module is all of that without a
window, so the rules can be tested and the viewport only has to draw.

Nothing here imports Qt.  Numpy only; the hand-over (``mesh.vtu``) is read with
VTK's reader in :func:`read_arrays`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Element kinds
# --------------------------------------------------------------------------- #

#: Gmsh's ``Mesh.*`` visibility toggle -> (Gmsh element family, dimension).
#: ``Trihedra`` is Gmsh's 4-node corner element (type 140, MSH_TRIH_4: the apex
#: and the three nodes it points at). Few meshes hold one; the toggle reports
#: "none in this mesh" for the rest, and ``gmsh_mesh_view`` draws the three
#: edges of each one in ``Mesh.Color.Trihedra`` (Plan 48 TL, DP-4450).
ELEMENT_KINDS = {
    'Lines': ('Line', 1),
    'Triangles': ('Triangle', 2), 'Quadrangles': ('Quadrangle', 2),
    'Tetrahedra': ('Tetrahedron', 3), 'Hexahedra': ('Hexahedron', 3),
    'Prisms': ('Prism', 3), 'Pyramids': ('Pyramid', 3),
    'Trihedra': ('Trihedron', 3),
}
_FAMILY_OF_TYPE = {1: 'Line', 2: 'Triangle', 3: 'Quadrangle',
                   4: 'Tetrahedron', 5: 'Hexahedron', 6: 'Prism',
                   7: 'Pyramid', 8: 'Line', 9: 'Triangle', 10: 'Quadrangle',
                   11: 'Tetrahedron', 12: 'Hexahedron', 13: 'Prism',
                   14: 'Pyramid', 15: 'Point', 16: 'Quadrangle',
                   17: 'Hexahedron', 18: 'Prism', 19: 'Pyramid',
                   21: 'Triangle', 23: 'Triangle', 25: 'Triangle',
                   26: 'Line', 27: 'Line', 28: 'Line',
                   # Plan 48 TL (DP-4450): MSH_TRIH_4, the four-node trihedron
                   # (apex, then the three nodes it points at)
                   140: 'Trihedron'}
_KIND_OF_FAMILY = {family: kind for kind, (family, _d) in ELEMENT_KINDS.items()}


def kind_of_type(element_type: int) -> str:
    """The visibility toggle (``Tetrahedra`` ...) of a Gmsh element type."""
    return _KIND_OF_FAMILY.get(_FAMILY_OF_TYPE.get(int(element_type), ''), '')


# --------------------------------------------------------------------------- #
# The hand-over as numpy
# --------------------------------------------------------------------------- #

#: The kind of a polyhedral cell (a ``VTK_POLYHEDRON``): the Gmsh toggles have
#: no such element, so it lives beside :data:`ELEMENT_KINDS`.  Plan 42 SG24.
POLYHEDRA = 'Polyhedra'
#: Their element type in the ``gmshElementType`` column (no Gmsh type is 0).
POLYHEDRON_TYPE = 0


@dataclass
class PolyhedraFaces:
    """The face streams of polyhedral cells (``VTK_POLYHEDRON``).

    ``cells`` are the cell indices of the polyhedra; ``face_counts`` how many
    faces each has; ``sizes`` the node count of every face (cell by cell) and
    ``nodes`` the nodes of all faces, face after face -- the VTK face stream
    without its count prefixes.
    """

    cells: np.ndarray
    face_counts: np.ndarray
    sizes: np.ndarray
    nodes: np.ndarray

    def owners(self) -> np.ndarray:
        """The polyhedron (cell index) each face belongs to."""
        return np.repeat(self.cells, self.face_counts)

    def faces_of(self, selected: np.ndarray):
        """``(width, (n, width) nodes, owner)`` per face width, for the
        polyhedra whose cell is set in the boolean mask *selected*."""
        owner = self.owners()
        keep = selected[owner]
        starts = np.concatenate(([0], np.cumsum(self.sizes, dtype=np.int64)))
        for width in np.unique(self.sizes[keep]):
            chosen = np.flatnonzero(keep & (self.sizes == width))
            index = starts[chosen][:, None] + np.arange(int(width))
            yield int(width), self.nodes[index], owner[chosen]


@dataclass
class MeshArrays:
    """``mesh.vtu`` as flat arrays: nodes, the connectivity, per-cell columns."""

    points: np.ndarray
    offsets: np.ndarray            # len(cells) + 1
    connectivity: np.ndarray
    columns: dict = field(default_factory=dict)
    #: Face streams of the polyhedral cells (snappyHexMesh / polyMesh), if any.
    polyhedra: 'PolyhedraFaces | None' = None

    @property
    def cell_count(self) -> int:
        return len(self.offsets) - 1

    def column(self, name: str, default=0) -> np.ndarray:
        found = self.columns.get(name)
        if found is None:
            return np.full(self.cell_count, default, dtype=np.int64)
        return found

    def nodes_of(self, cell: int) -> np.ndarray:
        return self.connectivity[self.offsets[cell]:self.offsets[cell + 1]]

    def sizes(self) -> np.ndarray:
        return np.diff(self.offsets)

    def centres(self) -> np.ndarray:
        """Each element's centroid (mean of its nodes)."""
        counts = self.sizes()
        owner = np.repeat(np.arange(self.cell_count), counts)
        sums = np.zeros((self.cell_count, 3))
        np.add.at(sums, owner, self.points[self.connectivity])
        return sums / np.maximum(counts, 1)[:, None]


def read_arrays(path) -> MeshArrays:
    """Read a hand-over ``.vtu`` into :class:`MeshArrays`."""
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    if grid is None or grid.GetNumberOfCells() < 1:
        raise ValueError(f'{Path(path).name} holds no elements to display')
    cells = grid.GetCells()
    columns = {}
    data = grid.GetCellData()
    for index in range(data.GetNumberOfArrays()):
        array = data.GetArray(index)
        if array is not None and array.GetNumberOfComponents() == 1:
            columns[array.GetName()] = vtk_to_numpy(array).copy()
    return MeshArrays(
        points=vtk_to_numpy(grid.GetPoints().GetData()).astype(np.float64),
        offsets=vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64),
        connectivity=vtk_to_numpy(cells.GetConnectivityArray()).astype(
            np.int64),
        columns=columns)


def read_group_names(path) -> dict:
    """Physical group tag -> name from ``mesh.names.json`` beside *path*."""
    import json

    try:
        text = (Path(path).parent / 'mesh.names.json').read_text(
            encoding='utf-8')
        table = dict(json.loads(text).get('physical') or {})
    except (OSError, ValueError):
        return {}
    names = {}
    for key, name in table.items():
        try:
            names[int(str(key).split(':')[-1])] = str(name)
        except ValueError:
            continue
    return names


def kinds_present(arrays: MeshArrays) -> dict:
    """``{'Tetrahedra': 1140, ...}``: every kind, with how many the mesh has."""
    counts = {kind: 0 for kind in ELEMENT_KINDS}
    types, number = np.unique(arrays.column('gmshElementType'),
                              return_counts=True)
    for element_type, count in zip(types, number):
        kind = kind_of_type(element_type)
        if kind:
            counts[kind] += int(count)
    return counts


# --------------------------------------------------------------------------- #
# Visibility window (G-17, T-03)
# --------------------------------------------------------------------------- #

def parse_ranges(text) -> set:
    """``"1-5, 8, 10-12"`` as a set of tags (Gmsh's visibility list syntax).

    A bare ``*`` or an empty string means every tag (returned as ``None``).
    """
    if text is None:
        return None
    text = str(text).strip()
    if text in ('', '*'):
        return None
    tags: set = set()
    for item in re.split(r'[,\s;]+', text):
        if not item:
            continue
        match = re.fullmatch(r'(\d+)(?:\s*[-:]\s*(\d+))?', item)
        if not match:
            raise ValueError(
                f'"{item}" is not a tag or a range; write 5 or 3-9')
        low = int(match.group(1))
        high = int(match.group(2) if match.group(2) is not None else low)
        if high < low:
            raise ValueError(f'the range {item} runs backwards')
        tags.update(range(low, high + 1))
    return tags


@dataclass(frozen=True)
class VisibilityFilter:
    """The Visibility window's filter: dimensions, tag ranges, physical group."""

    dims: tuple = (0, 1, 2, 3)
    tags: str = ''                 # "1-5, 8"; empty means every tag
    physical: str = ''             # a physical-group name, a tag, or empty

    def matches(self, record, group_names=None) -> bool:
        if record.dim not in self.dims:
            return False
        wanted = parse_ranges(self.tags)
        if wanted is not None and record.tag not in wanted:
            return False
        group = self.physical.strip()
        if group:
            names = [str((group_names or {}).get(t, ''))
                     for t in (record.physical or ())]
            tags = [str(t) for t in (record.physical or ())]
            if group not in names and group not in tags:
                return False
        return True


def matching_entities(records, filt: VisibilityFilter,
                      group_names=None) -> list:
    """The ``(dim, tag)`` of every entity the filter selects."""
    return [r.key for r in records if filt.matches(r, group_names)]


def visibility_text(entities) -> str:
    """``[(2, 3), (2, 4)]`` as the ``2:3, 2:4`` the ``visibility`` action takes."""
    return ', '.join(f'{dim}:{tag}' for dim, tag in entities)


def visibility_steps(records, selected, mode: str) -> list:
    """The ``visibility`` action calls that make *selected* shown or hidden.

    ``mode`` is ``'show'`` (show them, leave the rest), ``'hide'``, ``'only'``
    (hide everything, then show them -- Gmsh's *Apply* with the others
    unticked) or ``'all'`` (show every entity).
    """
    everything = [r.key for r in records]
    selected = [tuple(e) for e in selected]
    if mode == 'all':
        return [{'entities': visibility_text(everything), 'visible': True,
                 'recursive': False}] if everything else []
    if mode == 'only':
        rest = [e for e in everything if e not in set(selected)]
        steps = []
        if rest:
            steps.append({'entities': visibility_text(rest),
                          'visible': False, 'recursive': False})
        if selected:
            steps.append({'entities': visibility_text(selected),
                          'visible': True, 'recursive': False})
        return steps
    if mode in ('show', 'hide'):
        if not selected:
            return []
        return [{'entities': visibility_text(selected),
                 'visible': mode == 'show', 'recursive': False}]
    raise ValueError(f'unknown visibility mode {mode}')


class VisibilityModel:
    """Which entities are visible, as Gmsh's session holds it.

    Starts with every entity visible (a fresh Gmsh model).  ``apply`` updates
    the set the same way :func:`visibility_steps` tells Gmsh to.
    """

    def __init__(self, records=()):
        self._all = {tuple(r.key) for r in records}
        self._hidden: set = set()
        self.only_visible = False       # Mesh.MeshOnlyVisible

    def reset(self, records) -> None:
        self._all = {tuple(r.key) for r in records}
        self._hidden &= self._all

    @property
    def hidden(self) -> set:
        return set(self._hidden)

    @property
    def visible(self) -> set:
        return self._all - self._hidden

    def apply(self, steps) -> None:
        for step in steps:
            entities = {tuple(int(x) for x in pair.split(':'))
                        for pair in step['entities'].split(',') if pair.strip()}
            if step['visible']:
                self._hidden -= entities
            else:
                self._hidden |= entities & self._all

    def is_visible(self, dim: int, tag: int) -> bool:
        return (int(dim), int(tag)) not in self._hidden


def cells_visible(arrays: MeshArrays, hidden) -> np.ndarray:
    """Per cell: its entity is not hidden."""
    keep = np.ones(arrays.cell_count, dtype=bool)
    if not hidden:
        return keep
    dims = arrays.column('gmshEntityDim')
    tags = arrays.column('gmshEntityTag')
    for dim, tag in hidden:
        keep &= ~((dims == dim) & (tags == tag))
    return keep


# --------------------------------------------------------------------------- #
# Display options (G-16, O-08)
# --------------------------------------------------------------------------- #

#: Colour carousel (``Mesh.ColorCarousel``): 0 by element type, 1 by elementary
#: entity, 2 by physical group, 3 by partition.
COLOUR_MODES = ('Element type', 'Elementary entity', 'Physical group',
                'Partition')
#: ``Mesh.LabelType``: 0 number, 1 elementary entity, 2 physical group,
#: 3 partition, 4 coordinates.
LABEL_TYPES = ('Number', 'Elementary entity', 'Physical group', 'Partition',
               'Coordinates')


@dataclass(frozen=True)
class DisplayOptions:
    """Gmsh's ``Mesh.*`` display options, by their Gmsh names."""

    points: bool = False            # Mesh.Nodes
    lines: bool = True              # Mesh.Lines
    surface_edges: bool = True      # Mesh.SurfaceEdges
    surface_faces: bool = True      # Mesh.SurfaceFaces
    volume_edges: bool = True       # Mesh.VolumeEdges
    volume_faces: bool = True       # Mesh.VolumeFaces
    kinds: tuple = tuple(ELEMENT_KINDS) + (POLYHEDRA,)   # visible kinds
    draw_skin_only: bool = True     # Mesh.DrawSkinOnly
    explode: float = 1.0            # Mesh.Explode (1 = whole elements)
    light: bool = True              # Mesh.Light
    light_lines: int = 2            # Mesh.LightLines (Gmsh default 2)
    light_two_side: bool = True     # Mesh.LightTwoSide
    point_type: int = 0             # Mesh.NodeType (0 dot, 1 sphere)
    point_size: float = 4.0         # Mesh.NodeSize
    line_width: float = 1.0         # Mesh.LineWidth
    colour_mode: int = 1            # Mesh.ColorCarousel (Gmsh default: entity)
    node_labels: bool = False       # Mesh.NodeLabels
    element_labels: bool = False    # Mesh.ElementLabels (per kind below)
    label_type: int = 0             # Mesh.LabelType
    label_sampling: int = 1         # Mesh.LabelSampling (one label in N)
    label_kinds: tuple = ('Tetrahedra', 'Hexahedra', 'Prisms', 'Pyramids',
                          'Triangles', 'Quadrangles', 'Lines')
    normals: float = 0.0            # Mesh.Normals (length, 0 = off)
    tangents: float = 0.0           # Mesh.Tangents (length, 0 = off)
    voronoi: bool = False           # Mesh.Voronoi
    num_sub_edges: int = 2          # Mesh.NumSubEdges

    def with_(self, **changes) -> 'DisplayOptions':
        for name in changes:
            if name not in self.__dataclass_fields__:
                raise ValueError(f'{name} is not a display option')
        value = replace(self, **changes)
        value.validate()
        return value

    def validate(self) -> None:
        if not 0.0 < self.explode <= 1.0:
            raise ValueError('Explode runs from just above 0 to 1; 1 draws '
                             'the elements whole')
        if self.colour_mode not in range(len(COLOUR_MODES)):
            raise ValueError('colour by element type, entity, physical group '
                             'or partition')
        if self.label_type not in range(len(LABEL_TYPES)):
            raise ValueError('unknown label type')
        if int(self.label_sampling) < 1:
            raise ValueError('label sampling is one label every 1 or more '
                             'elements')
        if int(self.num_sub_edges) < 1:
            raise ValueError('an edge of a high-order element is '
                             'drawn in 1 or more pieces')
        if self.point_size <= 0 or self.line_width <= 0:
            raise ValueError('point size and line width must be positive')
        if self.normals < 0 or self.tangents < 0:
            raise ValueError('normal and tangent lengths cannot be negative')
        for kind in self.kinds:
            if kind not in ELEMENT_KINDS and kind != POLYHEDRA:
                raise ValueError(f'{kind} is not an element kind')

    def gmsh_options(self) -> dict:
        """The same options under the names Gmsh's option tree uses."""
        out = {
            'Mesh.Nodes': int(self.points), 'Mesh.Lines': int(self.lines),
            'Mesh.SurfaceEdges': int(self.surface_edges),
            'Mesh.SurfaceFaces': int(self.surface_faces),
            'Mesh.VolumeEdges': int(self.volume_edges),
            'Mesh.VolumeFaces': int(self.volume_faces),
            'Mesh.DrawSkinOnly': int(self.draw_skin_only),
            'Mesh.Explode': self.explode, 'Mesh.Light': int(self.light),
            'Mesh.LightLines': self.light_lines,
            'Mesh.LightTwoSide': int(self.light_two_side),
            'Mesh.NodeType': self.point_type,
            'Mesh.NodeSize': self.point_size,
            'Mesh.LineWidth': self.line_width,
            'Mesh.ColorCarousel': self.colour_mode,
            'Mesh.NodeLabels': int(self.node_labels),
            'Mesh.LabelType': self.label_type,
            'Mesh.LabelSampling': self.label_sampling,
            'Mesh.Normals': self.normals, 'Mesh.Tangents': self.tangents,
            'Mesh.Voronoi': int(self.voronoi),
            'Mesh.NumSubEdges': self.num_sub_edges,
        }
        for kind in ELEMENT_KINDS:
            out['Mesh.' + kind] = int(kind in self.kinds)
        for option, group in (('LineLabels', ('Lines',)),
                              ('SurfaceLabels', ('Triangles', 'Quadrangles')),
                              ('VolumeLabels', ('Tetrahedra', 'Hexahedra',
                                                'Prisms', 'Pyramids',
                                                'Trihedra'))):
            out['Mesh.' + option] = int(self.element_labels and any(
                kind in self.label_kinds for kind in group))
        return out


def kind_mask(arrays: MeshArrays, kinds) -> np.ndarray:
    """Per cell: its element kind is switched on."""
    types = arrays.column('gmshElementType')
    keep = np.zeros(arrays.cell_count, dtype=bool)
    for element_type in np.unique(types):
        if element_type == POLYHEDRON_TYPE:
            if POLYHEDRA in kinds:
                keep |= types == element_type
        elif kind_of_type(element_type) in kinds:
            keep |= types == element_type
    return keep


# --------------------------------------------------------------------------- #
# Colours (ColorCarousel)
# --------------------------------------------------------------------------- #

#: Twenty distinct colours (Gmsh's carousel is a fixed table too).
CAROUSEL = np.array([
    (230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200),
    (245, 130, 48), (145, 30, 180), (70, 240, 240), (240, 50, 230),
    (210, 245, 60), (250, 190, 212), (0, 128, 128), (220, 190, 255),
    (170, 110, 40), (255, 250, 200), (128, 0, 0), (170, 255, 195),
    (128, 128, 0), (255, 215, 180), (0, 0, 128), (128, 128, 128),
], dtype=np.uint8)


def colour_codes(arrays: MeshArrays, mode: int) -> np.ndarray:
    """The integer behind each element's colour for ``Mesh.ColorCarousel``."""
    if mode == 0:
        return arrays.column('gmshElementType')
    if mode == 1:
        return (arrays.column('gmshEntityDim') * 100000
                + arrays.column('gmshEntityTag'))
    if mode == 2:
        return arrays.column('gmshPhysical')
    if mode == 3:
        return arrays.column('gmshPartition')
    raise ValueError('colour mode must be 0..3')


def colour_rgb(codes, carousel=None) -> np.ndarray:
    """RGB for each code; equal codes share a colour, order of appearance
    does not matter (the sorted unique codes index the carousel).

    *carousel* is a ``(n, 3)`` table (Plan 48 GM2:
    :meth:`GmshDisplaySettings.carousel`, the ``Mesh.Color.Zero`` ..
    ``Nineteen`` options); the built-in table is used when none is given."""
    table = CAROUSEL if carousel is None else np.asarray(carousel, np.uint8)
    codes = np.asarray(codes)
    unique, inverse = np.unique(codes, return_inverse=True)
    return table[inverse % len(table)]


def colour_legend_label(arrays: MeshArrays, mode: int, code: int,
                        names=None) -> str:
    """The words for one colour code of ``Mesh.ColorCarousel`` *mode*."""
    names = names or {}
    code = int(code)
    if mode == 0:
        return _FAMILY_OF_TYPE.get(code, f'type {code}')
    if mode == 1:
        return f'{code // 100000}D entity {code % 100000}'
    if mode == 2:
        return names.get(code) or (f'physical {code}' if code else 'no group')
    return f'partition {code}' if code else 'not partitioned'


def colour_legend(arrays: MeshArrays, mode: int, names=None) -> list:
    """``[(label, (r, g, b)), ...]`` for the colours in use."""
    unique = np.unique(colour_codes(arrays, mode))
    rows = []
    for index, code in enumerate(unique):
        rgb = tuple(int(c) for c in CAROUSEL[index % len(CAROUSEL)])
        rows.append((colour_legend_label(arrays, mode, code, names), rgb))
    return rows


# --------------------------------------------------------------------------- #
# Faces of the volume elements (VolumeFaces, DrawSkinOnly)
# --------------------------------------------------------------------------- #

# Faces of each Gmsh volume family, in the hand-over's VTK node order.
_FACES = {
    'Tetrahedron': {3: ((0, 1, 2), (0, 1, 3), (1, 2, 3), (0, 2, 3))},
    'Hexahedron': {4: ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
                       (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))},
    'Prism': {3: ((0, 1, 2), (3, 4, 5)),
              4: ((0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5))},
    'Pyramid': {4: ((0, 1, 2, 3),),
                3: ((0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4))},
}


def face_label(width: int) -> str:
    """The key of the faces with *width* nodes in :func:`volume_faces`."""
    return {3: 'tri', 4: 'quad'}.get(int(width), 'ngon%d' % int(width))


def _pair_exact(nodes: np.ndarray):
    """Exact pairing of equal faces (the fallback of :func:`_pair_faces`)."""
    keys = np.sort(nodes, axis=1)
    _unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    order = np.argsort(inverse, kind='stable')
    grouped = inverse[order]
    new = np.empty(len(grouped), dtype=bool)
    new[0] = True
    np.not_equal(grouped[1:], grouped[:-1], out=new[1:])
    starts = np.flatnonzero(new)
    counts = np.diff(np.append(starts, len(grouped)))
    first = order[starts]
    second = np.where(counts >= 2, order[np.minimum(starts + 1,
                                                    len(grouped) - 1)], -1)
    return first, second


def _group_by_hash(hashed: np.ndarray):
    """``(first, other, counts)``: one entry per distinct value of *hashed*
    -- the index of its first use, of its second (the first again when it is
    used once) and how many uses it has.

    The values are uniform random words, so their top bits split them into
    equal buckets (one radix pass over a byte); each bucket is then sorted on
    its own, on several threads.  Sorting 90 million words in one piece is
    cache-bound and takes about five times longer.
    """
    from concurrent.futures import ThreadPoolExecutor
    import os

    count = len(hashed)
    bits = 6 if count > 1_000_000 else 0
    if bits:
        bucket = (hashed >> np.uint64(64 - bits)).astype(np.uint8)
        order = np.argsort(bucket, kind='stable')
        bounds = np.concatenate(
            ([0], np.cumsum(np.bincount(bucket, minlength=1 << bits))))
        del bucket
    else:
        order = np.arange(count)
        bounds = np.array([0, count])

    def sort_bucket(index):
        chosen = order[bounds[index]:bounds[index + 1]]
        words = hashed[chosen]
        by_word = np.argsort(words)
        chosen, words = chosen[by_word], words[by_word]
        size = len(words)
        if not size:
            return chosen, chosen, np.zeros(0, dtype=np.int64)
        new = np.empty(size, dtype=bool)
        new[0] = True
        np.not_equal(words[1:], words[:-1], out=new[1:])
        starts = np.flatnonzero(new)
        return (chosen[starts], chosen[np.minimum(starts + 1, size - 1)],
                np.diff(np.append(starts, size)))

    buckets = range(len(bounds) - 1)
    if len(buckets) > 1:
        with ThreadPoolExecutor(min(16, os.cpu_count() or 1)) as pool:
            parts = list(pool.map(sort_bucket, buckets))
    else:
        parts = [sort_bucket(0)]
    return tuple(np.concatenate([part[i] for part in parts])
                 for i in range(3))


def _face_hash(nodes: np.ndarray, word: np.ndarray) -> np.ndarray:
    """Order-free hash of each row of *nodes*: the sum of one random word per
    node.  Read in chunks on several threads (the table is read at random)."""
    from concurrent.futures import ThreadPoolExecutor
    import os

    count = len(nodes)
    out = np.empty(count, dtype=np.uint64)

    def chunk(start):
        stop = min(start + 2_000_000, count)
        part = nodes[start:stop]
        total = word[part[:, 0]]
        for column in range(1, part.shape[1]):
            total += word[part[:, column]]
        out[start:stop] = total

    starts = range(0, count, 2_000_000)
    if len(starts) > 1:
        with ThreadPoolExecutor(min(16, os.cpu_count() or 1)) as pool:
            list(pool.map(chunk, starts))
    else:
        for start in starts:
            chunk(start)
    return out


def _pair_faces(nodes: np.ndarray, point_count: int):
    """``(first, second)``: for every distinct face of *nodes* (one row per
    face use), the index of its first use and of its second (``-1`` when it
    is used once).

    Equal faces are found by an order-free hash (the sum of one random 64-bit
    word per node); the pairs are then checked against a second, independent
    hash, and anything that is not a clean pair (a collision, or a face used
    by three cells) goes to the exact grouping.  Hashing replaces sorting
    every row of the node table.
    """
    rng = np.random.default_rng(20261010)
    word = rng.integers(1, 2 ** 63, size=point_count, dtype=np.uint64)
    hashed = _face_hash(nodes, word)
    first, other, counts = _group_by_hash(hashed)
    del hashed
    if int(counts.max()) > 2:
        return _pair_exact(nodes)
    paired = np.flatnonzero(counts == 2)
    word = rng.integers(1, 2 ** 63, size=point_count, dtype=np.uint64)
    check = _face_hash(nodes, word)
    left, right = first[paired], other[paired]
    if not np.array_equal(check[left], check[right]):
        return _pair_exact(nodes)
    del check
    low = first.copy()
    high = np.full(len(first), -1, dtype=first.dtype)
    low[paired] = np.minimum(left, right)
    high[paired] = np.maximum(left, right)
    return low, high


def volume_faces(arrays: MeshArrays, cells) -> dict:
    """Faces of the volume elements *cells*, each once.

    Returns, for every face width (``tri``, ``quad`` and ``ngon<N>`` for the
    faces of polyhedra): ``{label: (n, width) nodes, label + '_owner': the
    first cell using the face, label + '_owner2': the second cell (-1 when
    there is none), label + '_skin': the face belongs to one of the cells
    only}`` and ``'widths'``, the widths present (always 3 and 4).

    The two owners let a caller redo the skin for any subset of *cells*
    without building the faces again (:func:`select_faces`).
    """
    types = arrays.column('gmshElementType')
    cells = np.asarray(cells, dtype=np.int64)
    small = len(arrays.points) < 2 ** 31
    node_type = np.int32 if small else np.int64
    pieces: dict = {3: ([], []), 4: ([], [])}

    def add(width, nodes, owners):
        found = pieces.setdefault(width, ([], []))
        found[0].append(np.asarray(nodes, dtype=node_type))
        found[1].append(np.asarray(owners, dtype=node_type))

    for cell_type in np.unique(types[cells]) if len(cells) else []:
        family = _FAMILY_OF_TYPE.get(int(cell_type), '')
        tables = _FACES.get(family, {})
        chosen = cells[types[cells] == cell_type]
        base = arrays.offsets[chosen]
        for width, table in tables.items():
            for face in table:
                add(width, np.stack([arrays.connectivity[base + i]
                                     for i in face], axis=1), chosen)
    if arrays.polyhedra is not None and len(cells):
        selected = np.zeros(arrays.cell_count, dtype=bool)
        selected[cells] = True
        for width, nodes, owners in arrays.polyhedra.faces_of(selected):
            add(width, nodes, owners)
    out = {'widths': sorted(pieces)}
    for width in out['widths']:
        label = face_label(width)
        nodes_all, owners_all = pieces[width]
        if not nodes_all:
            out.update({label: np.zeros((0, width), dtype=node_type),
                        label + '_owner': np.zeros(0, dtype=node_type),
                        label + '_owner2': np.zeros(0, dtype=node_type),
                        label + '_skin': np.zeros(0, dtype=bool)})
            continue
        nodes = np.concatenate(nodes_all)
        owners = np.concatenate(owners_all)
        del nodes_all
        first, second = _pair_faces(nodes, len(arrays.points))
        has_second = second >= 0
        out[label] = nodes[first]
        out[label + '_owner'] = owners[first]
        out[label + '_owner2'] = np.where(
            has_second, owners[np.maximum(second, 0)], -1).astype(node_type)
        out[label + '_skin'] = ~has_second
    return out


def select_faces(found: dict, label: str, selected: np.ndarray,
                 skin_only: bool, everything: bool = False) -> tuple:
    """``(nodes, owner)`` of the faces of *label* for the cells set in the
    boolean mask *selected*, from the table :func:`volume_faces` built once.

    With *skin_only* only the faces used by exactly one selected cell are
    returned; otherwise every face of a selected cell.  *everything* says all
    the cells of the table are selected, which skips the lookups.
    """
    nodes, owner = found[label], found[label + '_owner']
    owner2 = found[label + '_owner2']
    if everything:
        keep = found[label + '_skin'] if skin_only else None
        if keep is None:
            return nodes, owner
        return nodes[keep], owner[keep]
    first = selected[owner]
    second = (owner2 >= 0) & selected[np.maximum(owner2, 0)]
    keep = (first ^ second) if skin_only else (first | second)
    chosen = np.where(first, owner, owner2)[keep]
    return nodes[keep], chosen


def nodes_used(arrays: MeshArrays, cells) -> np.ndarray:
    """The sorted node numbers used by *cells*, without a loop over cells."""
    used = np.zeros(len(arrays.points), dtype=bool)
    cells = np.asarray(cells, dtype=np.int64)
    if len(cells) == arrays.cell_count:
        used[arrays.connectivity] = True
    elif len(cells):
        selected = np.zeros(arrays.cell_count, dtype=bool)
        selected[cells] = True
        used[arrays.connectivity[np.repeat(selected, arrays.sizes())]] = True
    return np.flatnonzero(used)


# --------------------------------------------------------------------------- #
# High-order elements: Mesh.NumSubEdges (O-08)
# --------------------------------------------------------------------------- #

#: Nodes per high-order element of each dimension: curves with 3 to 5,
#: triangles with 6 or 10, quadrangles with 8 or 9.
HIGH_ORDER_NODES = {1: (3, 4, 5), 2: (6, 8, 9, 10)}


def high_order_cells(arrays: MeshArrays, cells) -> np.ndarray:
    """The curve and surface elements among *cells* that carry more nodes than
    their corners.  The size is read from the hand-over, which keeps every
    node of these elements."""
    cells = np.asarray(cells, dtype=np.int64)
    if not len(cells):
        return cells
    sizes = arrays.sizes()[cells]
    dims = arrays.column('gmshEntityDim')[cells]
    keep = np.zeros(len(cells), dtype=bool)
    for dim, widths in HIGH_ORDER_NODES.items():
        keep |= (dims == dim) & np.isin(sizes, widths)
    return cells[keep]


def _shape_weights(width: int, pcoords: np.ndarray) -> np.ndarray:
    """Shape functions of the element with *width* nodes at *pcoords* (rows
    of ``(r, s)``), from VTK's own cell, whose node order is Gmsh's."""
    from vtkmodules.vtkCommonDataModel import (
        vtkBiQuadraticQuad, vtkLagrangeCurve, vtkLagrangeTriangle,
        vtkQuadraticQuad)

    cell = {3: vtkLagrangeCurve, 4: vtkLagrangeCurve, 5: vtkLagrangeCurve,
            6: vtkLagrangeTriangle, 10: vtkLagrangeTriangle,
            8: vtkQuadraticQuad, 9: vtkBiQuadraticQuad}[width]()
    cell.GetPointIds().SetNumberOfIds(width)
    cell.GetPoints().SetNumberOfPoints(width)
    for index in range(width):
        cell.GetPointIds().SetId(index, index)
    if hasattr(cell, 'Initialize'):                  # the Lagrange cells
        cell.Initialize()
    weights = np.zeros((len(pcoords), width))
    for row, pc in enumerate(pcoords):
        found = [0.0] * width
        cell.InterpolateFunctions([float(pc[0]), float(pc[1]), 0.0], found)
        weights[row] = found
    return weights


def _ring_edges(index: dict, ring: list) -> np.ndarray:
    return np.array([(index[ring[k]], index[ring[(k + 1) % len(ring)]])
                     for k in range(len(ring))], dtype=np.int64)


def _grid(shape: str, n: int):
    """``(reference points, sub-faces, outline pieces)`` of an element cut
    into ``n`` pieces on every edge.  Reference points are ``(r, s)`` in
    VTK's cell: a curve along r in 0..1, a triangle with corners (0,0),
    (1,0), (0,1), a quadrangle in 0..1 on both axes."""
    if shape == 'curve':
        points = np.array([[i / n, 0.0] for i in range(n + 1)])
        return (points, np.zeros((0, 3), np.int64),
                np.array([[i, i + 1] for i in range(n)], dtype=np.int64))
    index, points = {}, []
    if shape == 'triangle':
        for j in range(n + 1):
            for i in range(n + 1 - j):
                index[(i, j)] = len(points)
                points.append((i / n, j / n))
        faces = []
        for j in range(n):
            for i in range(n - j):
                faces.append((index[(i, j)], index[(i + 1, j)],
                              index[(i, j + 1)]))
                if i + j < n - 1:
                    faces.append((index[(i + 1, j)], index[(i + 1, j + 1)],
                                  index[(i, j + 1)]))
        ring = ([(i, 0) for i in range(n + 1)]
                + [(n - k, k) for k in range(1, n + 1)]
                + [(0, n - k) for k in range(1, n)])
        return (np.array(points), np.array(faces, dtype=np.int64),
                _ring_edges(index, ring))
    for j in range(n + 1):
        for i in range(n + 1):
            index[(i, j)] = len(points)
            points.append((i / n, j / n))
    faces = [(index[(i, j)], index[(i + 1, j)], index[(i + 1, j + 1)],
              index[(i, j + 1)]) for j in range(n) for i in range(n)]
    ring = ([(i, 0) for i in range(n + 1)]
            + [(n, k) for k in range(1, n + 1)]
            + [(n - k, n) for k in range(1, n + 1)]
            + [(0, n - k) for k in range(1, n)])
    return (np.array(points), np.array(faces, dtype=np.int64),
            _ring_edges(index, ring))


def subdivide_high_order(arrays: MeshArrays, cells, pieces: int) -> dict:
    """Draw the high-order curves and surfaces among *cells* the way Gmsh does
    with ``Mesh.NumSubEdges``: every edge is cut into *pieces* straight pieces
    laid on the element's curved edge, and each face into the sub-triangles or
    sub-quadrangles those pieces bound.

    Returns ``points`` ``(m, 3)``; ``triangles`` ``(k, 3)``, ``quads``
    ``(k, 4)`` and ``segments`` ``(s, 2)`` as indices into ``points``; and
    ``triangle_owner``, ``quad_owner``, ``segment_owner``: the element each
    piece came from."""
    pieces = int(pieces)
    if pieces < 1:
        raise ValueError('an edge is drawn in 1 or more pieces')
    out = {'points': np.zeros((0, 3)),
           'triangles': np.zeros((0, 3), np.int64),
           'quads': np.zeros((0, 4), np.int64),
           'segments': np.zeros((0, 2), np.int64),
           'triangle_owner': np.zeros(0, np.int64),
           'quad_owner': np.zeros(0, np.int64),
           'segment_owner': np.zeros(0, np.int64)}
    cells = high_order_cells(arrays, cells)
    if not len(cells):
        return out
    dims = arrays.column('gmshEntityDim')[cells]
    sizes = arrays.sizes()[cells]
    parts, base = [], 0
    for dim, width in sorted({(int(d), int(w)) for d, w in zip(dims, sizes)}):
        chosen = cells[(dims == dim) & (sizes == width)]
        shape = ('curve' if dim == 1 else
                 'triangle' if width in (6, 10) else 'quad')
        reference, faces, edges = _grid(shape, pieces)
        weights = _shape_weights(width, reference)
        nodes = arrays.connectivity[
            arrays.offsets[chosen][:, None] + np.arange(width)]
        coords = np.einsum('pn,enk->epk', weights, arrays.points[nodes])
        start = base + np.arange(len(chosen)) * len(reference)
        base += len(chosen) * len(reference)
        parts.append(coords.reshape(-1, 3))
        out['segments'] = np.concatenate([
            out['segments'],
            (start[:, None, None] + edges[None]).reshape(-1, 2)])
        out['segment_owner'] = np.concatenate([
            out['segment_owner'], np.repeat(chosen, len(edges))])
        if len(faces):
            key = 'triangle' if shape == 'triangle' else 'quad'
            out[key + 's'] = np.concatenate([
                out[key + 's'],
                (start[:, None, None] + faces[None]).reshape(
                    -1, faces.shape[1])])
            out[key + '_owner'] = np.concatenate([
                out[key + '_owner'], np.repeat(chosen, len(faces))])
    out['points'] = np.concatenate(parts)
    return out


# --------------------------------------------------------------------------- #
# Normals, tangents (O-08)
# --------------------------------------------------------------------------- #

def _typical_edge(arrays: MeshArrays) -> float:
    lengths = arrays.column('gmshMaxEdge', 0.0)
    positive = lengths[lengths > 0]
    if len(positive):
        return float(np.mean(positive))
    span = arrays.points.max(axis=0) - arrays.points.min(axis=0)
    return float(np.linalg.norm(span)) / 20.0 or 1.0


def surface_normals(arrays: MeshArrays, cells, length: float) -> np.ndarray:
    """Segments ``(n, 2, 3)``: from each surface element's centre along its
    normal, ``length`` times the mean edge length of the mesh."""
    cells = np.asarray(cells, dtype=np.int64)
    cells = cells[np.isin(arrays.column('gmshEntityDim')[cells], (2,))]
    if not len(cells):
        return np.zeros((0, 2, 3))
    base = arrays.offsets[cells]
    p0 = arrays.points[arrays.connectivity[base]]
    p1 = arrays.points[arrays.connectivity[base + 1]]
    p2 = arrays.points[arrays.connectivity[base + 2]]
    normal = np.cross(p1 - p0, p2 - p0)
    size = np.linalg.norm(normal, axis=1)
    normal = normal / np.where(size > 0, size, 1.0)[:, None]
    centre = arrays.centres()[cells]
    reach = length * _typical_edge(arrays)
    return np.stack([centre, centre + normal * reach], axis=1)


def curve_tangents(arrays: MeshArrays, cells, length: float) -> np.ndarray:
    """Segments ``(n, 2, 3)`` along each line element from its first to its
    second node, started at its centre."""
    cells = np.asarray(cells, dtype=np.int64)
    cells = cells[arrays.column('gmshEntityDim')[cells] == 1]
    if not len(cells):
        return np.zeros((0, 2, 3))
    base = arrays.offsets[cells]
    p0 = arrays.points[arrays.connectivity[base]]
    p1 = arrays.points[arrays.connectivity[base + 1]]
    direction = p1 - p0
    size = np.linalg.norm(direction, axis=1)
    direction = direction / np.where(size > 0, size, 1.0)[:, None]
    centre = arrays.centres()[cells]
    reach = length * _typical_edge(arrays)
    return np.stack([centre, centre + direction * reach], axis=1)


# --------------------------------------------------------------------------- #
# Voronoi (Mesh.Voronoi)
# --------------------------------------------------------------------------- #

def circumcentres(arrays: MeshArrays, tets) -> np.ndarray:
    """The circumcentre of each tetrahedron in *tets* (cell ids)."""
    tets = np.asarray(tets, dtype=np.int64)
    base = arrays.offsets[tets]
    p = [arrays.points[arrays.connectivity[base + i]] for i in range(4)]
    a = np.stack([p[1] - p[0], p[2] - p[0], p[3] - p[0]], axis=1)
    b = 0.5 * np.stack([np.sum(p[i] ** 2 - p[0] ** 2, axis=1)
                        for i in (1, 2, 3)], axis=1)
    determinant = np.linalg.det(a)
    centres = np.full((len(tets), 3), np.nan)
    good = np.abs(determinant) > 1e-300
    if np.any(good):
        centres[good] = np.linalg.solve(a[good], b[good][..., None])[..., 0]
    return centres


def voronoi_segments(arrays: MeshArrays, tets) -> np.ndarray:
    """The Voronoi (dual) edges of the Delaunay-like tetrahedra: a segment
    joins the circumcentres of two tetrahedra that share a face."""
    tets = np.asarray(tets, dtype=np.int64)
    if len(tets) < 2:
        return np.zeros((0, 2, 3))
    centres = circumcentres(arrays, tets)
    base = arrays.offsets[tets]
    nodes = np.stack([arrays.connectivity[base + i] for i in range(4)], axis=1)
    keys, owners = [], []
    for face in ((0, 1, 2), (0, 1, 3), (1, 2, 3), (0, 2, 3)):
        keys.append(np.sort(nodes[:, face], axis=1))
        owners.append(np.arange(len(tets)))
    keys = np.concatenate(keys)
    owners = np.concatenate(owners)
    order = np.lexsort(keys.T[::-1])
    keys, owners = keys[order], owners[order]
    same = np.all(keys[1:] == keys[:-1], axis=1)
    first, second = owners[:-1][same], owners[1:][same]
    segments = np.stack([centres[first], centres[second]], axis=1)
    return segments[np.all(np.isfinite(segments), axis=(1, 2))]


# --------------------------------------------------------------------------- #
# Labels (I-03)
# --------------------------------------------------------------------------- #

def element_label_rows(arrays: MeshArrays, cells, label_type: int,
                       names=None) -> tuple:
    """``(positions (n, 3), texts)`` for the element labels of ``Mesh.LabelType``."""
    # Plan 48 GM11a (DP-4454): the texts of one label type are built in bulk
    # (numpy columns), not by a Python loop over the elements.
    cells = np.asarray(cells, dtype=np.int64)
    centre = arrays.centres()[cells]
    names = names or {}
    if label_type == 0:
        texts = arrays.column('gmshElementTag')[cells].astype(np.int64) \
            .astype(str).tolist()
    elif label_type == 1:
        dim = arrays.column('gmshEntityDim')[cells].astype(np.int64)
        entity = arrays.column('gmshEntityTag')[cells].astype(np.int64)
        texts = np.char.add(np.array(list('PCSV'))[dim],
                            entity.astype(str)).tolist()
    elif label_type == 2:
        codes, inverse = np.unique(
            arrays.column('gmshPhysical')[cells].astype(np.int64),
            return_inverse=True)
        words = np.array([names.get(int(c)) or (str(int(c)) if c else '-')
                          for c in codes], dtype=object)
        texts = words[np.asarray(inverse).reshape(-1)].tolist() \
            if len(codes) else []
    elif label_type == 3:
        texts = arrays.column('gmshPartition')[cells].astype(np.int64) \
            .astype(str).tolist()
    elif label_type == 4:
        texts = ['%.4g, %.4g, %.4g' % tuple(row) for row in centre.tolist()]
    else:
        raise ValueError('unknown label type')
    return centre, texts


def node_label_rows(arrays: MeshArrays, cells, label_type: int) -> tuple:
    """Labels at the nodes of *cells*: the node number (its index plus one,
    the hand-over keeps Gmsh's order) or its coordinates."""
    cells = np.asarray(cells, dtype=np.int64)
    used = np.unique(np.concatenate(
        [arrays.nodes_of(int(c)) for c in cells])) if len(cells) else \
        np.zeros(0, dtype=np.int64)
    if label_type == 4:
        texts = ['%.4g, %.4g, %.4g' % tuple(row)
                 for row in arrays.points[used].tolist()]
    else:
        texts = (used.astype(np.int64) + 1).astype(str).tolist()
    return arrays.points[used], texts


# --------------------------------------------------------------------------- #
# Clipping planes (T-04)
# --------------------------------------------------------------------------- #

MAX_CLIP_PLANES = 6


@dataclass(frozen=True)
class ClipPlane:
    """``a x + b y + c z + d >= 0`` is kept (Gmsh passes the same four numbers
    to OpenGL, which keeps the non-negative side)."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 0.0
    on: bool = True

    def normal(self) -> np.ndarray:
        return np.array([self.a, self.b, self.c], dtype=float)

    def valid(self) -> bool:
        return float(np.linalg.norm(self.normal())) > 0.0

    def distance(self, points) -> np.ndarray:
        n = self.normal()
        return (np.asarray(points) @ n + self.d) / float(np.linalg.norm(n))


def check_planes(planes) -> list:
    planes = list(planes)
    if len(planes) > MAX_CLIP_PLANES:
        raise ValueError(f'Gmsh has {MAX_CLIP_PLANES} clipping planes; '
                         f'{len(planes)} were given')
    for index, plane in enumerate(planes):
        if plane.on and not plane.valid():
            raise ValueError(f'clipping plane {index} has a zero normal')
    return planes


def keep_whole_elements(arrays: MeshArrays, planes, rule: str = 'any',
                        curves: bool = True) -> np.ndarray:
    """Per cell: kept by every active plane.

    *rule* is the Section tool's whole-cell rule: ``any`` keeps an element
    when at least one of its nodes is on the kept side (it is only dropped
    when it lies wholly on the clipped side; Gmsh's *Keep whole elements*),
    ``centroid`` when its centre is, ``entire`` only when all its nodes are.
    With *curves* off, line and point elements are not clipped (Plan 42 SG12).
    """
    planes = [p for p in check_planes(planes) if p.on]
    keep = np.ones(arrays.cell_count, dtype=bool)
    if not planes:
        return keep
    owner = np.repeat(np.arange(arrays.cell_count), arrays.sizes())
    sizes = np.maximum(arrays.sizes(), 1)
    for plane in planes:
        distance = plane.distance(arrays.points[arrays.connectivity])
        if rule == 'entire':
            outside = np.zeros(arrays.cell_count, dtype=bool)
            np.logical_or.at(outside, owner, distance < 0.0)
            here = ~outside
        elif rule == 'centroid':
            total = np.zeros(arrays.cell_count)
            np.add.at(total, owner, distance)
            here = total / sizes >= 0.0
        else:
            inside = distance >= 0.0
            here = np.zeros(arrays.cell_count, dtype=bool)
            np.logical_or.at(here, owner, inside)
        keep &= here
    if not curves:
        keep |= arrays.column('gmshEntityDim') < 2
    return keep


# --------------------------------------------------------------------------- #
# Manipulator (T-05) and the camera (I-04)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CameraPose:
    """Gmsh's Manipulator: three rotation angles in degrees, a translation in
    model units along the screen axes, and a scale (zoom factor).

    Zero rotation looks along -Z with +Y up (the model seen from +Z), as Gmsh
    does.  The rotation is Rz . Ry . Rx about the rotation centre.
    """

    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    tx: float = 0.0
    ty: float = 0.0
    tz: float = 0.0
    scale: float = 1.0

    def validate(self) -> None:
        if not self.scale > 0:
            raise ValueError('the scale must be positive')


def rotation_matrix(rx: float, ry: float, rz: float) -> np.ndarray:
    ax, ay, az = (math.radians(v) for v in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = (math.cos(ax), math.sin(ax), math.cos(ay),
                              math.sin(ay), math.cos(az), math.sin(az))
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return mz @ my @ mx


def camera_vectors(pose: CameraPose, centre, distance: float) -> dict:
    """Position, focal point, view-up for *pose* looking at *centre*.

    The scene is turned by the rotation, shifted by the translation across the
    screen and made *scale* times larger (the camera comes closer).
    """
    pose.validate()
    rotation = rotation_matrix(pose.rx, pose.ry, pose.rz)
    centre = np.asarray(centre, dtype=float)
    # The model is turned; equivalently the camera moves round it the other way.
    back = rotation.T
    right = back @ np.array([1.0, 0.0, 0.0])
    up = back @ np.array([0.0, 1.0, 0.0])
    toward = back @ np.array([0.0, 0.0, 1.0])
    focal = centre - pose.tx * right - pose.ty * up - pose.tz * toward
    position = focal + toward * (distance / pose.scale)
    return {'position': position, 'focal': focal, 'up': up}


def pose_from_vectors(position, focal, up, centre, distance: float) -> CameraPose:
    """The inverse of :func:`camera_vectors` (Euler angles in degrees)."""
    position, focal, up = (np.asarray(v, dtype=float) for v in
                           (position, focal, up))
    toward = position - focal
    span = float(np.linalg.norm(toward))
    toward = toward / span
    up = up / np.linalg.norm(up)
    right = np.cross(up, toward)
    back = np.stack([right, up, toward], axis=1)     # columns: screen axes
    rotation = back.T
    sy = -rotation[2, 0]
    ry = math.asin(max(-1.0, min(1.0, sy)))
    if abs(math.cos(ry)) > 1e-9:
        rx = math.atan2(rotation[2, 1], rotation[2, 2])
        rz = math.atan2(rotation[1, 0], rotation[0, 0])
    else:
        rx = math.atan2(-rotation[1, 2], rotation[1, 1])
        rz = 0.0
    offset = np.asarray(centre, dtype=float) - focal
    return CameraPose(math.degrees(rx), math.degrees(ry), math.degrees(rz),
                      float(offset @ right), float(offset @ up),
                      float(offset @ toward), distance / span)


# --------------------------------------------------------------------------- #
# Vector export (F-17)
# --------------------------------------------------------------------------- #

#: Export format -> (file suffix, vtkGL2PSExporter file format name).
VECTOR_FORMATS = {'SVG': ('.svg', 'SVG'), 'PDF': ('.pdf', 'PDF'),
                  'EPS': ('.eps', 'EPS'), 'PS': ('.ps', 'PS')}
RASTER_FORMATS = {'PNG': '.png', 'JPEG': '.jpg'}


def export_view(render_window, path, fmt: str, *, scale: int = 1,
                hide=(), font: str | None = None,
                transparent: bool = False) -> Path:
    """Write the render window to *path* as PNG, JPEG, SVG, PDF, EPS or PS.

    Vector formats are written by :mod:`.vector_export` from the geometry on
    screen (VTK's GL2PS exporter would embed one bitmap); the file suffix is
    set to the format.  Raster formats draw the labels at the picture's size
    (:mod:`foammesh.rendering.label_pass`): VTK's own tiling leaves text at
    screen size.  *hide* are props left out; *font* is the label font key.
    """
    fmt = fmt.upper()
    path = Path(path)
    if fmt in RASTER_FORMATS:
        from foammesh.rendering import label_pass

        renderer = render_window.GetRenderers().GetFirstRenderer()
        rgb = label_pass.capture(render_window, renderer, int(scale), hide=hide)
        target = path.with_suffix(RASTER_FORMATS[fmt])
        label_pass.write_array(rgb, target, fmt)
        return target
    if fmt not in VECTOR_FORMATS:
        raise ValueError(f'{fmt} is not an image format; choose one of '
                         f'{", ".join([*RASTER_FORMATS, *VECTOR_FORMATS])}')
    from . import vector_export

    suffix, write = vector_export.WRITERS[fmt]
    width, height = (int(v) for v in render_window.GetSize())
    renderer = render_window.GetRenderers().GetFirstRenderer()
    scene = vector_export.scene_of(renderer, max(width, 1), max(height, 1),
                                   font=font, hide=hide,
                                   transparent=transparent)
    return write(scene, path.with_suffix(suffix))


# --------------------------------------------------------------------------- #
# Plan 48 GM2: every display option of Gmsh, bound to a viewport control
# --------------------------------------------------------------------------- #

#: Options of Gmsh's own window that draw nothing (Plan 48 section 5.1, I8).
#: Gmsh runs the double-click commands in its FLTK window (usually the ONELAB
#: editor); FoamMesh's double-click is its own.  ``Mesh.CpuTime`` and the
#: ``Mesh.Nb*`` counts are read-only status values, shown in Statistics.
NOTHING_TO_DRAW = {
    'Geometry.DoubleClickedPointCommand': 'runs in Gmsh\'s window (ONELAB)',
    'Geometry.DoubleClickedCurveCommand': 'runs in Gmsh\'s window (ONELAB)',
    'Geometry.DoubleClickedSurfaceCommand': 'runs in Gmsh\'s window (ONELAB)',
    'Geometry.DoubleClickedVolumeCommand': 'runs in Gmsh\'s window (ONELAB)',
    'Geometry.DoubleClickedEntityTag': 'set by Gmsh\'s double-click',
    'Mesh.CpuTime': 'read-only status value, shown in Statistics',
    'Mesh.NbNodes': 'read-only count, shown in Statistics',
    'Mesh.NbTriangles': 'read-only count, shown in Statistics',
    'Mesh.NbQuadrangles': 'read-only count, shown in Statistics',
    'Mesh.NbTetrahedra': 'read-only count, shown in Statistics',
    'Mesh.NbHexahedra': 'read-only count, shown in Statistics',
    'Mesh.NbPrisms': 'read-only count, shown in Statistics',
    'Mesh.NbPyramids': 'read-only count, shown in Statistics',
    'Mesh.NbTrihedra': 'read-only count, shown in Statistics',
}

#: The Mesh.* display options that the Elements group already holds, by the
#: :class:`DisplayOptions` field behind them.  They are bound to that control,
#: not duplicated.
_ELEMENTS_FIELD = {
    'Mesh.Nodes': 'points', 'Mesh.Lines': 'lines',
    'Mesh.SurfaceEdges': 'surface_edges', 'Mesh.SurfaceFaces': 'surface_faces',
    'Mesh.VolumeEdges': 'volume_edges', 'Mesh.VolumeFaces': 'volume_faces',
    'Mesh.DrawSkinOnly': 'draw_skin_only', 'Mesh.Explode': 'explode',
    'Mesh.Light': 'light', 'Mesh.LightLines': 'light_lines',
    'Mesh.LightTwoSide': 'light_two_side', 'Mesh.NodeType': 'point_type',
    'Mesh.NodeSize': 'point_size', 'Mesh.LineWidth': 'line_width',
    'Mesh.ColorCarousel': 'colour_mode', 'Mesh.NodeLabels': 'node_labels',
    'Mesh.LabelType': 'label_type', 'Mesh.LabelSampling': 'label_sampling',
    'Mesh.Normals': 'normals', 'Mesh.Tangents': 'tangents',
    'Mesh.NumSubEdges': 'num_sub_edges',
    'Mesh.LineLabels': 'element_labels', 'Mesh.SurfaceLabels': 'element_labels',
    'Mesh.VolumeLabels': 'element_labels',
}
for _kind in ELEMENT_KINDS:
    _ELEMENTS_FIELD['Mesh.' + _kind] = 'kinds'

#: What VTK does for each family of options (the drawing is Plan 48 GM11a).
_VTK = {
    'visibility': 'actor visibility',
    'size': 'point size / line width',
    'type': 'sphere glyphs / tube filter',
    'labels': 'label mapper',
    'arrows': 'arrow glyphs',
    'light': 'actor lighting',
    'orphans': 'computed from the model topology, then coloured',
    'marker': 'representation and glyphs at entity centres',
    'offset': 'actor position',
    'element filter': 'cell-type filter',
    'dual': 'dual mesh computed, then drawn',
    'normals': 'normals filter',
    'clip': 'planes of the shared Section tool',
    'colour': 'actor colour',
    'carousel': 'lookup table for the entity / partition carousel',
}


def _vtk_family(name: str, kind: str) -> str:
    label = name.split('.', 1)[1]
    if kind == 'color':
        return 'carousel' if label.startswith('Color.') and label[6:] in (
            'Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven '
            'Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen '
            'Nineteen').split() else 'colour'
    if label == 'Clip':
        return 'clip'
    if label.endswith('Labels') or label.startswith('Label'):
        return 'labels'
    if label in ('Points', 'Curves', 'Surfaces', 'Volumes'):
        return 'visibility'
    if label.endswith(('Size', 'Width', 'SelectSize', 'SelectWidth')):
        return 'size'
    if label.endswith('Type') and label != 'LabelType':
        return 'marker' if label in ('SurfaceType', 'VolumeType') else 'type'
    if label in ('Normals', 'Tangents'):
        return 'arrows'
    if label.startswith('Light'):
        return 'light'
    if label == 'HighlightOrphans':
        return 'orphans'
    if label.startswith('Offset'):
        return 'offset'
    if label == 'Dual':
        return 'dual'
    if label == 'SmoothNormals':
        return 'normals'
    return 'element filter'


@dataclass(frozen=True)
class DisplayBinding:
    """One Gmsh display option and the FoamMesh control that owns it."""

    option: str
    kind: str                  # the catalogue kind
    control: str               # the viewport control that holds the value
    setting: str               # the key it is stored under
    vtk: str                   # what VTK does with it (drawing: GM11a)
    shared: bool = False       # held by an existing control, not a new one


def _build_bindings() -> dict:
    from . import option_catalogue as catalogue

    out = {}
    for option in catalogue.CATALOGUE.values():
        if option.page != 'display' or option.name in NOTHING_TO_DRAW:
            continue
        name = option.name
        group, label = name.split('.', 1)
        family = _vtk_family(name, option.kind)
        if option.kind == 'bitmask' and label == 'Clip':
            control, shared = 'section/planes', True
        elif name in _ELEMENTS_FIELD:
            control, shared = 'elements/' + _ELEMENTS_FIELD[name], True
        elif option.kind == 'color':
            control, shared = 'display/colour/' + name, False
        else:
            control, shared = 'display/' + group.lower() + '/' + label, False
        vtk = _VTK[family]
        if control.startswith('elements/') and                 _ELEMENTS_FIELD.get(name) != 'kinds':
            vtk = 'already drawn by the Elements group'
        out[name] = DisplayBinding(name, option.kind, control,
                                   'gmsh.display.' + name, vtk, shared)
    return out


#: Every Gmsh display option that draws something, by its Gmsh name.
DISPLAY_BINDINGS = _build_bindings()


def oc_get(name: str):
    """The catalogue entry of a display option (the panel builds on it)."""
    from . import option_catalogue as catalogue

    return catalogue.get(name)


#: Where the drawing settings are kept between runs.  A test replaces this
#: with a ``(read, write)`` pair of plain functions.
SETTINGS_KEY = 'gmshDisplay/settings'
_STORE = None


def _qsettings():
    from PySide6.QtCore import QSettings

    return QSettings('FoamMesh', 'FoamMesh')


def load_saved_settings() -> 'GmshDisplaySettings':
    """The drawing settings the user last kept (defaults when none)."""
    try:
        text = _STORE[0]() if _STORE else _qsettings().value(SETTINGS_KEY, '')
    except Exception:                                   # noqa: BLE001
        text = ''
    return GmshDisplaySettings.from_json(text)


def save_settings(settings: 'GmshDisplaySettings') -> bool:
    """Keep *settings* for the next run; ``False`` if nothing could be written."""
    try:
        if _STORE:
            _STORE[1](settings.to_json())
        else:
            _qsettings().setValue(SETTINGS_KEY, settings.to_json())
    except Exception:                                   # noqa: BLE001
        return False
    return True


def bindings_for(control: str) -> tuple:
    """The options a viewport control holds."""
    return tuple(b for b in DISPLAY_BINDINGS.values() if b.control == control)


def clip_mask(planes) -> int:
    """``Geometry.Clip`` / ``Mesh.Clip`` for the Section tool's planes: bit
    ``i`` (value ``2**i``) is plane ``i`` switched on."""
    return sum(1 << index for index, plane in enumerate(planes) if plane.on)


def planes_with_mask(planes, mask) -> list:
    """*planes* with each one switched on or off by the bits of *mask*; planes
    the mask names beyond the list are added as default planes, so that every
    set bit has a plane.  The plane equations are kept."""
    mask = int(mask)
    if mask < 0 or mask >= 1 << MAX_CLIP_PLANES:
        raise ValueError(f'a clip mask is 0 to {(1 << MAX_CLIP_PLANES) - 1}: '
                         'bit i is clipping plane i')
    planes = list(planes)
    while len(planes) < MAX_CLIP_PLANES and (mask >> len(planes)):
        planes.append(ClipPlane(on=False))
    return [replace(plane, on=bool(mask >> index & 1))
            for index, plane in enumerate(planes)]


class GmshDisplaySettings:
    """The values of every bound display option, by Gmsh name.

    Defaults are the catalogue's; a value is checked by the same validator the
    Options panel uses, so a viewport control cannot hold what Gmsh would
    refuse.  Only what differs from the default is stored
    (:meth:`to_json`).  The 24 options of :class:`DisplayOptions` are mirrored
    both ways (:meth:`display_options`, :meth:`take_display_options`) so that
    the Elements group and this object cannot disagree.
    """

    def __init__(self, values=None):
        from . import option_catalogue as catalogue

        self._catalogue = catalogue
        self._values: dict = {}
        #: Plan 48 GM11a (DP-4456). Options the user touched, whatever the
        #: value: a value equal to Gmsh's default is not stored in
        #: ``_values`` (so it is not saved), but it still overrides the
        #: Elements group's own defaults, which differ for four options.
        self._held: set = set()
        for name, raw in dict(values or {}).items():
            self.set(name, raw)

    # -- values ------------------------------------------------------------- #

    def get(self, name: str):
        if name not in DISPLAY_BINDINGS:
            raise KeyError(f'{name} is not a display option that draws')
        if name in self._values:
            return self._values[name]
        default = self._catalogue.get(name).default
        return tuple(default) if isinstance(default, (list, tuple)) \
            else default

    def set(self, name: str, raw) -> None:
        if name not in DISPLAY_BINDINGS:
            reason = NOTHING_TO_DRAW.get(name)
            raise ValueError(
                f'{name} draws nothing in FoamMesh ({reason})' if reason
                else f'{name} is not a display option')
        value = self._catalogue.parse_value(name, raw)
        option = self._catalogue.get(name)
        self._held.add(name)
        if self._catalogue.same_value(option, value, option.default):
            self._values.pop(name, None)
        else:
            self._values[name] = value

    def changed(self) -> dict:
        return dict(self._values)

    def held(self) -> frozenset:
        """The options set at least once since the last reset, at any value."""
        return frozenset(self._held)

    def reset(self) -> None:
        self._values.clear()
        self._held.clear()

    def colour(self, name: str) -> tuple:
        """The colour option *name* as ``(r, g, b, a)``."""
        value = self.get(name)
        if DISPLAY_BINDINGS[name].kind != 'color':
            raise ValueError(f'{name} is not a colour')
        return tuple(int(v) for v in value)

    def carousel(self) -> np.ndarray:
        """``Mesh.Color.Zero`` .. ``Nineteen`` as a ``(20, 3)`` table, the
        lookup table of the entity / partition colour modes."""
        names = ('Zero One Two Three Four Five Six Seven Eight Nine Ten '
                 'Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen '
                 'Eighteen Nineteen').split()
        return np.array([self.colour('Mesh.Color.' + word)[:3]
                         for word in names], dtype=np.uint8)

    # -- every value, as Gmsh holds it ---------------------------------------- #

    def gmsh_options(self) -> dict:
        """``{name: value}`` for every bound option, for ``options_apply``."""
        return {name: self.get(name) for name in DISPLAY_BINDINGS}

    # -- the Section tool ------------------------------------------------------ #

    def take_planes(self, planes) -> None:
        """Make ``Geometry.Clip`` / ``Mesh.Clip`` the section tool's planes."""
        mask = clip_mask(planes)
        self.set('Geometry.Clip', mask)
        self.set('Mesh.Clip', mask)

    def planes_for(self, planes, group: str = 'Mesh') -> list:
        return planes_with_mask(planes, self.get(group + '.Clip'))

    # -- the Elements group ----------------------------------------------------- #

    def display_options(self, base: 'DisplayOptions | None' = None):
        """The :class:`DisplayOptions` these values give (on top of *base*)."""
        base = base or DisplayOptions()
        changes = {}
        kinds = list(base.kinds)
        for name, field_name in _ELEMENTS_FIELD.items():
            if name not in self._values and name not in self._held:
                continue
            value = self.get(name)
            if field_name == 'kinds':
                kind = name.split('.', 1)[1]
                if value and kind not in kinds:
                    kinds.append(kind)
                if not value and kind in kinds:
                    kinds.remove(kind)
                changes['kinds'] = tuple(kinds)
            elif field_name == 'element_labels':
                changes['element_labels'] = bool(value) or \
                    changes.get('element_labels', False)
            elif isinstance(getattr(base, field_name), bool):
                changes[field_name] = bool(value)
            elif isinstance(getattr(base, field_name), int):
                changes[field_name] = int(value)
            else:
                changes[field_name] = float(value)
        return base.with_(**changes) if changes else base

    def take_display_options(self, options: 'DisplayOptions') -> None:
        """Hold the values the Elements group has."""
        for name, value in options.gmsh_options().items():
            if name in DISPLAY_BINDINGS and name in _ELEMENTS_FIELD:
                self.set(name, value)

    # -- persistence ---------------------------------------------------------- #

    def to_json(self) -> str:
        import json

        return json.dumps(
            {name: list(value) if isinstance(value, tuple) else value
             for name, value in sorted(self._values.items())},
            separators=(',', ':'))

    @classmethod
    def from_json(cls, text) -> 'GmshDisplaySettings':
        """Read what :meth:`to_json` wrote; a name this build does not bind
        is dropped (a setting from a newer or older release), never an
        error."""
        import json

        settings = cls()
        try:
            stored = json.loads(str(text)) if text else {}
        except ValueError:
            return settings
        if not isinstance(stored, dict):
            return settings
        for name, raw in stored.items():
            try:
                settings.set(name, raw)
            except (ValueError, KeyError):
                continue
            except Exception:                           # noqa: BLE001
                continue
        return settings
