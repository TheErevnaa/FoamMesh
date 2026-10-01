#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
import weakref
from enum import Enum, auto
from dataclasses import dataclass, replace

from PySide6.QtGui import QColor
from PySide6.QtCore import QObject, Signal
from vtkmodules.vtkCommonDataModel import vtkDataObject, vtkPlane, vtkPolyData
from vtkmodules.vtkFiltersCore import (
    vtkAppendPolyData, vtkClipPolyData, vtkThreshold, vtkPassThrough, vtkCutter,
    vtkFeatureEdges)
from vtkmodules.vtkFiltersExtraction import vtkExtractPolyDataGeometry, vtkExtractGeometry
from vtkmodules.vtkFiltersGeneral import vtkTableBasedClipDataSet
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkDataSetMapper, vtkActor, vtkMapper
from vtkmodules.vtkRenderingLOD import vtkQuadricLODActor

from foammesh.support import disposal
from foammesh.support.vtk_threads import vtk_run_in_thread
from foammesh.support.mesh import Bounds
from foammesh.support.colormap import qualityBandLut, sequentialRedLut
from foammesh.app import app
from foammesh.view.theming.tokens import PALETTE_FAMILIES, PATCH_TOKENS
from foammesh.view.theming.patch_palette import palette_colours, slot_colour
from foammesh.view.theming.vtk_theme import rgb


#: A vtkLightKit is installed on every viewport and was then almost entirely
#: ignored: diffuse 0.3 with ambient 0.3 is a direction-independent wash, which
#: is why a box used to render with its top, front and end faces at the same
#: brightness. Diffuse now carries the form, ambient only lifts the shadow
#: side, and a narrow specular gives the surface a material to read.
BASE_DIFFUSE = 0.82
BASE_AMBIENT = 0.20
BASE_SPECULAR = 0.14
BASE_SPECULAR_POWER = 30

#: Interior mesh edges are information while a cell is several pixels across
#: and noise once it is not. At full strength they tile the surface and become
#: the fill -- which is what a "blue mesh" actually was. Kept, but faint; the
#: silhouette below is what carries the shape.
#:
#: DP-710 (viewport audit 0925 F7). "Faint" went too far: at 0.22 in a muted
#: grey the S5 grid measured a mean edge gradient of 2.77 at fit and 1.38 at
#: 3x zoom -- the grid the user asked to see was effectively not drawn, and
#: nothing let them turn it up. The default is now 0.6 in a colour picked to
#: contrast with the surface the lines lie on, and the Mesh lines control
#: sets opacity, colour and width for every actor (see `setMeshLineStyle`).
EDGE_OPACITY = 0.6
HIGHLIGHT_EDGE_OPACITY = 0.45


def applySurfaceMaterial(prop) -> None:
    """Light a surface the way the main viewport lights every part.

    DP-819. The split-by-angle preview kept VTK's defaults -- diffuse 1, no
    ambient, no specular -- so a patch drawn there in the very colour it
    wears in the viewport still shaded to a different tone.
    """
    prop.SetInterpolationToPhong()
    prop.SetDiffuse(BASE_DIFFUSE)
    prop.SetAmbient(BASE_AMBIENT)
    prop.SetSpecular(BASE_SPECULAR)
    prop.SetSpecularPower(BASE_SPECULAR_POWER)


@dataclass(frozen=True)
class MeshLineStyle:
    """How interior mesh edges are drawn, on every actor at once.

    ``color`` is ``'auto'`` -- dark lines on a light surface, light lines on a
    dark one -- or a ``#rrggbb`` the user chose.
    """
    opacity: float = EDGE_OPACITY
    color: str = 'auto'
    width: float = 1.0


MESH_LINE_WIDTH_RANGE = (1.0, 3.0)
#: Auto colours: what a line reads as against a light and a dark surface.
MESH_LINE_DARK = '#1c2530'
MESH_LINE_LIGHT = '#eef2f7'

_meshLineStyle = MeshLineStyle()
#: Every live actor, so one change of style reaches all of them without each
#: caller having to know which manager owns which actor.
_liveActors = weakref.WeakSet()


def meshLineStyle() -> MeshLineStyle:
    return _meshLineStyle


class _MeshLineStyleNotifier(QObject):
    changed = Signal()


_styleNotifier = None


def meshLineStyleNotifier() -> _MeshLineStyleNotifier:
    """DP-738. Says when the mesh-line style changed, whoever changed it.

    The toolbar and the display panel each hold a control for the one style;
    each re-reads it on this signal, so the two never show different values.
    """
    global _styleNotifier
    if _styleNotifier is None:
        _styleNotifier = _MeshLineStyleNotifier()
    return _styleNotifier


def _validColor(value) -> str:
    text = str(value or 'auto')
    if text == 'auto':
        return text
    color = QColor(text)
    return color.name() if color.isValid() else 'auto'


def setMeshLineStyle(opacity=None, color=None, width=None) -> MeshLineStyle:
    """Change the mesh-line style and repaint every live actor with it."""
    global _meshLineStyle
    current = _meshLineStyle
    low, high = MESH_LINE_WIDTH_RANGE
    _meshLineStyle = MeshLineStyle(
        opacity=min(1.0, max(0.0, float(
            current.opacity if opacity is None else opacity))),
        color=_validColor(current.color if color is None else color),
        width=min(high, max(low, float(
            current.width if width is None else width))))
    for actor in list(_liveActors):
        try:
            actor.applyMeshLineStyle()
        except RuntimeError:
            # A C++ object Qt already destroyed; nothing left to paint.
            pass
    meshLineStyleNotifier().changed.emit()
    return _meshLineStyle


def meshLineColorFor(surface: QColor, style: MeshLineStyle = None) -> str:
    """The colour interior edges take on a surface of colour *surface*."""
    style = style or _meshLineStyle
    if style.color != 'auto':
        return style.color
    lum = (0.2126 * surface.redF() + 0.7152 * surface.greenF()
           + 0.0722 * surface.blueF())
    return MESH_LINE_DARK if lum >= 0.45 else MESH_LINE_LIGHT


def highlightEdgeOpacity(style: MeshLineStyle = None) -> float:
    """A selected part's edges stay a step stronger than everyone else's."""
    style = style or _meshLineStyle
    return min(1.0, max(HIGHLIGHT_EDGE_OPACITY, style.opacity * 1.5))

#: Feature/boundary edges drawn as a separate prop. This is the outline that
#: interior edges used to be relied on for, at a width that survives a mesh
#: dense enough to hide every individual cell.
SILHOUETTE_WIDTH = 1.6
HIGHLIGHT_SILHOUETTE_WIDTH = 3.0
SILHOUETTE_FEATURE_ANGLE = 30.0

#: R5/R44. The selection cue used to be the outline alone, and it did not
#: read: on the split tee, clicking `inlet`, `outlet_branch` and `wall_main`
#: in turn left every pixel of the render identical -- a 1px edge net at 45%
#: opacity plus a 1.6->3.0px outline, drawn in a cyan that sits next to the
#: teal and blue the patch palette already uses. On the small caps, which are
#: 3% of the surface each, the outline is nearly the whole patch and still
#: invisible. So the *area* now carries the cue: the selected surface is
#: painted the role colour and lifted out of the surrounding shading. The
#: paint is render state only -- `Properties.color` and the Display Control
#: swatch keep the part's real colour, and leaving the highlight restores it.
HIGHLIGHT_AMBIENT = 0.45

#: Role colours for the selection cue, shared by `setHighlightRole` and
#: `applyTheme`; they were duplicated, which is how the outline and the edges
#: came to disagree about which colour "selected" is.
HIGHLIGHT_ROLE_COLORS = {
    'selected': '#00a6d6',
    'preview': '#f59e0b',
    'stale': '#94a3b8',
    'invalid': '#ef4444',
}

#: Past this many displayed cells, wireframe stops being information. Every
#: cell edge drawn at a few pixels a cell is a grey rectangle, so wireframe
#: falls back to feature edges -- and says that it has.
WIREFRAME_CELL_LIMIT = 250_000


class DisplayMode(Enum):
    WIREFRAME      = auto()  # noqa: E221
    SURFACE        = auto()  # noqa: E221
    SURFACE_EDGE   = auto()  # noqa: E221


class CutMode(Enum):
    """How a clip plane treats the cells it passes through.

    Measured on ``test_cases/gmsh/duct`` and the snappy annulus: the only cut
    that has ever existed here is ``CRINKLE`` -- ``vtkExtractGeometry`` with
    ``ExtractBoundaryCells`` keeps every straddling cell whole. So a clipped
    mesh was never the hollow shell it was assumed to be; it was a jagged
    whole-cell face that reads as rubble rather than as a section.

    ``SMOOTH`` truncates cells at the plane, which is the flat readable face a
    section is *for*. Both are wanted: smooth to see inside, crinkle to count
    prism layers, because crinkle is the mode that keeps cell shapes intact.
    """
    SMOOTH  = auto()  # noqa: E221
    CRINKLE = auto()
    #: Plan 37 UF7. The whole cells the first plane meets, on both sides,
    #: masked by the other planes (`foammesh.rendering.cut_cells`).
    CUT_CELLS = auto()


class MeshQualityIndex(Enum):
    ASPECT_RATIO = 'cellAspectRatio'
    NON_ORTHO_ANGLE = 'nonOrthoAngle'
    SKEWNESS = 'skewness'
    VOLUME = 'cellVolume'

    @classmethod
    def values(cls):
        return [c.value for c in cls]


#: DP-714. Metrics whose bad cells are the low values: the tiny cells.
QUALITY_WORST_IS_LOW = frozenset({MeshQualityIndex.VOLUME})

#: DP-737. The relative polygon offset of the poor-cell overlay. A flagged
#: cell on the boundary has a face exactly where the patch face over it is;
#: with no offset the two tie in the depth test and the red shimmers through
#: the faded patch as the model turns. Negative pulls the cells in front.
POOR_CELL_DEPTH_BIAS = -2.0


@dataclass
class Properties:
    visibility: bool
    opacity: float
    color: QColor
    displayMode: DisplayMode
    cutEnabled: bool
    highlighted: bool

    def merge(self, properties):
        self.visibility = properties.visibility if properties.visibility == self.visibility else None
        self.opacity = properties.opacity if properties.opacity == self.opacity else None
        self.color = properties.color if properties.color == self.color else None
        self.displayMode = properties.displayMode if properties.displayMode == self.displayMode else None
        self.cutEnabled = properties.cutEnabled if properties.cutEnabled == self.cutEnabled else None


class ActorType(Enum):
    GEOMETRY = auto()
    BOUNDARY = auto()
    MESH = auto()


class ActorInfo(QObject):
    sourceChanged = Signal(str)
    nameChanged = Signal(str)
    #: The actor's colour changed, from any cause -- a theme swap, a palette
    #: assignment, or the user picking one. Display Control's swatches were
    #: painted once when the row was built, which happens *before* the palette
    #: is assigned, so they showed the pre-palette colour forever.
    colorChanged = Signal()
    #: Plan 35 CR3 step 10. The outline computed on the VTK thread is in.
    silhouetteChanged = Signal()

    def __init__(self, dataSet, id_, name, type_):
        super().__init__()

        self._dataSet = dataSet
        self._id = id_
        self._name = name
        self._type = type_

        self._cellFilter = vtkPassThrough()
        self._cellFilter.SetInputData(dataSet)
        self._cutFilters = [vtkPassThrough()]
        self._cutFilters[0].SetInputConnection(self._cellFilter.GetOutputPort())

        self._mqEnabled = False
        self._mqIndex = MeshQualityIndex.VOLUME
        self._mqMax = 0
        self._mqMin = 0
        self._mqHigh = 0
        self._mqLow = 0

        self._mapper: vtkMapper = self._initMapper()
        self._mapper.SetInputConnection(self._cutFilters[0].GetOutputPort())
        self._mapper.ScalarVisibilityOff()
        self._mapper.SetScalarModeToUseCellFieldData()
        self._mapper.SetColorModeToMapScalars()
        self._mapper.SetLookupTable(sequentialRedLut)
        #: Standing depth-test bias, in polygon-offset units. Restored
        #: whenever something that suspends it is taken back off.
        self._depthBias = 0.0

        self._actor = self._createActor()
        self._actor.SetMapper(self._mapper)
        self._actor.SetObjectName(self._id)
        prop = self._actor.GetProperty()
        applySurfaceMaterial(prop)

        # The outline follows the same pipeline output as the surface, so a
        # clip, a slice or a quality threshold reshapes both together.
        self._silhouetteFilter = vtkFeatureEdges()
        self._silhouetteFilter.BoundaryEdgesOn()
        self._silhouetteFilter.FeatureEdgesOn()
        self._silhouetteFilter.SetFeatureAngle(SILHOUETTE_FEATURE_ANGLE)
        self._silhouetteFilter.ManifoldEdgesOff()
        self._silhouetteFilter.NonManifoldEdgesOff()
        self._silhouetteFilter.ColoringOff()
        self._silhouetteFilter.SetInputConnection(
            self._cutFilters[0].GetOutputPort())
        self._silhouetteMapper = vtkPolyDataMapper()
        self._silhouetteMapper.SetInputConnection(
            self._silhouetteFilter.GetOutputPort())
        self._silhouetteMapper.ScalarVisibilityOff()
        self._silhouetteMapper.SetResolveCoincidentTopologyToPolygonOffset()
        self._silhouette = vtkActor()
        self._silhouette.SetMapper(self._silhouetteMapper)
        self._silhouette.SetObjectName(f'{self._id}:silhouette')
        self._silhouette.GetProperty().SetLighting(False)
        self._silhouette.GetProperty().SetLineWidth(SILHOUETTE_WIDTH)
        self._silhouette.GetProperty().SetRenderLinesAsTubes(True)
        # The picker must return the surface, never its outline, or clicking a
        # patch in the viewport would select something Display Control has no
        # row for.
        self._silhouette.PickableOff()

        self._highlightColor = '#00a6d6'
        self._surfaceColor = '#b3c0cf'
        self._silhouetteColor = '#233040'
        # Plan 35 CR3 step 10. The surface and outline a worker (or the VTK
        # thread) already computed for the uncut data, drawn instead of this
        # actor's own geometry and feature-edge filters -- which would
        # otherwise run on the GUI thread at the first paint.
        self._precomputedSurface = None
        self._precomputedEdges = None
        self._highlightRole = None
        #: Slot in a categorical palette, or None for the neutral surface
        #: colour. Held as an index into a named family rather than as a colour
        #: so a theme change recolours the actor instead of stranding it on the
        #: previous theme's palette.
        self._paletteIndex = None
        self._paletteFamily = 'patch'
        #: A colour the user picked is theirs; a theme change must not take it.
        self._colorIsDefault = True
        self._paletteColors = {}
        self._cutMode = CutMode.SMOOTH
        #: Name of the per-face field currently colouring this actor.
        self._faceScalarName = None

        self._properties = None
        self._applySurfaceColor()
        self._properties = Properties(bool(self._actor.GetVisibility()),
                                      prop.GetOpacity(),
                                      QColor.fromRgbF(*prop.GetColor()),
                                      DisplayMode.SURFACE,
                                      True, False)

        self._disposed = False
        _liveActors.add(self)
        disposal.track(self, 'ActorInfo')

    #: Plan 35 CR3 step 7. The applicators used to be held as a dict of
    #: bound methods on the instance, so every actor referred to itself and
    #: could only be freed by the cyclic collector -- on whatever thread
    #: tripped it, with whatever OpenGL context was current there. Held by
    #: name on the class, the actor carries no cycle of its own.
    _DISPLAY_MODE_APPLICATORS = {
        DisplayMode.WIREFRAME: '_applyWireframeMode',
        DisplayMode.SURFACE: '_applySurfaceMode',
        DisplayMode.SURFACE_EDGE: '_applySurfaceEdgeMode',
    }

    def _applyDisplayMode(self, mode):
        getattr(self, self._DISPLAY_MODE_APPLICATORS[mode])()

    def isDisposed(self) -> bool:
        return self._disposed

    def dispose(self, renderWindow=None):
        """Release this actor's GPU resources and connections, on the GUI thread.

        Plan 35 CR3 step 6. Called by whatever takes the actor out of the
        scene, after its props have left the renderer, so that the OpenGL
        buffers are released against the window they were drawn in rather
        than by a destructor the collector runs on some other thread.
        Idempotent. The actor must not be shown again afterwards.
        """
        if self._disposed:
            return
        self._disposed = True
        _liveActors.discard(self)
        disposal.untrack(self, 'ActorInfo')
        disposal.disconnect_all(
            self.sourceChanged, self.nameChanged, self.colorChanged,
            self.silhouetteChanged)
        self._precomputedSurface = None
        self._precomputedEdges = None
        disposal.release_graphics_resources(self.renderProps(), renderWindow)
        # The pipeline itself is left intact: with the applicator cycle gone
        # the actor holds no reference to itself, so once its owner drops it
        # reference counting frees the filters here, on this thread. Tearing
        # them down early would only turn a stray late caller -- a selection
        # still holding the part -- into a crash instead of a no-op.

    def id(self):
        return self._id

    def name(self):
        return self._name

    def type(self):
        return self._type

    def dataSet(self):
        return self._dataSet

    def actor(self):
        return self._actor

    def renderProps(self):
        """Every prop this actor owns, in the order a renderer should add them.

        The surface is no longer the whole picture: a feature-edge outline
        rides alongside it, so callers that add or remove "the actor" have to
        move both or the outline is orphaned in the scene.
        """
        return self._actor, self._silhouette

    def properties(self):
        return self._properties

    def bounds(self):
        return Bounds(*self._dataSet.GetBounds())

    def isVisible(self):
        return self._properties.visibility

    def color(self):
        return self._properties.color

    def usePrecomputed(self, surface=None, edges=None):
        """Draw ``surface`` and ``edges`` in place of this actor's own filters.

        Plan 35 CR3 step 10. Both describe the *uncut, unfiltered* data: while
        a clip, a slice or a quality threshold is on, the filters run as
        before, and the precomputed parts come back when it is taken off.
        """
        self._precomputedSurface = surface
        self._precomputedEdges = edges
        self._wirePrecomputed()

    def _precomputedActive(self) -> bool:
        return (len(self._cutFilters) == 1
                and isinstance(self._cellFilter, vtkPassThrough))

    def _wirePrecomputed(self):
        if self._precomputedEdges is not None and self._precomputedActive():
            self._silhouetteMapper.SetInputData(self._precomputedEdges)
        else:
            self._silhouetteMapper.SetInputConnection(
                self._silhouetteFilter.GetOutputPort())

    def setDataSet(self, dataSet, *, like=None):
        """New data under the same actor.

        ``like`` is another actor for the same data whose precomputed surface
        and outline (step 10) this one takes over; without it they are
        dropped, since they described the old data.
        """
        self._dataSet = dataSet
        self._precomputedSurface = getattr(like, '_precomputedSurface', None)
        self._precomputedEdges = getattr(like, '_precomputedEdges', None)
        self._wirePrecomputed()

        self._cellFilter.SetInputData(dataSet)

        self._mapper.Update()

        self.sourceChanged.emit(self._id)

    def setName(self, name):
        self._name = name
        self.nameChanged.emit(name)

    def setVisible(self, visibility):
        self._properties.visibility = visibility
        self._actor.SetVisibility(visibility)
        self._silhouette.SetVisibility(visibility)

    def setOpacity(self, opacity):
        self._properties.opacity = opacity
        self._actor.GetProperty().SetOpacity(opacity)
        # An outline at full strength around a surface faded to 20% reads as a
        # wireframe cage, so the outline fades with what it outlines.
        self._silhouette.GetProperty().SetOpacity(opacity)

    def _applyDepthBias(self, units: float):
        self._mapper.SetResolveCoincidentTopologyToPolygonOffset()
        self._mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(
            units, units)

    def setDepthBias(self, units: float):
        """Break a tie with another surface drawn in the same place (DP-141).

        Two actors can describe the same square metre of mesh -- a region's
        volume and the named boundary patches that are its skin are the
        standing example -- and when they do, the depth buffer has no answer:
        which one a pixel shows comes down to float error in two different
        triangulations of the same face. MEASURED on a snapped elbow: the
        result is per-pixel salt-and-pepper across the whole model, and the
        patch palette that exists to tell inlet from outlet from wall is
        shredded into noise.

        A positive ``units`` pushes this actor away from the camera by that
        many depth units *relative* to everything else, so the tie stops
        being a tie. It moves nothing in the scene -- the offset is applied
        in the depth test, not to the geometry -- so the picture is the same
        picture, minus the fight.
        """
        self._depthBias = float(units)
        self._applyDepthBias(self._depthBias)

    def setColor(self, color: QColor):
        self._properties.color = color
        self._colorIsDefault = False
        self._actor.GetProperty().SetColor(color.redF(), color.greenF(), color.blueF())
        if not self._properties.highlighted:
            self._actor.GetProperty().SetEdgeColor(*rgb(self._meshLineColor()))
        self.colorChanged.emit()

    def setFaceScalars(self, name: str, face_ids, values, *,
                       start_face: int | None = None, value_range=None,
                       lookup_table=None) -> bool:
        """Colour this actor by a per-face field.

        ``face_ids`` are the mesh's own face labels. A patch's cells are its
        faces in label order from ``start_face``, so face ``f`` is cell
        ``f - start_face``; with ``start_face`` given the values are scattered
        there, and a field over part of a patch lands on that part.

        DP-713 (viewport audit 0925 F8). This used to scatter by position --
        the first value onto the first cell whatever face it was measured on
        -- so a field over any subset of a patch painted the wrong faces. With
        no ``start_face`` a field is only taken when it has one value per
        cell, the one case where position and label agree.

        ``value_range`` is the range the colours span; the caller passes one
        range for every patch, so a colour means the same distance
        everywhere. Faces with no measurement stay ``nan`` and are drawn in
        the lookup table's nan colour -- absent, not zero, because zero on a
        deviation field is a claim of perfection.
        """
        import numpy as np
        from vtkmodules.util.numpy_support import numpy_to_vtk

        data = self._dataSet
        if data is None:
            return False
        count = data.GetNumberOfCells()
        values = np.asarray(values, dtype=np.float64).ravel()
        if count <= 0 or values.size == 0:
            return False

        scalars = np.full(count, np.nan, dtype=np.float64)
        if start_face is not None:
            cells = (np.asarray(face_ids, dtype=np.int64).ravel()
                     - int(start_face))
            if cells.size != values.size:
                return False
            inside = (cells >= 0) & (cells < count)
            scalars[cells[inside]] = values[inside]
        elif values.size == count:
            scalars[:] = values
        else:
            return False

        finite = scalars[np.isfinite(scalars)]
        if finite.size == 0:
            return False

        array = numpy_to_vtk(scalars, deep=True)
        array.SetName(name)
        data.GetCellData().AddArray(array)
        data.GetCellData().SetActiveScalars(name)

        if value_range is None:
            value_range = (float(finite.min()), float(finite.max()))
        low, high = (float(value_range[0]), float(value_range[1]))
        if lookup_table is not None:
            lookup_table.SetTableRange(low, high)
            self._mapper.SetLookupTable(lookup_table)
        self._mapper.SetScalarModeToUseCellFieldData()
        self._mapper.SelectColorArray(name)
        self._mapper.SetScalarRange(low, high)
        self._mapper.UseLookupTableScalarRangeOff()
        self._mapper.ScalarVisibilityOn()
        self._mapper.Update()
        self._faceScalarName = name
        return True

    def faceScalars(self, name: str | None = None):
        """The per-face field this actor is coloured by, in cell order."""
        from vtkmodules.util.numpy_support import vtk_to_numpy

        name = name or getattr(self, '_faceScalarName', None)
        if name is None or self._dataSet is None:
            return None
        array = self._dataSet.GetCellData().GetArray(name)
        return None if array is None else vtk_to_numpy(array).copy()

    def scalarRange(self):
        """The range the mapper spans its colours over."""
        return tuple(self._mapper.GetScalarRange())

    def clearFaceScalars(self):
        if getattr(self, '_faceScalarName', None) is None:
            return
        self._mapper.ScalarVisibilityOff()
        # DP-713. The deviation scale is lent for the colouring only; the
        # quality colouring shares this mapper and expects its own back.
        self._mapper.SetLookupTable(sequentialRedLut)
        self._faceScalarName = None
        self._mapper.Update()

    def faceScalarName(self):
        return getattr(self, '_faceScalarName', None)

    def resetColor(self):
        """Return to the themed palette colour, discarding a chosen one."""
        self._colorIsDefault = True
        self._applySurfaceColor()

    def setPaletteIndex(self, index: int | None, family: str = 'patch'):
        """Give this actor a slot in one of the categorical palettes.

        Boundary patches all rendered in VTK's default white, so the only way
        to tell an inlet from a wall was to click it. The index rather than the
        colour is stored because the colour belongs to the active theme.

        ``family`` selects which palette the slot indexes: ``patch`` for
        boundaries, ``zone`` for cell and face zones. Zones deliberately draw
        from a different hue family so a zone is never read as a patch.
        """
        self._paletteIndex = index
        self._paletteFamily = family
        self._applySurfaceColor()

    def setExplodeOffset(self, offset):
        """Push this actor away from the scene centre by ``offset``.

        For a conjugate or multi-zone mesh this is the fastest way to see what
        is actually in the case: parts that hide inside other parts come out
        where they can be counted. The outline moves with the surface, or the
        two separate and the picture stops making sense.
        """
        self._actor.SetPosition(*offset)
        self._silhouette.SetPosition(*offset)

    def setCutMode(self, mode: CutMode):
        """Choose whether a clip truncates cells or keeps them whole.

        Takes effect on the next :meth:`clip`; the caller re-applies the active
        planes, because rebuilding the chain here would silently discard them.
        """
        self._cutMode = mode

    def cutMode(self) -> CutMode:
        return self._cutMode

    def setDisplayMode(self, mode):
        """Draw this part the way it was asked to be drawn, selected or not.

        The three mode applicators used to refuse to run while the part was
        highlighted, and Display Control's Wireframe / Surface / Surface with
        Edges actions act on *the selected rows* -- which are exactly the
        highlighted ones. So the only parts the mode could be applied to were
        the only parts it refused to touch: asking a selected patch for
        wireframe recorded WIREFRAME in its properties and left it rendering
        as a surface until the selection happened to be dropped.

        The highlight is a layer on top of the mode, not a mode of its own,
        so re-asserting it applies the new mode first and then repaints the
        selection cue over it.
        """
        self._properties.displayMode = mode
        if self._properties.highlighted:
            self._highlightOn()
        else:
            self._applyDisplayMode(mode)

    def setCutEnabled(self, enabled):
        self._properties.cutEnabled = enabled

    def setHighlighted(self, highlighted):
        if self._properties.highlighted != highlighted:
            self._properties.highlighted = highlighted
            if highlighted:
                self._highlightOn()
            else:
                self._highlightOff()

    def setHighlightRole(self, role):
        """Apply a predictable selection-state cue in addition to status text."""
        self._highlightRole = role
        if role is None:
            if app.themeManager is not None and app.themeManager.tokens is not None:
                self._highlightColor = app.themeManager.tokens.value(
                    'accent.default')
            else:
                self._highlightColor = HIGHLIGHT_ROLE_COLORS['selected']
            self.setHighlighted(False)
            return
        self._highlightColor = HIGHLIGHT_ROLE_COLORS[role]
        if self._properties.highlighted:
            # R44. Display Control turns the highlight on the moment its row is
            # selected, which is *before* the role reaches here, so
            # `setHighlighted(True)` is a no-op and only the edge colour used
            # to be corrected. MEASURED: the outline then stayed on the theme
            # accent while the edges wore the role colour -- two cues
            # disagreeing about the same selection. Re-assert the whole cue.
            self._highlightOn()
        else:
            self.setHighlighted(True)

    def applyTheme(self, tokens):
        self._highlightColor = (
            HIGHLIGHT_ROLE_COLORS.get(self._highlightRole)
            or tokens.value('accent.default'))
        # DP-710. Interior edges no longer wear a fixed muted grey; their
        # colour follows the Mesh lines style and the surface they lie on.
        self._surfaceColor = tokens.value('viewport.surface')
        self._silhouetteColor = tokens.value('viewport.silhouette')
        self._paletteColors = {
            family: palette_colours(tokens, family)
            for family in PALETTE_FAMILIES
        }
        self._applySurfaceColor()
        if self._properties.highlighted:
            self._actor.GetProperty().SetEdgeColor(*rgb(self._highlightColor))
            self._silhouette.GetProperty().SetColor(*rgb(self._highlightColor))
        else:
            self._actor.GetProperty().SetEdgeColor(*rgb(self._meshLineColor()))
            self._silhouette.GetProperty().SetColor(*rgb(self._silhouetteColor))

    def _paletteColor(self) -> str:
        """The themed colour this actor should wear when it has not been set."""
        families = getattr(self, '_paletteColors', None) or {}
        colors = families.get(getattr(self, '_paletteFamily', 'patch'), ())
        # DP-819. The split preview reads the same slot the same way.
        return slot_colour(colors, self._paletteIndex) or self._surfaceColor

    def _currentSurfaceColor(self) -> QColor:
        """The colour this actor wears when nothing is highlighting it."""
        if self._colorIsDefault:
            return QColor(self._paletteColor())
        return self._properties.color

    def _applySurfaceColor(self):
        if not self._colorIsDefault:
            return
        color = QColor(self._paletteColor())
        # R5/R44. A highlighted actor is wearing the selection colour, so a
        # theme swap or a palette re-assignment must not paint over it -- the
        # part's own colour is still recorded, and returns when the highlight
        # leaves.
        if self._properties is None or not self._properties.highlighted:
            self._actor.GetProperty().SetColor(
                color.redF(), color.greenF(), color.blueF())
            self._actor.GetProperty().SetEdgeColor(
                *rgb(meshLineColorFor(color)))
            self._silhouette.GetProperty().SetColor(*rgb(self._silhouetteColor))
        if self._properties is not None:
            self._properties.color = color
            self.colorChanged.emit()

    def clip(self, planes):
        for i in reversed(range(1, len(self._cutFilters))):
            self._cutFilters[i].RemoveAllInputConnections(0)
            self._cutFilters.pop()

        inputFilter = self._cutFilters[0]
        if planes and self._properties.cutEnabled:
            # Plan 37 UF7. Cut cells is one stage over all the planes (the
            # first is the one cutting, the rest mask), not a chain of clips.
            cells = (self._cutCellsFilter(planes)
                     if self._cutMode is CutMode.CUT_CELLS else None)
            for f in ([cells] if cells is not None
                      else [self._clipFilter(c) for c in planes]):
                f.SetInputConnection(inputFilter.GetOutputPort())
                self._cutFilters.append(f)
                inputFilter = f

        self._connectMapper(inputFilter)
        self._mapper.Update()

    def slice(self, planes):
        """Cut the actor with one or more planes.

        A single plane used to be the only option, which made a boundary layer
        in a corner impossible to inspect: that needs two or three slices at
        once. ``None`` and a bare plane are still accepted, because that is what
        every existing caller passes.
        """
        if planes is None:
            planes = []
        elif isinstance(planes, vtkPlane):
            planes = [planes]

        for i in reversed(range(1, len(self._cutFilters))):
            self._cutFilters[i].RemoveAllInputConnections(0)
            self._cutFilters.pop()

        inputFilter = self._cutFilters[0]
        if planes and self._properties.cutEnabled:
            # Independent cuts of the same source, unioned. Chaining them would
            # cut the first slice's surface with the second plane and leave a
            # line, which is not what "slice at X and at Y" means.
            cutters = []
            for plane in planes:
                f = vtkCutter()
                f.SetCutFunction(plane)
                f.GenerateTrianglesOff()
                f.SetInputConnection(inputFilter.GetOutputPort())
                cutters.append(f)

            if len(cutters) == 1:
                self._cutFilters.append(cutters[0])
                inputFilter = cutters[0]
            else:
                union = vtkAppendPolyData()
                for f in cutters:
                    union.AddInputConnection(f.GetOutputPort())
                self._cutFilters.extend(cutters)
                self._cutFilters.append(union)
                inputFilter = union

        self._connectMapper(inputFilter)
        self._mapper.Update()

    def _connectMapper(self, input_filter):
        self._mapper.SetInputConnection(input_filter.GetOutputPort())
        self._silhouetteFilter.SetInputConnection(input_filter.GetOutputPort())
        self._wirePrecomputed()

    def getScalarRange(self, index: MeshQualityIndex) -> (float, float):
        return 0, 1

    def hasScalar(self, index: MeshQualityIndex) -> bool:
        """Whether this actor's data actually carries the named metric.

        A threshold over an array that is not there silently colours nothing,
        which reads as "the mesh is fine". The quality controls ask first.
        """
        return False

    def setScalar(self, index: MeshQualityIndex):
        pass

    def setScalarBand(self, low, high):
        pass

    def attachQualityField(self, name: str, values) -> bool:
        """Only a volume mesh carries per-cell quality; everything else declines."""
        return False

    def clearCellFilter(self):
        pass

    def applyCellFilter(self):
        pass

    def _setEdgeOpacity(self, opacity: float):
        prop = self._actor.GetProperty()
        # VTK gained per-property edge opacity in 9.3. Without it the edges
        # simply stay solid, which is the behaviour that existed before.
        if hasattr(prop, 'SetEdgeOpacity'):
            prop.SetEdgeOpacity(opacity)

    def displayedCellCount(self) -> int:
        """Cells currently reaching the mapper.

        Falls back to the source dataset when the pipeline has not been pulled
        yet -- which is the usual state right after a load, and exactly when
        the size question is first asked. Forcing an ``Update()`` here to get a
        precise number would pull the whole pipeline on the mesh least able to
        afford it.
        """
        try:
            data = self._mapper.GetInput()
            count = data.GetNumberOfCells() if data is not None else 0
        except Exception:
            count = 0
        if count:
            return count
        return self._dataSet.GetNumberOfCells() if self._dataSet else 0

    def wireframeIsFeatureEdgesOnly(self) -> bool:
        """Whether wireframe has fallen back to drawing feature edges.

        WP3.4: wireframe on a million-cell surface is a grey rectangle -- every
        cell edge drawn, nothing readable. Past the threshold this draws the
        feature edges instead, which is a different picture from the one the
        control names, so callers surface it rather than letting the user
        believe they are looking at the full wireframe.
        """
        return (self._properties.displayMode is DisplayMode.WIREFRAME
                and self.displayedCellCount() > WIREFRAME_CELL_LIMIT)

    def _applyWireframeMode(self):
        # No highlight guard: `setDisplayMode` decides whether the cue has to
        # be repainted afterwards, and an applicator that silently declines to
        # draw is how the Wireframe action came to be a no-op on every part it
        # can act on.
        if self.wireframeIsFeatureEdgesOnly():
            # Draw the shape, not a grey rectangle: the surface goes away and
            # the feature-edge outline becomes the whole picture.
            self._actor.SetVisibility(False)
            self._silhouette.SetVisibility(self._properties.visibility)
            return

        self._actor.SetVisibility(self._properties.visibility)
        self._actor.GetProperty().SetRepresentationToWireframe()
        # Wireframe already is the edges; a second outline on top of it
        # only thickens lines that are the whole picture.
        self._silhouette.SetVisibility(False)

    def _applySurfaceMode(self):
        self._actor.GetProperty().SetRepresentationToSurface()
        self._actor.GetProperty().EdgeVisibilityOff()
        self._silhouette.SetVisibility(self._properties.visibility)

    def _meshLineColor(self) -> str:
        return meshLineColorFor(self._currentSurfaceColor())

    def applyMeshLineStyle(self):
        """DP-710. Repaint the interior edges with the current style."""
        if self._properties.highlighted:
            self._highlightOn()
        elif self._properties.displayMode is DisplayMode.SURFACE_EDGE:
            self._applySurfaceEdgeMode()

    def _applySurfaceEdgeMode(self):
        style = meshLineStyle()
        self._actor.GetProperty().SetRepresentationToSurface()
        self._actor.GetProperty().EdgeVisibilityOn()
        self._actor.GetProperty().SetLineWidth(style.width)
        self._setEdgeOpacity(style.opacity)
        if not self._properties.highlighted:
            self._actor.GetProperty().SetEdgeColor(*rgb(self._meshLineColor()))
        self._silhouette.SetVisibility(self._properties.visibility)

    def _highlightOn(self):
        # Selection used to be signalled by repainting every interior edge in
        # the accent colour at double width. On a mesh dense enough that a cell
        # is a couple of pixels across, that is not an outline -- it is a fill,
        # and the selected patch became a solid slab of accent. The outline now
        # carries the cue and the surface keeps its own colour.
        # A part the user asked to see as a wireframe stays a wireframe:
        # highlighting says "this one", not "and now look at it differently".
        if self._properties.displayMode is DisplayMode.WIREFRAME:
            self._applyWireframeMode()
        else:
            self._applySurfaceEdgeMode()
        self._setEdgeOpacity(highlightEdgeOpacity())
        self._actor.GetProperty().SetEdgeColor(*rgb(self._highlightColor))
        self._actor.GetProperty().SetLineWidth(meshLineStyle().width)
        # R5/R44. The outline alone does not read -- see HIGHLIGHT_AMBIENT. The
        # surface itself takes the role colour and brightens, so a cap that is
        # 3% of the model says "this one" without the user hunting for a
        # 3px line, and so a tree selection changes something the eye lands on.
        self._actor.GetProperty().SetColor(*rgb(self._highlightColor))
        self._actor.GetProperty().SetAmbient(HIGHLIGHT_AMBIENT)
        self._silhouette.GetProperty().SetColor(*rgb(self._highlightColor))
        self._silhouette.GetProperty().SetLineWidth(HIGHLIGHT_SILHOUETTE_WIDTH)

    def _highlightOff(self):
        self._applyDisplayMode(self._properties.displayMode)
        self._actor.GetProperty().SetDiffuse(BASE_DIFFUSE)
        self._actor.GetProperty().SetAmbient(BASE_AMBIENT)
        # R5/R44. Give the part its own colour back -- the palette slot it was
        # assigned, or the colour the user picked in Display Control, which the
        # selection paint borrowed the surface from but never replaced.
        color = self._currentSurfaceColor()
        self._actor.GetProperty().SetColor(
            color.redF(), color.greenF(), color.blueF())
        self._actor.GetProperty().SetEdgeColor(*rgb(self._meshLineColor()))
        self._actor.GetProperty().SetLineWidth(meshLineStyle().width)
        self._silhouette.GetProperty().SetColor(*rgb(self._silhouetteColor))
        self._silhouette.GetProperty().SetLineWidth(SILHOUETTE_WIDTH)

    def _createActor(self):
        """The prop this actor draws through.

        A hook rather than a fixed `vtkActor` so the volume mesh -- the only
        thing here that reaches tens of millions of cells -- can carry level of
        detail without every patch and overlay paying for it.
        """
        return vtkActor()

    def _initMapper(self):
        raise NotImplementedError

    def _clipFilter(self, cutter: vtkPlane):
        raise NotImplementedError

    def _cutCellsFilter(self, planes):
        """Plan 37 UF7. The Cut cells stage, or None to clip as before."""
        return None


class MeshActor(ActorInfo):
    def __init__(self, dataSet, id_, name):
        super().__init__(dataSet, id_, name, ActorType.MESH)
        # Keep the complete volume dataset for quality thresholds and clipping,
        # but send only its exterior surface to the renderer.  This avoids
        # transferring/rendering millions of hidden interior faces.
        self._surfaceFilter = vtkGeometryFilter()
        self._surfaceFilter.SetInputConnection(self._cutFilters[-1].GetOutputPort())
        self._mapper.SetInputConnection(self._surfaceFilter.GetOutputPort())
        # vtkFeatureEdges needs polydata, and a volume mesh only becomes that
        # after the geometry filter, so the outline hangs off the surface too.
        self._silhouetteFilter.SetInputConnection(
            self._surfaceFilter.GetOutputPort())

    def _connectMapper(self, input_filter):
        self._surfaceFilter.SetInputConnection(input_filter.GetOutputPort())
        self._mapper.SetInputConnection(self._surfaceFilter.GetOutputPort())
        self._silhouetteFilter.SetInputConnection(
            self._surfaceFilter.GetOutputPort())
        self._wirePrecomputed()
        self._invalidateLevelOfDetail()

    def _wirePrecomputed(self):
        """Step 10: the worker's exterior surface, while nothing cuts it.

        Without it, the first paint ran ``vtkGeometryFilter`` over the whole
        volume on the GUI thread.
        """
        super()._wirePrecomputed()
        surfaceFilter = getattr(self, '_surfaceFilter', None)
        if surfaceFilter is None:
            return      # still in ActorInfo.__init__
        if self._precomputedSurface is not None and self._precomputedActive():
            self._mapper.SetInputData(self._precomputedSurface)
        else:
            self._mapper.SetInputConnection(surfaceFilter.GetOutputPort())

    def _invalidateLevelOfDetail(self):
        """Make the decimated copy follow what the volume now shows.

        DP-814. ``vtkQuadricLODActor`` draws its decimated copy on every
        interactive frame -- a camera drag and, above all, a section-handle
        drag, because the widget raises the render rate for the drag -- and
        rebuilds it only when the *actor* is modified. A clip, a slice or a
        quality threshold changes the mapper's input and not the actor, so
        the copy stayed the mesh it was first built from: while a plane was
        dragged the volume drew uncut, the cut appearing only when the
        mouse was let go, and a sectioned mesh turned into the whole mesh
        for as long as the camera moved.
        """
        actor = getattr(self, '_actor', None)
        if actor is not None:
            actor.Modified()

    def setDataSet(self, dataSet, *, like=None):
        # DP-814. A reloaded mesh is new data under the same actor.
        self._invalidateLevelOfDetail()
        super().setDataSet(dataSet, like=like)

    def _createActor(self):
        """WP6.2. Level of detail, on the one actor that can be enormous.

        `vtkQuadricLODActor` keeps a decimated copy and draws it only while the
        render window is asking for an interactive frame rate; at rest it draws
        the real thing. That makes the trade automatic and bounded: detail is
        dropped exactly while the camera is moving and never in a still frame,
        which is the only form of it a user cannot be misled by.

        Construction of the decimated copy is deferred, because building it
        eagerly would cost seconds and hundreds of megabytes on a mesh large
        enough to want it -- on the load path, before the user has done
        anything that needs it.
        """
        actor = vtkQuadricLODActor()
        actor.DeferLODConstructionOn()
        actor.SetStatic(True)
        return actor

    def _initMapper(self) -> vtkDataSetMapper:
        return vtkDataSetMapper()

    def _clipFilter(self, cutter: vtkPlane):
        if self._cutMode is CutMode.CRINKLE:
            f = vtkExtractGeometry()
            f.SetImplicitFunction(cutter)
            f.ExtractInsideOff()
            # Whole cells, so cell shapes stay legible -- the view that answers
            # "are my prism layers actually there".
            f.SetExtractBoundaryCells(True)
            return f

        # Truncating cells at the plane is what produces a flat section face.
        # The table-based variant handles the polyhedral cells snappyHexMesh
        # emits; it logs a warning for non-manifold ones and keeps going.
        f = vtkTableBasedClipDataSet()
        f.SetClipFunction(cutter)
        f.InsideOutOff()
        return f

    def _cutCellsFilter(self, planes):
        from foammesh.rendering.cut_cells import CutCellsFilter
        return CutCellsFilter(planes)

    def getNumberOfDisplayedCells(self) -> int:
        # R140. A VTK filter is lazy: until something pulls on it, its output
        # is the empty data set it was constructed with. The toolbar asks for
        # this the moment the actors are built, which is before the viewport
        # has painted, so a mesh that was plainly on screen was counted as
        # `0 cells` and only came right after the *next* stage forced a
        # render. Pull on the filter here rather than trust that someone
        # else already did.
        cut = self._cutFilters[-1]
        cut.Update()
        return cut.GetOutput().GetNumberOfCells()

    def hasScalar(self, index: MeshQualityIndex) -> bool:
        return self._dataSet.GetCellData().GetScalars(index.value) is not None

    def getScalarRange(self, index: MeshQualityIndex) -> tuple[float, float]:
        # print(f'Name: {self.name()} Field: {index.value}')
        scalars = self._dataSet.GetCellData().GetScalars(index.value)
        if scalars is None:
            return 0, 1

        left, right = scalars.GetRange()
        return left, right

    def setScalar(self, index: MeshQualityIndex):
        self._mqIndex = index

    def setScalarBand(self, low, high):
        self._mqLow = low
        self._mqHigh = high

    def attachQualityField(self, name: str, values) -> bool:
        """Attach one per-cell quality array to this actor's volume dataset.

        **One value per cell, or nothing.** The values are computed off
        ``constant/polyMesh`` and the dataset comes from the VTK reader, so
        their agreeing on cell *count* is what establishes that they are the
        same mesh in the same order. Attaching a shorter or longer array by
        padding it would paint arbitrary cells -- a picture that looks
        authoritative and is noise, which is worse than colouring nothing.

        *Measured*, because the ordering is the whole load-bearing assumption:
        on gmsh/duct, gmsh/annulus, snappyhexmesh/duct and snappyhexmesh/elbow
        the counts match exactly, and per-index cell volumes agree with VTK's
        own to a median of 1e-6 (tetrahedra) and 5e-3 (polyhedra, where
        ``vtkCellSizeFilter`` tessellates differently from OpenFOAM's pyramid
        rule) against 50--1600 for a deliberately shuffled pairing. The
        decomposed reads agree to the same figures as the reconstructed ones,
        so a processor layout preserves global cell order and is not refused.

        The array is added without becoming the active scalars: the threshold
        selects it by name through ``SetInputArrayToProcess``, and claiming the
        active slot here would recolour every actor that had not asked.
        """
        import numpy as np
        from vtkmodules.util.numpy_support import numpy_to_vtk

        data = self._dataSet
        if data is None:
            return False
        count = data.GetNumberOfCells()
        values = np.asarray(values, dtype=np.float64)
        if count <= 0 or values.size != count:
            return False

        array = numpy_to_vtk(values, deep=True)
        array.SetName(name)
        data.GetCellData().AddArray(array)
        return True

    def clearCellFilter(self):
        self._cellFilter = vtkPassThrough()

        self._cellFilter.SetInputData(self._dataSet)

        self._cutFilters[0].RemoveAllInputConnections(0)
        self._cutFilters[0].SetInputConnection(self._cellFilter.GetOutputPort())
        self._wirePrecomputed()

        self._mapper.ScalarVisibilityOff()
        # DP-714. The band's scale was lent for the colouring only.
        self._mapper.SetLookupTable(sequentialRedLut)

        # Whatever suspended the standing bias is off too, so it comes back.
        self._applyDepthBias(self._depthBias)

        self._mapper.Update()
        self._invalidateLevelOfDetail()

    def applyCellFilter(self):
        self._cellFilter = vtkThreshold()
        self._cellFilter.AllScalarsOff()
        self._cellFilter.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)

        self._cellFilter.SetLowerThreshold(self._mqLow)
        self._cellFilter.SetUpperThreshold(self._mqHigh)
        self._cellFilter.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, self._mqIndex.value)

        self._cellFilter.SetInputData(self._dataSet)

        self._cutFilters[0].RemoveAllInputConnections(0)
        self._cutFilters[0].SetInputConnection(self._cellFilter.GetOutputPort())
        self._wirePrecomputed()

        self._mapper.ScalarVisibilityOn()
        # DP-714. A scale of its own on the band, not the shared table's 0-1
        # range, which painted every value above 1 the same red.
        self._mapper.SetLookupTable(qualityBandLut(
            self._mqLow, self._mqHigh,
            worstIsHigh=self._mqIndex not in QUALITY_WORST_IS_LOW))
        self._mapper.SetScalarRange(self._mqLow, self._mqHigh)
        self._mapper.UseLookupTableScalarRangeOn()
        self._mapper.SelectColorArray(self._mqIndex.value)

        # A thresholded volume is no longer a duplicate of its own skin -- it
        # is the subset of cells the user asked to be shown, and asking to see
        # the worst cells and then being shown the patches that cover them is
        # not an answer. So the bias that keeps the whole volume behind its
        # patches (DP-141) stands down for as long as the filter is on --
        # and turns round (DP-737): at zero a boundary cell's face tied with
        # the patch face over it and shimmered through it, so the cells are
        # pulled in front instead. clearCellFilter puts the standing bias back.
        self._applyDepthBias(POOR_CELL_DEPTH_BIAS)

        self._mapper.Update()
        self._invalidateLevelOfDetail()


class BoundaryActor(ActorInfo):
    def __init__(self, dataSet, id_, name):
        super().__init__(dataSet, id_, name, ActorType.BOUNDARY)

    def _initMapper(self) -> vtkPolyDataMapper:
        return vtkPolyDataMapper()

    def _clipFilter(self, cutter: vtkPlane):
        if self._cutMode is CutMode.CRINKLE:
            f = vtkExtractPolyDataGeometry()
            f.SetImplicitFunction(cutter)
            f.ExtractInsideOff()
            f.SetExtractBoundaryCells(True)
            return f

        # A patch clipped whole-face leaves a ragged edge against the volume
        # mesh's flat section face. Both sides have to be cut the same way or
        # the section stops looking like one plane.
        f = vtkClipPolyData()
        f.SetClipFunction(cutter)
        f.InsideOutOff()
        return f

    def _cutCellsFilter(self, planes):
        from foammesh.rendering.cut_cells import CutCellsFilter
        return CutCellsFilter(planes, polyData=True)


class RegionMarkerActor(ActorInfo):
    """Where a region seed sits, drawn as a glyph rather than a form field.

    A region is three numbers on a card, and nothing in the viewport said
    where they landed. So a seed that fell outside the geometry - the single
    most common way a snappy run dies - looked exactly like one that fell
    inside it, until the mesher said `cannot find cell`. The marker makes the
    point a thing the user can see, select and hide with everything else.

    The glyph is sized from the model, not fixed: a marker in metres is
    invisible on a millimetre part and swallows a kilometre one.
    """

    #: The glyph's radius as a fraction of the model's longest side. Big
    #: enough to find on a dense surface, small enough not to hide the
    #: feature the seed was placed next to.
    RADIUS_FRACTION = 0.012
    MIN_RADIUS = 1e-9

    @classmethod
    def radiusFor(cls, bounds) -> float:
        """A glyph radius that reads the same on any model scale."""
        if bounds is None:
            return 1.0
        longest = max(bounds.size() or (0.0,))
        return max(longest * cls.RADIUS_FRACTION, cls.MIN_RADIUS)

    @classmethod
    def build(cls, region_id, name, point, bounds):
        """The marker for one stored region, or ``None`` without a point."""
        from foammesh.rendering.vtk_loader import spherePolyData

        if point is None:
            return None
        try:
            centre = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            return None
        if len(centre) != 3:
            return None
        return cls(spherePolyData(centre, cls.radiusFor(bounds)),
                   f'region:{region_id}', str(name))

    def __init__(self, dataSet, id_, name):
        super().__init__(dataSet, id_, name, ActorType.GEOMETRY)

        # Opaque enough to find, transparent enough that the surface it sits
        # against is still readable through it.
        self.setOpacity(0.65)

    def _initMapper(self) -> vtkPolyDataMapper:
        return vtkPolyDataMapper()

    def _clipFilter(self, cutter: vtkPlane):
        # A marker is a landmark, not part of the model: a section that cuts
        # the geometry away must not take the seed with it, or the user loses
        # the one thing the section was opened to check.
        f = vtkExtractPolyDataGeometry()
        f.SetImplicitFunction(cutter)
        f.ExtractInsideOff()
        f.SetExtractBoundaryCells(True)
        return f


class ExcludeMarkerActor(RegionMarkerActor):
    """Plan 37 UF16. Where an exclude point sits: a cube, not a seed's ball.

    A `RegionMarkerActor` in every other way -- sized from the model, shown
    with the seeds on Domain & Regions, kept out of the base-grid box and out
    of a section's cut -- so the one difference on screen is the shape (and
    the warning colour the manager gives it).
    """

    @classmethod
    def build(cls, point_id, name, point, bounds):
        from vtkmodules.vtkFiltersSources import vtkCubeSource

        if point is None:
            return None
        try:
            centre = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            return None
        if len(centre) != 3:
            return None
        side = 2.0 * cls.radiusFor(bounds)
        source = vtkCubeSource()
        source.SetCenter(*centre)
        source.SetXLength(side)
        source.SetYLength(side)
        source.SetZLength(side)
        source.Update()
        return cls(source.GetOutput(), f'exclude:{point_id}', str(name))


class GeometryActor(ActorInfo):
    def __init__(self, dataSet, id_, name):
        super().__init__(dataSet, id_, name, ActorType.GEOMETRY)

        self.setOpacity(0.9)
        self._silhouetteTask = None
        self._computeSilhouetteLater()

    def setDataSet(self, dataSet, *, like=None):
        super().setDataSet(dataSet, like=like)
        if self._precomputedEdges is None:
            self._computeSilhouetteLater()

    def _computeSilhouetteLater(self):
        """Plan 35 CR3 step 10: the outline is computed on the VTK thread.

        Until it is in, the outline draws nothing rather than running
        ``vtkFeatureEdges`` over the whole surface at the first paint. With no
        running event loop (a script, a synchronous test) the filter stays
        wired and runs as it always did.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        data = self._dataSet
        if data is None:
            return
        self._precomputedEdges = vtkPolyData()
        self._wirePrecomputed()
        self._silhouetteTask = loop.create_task(self._computeSilhouette(data))

    async def _computeSilhouette(self, data):
        from foammesh.core.mesh.mesh_preview import feature_edges

        try:
            edges = await vtk_run_in_thread(feature_edges, data)
        except Exception:                                   # noqa: BLE001
            logging.getLogger(__name__).exception(
                'outline of %s not computed', self._id)
            edges = None
        if self.isDisposed() or self._dataSet is not data:
            return
        if edges is None:
            # Fall back to the filter: slower, but the outline is there.
            self._precomputedEdges = None
            self._wirePrecomputed()
        else:
            self._precomputedEdges = edges
            self._wirePrecomputed()
        self.silhouetteChanged.emit()

    def _initMapper(self) -> vtkPolyDataMapper:
        return vtkPolyDataMapper()

    def _clipFilter(self, cutter: vtkPlane):
        f = vtkClipPolyData()
        f.SetClipFunction(cutter)
        f.InsideOutOff()
        return f

    def _cutCellsFilter(self, planes):
        from foammesh.rendering.cut_cells import CutCellsFilter
        return CutCellsFilter(planes, polyData=True)
