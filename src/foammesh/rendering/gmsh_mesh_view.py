"""The Gmsh session mesh drawn the way Gmsh's own Mesh options draw it.

Plan 41 GN17.  :class:`GmshMeshView` reads the hand-over ``mesh.vtu`` as flat
arrays (:mod:`foammesh.core.gmsh.session_display`) and builds the actors from
the display options: points, lines, surface and volume faces and edges, the
element kinds that are switched on, only the entities that are visible, the
six clipping planes, Explode, the colour carousel (element type / entity /
physical group / partition), element and node labels, normals, tangents and
the Voronoi diagram.  It touches the scene through the viewport widget only
(``addActor``, ``removeActor``, ``refresh``), like :class:`GmshEntityView`.
"""
from __future__ import annotations

import numpy as np

from foammesh.core.gmsh import session_display as sd
from foammesh.core.selection import mesh_on_screen as _on_screen

_PICK_PRIORITY = 1.6        # ahead of the geometry picker (1.5)

_FACE_GREY = (0.72, 0.75, 0.8)
_EDGE = (0.1, 0.12, 0.15)
_NORMAL = (0.9, 0.2, 0.2)
_TANGENT = (0.2, 0.45, 0.95)
_VORONOI = (0.15, 0.6, 0.3)
_LABEL = (1.0, 1.0, 1.0)
_DUAL = (0.1, 0.12, 0.15)


def _unit(rgba) -> tuple:
    """A Gmsh colour (0-255) as the 0-1 triple VTK takes."""
    return tuple(float(c) / 255.0 for c in rgba[:3])

# -- Plan 48 GM11a: what the drawing options of Gmsh's Mesh branch need ------- #

#: ``Mesh.Color.<Kind>`` for each element family (Mesh.ColorCarousel = 0).
_FAMILY_COLOUR = {
    'Line': 'Mesh.Color.Lines', 'Triangle': 'Mesh.Color.Triangles',
    'Quadrangle': 'Mesh.Color.Quadrangles',
    'Tetrahedron': 'Mesh.Color.Tetrahedra',
    'Hexahedron': 'Mesh.Color.Hexahedra', 'Prism': 'Mesh.Color.Prisms',
    'Pyramid': 'Mesh.Color.Pyramids', 'Trihedron': 'Mesh.Color.Trihedra',
}
#: Gmsh's element type of a trihedron (MSH_TRIH_4).
TRIHEDRON_TYPE = 140
#: Nodes that are corners, by family; the rest are second-order nodes.
_CORNERS = {'Line': 2, 'Triangle': 3, 'Quadrangle': 4, 'Tetrahedron': 4,
            'Hexahedron': 8, 'Prism': 6, 'Pyramid': 5, 'Trihedron': 4}
#: Gmsh draws the dual with lines stippled 0x0F0F.
DUAL_STIPPLE = 0x0F0F
#: Everything the colour of a cell can depend on.
_COLOUR_OPTIONS = tuple(sorted(
    name for name in sd.DISPLAY_BINDINGS
    if name.startswith('Mesh.Color.')))


def options_of(settings, base: 'sd.DisplayOptions') -> 'sd.DisplayOptions':
    """The Elements-group options *settings* gives, on top of *base*.

    Unlike :meth:`GmshDisplaySettings.display_options`, which applies only the
    values that differ from Gmsh's defaults, every one of the 24 is taken, so
    a value set back to Gmsh's default (``Mesh.Lines`` 0, ``Mesh.SurfaceFaces``
    0 ...) reaches the drawing too.  The group puts its own values into the
    settings (``take_display_options``) before the view is told.
    """
    changes = {}
    kinds = [k for k in base.kinds if k not in sd.ELEMENT_KINDS]
    labels = False
    for name, field_name in sd._ELEMENTS_FIELD.items():
        value = settings.get(name)
        if field_name == 'kinds':
            if value:
                kinds.append(name.split('.', 1)[1])
        elif field_name == 'element_labels':
            labels = labels or bool(value)
        elif isinstance(getattr(base, field_name), bool):
            changes[field_name] = bool(value)
        elif isinstance(getattr(base, field_name), int):
            changes[field_name] = int(value)
        else:
            changes[field_name] = float(value)
    ordered = [k for k in base.kinds if k in kinds] +         [k for k in kinds if k not in base.kinds]
    changes['kinds'] = tuple(ordered)
    changes['element_labels'] = labels
    return base.with_(**changes)


def node_labels(arrays, cells, label_type: int) -> tuple:
    """``sd.node_label_rows`` without a loop over the cells: the node numbers
    (an integer array) or the coordinates."""
    used = sd.nodes_used(arrays, cells)
    if label_type == 4:
        return arrays.points[used], ['%.4g, %.4g, %.4g' % tuple(t)
                                     for t in arrays.points[used].tolist()]
    return arrays.points[used], used.astype(np.int64) + 1


def element_labels(arrays, cells, label_type: int, names=None) -> tuple:
    """``sd.element_label_rows`` with numpy columns instead of a Python loop
    over the elements (the texts of one label type are built in bulk)."""
    cells = np.asarray(cells, dtype=np.int64)
    centre = arrays.centres()[cells]
    if label_type == 0:
        return centre, arrays.column('gmshElementTag')[cells].astype(np.int64)
    if label_type == 3:
        return centre, arrays.column('gmshPartition')[cells].astype(np.int64)
    if label_type == 1:
        dim = arrays.column('gmshEntityDim')[cells].astype(np.int64)
        entity = arrays.column('gmshEntityTag')[cells].astype(np.int64)
        letters = np.array(list('PCSV'))[dim]
        return centre, np.char.add(letters, entity.astype(str)).tolist()
    if label_type == 2:
        names = names or {}
        codes, inverse = np.unique(
            arrays.column('gmshPhysical')[cells].astype(np.int64),
            return_inverse=True)
        words = np.array([names.get(int(c)) or (str(int(c)) if c else '-')
                          for c in codes], dtype=object)
        return centre, words[inverse].tolist()
    if label_type == 4:
        return centre, ['%.4g, %.4g, %.4g' % tuple(t) for t in centre.tolist()]
    raise ValueError('unknown label type')


def type_colours(types: np.ndarray, settings) -> np.ndarray:
    """RGB per cell of ``Mesh.ColorCarousel = 0``: the ``Mesh.Color.*`` of
    the element's family (one lookup per distinct type, no loop over cells)."""
    unique, inverse = np.unique(types, return_inverse=True)
    table = np.zeros((len(unique), 3), dtype=np.uint8)
    for row, element_type in enumerate(unique):
        family = sd._FAMILY_OF_TYPE.get(int(element_type), '')
        name = _FAMILY_COLOUR.get(family)
        if name is None:
            table[row] = sd.CAROUSEL[row % len(sd.CAROUSEL)]
        else:
            table[row] = settings.colour(name)[:3]
    return table[inverse]


def split_second_order(arrays: 'sd.MeshArrays', cells) -> tuple:
    """``(corner nodes, second-order nodes)`` of *cells*: the nodes
    ``Mesh.Color.Nodes`` and ``Mesh.Color.NodesSup`` colour."""
    cells = np.asarray(cells, dtype=np.int64)
    if not len(cells):
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty
    sizes = arrays.sizes()[cells]
    owner = np.repeat(np.arange(len(cells)), sizes)
    start = np.concatenate(([0], np.cumsum(sizes)))[:-1]
    position = np.arange(int(sizes.sum())) - np.repeat(start, sizes)
    types = arrays.column('gmshElementType')[cells]
    corners = _corners_by_type(types)
    flat = np.repeat(arrays.offsets[cells], sizes) + position
    nodes = arrays.connectivity[flat]
    sup = position >= corners[owner]
    sup_nodes = np.unique(nodes[sup])
    first = np.unique(nodes[~sup])
    return np.setdiff1d(first, sup_nodes, assume_unique=True), sup_nodes


def _corners_by_type(types: np.ndarray) -> np.ndarray:
    unique, inverse = np.unique(types, return_inverse=True)
    table = np.array([_CORNERS.get(sd._FAMILY_OF_TYPE.get(int(t), ''), 1 << 30)
                      for t in unique], dtype=np.int64)
    return table[inverse]


def _edge_ring(width: int) -> tuple:
    return tuple((k, (k + 1) % width) for k in range(width))


def dual_segments(arrays: 'sd.MeshArrays', cells, faces: dict | None) -> tuple:
    """The barycentric dual Gmsh draws for ``Mesh.Dual``, as indexed lines.

    Returns ``(points (n, 3) float32, lines (m, 2) int64)``.  A surface
    element gives a line from its barycentre to the middle of each of its
    edges; a volume element a line from its barycentre to the barycentre of
    each face, and from there to the middle of each edge of the face
    (``drawBarycentricDual``).  Faces come from the face table, so a face two
    elements share is cut into edge lines once.  No loop over elements.
    """
    cells = np.asarray(cells, dtype=np.int64)
    centres = arrays.centres().astype(np.float32)
    point_blocks, line_blocks = [centres], []
    used = centres.shape[0]

    def add_points(block):
        nonlocal used
        start = used
        point_blocks.append(np.asarray(block, dtype=np.float32))
        used += len(block)
        return start

    dims = arrays.column('gmshEntityDim')
    surface = cells[(dims[cells] == 2) & np.isin(
        arrays.sizes()[cells], (3, 4))]
    for width in (3, 4):
        chosen = surface[arrays.sizes()[surface] == width]
        if not len(chosen):
            continue
        base = arrays.offsets[chosen]
        nodes = np.stack([arrays.connectivity[base + i]
                          for i in range(width)], axis=1)
        for a, b in _edge_ring(width):
            middle = 0.5 * (arrays.points[nodes[:, a]]
                            + arrays.points[nodes[:, b]])
            first = add_points(middle)
            line_blocks.append(np.stack(
                [chosen, first + np.arange(len(chosen))], axis=1))
    if faces is not None:
        selected = np.zeros(arrays.cell_count, dtype=bool)
        selected[cells[dims[cells] == 3]] = True
        for width in faces['widths']:
            label = sd.face_label(width)
            nodes = np.asarray(faces[label], dtype=np.int64)
            owner = np.asarray(faces[label + '_owner'], dtype=np.int64)
            owner2 = np.asarray(faces[label + '_owner2'], dtype=np.int64)
            if not len(nodes):
                continue
            one = selected[owner]
            two = (owner2 >= 0) & selected[np.maximum(owner2, 0)]
            keep = np.flatnonzero(one | two)
            if not len(keep):
                continue
            nodes, owner, owner2 = nodes[keep], owner[keep], owner2[keep]
            one, two = one[keep], two[keep]
            centre = arrays.points[nodes].mean(axis=1)
            at = add_points(centre) + np.arange(len(keep))
            line_blocks.append(np.stack([owner[one], at[one]], axis=1))
            line_blocks.append(np.stack([owner2[two], at[two]], axis=1))
            for a, b in _edge_ring(width):
                middle = 0.5 * (arrays.points[nodes[:, a]]
                                + arrays.points[nodes[:, b]])
                first = add_points(middle)
                line_blocks.append(np.stack(
                    [at, first + np.arange(len(keep))], axis=1))
    if not line_blocks:
        return np.zeros((0, 3), np.float32), np.zeros((0, 2), np.int64)
    return (np.concatenate(point_blocks),
            np.concatenate(line_blocks).astype(np.int64))


def _poly(points: np.ndarray, polys=(), lines=(), verts=()):
    """A vtkPolyData from numpy point and connectivity blocks."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    data = vtkPolyData()
    if isinstance(points, vtkPoints):
        vtk_points = points              # the cached node table, shared
    else:
        vtk_points = vtkPoints()
        vtk_points.SetData(numpy_to_vtk(np.ascontiguousarray(points, float),
                                        deep=True))
    data.SetPoints(vtk_points)

    def cells(block):
        array = vtkCellArray()
        block = np.asarray(block, dtype=np.int64)
        if not len(block):
            return array
        width = block.shape[1]
        offsets = np.arange(0, (len(block) + 1) * width, width,
                            dtype=np.int64)
        array.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                      numpy_to_vtkIdTypeArray(block.reshape(-1), deep=True))
        return array

    if len(polys):
        data.SetPolys(cells(polys))
    if len(lines):
        data.SetLines(cells(lines))
    if len(verts):
        data.SetVerts(cells(np.asarray(verts).reshape(-1, 1)))
    return data


def _paint(data, rgb: np.ndarray) -> None:
    from vtkmodules.util.numpy_support import numpy_to_vtk

    array = numpy_to_vtk(np.ascontiguousarray(rgb, dtype=np.uint8), deep=True)
    array.SetName('gmshRGB')
    data.GetCellData().SetScalars(array)


def clip_plane_of(plane) -> 'sd.ClipPlane':
    """A ``vtkPlane`` (normal into the kept half) as ``a x + b y + c z + d``."""
    normal = np.array(plane.GetNormal(), dtype=float)
    normal /= np.linalg.norm(normal)
    origin = np.array(plane.GetOrigin(), dtype=float)
    return sd.ClipPlane(float(normal[0]), float(normal[1]), float(normal[2]),
                        float(-normal @ origin), True)


class GmshMeshView:
    """Draw ``mesh.vtu`` by Gmsh's display options."""

    def __init__(self, view):
        self._view = view
        self.arrays: sd.MeshArrays | None = None
        self.options = sd.DisplayOptions()
        self.hidden: set = set()
        self.planes: list = []
        self.whole_elements = True
        #: Plan 42 SG12. The whole-cell rule of the Section tool and whether
        #: line and point elements are clipped with the rest.
        self.clip_rule = 'any'
        self.clip_curves = True
        self.group_names: dict = {}
        self._actors: dict = {}
        self.report: dict = {}
        self.source = None
        #: Plan 42 SG9. The shared section: ``(is_clip, [vtkPlane])``.
        self.section: tuple | None = None
        #: Plan 42 SG24 (DP-1427). What is built once per mesh and reused by
        #: every display change: the volume face table (with both owners of
        #: each face), the node table as VTK points, the colours per mode.
        self._built: dict = {}
        #: Plan 48 GM11a. The values of the Gmsh display options that no
        #: Elements control holds (colours, dual, smooth normals, clip mask);
        #: ``None`` draws as before.  Read live by every rebuild.
        self.settings = None
        #: Plan 48 PK (DP-4503). What the Cells filter of the Pick tool hits:
        #: ``(kind, node block, element index per row)`` for the faces and
        #: segments the last rebuild drew, the locators over them (built on the
        #: first pick), and the picker that is fed.
        self._pick_parts: list = []
        self._pick_locators: list | None = None
        self._cells_picker = None
        self._pick_observers: list = []
        self._fallback = None
        self.service_provider = None

    # -- loading ----------------------------------------------------------- #

    def show(self, path, group_names=None) -> dict:
        self.arrays = sd.read_arrays(path)
        self._built = {}
        self.group_names = dict(group_names or sd.read_group_names(path))
        report = self.rebuild()
        self._announce()
        return report

    def showSource(self, source) -> dict:
        """Draw an :class:`~foammesh.core.mesh.element_display.ElementSource`
        (Plan 42 SG9): the arrays of a Gmsh hand-over or of a snappy scene."""
        if source.arrays is not self.arrays:
            self._built = {}
        self.arrays = source.arrays
        self.group_names = dict(source.names)
        self.source = source
        report = self.rebuild()
        self._announce()
        return report

    def clear(self) -> None:
        self._drop()
        self._release_pick()
        self._pick_parts, self._pick_locators = [], None
        self.arrays = None
        self.source = None
        self.report = {}
        self._built = {}

    @property
    def loaded(self) -> bool:
        return self.arrays is not None

    # -- state ------------------------------------------------------------- #

    def setOptions(self, options: sd.DisplayOptions) -> dict:
        options.validate()
        self.options = options
        return self.rebuild()

    def applySettings(self, settings) -> dict:
        """Draw by the values of the Gmsh display options (Plan 48 GM11a).

        *settings* is a :class:`~foammesh.core.gmsh.session_display.GmshDisplaySettings`.
        The 24 options the Elements group holds are taken into the options of
        this view, all of them, defaults included (the group puts its own
        values into *settings* first: ``take_display_options``);
        the rest -- the 43 colours with the carousel table, ``Mesh.Dual``,
        ``Mesh.SmoothNormals`` and ``Mesh.Clip`` -- are read from *settings*
        by every rebuild."""
        self.settings = settings
        if settings is not None:
            self.options = options_of(settings, self.options)
        return self.rebuild() if self.arrays is not None else {}

    def setHidden(self, hidden) -> dict:
        self.hidden = {tuple(h) for h in hidden}
        return self.rebuild()

    def setClip(self, planes, whole: bool = True) -> dict:
        self.planes = sd.check_planes(planes)
        self.whole_elements = bool(whole)
        return self.rebuild()

    def applySection(self, clip: bool | None, planes, whole: bool = False,
                     rule: str = 'any', curves: bool = True,
                     draw: bool = True) -> dict:
        """The Section tool's section (Plan 42 SG12).

        *clip* is ``True`` for a clip, ``False`` for a slice, ``None`` for no
        section; *planes* are its ``vtkPlane`` s (the normal points into the
        kept half).  A clip is the Gmsh clip: *whole* keeps an element whole by
        *rule* (``any``, ``centroid`` or ``entire``), otherwise the elements
        are cut at the planes; *curves* off leaves lines and points alone.
        """
        self.clip_rule = rule
        self.clip_curves = bool(curves)
        self.whole_elements = bool(whole)
        if clip is None or not planes:
            self.planes, self.section = [], None
        elif clip:
            self.planes = [clip_plane_of(plane) for plane in planes]
            self.section = None
        else:
            self.planes, self.section = [], (False, list(planes))
        if not draw or self.arrays is None:
            return {}
        return self.rebuild()

    def setSection(self, section) -> dict:
        """The shared section on the producer's actors (Plan 42 SG9).

        *section* is ``(is_clip, planes)`` from the Section tool, or ``None``.
        A clip keeps what the standard actors keep; a slice keeps the line
        where the planes meet the elements.
        """
        self.section = section if section and section[1] else None
        return self.rebuild() if self.arrays is not None else {}

    def actor(self, name: str):
        return self._actors.get(name)

    def actorNames(self) -> list:
        return sorted(self._actors)

    # -- drawing ------------------------------------------------------------ #

    def _drop(self) -> None:
        for actor in self._actors.values():
            self._view.removeActor(actor)
        self._actors = {}

    def _put(self, name: str, actor) -> None:
        actor.SetObjectName(name) if hasattr(actor, 'SetObjectName') else None
        self._actors[name] = actor
        self._view.addActor(actor)

    def effective_planes(self) -> list:
        """The clipping planes in force: the Section tool's, switched on and
        off by the bits of ``Mesh.Clip`` once that option is set."""
        settings = self.settings
        if settings is not None and 'Mesh.Clip' in settings.changed():
            return sd.planes_with_mask(self.planes, settings.get('Mesh.Clip'))
        return self.planes

    def _selected(self) -> np.ndarray:
        arrays, options = self.arrays, self.options
        keep = (sd.kind_mask(arrays, options.kinds)
                & sd.cells_visible(arrays, self.hidden))
        planes = self.effective_planes()
        if planes and self.whole_elements:
            keep &= sd.keep_whole_elements(
                arrays, planes, self.clip_rule, self.clip_curves)
        return np.nonzero(keep)[0]

    # -- what is built once per mesh (DP-1427) ------------------------------- #

    def _points(self):
        """The node table as ``vtkPoints``, made once and shared by every
        polydata the producer draws whole (nodes, unshrunk faces, lines)."""
        if 'points' not in self._built:
            from vtkmodules.util.numpy_support import numpy_to_vtk
            from vtkmodules.vtkCommonCore import vtkPoints

            table = vtkPoints()
            table.SetData(numpy_to_vtk(
                np.ascontiguousarray(self.arrays.points, float), deep=True))
            self._built['points'] = table
        return self._built['points']

    def _colour_key(self) -> tuple:
        settings = self.settings
        if settings is None:
            return ()
        return tuple(tuple(settings.get(name)) for name in _COLOUR_OPTIONS)

    def _colours(self, mode: int) -> np.ndarray:
        """RGB of every element for a colouring, found once per mode (and
        per set of ``Mesh.Color.*`` values)."""
        colours = self._built.setdefault('colours', {})
        key = (mode, self._colour_key())
        if key not in colours:
            settings = self.settings
            if settings is None:
                colours[key] = sd.colour_rgb(sd.colour_codes(self.arrays, mode))
            elif mode == 0:
                colours[key] = type_colours(
                    self.arrays.column('gmshElementType'), settings)
            else:
                colours[key] = sd.colour_rgb(
                    sd.colour_codes(self.arrays, mode), settings.carousel())
        return colours[key]

    def legend_colours(self, mode: int | None = None) -> list:
        """The RGB of each colour in use, in the order of the sorted colour
        codes (the order a legend lists them)."""
        if self.arrays is None:
            return []
        mode = self.options.colour_mode if mode is None else mode
        _codes, first = np.unique(sd.colour_codes(self.arrays, mode),
                                  return_index=True)
        table = self._colours(mode)
        return [tuple(int(c) for c in table[at]) for at in first]

    def legend(self, mode: int | None = None) -> list:
        """``[(label, (r, g, b))]`` for the colours in use, in the colours
        this view draws (the carousel table and the element-type colours of
        the settings)."""
        if self.arrays is None:
            return []
        mode = self.options.colour_mode if mode is None else mode
        codes = np.unique(sd.colour_codes(self.arrays, mode))
        return [(sd.colour_legend_label(self.arrays, mode, int(code),
                                        self.group_names), rgb)
                for code, rgb in zip(codes, self.legend_colours(mode))]

    def _face_table(self) -> dict:
        """The faces of all the volume elements with both owners of each,
        built on the first draw and kept: a display change (colour, shrink,
        what is hidden, the kinds on) only selects from it."""
        if 'faces' not in self._built:
            arrays = self.arrays
            every = np.flatnonzero(arrays.column('gmshEntityDim') == 3)
            self._built['faces'] = sd.volume_faces(arrays, every)
            self._built['volume_cells'] = len(every)
        return self._built['faces']

    def rebuild(self) -> dict:
        if self.arrays is None:
            return {}
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        self._drop()
        self._pick_parts, self._pick_locators = [], None
        arrays, options = self.arrays, self.options
        cells = self._selected()
        dims = arrays.column('gmshEntityDim')[cells]
        rgb_all = self._colours(options.colour_mode)
        explode = options.explode
        report = {'cells': int(len(cells)), 'hidden_entities': len(self.hidden),
                  'faces': 0, 'segments': 0}

        def actor_of(data, *, colour_cells=True):
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(data)
            if colour_cells:
                mapper.SetColorModeToDirectScalars()
                mapper.SetScalarModeToUseCellData()
                mapper.ScalarVisibilityOn()
            else:
                mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            return actor

        def explode_block(block, owner):
            """Faces as polydata (shared nodes, or shrunk about the centre)."""
            if explode < 1.0:
                corners = arrays.points[block]
                centre = corners.mean(axis=1, keepdims=True)
                shrunk = (centre + (corners - centre) * explode).reshape(-1, 3)
                ids = np.arange(len(shrunk)).reshape(block.shape)
                return _poly(shrunk, polys=ids)
            return _poly(self._points(), polys=block)

        # -- surface elements (2D) --------------------------------------- #
        surface = cells[dims == 2]
        if len(surface) and (options.surface_faces or options.surface_edges):
            types = arrays.column('gmshElementType')[surface]
            for width, label in ((3, 'Surface'), (4, 'SurfaceQuads')):
                chosen = surface[arrays.sizes()[surface] == width]
                if not len(chosen):
                    continue
                block = np.stack([arrays.connectivity[arrays.offsets[chosen] + i]
                                  for i in range(width)], axis=1)
                self._pick_parts.append(('surface', block, chosen))
                data = explode_block(block, chosen)
                _paint(data, rgb_all[chosen])
                actor = actor_of(data)
                # Surface elements win over the coincident skin of the volume.
                actor.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                actor.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(-2.0, -2.0)
                self._style_faces(actor, options.surface_faces,
                                  options.surface_edges)
                self._put('gmshMesh' + label, actor)
                report['faces'] += len(chosen)

        # -- volume elements (3D) ---------------------------------------- #
        volumes = cells[dims == 3]
        if len(volumes) and (options.volume_faces or options.volume_edges):
            found = self._face_table()
            selected = np.zeros(arrays.cell_count, dtype=bool)
            selected[volumes] = True
            everything = len(volumes) == self._built['volume_cells']
            # A plane that cuts through elements opens the model up, so
            # the faces inside it are drawn whatever 'skin only' says.
            cutting = (not self.whole_elements
                       and any(p.on for p in self.effective_planes()))
            skin_only = bool(options.draw_skin_only and not cutting)
            for width in found['widths']:
                label = sd.face_label(width)
                block, owner = sd.select_faces(found, label, selected,
                                               skin_only, everything)
                if not len(block):
                    continue
                block = np.asarray(block, dtype=np.int64)
                self._pick_parts.append(('volume', block, np.asarray(owner)))
                data = explode_block(block, owner)
                _paint(data, rgb_all[owner])
                actor = actor_of(data)
                # The standard mesh is not hidden any more (Plan 42 SG9), so
                # these faces win over its coincident skin.
                actor.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                actor.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(-2.0, -2.0)
                self._style_faces(actor, options.volume_faces,
                                  options.volume_edges)
                # An actor key, not a screen name: the face label joins
                # the key in camel case (tri -> gmshMeshVolumeTri).
                self._put('gmshMeshVolume' + label[:1].upper() + label[1:],
                          actor)
                report['faces'] += len(block)

        # -- high-order curves and surfaces (Mesh.NumSubEdges) ----------- #
        self._curved(cells, rgb_all, report, actor_of)

        # -- trihedra (Mesh.Trihedra, Mesh.Color.Trihedra; DP-4450) ------- #
        self._draw_trihedra(cells, rgb_all, report, actor_of)

        # -- curve elements and nodes ------------------------------------ #
        curves = cells[dims == 1]
        curves = curves[arrays.sizes()[curves] == 2]
        if len(curves) and options.lines:
            block = np.stack([arrays.connectivity[arrays.offsets[curves]],
                              arrays.connectivity[arrays.offsets[curves] + 1]],
                             axis=1)
            self._pick_parts.append(('line', block, curves))
            data = _poly(self._points(), lines=block)
            _paint(data, rgb_all[curves])
            actor = actor_of(data)
            prop = actor.GetProperty()
            prop.SetLineWidth(max(options.line_width, 1.0) * 2.0)
            prop.SetLighting(options.light_lines in (1, 2))
            self._put('gmshMeshLines', actor)
            report['segments'] += len(curves)
        if options.points and len(cells):
            settings = self.settings
            if settings is None:
                groups = (('gmshMeshPoints', sd.nodes_used(arrays, cells),
                           _EDGE),)
            else:
                # Mesh.Color.Nodes for the corners, NodesSup for the nodes a
                # second-order element adds.
                corner, sup = split_second_order(arrays, cells)
                groups = (('gmshMeshPoints', corner,
                           _unit(settings.colour('Mesh.Color.Nodes'))),
                          ('gmshMeshPointsSup', sup,
                           _unit(settings.colour('Mesh.Color.NodesSup'))))
            report['points'] = 0
            for name, used, colour in groups:
                if not len(used) and name != 'gmshMeshPoints':
                    continue
                data = _poly(self._points(), verts=used)
                actor = actor_of(data, colour_cells=False)
                prop = actor.GetProperty()
                prop.SetColor(*colour)
                prop.SetPointSize(options.point_size)
                prop.SetRenderPointsAsSpheres(options.point_type == 1)
                prop.SetLighting(False)
                self._put(name, actor)
                report['points'] += int(len(used))

        self._draw_vectors(cells, report)
        self._draw_dual(cells, report)
        self._smooth(report)
        self._draw_labels(cells, report)
        renderer = getattr(self._view, 'renderer', None)
        if callable(renderer):
            renderer().SetTwoSidedLighting(bool(options.light_two_side))
        self._cut(report)
        self._shared_section(report)
        self.report = report
        self._view.refresh()
        return report

    def _draw_trihedra(self, cells, rgb_all, report: dict, actor_of) -> None:
        """A trihedron (Gmsh type 140: apex, then the three nodes it points
        at) as its three edges, in ``Mesh.Color.Trihedra`` with the carousel
        off.  The kind filter has already applied ``Mesh.Trihedra``."""
        arrays = self.arrays
        types = arrays.column('gmshElementType')[cells]
        chosen = cells[types == TRIHEDRON_TYPE]
        if not len(chosen):
            return
        first = arrays.offsets[chosen]
        apex = arrays.connectivity[first]
        block = np.stack([np.stack([apex, arrays.connectivity[first + k]],
                                   axis=1) for k in (1, 2, 3)],
                         axis=1).reshape(-1, 2)
        owner = np.repeat(chosen, 3)
        self._pick_parts.append(('line', block, owner))
        data = _poly(self._points(), lines=block)
        _paint(data, rgb_all[owner])
        actor = actor_of(data)
        prop = actor.GetProperty()
        prop.SetLineWidth(max(self.options.line_width, 1.0) * 2.0)
        prop.SetLighting(False)
        self._put('gmshMeshTrihedra', actor)
        report['trihedra'] = int(len(chosen))

    def _curved(self, cells, rgb_all, report: dict, actor_of) -> None:
        """Curves and surfaces of order 2 and up: every edge is cut into
        ``Mesh.NumSubEdges`` straight pieces laid on the curved edge, and the
        faces into the sub-faces those pieces bound (O-08).  The outline is
        its own actor, so a face is not scored across by its sub-faces."""
        arrays, options = self.arrays, self.options
        curved = sd.high_order_cells(arrays, cells)
        report['curved'] = int(len(curved))
        if not len(curved):
            return
        drawn = sd.subdivide_high_order(arrays, curved,
                                        options.num_sub_edges)
        points = drawn['points']
        if options.surface_faces:
            for key, label in (('triangles', 'CurvedSurface'),
                               ('quads', 'CurvedSurfaceQuads')):
                block = drawn[key]
                if not len(block):
                    continue
                data = _poly(points, polys=block)
                _paint(data, rgb_all[drawn[key[:-1] + '_owner']])
                actor = actor_of(data)
                actor.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                actor.GetMapper(
                ).SetRelativeCoincidentTopologyPolygonOffsetParameters(
                    -2.0, -2.0)
                self._style_faces(actor, True, False)
                self._put('gmshMesh' + label, actor)
                report['faces'] += len(block)
        segments, owner = drawn['segments'], drawn['segment_owner']
        on_surface = arrays.column('gmshEntityDim')[owner] == 2
        for name, wanted, colour in (
                ('gmshMeshCurvedEdges', on_surface & bool(
                    options.surface_edges), False),
                ('gmshMeshLinesCurved', ~on_surface & bool(options.lines),
                 True)):
            chosen = np.nonzero(wanted)[0]
            if not len(chosen):
                continue
            data = _poly(points, lines=segments[chosen])
            if colour:
                _paint(data, rgb_all[owner[chosen]])
            actor = actor_of(data, colour_cells=colour)
            prop = actor.GetProperty()
            if colour:
                prop.SetLineWidth(max(options.line_width, 1.0) * 2.0)
                prop.SetLighting(options.light_lines in (1, 2))
            else:
                prop.SetColor(*_EDGE)
                prop.SetLighting(False)
                prop.SetLineWidth(max(options.line_width, 1.0))
            actor.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
            actor.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(
                -3.0, -3.0)
            self._put(name, actor)
            report['segments'] += len(chosen)

    def _style_faces(self, actor, faces: bool, edges: bool) -> None:
        prop = actor.GetProperty()
        prop.SetLighting(bool(self.options.light))
        prop.SetEdgeColor(*_EDGE)
        if faces:
            prop.SetRepresentationToSurface()
            prop.SetEdgeVisibility(bool(edges))
        elif edges:
            prop.SetRepresentationToWireframe()
            prop.SetLineWidth(max(self.options.line_width, 1.0))
        else:
            actor.SetVisibility(False)
        prop.SetLineWidth(max(self.options.line_width, 1.0))

    def _cut(self, report: dict) -> None:
        """Clip planes with 'keep whole elements' off: the faces are cut."""
        active = [p for p in self.effective_planes() if p.on]
        if self.whole_elements or not active:
            return
        from vtkmodules.vtkCommonDataModel import vtkPlane
        from vtkmodules.vtkFiltersCore import vtkClipPolyData

        for name, actor in list(self._actors.items()):
            if actor.IsA('vtkActor2D'):               # labels are not cut
                continue
            if not self.clip_curves and name in ('gmshMeshLines',
                                                 'gmshMeshLinesCurved',
                                                 'gmshMeshPoints'):
                continue
            mapper = actor.GetMapper()
            data = mapper.GetInput()
            if data is None or not (data.GetNumberOfPolys()
                                    or data.GetNumberOfLines()
                                    or data.GetNumberOfVerts()):
                continue
            for plane in active:
                implicit = vtkPlane()
                normal = plane.normal() / np.linalg.norm(plane.normal())
                implicit.SetNormal(*normal)
                implicit.SetOrigin(*(-plane.d / np.linalg.norm(
                    plane.normal()) * normal))
                clip = vtkClipPolyData()
                clip.SetInputData(data)
                clip.SetClipFunction(implicit)
                clip.GenerateClipScalarsOff()
                clip.Update()
                data = clip.GetOutput()
            mapper.SetInputData(data)
        report['cut'] = True

    def _shared_section(self, report: dict) -> None:
        """Cut every drawn actor by the Section tool's planes."""
        if self.section is None:
            return
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkCutter
        from vtkmodules.vtkFiltersCore import vtkClipPolyData

        is_clip, planes = self.section
        for name, actor in list(self._actors.items()):
            if actor.IsA('vtkActor2D'):
                continue
            mapper = actor.GetMapper()
            data = mapper.GetInput()
            if data is None or not (data.GetNumberOfPolys()
                                    or data.GetNumberOfLines()
                                    or data.GetNumberOfVerts()):
                continue
            if is_clip:
                for plane in planes:
                    clip = vtkClipPolyData()
                    clip.SetInputData(data)
                    clip.SetClipFunction(plane)
                    clip.InsideOutOff()
                    clip.GenerateClipScalarsOff()
                    clip.Update()
                    data = clip.GetOutput()
            else:
                union = vtkAppendPolyData()
                for plane in planes:
                    cutter = vtkCutter()
                    cutter.SetCutFunction(plane)
                    cutter.SetInputData(data)
                    cutter.Update()
                    piece = cutter.GetOutput()
                    union.AddInputData(piece)
                union.Update()
                data = union.GetOutput()
            mapper.SetInputData(data)
        report['section'] = 'clip' if is_clip else 'slice'

    def _draw_vectors(self, cells: np.ndarray, report: dict) -> None:
        arrays, options = self.arrays, self.options
        settings = self.settings
        normal = _NORMAL if settings is None else _unit(
            settings.colour('Mesh.Color.Normals'))
        tangent = _TANGENT if settings is None else _unit(
            settings.colour('Mesh.Color.Tangents'))
        for name, length, colour, build in (
                ('gmshMeshNormals', options.normals, normal,
                 sd.surface_normals),
                ('gmshMeshTangents', options.tangents, tangent,
                 sd.curve_tangents)):
            if length <= 0 or not len(cells):
                continue
            segments = build(arrays, cells, length)
            if not len(segments):
                continue
            actor = self._segments(segments, colour)
            self._put(name, actor)
            report[name[8:].lower()] = int(len(segments))
        # Gmsh draws the dual instead of the Voronoi diagram when both are on.
        if options.voronoi and not self._dual_on():
            tets = cells[arrays.column('gmshElementType')[cells] == 4]
            segments = sd.voronoi_segments(arrays, tets)
            if len(segments):
                self._put('gmshMeshVoronoi', self._segments(segments, _VORONOI))
                report['voronoi'] = int(len(segments))

    def _dual_on(self) -> bool:
        settings = self.settings
        return bool(settings is not None and settings.get('Mesh.Dual'))

    def _draw_dual(self, cells: np.ndarray, report: dict) -> None:
        """``Mesh.Dual``: the barycentric dual, stippled lines in the
        foreground colour (Gmsh's ``drawBarycentricDual``)."""
        if not self._dual_on() or not len(cells):
            return
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        dims = self.arrays.column('gmshEntityDim')[cells]
        faces = self._face_table() if np.any(dims == 3) else None
        points, lines = dual_segments(self.arrays, cells, faces)
        report['dual'] = int(len(lines))
        if not len(lines):
            return
        data = _poly(points, lines=lines)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*_DUAL)
        prop.SetLineWidth(max(self.options.line_width, 1.0))
        prop.SetLighting(False)
        prop.SetLineStipplePattern(DUAL_STIPPLE)
        prop.SetLineStippleRepeatFactor(1)
        actor.PickableOff()
        self._put('gmshMeshDual', actor)

    def _smooth(self, report: dict) -> None:
        """``Mesh.SmoothNormals``: the surface and volume faces are shaded
        with the normals averaged at the nodes (a normals filter, Phong
        interpolation)."""
        settings = self.settings
        if settings is None or not settings.get('Mesh.SmoothNormals'):
            return
        from vtkmodules.vtkFiltersCore import vtkPolyDataNormals

        smoothed = 0
        for name, actor in self._actors.items():
            if not name.startswith(('gmshMeshSurface', 'gmshMeshVolume',
                                    'gmshMeshCurvedSurface')):
                continue
            mapper = actor.GetMapper()
            data = mapper.GetInput()
            if data is None or not data.GetNumberOfPolys():
                continue
            filt = vtkPolyDataNormals()
            filt.SetInputData(data)
            filt.ComputePointNormalsOn()
            filt.ComputeCellNormalsOff()
            filt.SplittingOff()
            filt.ConsistencyOff()
            filt.AutoOrientNormalsOff()
            filt.Update()
            mapper.SetInputData(filt.GetOutput())
            actor.GetProperty().SetInterpolationToPhong()
            smoothed += 1
        report['smooth_normals'] = smoothed

    def _segments(self, segments: np.ndarray, colour):
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        count = len(segments)
        points = segments.reshape(-1, 3)
        ids = np.arange(2 * count).reshape(count, 2)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(_poly(points, lines=ids))
        mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*colour)
        prop.SetLineWidth(2.0)
        prop.SetLighting(False)
        actor.PickableOff()
        return actor

    def _draw_labels(self, cells: np.ndarray, report: dict) -> None:
        arrays, options = self.arrays, self.options
        if not len(cells):
            return
        if options.element_labels:
            labelled = cells[sd.kind_mask(arrays, options.label_kinds)[cells]]
            labelled = labelled[::int(options.label_sampling)]
            if len(labelled):
                where, texts = element_labels(
                    arrays, labelled, options.label_type, self.group_names)
                self._put('gmshMeshElementLabels', self._labels(where, texts))
                report['element_labels'] = len(texts)
        if options.node_labels:
            where, texts = node_labels(arrays, cells, options.label_type)
            where = where[::int(options.label_sampling)]
            texts = texts[::int(options.label_sampling)]
            if len(texts):
                self._put('gmshMeshNodeLabels', self._labels(where, texts))
                report['node_labels'] = len(texts)

    def _labels(self, where: np.ndarray, texts):
        from vtkmodules.util.numpy_support import numpy_to_vtk
        from vtkmodules.vtkCommonCore import vtkStringArray
        from vtkmodules.vtkRenderingCore import vtkActor2D
        from vtkmodules.vtkRenderingLabel import vtkLabeledDataMapper

        data = _poly(where, verts=np.arange(len(where)))
        if isinstance(texts, np.ndarray) and texts.dtype.kind in 'iu':
            # numbers go to VTK as one array (the mapper formats them)
            labels = numpy_to_vtk(np.ascontiguousarray(texts, dtype=np.int64),
                                  deep=True)
        else:
            labels = vtkStringArray()
            for text in texts:
                labels.InsertNextValue(text)
        labels.SetName('gmshLabel')
        data.GetPointData().AddArray(labels)
        mapper = vtkLabeledDataMapper()
        mapper.SetInputData(data)
        mapper.SetLabelModeToLabelFieldData()
        mapper.SetFieldDataName('gmshLabel')
        prop = mapper.GetLabelTextProperty()
        prop.SetFontSize(11)
        prop.SetColor(*_LABEL)
        prop.SetBackgroundColor(0.1, 0.12, 0.15)
        prop.SetBackgroundOpacity(0.7)
        prop.SetJustificationToCentered()
        actor = vtkActor2D()
        actor.SetMapper(mapper)
        return actor


    # -- picking mesh elements (Plan 48 PK, DP-4503) ------------------------- #

    def _announce(self) -> None:
        """A mesh is on screen: the Pick tool may offer Cells."""
        _on_screen.register(self)
        self._install_fallback_driver()

    def _release_pick(self) -> None:
        self.detach_picker()
        self._remove_fallback_driver()
        _on_screen.unregister(self)

    def element_ids(self, cells) -> list:
        """The element tags of drawn-array rows *cells*: Gmsh's own tag when
        the hand-over carries one, else the row (a polyMesh cell index)."""
        cells = np.asarray(cells, dtype=np.int64)
        if self.arrays is None:
            return []
        tags = self.arrays.columns.get('gmshElementTag')
        found = cells if tags is None else np.asarray(tags)[cells]
        return [int(t) for t in found]

    def cell_of_element(self, tag: int):
        """The array row of element *tag*, or None."""
        if self.arrays is None:
            return None
        tags = self.arrays.columns.get('gmshElementTag')
        if tags is None:
            return int(tag) if 0 <= int(tag) < self.arrays.cell_count \
                else None
        found = np.nonzero(np.asarray(tags) == int(tag))[0]
        return int(found[0]) if len(found) else None

    def has_element(self, tag: int) -> bool:
        return self.cell_of_element(tag) is not None

    def _locators(self) -> list:
        """``(kind, polydata, locator, rows)`` over what is drawn."""
        if self._pick_locators is None:
            from vtkmodules.vtkCommonDataModel import vtkStaticCellLocator

            built = []
            # A polyMesh scene (no Gmsh tags) draws its patch faces as 2D
            # rows; those are not cells, so only volume cells are hit there.
            gmsh_tags = 'gmshElementTag' in self.arrays.columns
            for kind, block, rows in self._pick_parts:
                if not len(block) or (kind != 'volume' and not gmsh_tags):
                    continue
                data = (_poly(self._points(), lines=block) if kind == 'line'
                        else _poly(self._points(), polys=block))
                locator = vtkStaticCellLocator()
                locator.SetDataSet(data)
                locator.BuildLocator()
                built.append((kind, data, locator, np.asarray(rows)))
            self._pick_locators = built
        return self._pick_locators

    def pickElement(self, x: float, y: float):
        """The element tag under a display point, or None.

        A surface element wins over the face of the volume element behind it
        (they are drawn in the same place); otherwise the nearest drawn face
        decides, and its owner is the element picked."""
        from vtkmodules.vtkCommonCore import mutable

        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is None or self.arrays is None:
            return None
        ends = []
        for depth in (0.0, 1.0):
            renderer.SetDisplayPoint(float(x), float(y), depth)
            renderer.DisplayToWorld()
            w = renderer.GetWorldPoint()
            ends.append([w[i] / (w[3] or 1.0) for i in range(3)])
        best = None
        for kind, data, locator, rows in self._locators():
            tolerance = data.GetLength() * (0.004 if kind == 'line' else 0.0)
            t, point, pcoords = mutable(0.0), [0.0] * 3, [0.0] * 3
            sub, cell = mutable(0), mutable(0)
            if not locator.IntersectWithLine(ends[0], ends[1], tolerance, t,
                                             point, pcoords, sub, cell):
                continue
            # coincident surface elements and lines win a tie with the skin
            depth = float(t) - (2e-3 if kind != 'volume' else 0.0)
            if best is None or depth < best[0]:
                best = (depth, int(rows[int(cell)]))
        if best is None:
            return None
        return self.element_ids([best[1]])[0]

    # -- the picker that is fed ----------------------------------------------- #

    def attach_picker(self, picker) -> None:
        """Feed *picker* (an ``EntityPicker``) the element under the cursor
        while its filter is Cells; the geometry picker keeps the other
        filters."""
        self._cells_picker = picker
        if self._pick_observers:
            return
        interactor = getattr(self._view, 'interactor', None)
        interactor = interactor() if callable(interactor) else None
        if interactor is None or not hasattr(interactor, 'AddObserver'):
            return
        for event, callback in (('MouseMoveEvent', self._pick_moved),
                                ('LeftButtonPressEvent', self._pick_pressed),
                                ('KeyPressEvent', self._pick_key)):
            self._pick_observers.append((interactor, interactor.AddObserver(
                event, callback, _PICK_PRIORITY)))

    def detach_picker(self) -> None:
        for subject, tag in self._pick_observers:
            try:
                subject.RemoveObserver(tag)
            except Exception:                                 # noqa: BLE001
                pass
        self._pick_observers = []
        self._cells_picker = None

    def _cells_live(self) -> bool:
        picker = self._cells_picker
        return bool(picker is not None and picker.active
                    and picker.filter == 'Cells')

    def _take(self, interactor) -> None:
        for subject, tag in self._pick_observers:
            if subject is interactor:
                command = interactor.GetCommand(tag)
                if command is not None:
                    command.SetAbortFlag(1)

    def _pick_moved(self, interactor, _event) -> None:
        if self._cells_live():
            x, y = interactor.GetEventPosition()
            self._cells_picker.hover_element(self.pickElement(x, y))

    def _pick_pressed(self, interactor, _event) -> None:
        if not self._cells_live():
            return
        x, y = interactor.GetEventPosition()
        found = self.click_element(self.pickElement(x, y),
                                   shift=bool(interactor.GetShiftKey()))
        if found is not None:
            self._take(interactor)

    def _pick_key(self, interactor, _event) -> None:
        if self._cells_live() and self._cells_picker.key(
                interactor.GetKeySym()):
            self._take(interactor)

    def click_element(self, tag, *, shift: bool = False):
        """What a click on element *tag* does: the one path of a viewport
        click and of the local API."""
        if tag is None or not self._cells_live():
            return None
        return self._cells_picker.click_element(int(tag), shift=shift)

    def click_elements(self, tags, *, extend: bool = False) -> list:
        """Click each of *tags* as the viewport would; the first replaces the
        pending list unless *extend*.  Refuses a tag the mesh does not hold."""
        if not self._cells_live():
            raise ValueError('there is no Cells pick in progress')
        tags = [int(t) for t in tags]
        missing = [t for t in tags if not self.has_element(t)]
        if missing:
            raise ValueError('the mesh on screen holds no element %s'
                             % ', '.join(str(t) for t in missing[:5]))
        for index, tag in enumerate(tags):
            self.click_element(tag, shift=extend or index > 0)
        return [e[1] for e in self._cells_picker.selection]

    # -- the Pick tool where no panel serves it (snappy) ------------------------ #

    def _install_fallback_driver(self) -> None:
        """The mesh viewport serves Cells requests itself when no geometry
        panel has registered as the Pick tool (the snappy route)."""
        service = self._selection_service()
        if service is None or service.has_pick_driver() or \
                self._fallback is not None:
            return
        from foammesh.core.gmsh import session_entities as entities

        picker = entities.EntityPicker(entities.EntityMap([], []))
        self._fallback = (service, picker)
        service.set_pick_driver(self._serve_cells, source=self)
        service.pick_changed.connect(self._service_pick_changed)

    def _remove_fallback_driver(self) -> None:
        held, self._fallback = self._fallback, None
        if held is None:
            return
        service, picker = held
        try:
            service.pick_changed.disconnect(self._service_pick_changed)
        except (RuntimeError, TypeError):
            pass
        if service.pick_source is self:
            service.set_pick_driver(None)
        picker.abort()

    def _selection_service(self):
        """The application's selection service (``service_provider`` stands
        in for it where there is no application)."""
        if self.service_provider is not None:
            return self.service_provider()
        try:
            from foammesh.app import app

            return app.selectionService()
        except Exception:                                     # noqa: BLE001
            return None

    def pick_service(self):
        """The service the Pick tool runs on, or None."""
        return self._selection_service()

    def hasModel(self) -> bool:                    # the SharedPick contract
        return False

    def _serve_cells(self, request) -> bool:
        service, picker = self._fallback
        if 'Cells' not in request.filters:
            service.refuse('Only mesh cells can be picked here; %s needs the '
                           'geometry.' % ', '.join(request.filters))
            return False
        if self.arrays is None:
            service.refuse('There is no mesh on screen to pick cells in.')
            return False
        picker.abort()
        picker.begin('Cells', on_accept=service.accept_pick,
                     on_abort=service.cancel_pick)
        self.attach_picker(picker)
        return True

    def _service_pick_changed(self, request) -> None:
        held = self._fallback
        if held is not None and request is None and held[1].active:
            held[1].abort()

    def cells_state(self) -> dict:
        """What the Cells pick holds now (the local API reads it)."""
        picker = self._cells_picker
        return {'on_screen': self.arrays is not None,
                'active': self._cells_live(),
                'filter': picker.filter if picker is not None else None,
                'elements': ([e[1] for e in picker.selection]
                             if picker is not None else []),
                'hovered': (picker.hovered[1] if picker is not None
                            and picker.hovered else None),
                'element_count': (0 if self.arrays is None
                                  else int(self.arrays.cell_count))}
