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

_FACE_GREY = (0.72, 0.75, 0.8)
_EDGE = (0.1, 0.12, 0.15)
_NORMAL = (0.9, 0.2, 0.2)
_TANGENT = (0.2, 0.45, 0.95)
_VORONOI = (0.15, 0.6, 0.3)
_LABEL = (1.0, 1.0, 1.0)


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

    # -- loading ----------------------------------------------------------- #

    def show(self, path, group_names=None) -> dict:
        self.arrays = sd.read_arrays(path)
        self._built = {}
        self.group_names = dict(group_names or sd.read_group_names(path))
        return self.rebuild()

    def showSource(self, source) -> dict:
        """Draw an :class:`~foammesh.core.mesh.element_display.ElementSource`
        (Plan 42 SG9): the arrays of a Gmsh hand-over or of a snappy scene."""
        if source.arrays is not self.arrays:
            self._built = {}
        self.arrays = source.arrays
        self.group_names = dict(source.names)
        self.source = source
        return self.rebuild()

    def clear(self) -> None:
        self._drop()
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

    def _selected(self) -> np.ndarray:
        arrays, options = self.arrays, self.options
        keep = (sd.kind_mask(arrays, options.kinds)
                & sd.cells_visible(arrays, self.hidden))
        if self.planes and self.whole_elements:
            keep &= sd.keep_whole_elements(
                arrays, self.planes, self.clip_rule, self.clip_curves)
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

    def _colours(self, mode: int) -> np.ndarray:
        """RGB of every element for a colouring, found once per mode."""
        colours = self._built.setdefault('colours', {})
        if mode not in colours:
            colours[mode] = sd.colour_rgb(sd.colour_codes(self.arrays, mode))
        return colours[mode]

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
                       and any(p.on for p in self.planes))
            skin_only = bool(options.draw_skin_only and not cutting)
            for width in found['widths']:
                label = sd.face_label(width)
                block, owner = sd.select_faces(found, label, selected,
                                               skin_only, everything)
                if not len(block):
                    continue
                block = np.asarray(block, dtype=np.int64)
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

        # -- curve elements and nodes ------------------------------------ #
        curves = cells[dims == 1]
        curves = curves[arrays.sizes()[curves] == 2]
        if len(curves) and options.lines:
            block = np.stack([arrays.connectivity[arrays.offsets[curves]],
                              arrays.connectivity[arrays.offsets[curves] + 1]],
                             axis=1)
            data = _poly(self._points(), lines=block)
            _paint(data, rgb_all[curves])
            actor = actor_of(data)
            prop = actor.GetProperty()
            prop.SetLineWidth(max(options.line_width, 1.0) * 2.0)
            prop.SetLighting(options.light_lines in (1, 2))
            self._put('gmshMeshLines', actor)
            report['segments'] += len(curves)
        if options.points and len(cells):
            used = sd.nodes_used(arrays, cells)
            data = _poly(self._points(), verts=used)
            actor = actor_of(data, colour_cells=False)
            prop = actor.GetProperty()
            prop.SetColor(*_EDGE)
            prop.SetPointSize(options.point_size)
            prop.SetRenderPointsAsSpheres(options.point_type == 1)
            prop.SetLighting(False)
            self._put('gmshMeshPoints', actor)
            report['points'] = int(len(used))

        self._draw_vectors(cells, report)
        self._draw_labels(cells, report)
        renderer = getattr(self._view, 'renderer', None)
        if callable(renderer):
            renderer().SetTwoSidedLighting(bool(options.light_two_side))
        self._cut(report)
        self._shared_section(report)
        self.report = report
        self._view.refresh()
        return report

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
        active = [p for p in self.planes if p.on]
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
        for name, length, colour, build in (
                ('gmshMeshNormals', options.normals, _NORMAL,
                 sd.surface_normals),
                ('gmshMeshTangents', options.tangents, _TANGENT,
                 sd.curve_tangents)):
            if length <= 0 or not len(cells):
                continue
            segments = build(arrays, cells, length)
            if not len(segments):
                continue
            actor = self._segments(segments, colour)
            self._put(name, actor)
            report[name[8:].lower()] = int(len(segments))
        if options.voronoi:
            tets = cells[arrays.column('gmshElementType')[cells] == 4]
            segments = sd.voronoi_segments(arrays, tets)
            if len(segments):
                self._put('gmshMeshVoronoi', self._segments(segments, _VORONOI))
                report['voronoi'] = int(len(segments))

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
                where, texts = sd.element_label_rows(
                    arrays, labelled, options.label_type, self.group_names)
                self._put('gmshMeshElementLabels', self._labels(where, texts))
                report['element_labels'] = len(texts)
        if options.node_labels:
            where, texts = sd.node_label_rows(arrays, cells, options.label_type)
            where = where[::int(options.label_sampling)]
            texts = texts[::int(options.label_sampling)]
            if len(texts):
                self._put('gmshMeshNodeLabels', self._labels(where, texts))
                report['node_labels'] = len(texts)

    def _labels(self, where: np.ndarray, texts):
        from vtkmodules.vtkCommonCore import vtkStringArray
        from vtkmodules.vtkRenderingCore import vtkActor2D
        from vtkmodules.vtkRenderingLabel import vtkLabeledDataMapper

        data = _poly(where, verts=np.arange(len(where)))
        labels = vtkStringArray()
        labels.SetName('gmshLabel')
        for text in texts:
            labels.InsertNextValue(text)
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
